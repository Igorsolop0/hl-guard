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

CALLS: list[tuple[str, dict[str, Any]]] = [
    ("markets", {"coins": ["BTC", "ETH", "SOL"]}),
    ("order_book", {"coin": "BTC", "depth": 5}),
    ("candles", {"coin": "BTC", "interval": "1h", "count": 7}),
    ("account", {}),
    ("positions", {}),
    ("open_orders", {}),
    ("fills", {"limit": 20}),
    ("risk_status", {}),
]


def p95(values: list[float]) -> float:
    return statistics.quantiles(values, n=20, method="inclusive")[18]


async def probe(config: Path, rounds: int, http_log: Path) -> dict[str, list[tuple[int, int]]]:
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).with_name("serve_fake.py")), str(config)],
        env={**os.environ, "HL_GUARD_HTTP_LOG": str(http_log), "HL_GUARD_FAKE_LIVE_SIZED": "1"},
    )
    spans: dict[str, list[tuple[int, int]]] = {name: [] for name, _ in CALLS}
    async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        for name, args in CALLS:
            await session.call_tool(name, args)
        http_log.write_text("")
        for _ in range(rounds):
            for name, args in CALLS:
                start = time.time_ns()
                result = await session.call_tool(name, args)
                end = time.time_ns()
                if result.is_error:
                    raise SystemExit(f"{name} failed: {result.content}")
                spans[name].append((start, end))
    return spans


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure server overhead per read tool: call time minus HTTP time, "
        "against recorded answers sized like testnet. Exit 1 when a p95 is above the rule."
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--p95-max-ms", type=float, default=20.0)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        http_log = Path(tmp) / "http.log"
        spans = anyio.run(probe, args.config, args.rounds, http_log)
        http = [tuple(map(int, line.split())) for line in http_log.read_text().splitlines()]
    worst = 0.0
    rows = {}
    for name, calls in spans.items():
        overhead = []
        for start, end in calls:
            inside = sum(e - s for s, e in http if start <= s and e <= end)
            overhead.append((end - start - inside) / 1e6)
        rows[name] = {
            "calls": len(overhead),
            "p50_ms": round(statistics.median(overhead), 3),
            "p95_ms": round(p95(overhead), 3),
            "max_ms": round(max(overhead), 3),
        }
        worst = max(worst, rows[name]["p95_ms"])
    print(json.dumps({"http_requests": len(http), "tools": rows}, indent=1))
    print(f"worst p95 {worst:.3f} ms, rule p95 <= {args.p95_max_ms} ms")
    return 0 if worst <= args.p95_max_ms else 1


if __name__ == "__main__":
    sys.exit(main())
