"""Time the write path against the loopback venue.

Rejected proposals: call time minus /info time. Approved orders: from the
approval record to the placed decision in the ledger, both written by the
real processes. Exit 1 when a p95 breaks the rule. The approvals rotate over
the policy coins and wait out each cooldown. ``--seed-records`` first writes
that many proposal and rejection records, so the probe runs at that ledger size.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hl_guard.ledger import ledger as chain
from server.lanes.approve_pty import approve_in_a_terminal

LANES = Path(__file__).parent
BTC = {"coin": "BTC", "side": "buy", "size": "0.0002", "limit_px": "81171", "stop_loss_px": "80000"}
REJECTS: list[tuple[str, dict[str, Any]]] = [
    ("per_order_usd_max", {**BTC, "size": "0.002"}),
    ("price_band_pct_max", {**BTC, "limit_px": "81900", "stop_loss_px": "81000"}),
]
RESTING = [
    {**BTC, "limit_px": "80900"},
    {"coin": "ETH", "side": "buy", "size": "0.005", "limit_px": "2370", "stop_loss_px": "2350"},
    {"coin": "SOL", "side": "buy", "size": "0.1", "limit_px": "103.2", "stop_loss_px": "102"},
]
COOLDOWN_S = 61


def p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=20, method="inclusive")[18]


def seed(ledger: Path, n: int) -> None:
    if ledger.exists():
        raise SystemExit(f"{ledger} exists; seed an empty data dir")
    prev, start = chain.GENESIS, time.time_ns() // 1_000_000 - n
    lines = []
    for seq in range(1, n + 1):
        pid = f"{seq // 2:012x}"
        if seq % 2:
            kind, payload = (
                "proposal",
                {"id": pid, "tool": "propose_order", "order": {}, "hash": ""},
            )
        else:
            kind, payload = (
                "decision",
                {"id": pid, "outcome": "rejected", "rule": "r", "detail": ""},
            )
        ts = start + seq
        digest = chain._digest(seq, ts, kind, payload, prev)
        body = {"seq": seq, "ts_ms": ts, "kind": kind, "payload": payload, "prev": prev}
        lines.append(chain._canonical({**body, "hash": digest}))
        prev = digest
    fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(b"\n".join(lines) + b"\n")


def text(result: Any) -> Any:
    return json.loads(result.content[0].text)


async def probe(
    config: Path, rejects: int, approvals: int, http_log: Path
) -> tuple[dict[str, list[tuple[int, int]]], list[str]]:
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(LANES / "serve_venue.py"), str(config)],
        env={**os.environ, "HL_GUARD_VENUE_HTTP_LOG": str(http_log)},
    )
    spans: dict[str, list[tuple[int, int]]] = {name: [] for name, _ in REJECTS}
    approved: list[str] = []
    entered: dict[str, float] = {}
    every = max(1, rejects // max(1, approvals))
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.call_tool("propose_order", REJECTS[1][1])
        for i in range(rejects):
            name, args = REJECTS[i % len(REJECTS)]
            start = time.time_ns()
            result = text(await session.call_tool("propose_order", args))
            spans[name].append((start, time.time_ns()))
            if result.get("rule") != name:
                raise SystemExit(f"{name} gave {result}")
            if i % every == every - 1 and len(approved) < approvals:
                order = RESTING[len(approved) % len(RESTING)]
                await anyio.sleep(max(0.0, entered.get(order["coin"], 0.0) - time.monotonic()))
                pending = text(await session.call_tool("propose_order", order))
                pid = pending["id"]
                await anyio.to_thread.run_sync(approve_in_a_terminal, config, pid)
                for _ in range(200):
                    status = text(await session.call_tool("proposal_status", {"id": pid}))
                    if status["status"] in ("placed", "failed", "rejected"):
                        break
                    await anyio.sleep(0.05)
                if status["status"] != "placed":
                    raise SystemExit(f"approved order {pid} ended {status}")
                await session.call_tool(
                    "cancel_order", {"coin": order["coin"], "oid": status["oid"]}
                )
                entered[order["coin"]] = time.monotonic() + COOLDOWN_S
                approved.append(pid)
    return spans, approved


def approval_latency(ledger: Path, ids: list[str]) -> list[float]:
    approved_at: dict[str, int] = {}
    placed_at: dict[str, int] = {}
    for line in ledger.read_text().splitlines():
        record = json.loads(line)
        pid = record["payload"].get("id")
        if record["kind"] == "approval":
            approved_at[pid] = record["ts_ms"]
        if record["kind"] == "decision" and record["payload"].get("outcome") == "placed":
            placed_at[pid] = record["ts_ms"]
    return [float(placed_at[i] - approved_at[i]) for i in ids]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--rejects", type=int, default=500)
    parser.add_argument("--approvals", type=int, default=20)
    parser.add_argument("--reject-p95-max-ms", type=float, default=20.0)
    parser.add_argument("--placed-p95-max-ms", type=float, default=3000.0)
    parser.add_argument("--seed-records", type=int, default=0)
    args = parser.parse_args()
    import yaml

    lane = yaml.safe_load(args.config.read_text())
    policy = yaml.safe_load((args.config.parent / lane["policy"]).read_text())
    if policy["cooldown_s"] > COOLDOWN_S - 1 or policy["entries_per_day_max"] < args.approvals:
        raise SystemExit(
            f"the policy needs cooldown_s <= {COOLDOWN_S - 1} and entries_per_day_max >= "
            f"{args.approvals}; it has {policy['cooldown_s']} and {policy['entries_per_day_max']}"
        )
    data_dir = Path(lane["data_dir"])
    ledger = (
        data_dir if data_dir.is_absolute() else args.config.parent / data_dir
    ) / "ledger.jsonl"
    if args.seed_records:
        seed(ledger, args.seed_records)
    with tempfile.TemporaryDirectory() as tmp:
        http_log = Path(tmp) / "http.log"
        spans, approved = anyio.run(probe, args.config, args.rejects, args.approvals, http_log)
        http = [tuple(map(int, line.split())) for line in http_log.read_text().splitlines()]
    rows: dict[str, Any] = {}
    worst = 0.0
    for name, calls in spans.items():
        overhead = []
        for start, end in calls:
            inside = sum(e - s for s, e in http if start <= s and e <= end)
            overhead.append((end - start - inside) / 1e6)
        total = [(end - start) / 1e6 for start, end in calls]
        rows[name] = {
            "calls": len(overhead),
            "p50_ms": round(statistics.median(overhead), 3),
            "p95_ms": round(p95(overhead), 3),
            "max_ms": round(max(overhead), 3),
            "total_p95_ms": round(p95(total), 3),
        }
        worst = max(worst, rows[name]["p95_ms"])
    placed = approval_latency(ledger, approved)
    rows["approval_to_placed"] = {
        "orders": len(placed),
        "p50_ms": statistics.median(placed),
        "p95_ms": p95(placed),
        "max_ms": max(placed),
    }
    print(
        json.dumps(
            {"records": args.seed_records, "http_requests": len(http), "rows": rows}, indent=1
        )
    )
    ok = worst <= args.reject_p95_max_ms and p95(placed) <= args.placed_p95_max_ms
    print(
        f"reject p95 {worst:.3f} ms (rule <= {args.reject_p95_max_ms}), approval to placed p95 "
        f"{p95(placed):.0f} ms (rule <= {args.placed_p95_max_ms})"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
