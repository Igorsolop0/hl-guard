"""Helpers for tools/e2e/run.sh: refuse anything but testnet, build the
scenario from a live market read, wait for the operator's approval, and judge
the transcript and the ledger."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from hl_guard.policy.load import PolicyError, load_policy
from hl_guard.policy.model import Policy
from hl_guard.server.config import ConfigError, ServeConfig, load_config, read_mode, resolve_network
from hl_guard.server.tools_write import to_price_grid

NETWORK = "testnet"
ENTRY_USD = Decimal(12)
OVERSIZE = 3
BAND_SHARE = Decimal("0.6")
BAND_PCT_MAX = Decimal("0.3")
STOP_PCT_MAX = Decimal(1)
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2


class Refused(Exception):
    pass


def _load(config_path: Path) -> tuple[ServeConfig, Policy]:
    try:
        config = load_config(config_path)
        policy = load_policy(config.policy_path)
        network = resolve_network(policy.network, read_mode(config.data_dir), NETWORK)
    except (ConfigError, PolicyError) as exc:
        raise Refused(f"e2e refused: {exc}") from None
    if network != NETWORK:
        raise Refused(f"e2e refused: the network is {network}; the e2e runs on testnet only")
    if config.agent_key_path is None:
        raise Refused("e2e refused: the config has no agent_key_file; run hl-guard init first")
    if policy.per_order_usd_max < ENTRY_USD + 3:
        raise Refused(
            f"e2e refused: per_order_usd_max {policy.per_order_usd_max} is below "
            f"{ENTRY_USD + 3}; the approved entry is about {ENTRY_USD} USD"
        )
    return config, policy


def preflight(config_path: Path, coin: str) -> list[str]:
    config, policy = _load(config_path)
    if coin not in policy.coins:
        raise Refused(f"e2e refused: {coin} is not in the policy coins {sorted(policy.coins)}")
    return [
        f"network {NETWORK}",
        f"account {config.account}",
        f"ledger {config.ledger_path}",
        f"approval_ttl_s {config.approval_ttl_s}",
    ]


@dataclass(frozen=True)
class Result:
    tool: str
    is_error: bool
    value: Any


def tool_results(lines: Iterable[str]) -> Iterator[Result]:
    """Every tools/call of a tools/drive.py transcript, with the tool it called."""
    calling = ""
    for line in lines:
        if line.startswith(">> tools/call "):
            calling = json.loads(line.removeprefix(">> tools/call ")).get("name", "")
        if not line.startswith("<< tools/call "):
            continue
        result = json.loads(line.removeprefix("<< tools/call "))
        for item in result.get("content", []):
            text = item.get("text")
            if isinstance(text, str):
                yield Result(calling, bool(result.get("isError")), json.loads(text))


def _up(value: Decimal, decimals: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_CEILING)


@dataclass(frozen=True)
class Plan:
    coin: str
    limit_px: Decimal
    stop_loss_px: Decimal
    small: Decimal
    big: Decimal

    def entry(self, size: Decimal) -> dict[str, Any]:
        return {
            "tool": "propose_order",
            "arguments": {
                "coin": self.coin,
                "side": "buy",
                "size": str(size),
                "limit_px": str(self.limit_px),
                "stop_loss_px": str(self.stop_loss_px),
                "tif": "Ioc",
                "leverage": 1,
            },
        }


def _first(results: list[Result], tool: str) -> Any:
    return next((r.value for r in results if r.tool == tool and not r.is_error), None)


def plan(read: list[Result], coin: str, policy: Policy) -> Plan:
    markets = _first(read, "markets")
    if markets is None or markets.get("network") != NETWORK:
        raise Refused("e2e refused: the market read is missing or not from testnet")
    positions = _first(read, "positions")
    if positions is None:
        raise Refused("e2e refused: the positions read is missing")
    if any(p.get("coin") == coin and Decimal(str(p.get("size", "0"))) != 0 for p in positions):
        raise Refused(
            f"e2e refused: the account holds a {coin} position, which the run would close"
        )
    market = next(m for m in markets["markets"] if m["name"] == coin)
    mark, decimals = Decimal(str(market["mark_px"])), int(market["sz_decimals"])
    band = min(policy.price_band_pct_max * BAND_SHARE, BAND_PCT_MAX)
    limit = to_price_grid(mark * (1 + band / 100), decimals, up=True)
    stop_pct = min(policy.stop_loss_max_pct / 2, STOP_PCT_MAX)
    stop = to_price_grid(limit * (1 - stop_pct / 100), decimals, up=False)
    small = _up(ENTRY_USD / limit, decimals)
    big = _up(policy.per_order_usd_max * OVERSIZE / limit, decimals)
    if small * limit > policy.per_order_usd_max:
        raise Refused(f"e2e refused: one size step of {coin} is above per_order_usd_max")
    return Plan(coin, limit, stop, small, big)


def scenario(p: Plan, config_path: Path, ttl_s: int) -> list[dict[str, Any]]:
    wait = [sys.executable, str(Path(__file__).resolve()), "wait", "--config", str(config_path)]
    return [
        {"tool": "risk_status", "arguments": {}},
        p.entry(p.big),
        p.entry(p.small),
        {"run": [*wait, "--id", "{id}", "--timeout", str(ttl_s + 10)]},
        {"wait": 5},
        {"tool": "proposal_status", "arguments": {"id": "{id}"}},
        {"tool": "positions", "arguments": {}},
        {"tool": "close_position", "arguments": {"coin": p.coin}},
        {"wait": 3},
        {"tool": "positions", "arguments": {}},
    ]


def wait_for_answer(config_path: Path, pid: str, timeout_s: float) -> int:
    """Waits until ``pid`` no longer waits for approval. It never approves."""
    deadline = time.monotonic() + timeout_s
    command = ["hl-guard", "pending", "--config", str(config_path)]
    while time.monotonic() < deadline:
        listed = subprocess.run(command, capture_output=True, text=True, check=False)
        if listed.returncode != 0:
            print(listed.stdout + listed.stderr)
            return EXIT_FAILED
        if not any(line.startswith(f"{pid}:") for line in listed.stdout.splitlines()):
            print(f"proposal {pid} no longer waits for approval")
            return EXIT_OK
        time.sleep(2)
    print(f"proposal {pid} still waits after {timeout_s:g} s")
    return EXIT_FAILED


def watch(lines: Iterable[str], config_path: Path) -> None:
    for line in lines:
        print(line, end="", flush=True)
        for result in tool_results([line]):
            value = result.value
            if isinstance(value, dict) and value.get("result") == "pending_approval":
                print(
                    "\n>>> APPROVE NOW in a second terminal, in this folder:\n"
                    f">>>   uv run hl-guard approve {value['id']} --config {config_path}\n",
                    file=sys.stderr,
                    flush=True,
                )


def _kinds(ledger_lines: Iterable[str], after_seq: int) -> list[str]:
    records = [json.loads(line) for line in ledger_lines if line.strip()]
    return [r["kind"] for r in records if r["seq"] > after_seq]


def judge(transcript: list[str], ledger_lines: list[str], after_seq: int, coin: str) -> list[str]:
    """One line per check; a line that starts with FAIL fails the run."""
    results = list(tool_results(transcript))
    proposals = [r.value for r in results if r.tool in ("propose_order", "close_position")]
    statuses = [r.value for r in results if r.tool == "proposal_status"]
    position_reads = [r.value for r in results if r.tool == "positions"]
    rows: list[tuple[bool, str]] = []
    first = proposals[0] if proposals else {}
    rows.append(
        (
            first.get("result") == "rejected" and first.get("rule") == "per_order_usd_max",
            f"oversized entry rejected by per_order_usd_max: {_short(first)}",
        )
    )
    second = proposals[1] if len(proposals) > 1 else {}
    rows.append(
        (
            second.get("result") in ("pending_approval", "placed"),
            f"small entry waited for approval or was placed: {_short(second)}",
        )
    )
    status = statuses[0] if statuses else {}
    rows.append((status.get("status") == "placed", f"entry placed: {_short(status)}"))
    held = _held(position_reads[0], coin) if position_reads else Decimal(0)
    rows.append((held != 0, f"{coin} position after the entry: {held}"))
    close = proposals[2] if len(proposals) > 2 else {}
    rows.append(
        (
            close.get("result") == "placed" and close.get("status") == "filled",
            f"close filled: {_short(close)}",
        )
    )
    left = _held(position_reads[-1], coin) if len(position_reads) > 1 else None
    rows.append((left == 0, f"{coin} position after the close: {left}"))
    kinds = _kinds(ledger_lines, after_seq)
    for kind in ("proposal", "decision", "approval", "order", "ack", "fill"):
        rows.append((kind in kinds, f"ledger has a new {kind} record"))
    return [f"{'ok  ' if passed else 'FAIL'} {text}" for passed, text in rows]


def _held(positions: list[dict[str, Any]], coin: str) -> Decimal:
    return sum(
        (Decimal(str(p.get("size", "0"))) for p in positions if p.get("coin") == coin),
        Decimal(0),
    )


def _short(value: Any) -> str:
    return json.dumps(value)[:300]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="e2e.py")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "scenario", "wait", "watch", "judge"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name in ("preflight", "scenario", "judge"):
            command.add_argument("--coin", default="BTC")
        if name in ("scenario", "judge"):
            command.add_argument("--transcript", type=Path, required=True)
        if name == "wait":
            command.add_argument("--id", required=True)
            command.add_argument("--timeout", type=float, required=True)
        if name == "judge":
            command.add_argument("--ledger-lines", type=Path, required=True)
            command.add_argument("--after-seq", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            print("\n".join(preflight(args.config, args.coin)))
        elif args.command == "scenario":
            config, policy = _load(args.config)
            read = list(tool_results(args.transcript.read_text().splitlines()))
            steps = scenario(plan(read, args.coin, policy), args.config, config.approval_ttl_s)
            print(json.dumps(steps, indent=1))
        elif args.command == "wait":
            return wait_for_answer(args.config, args.id, args.timeout)
        elif args.command == "watch":
            watch(sys.stdin, args.config)
        else:
            rows = judge(
                args.transcript.read_text().splitlines(),
                args.ledger_lines.read_text().splitlines(),
                args.after_seq,
                args.coin,
            )
            print("\n".join(rows))
            return EXIT_FAILED if any(row.startswith("FAIL") for row in rows) else EXIT_OK
    except Refused as exc:
        print(exc, file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
