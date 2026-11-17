import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from hl_guard import __version__

_EXIT_CODES = """exit codes:
  probe-sign     0 signature verified, 1 not verified, 2 refused
  policy check   0 policy ok, 2 refused or unreadable
  gate try       0 every proposal allowed, 1 any rejected, 2 bad input
  ledger verify  0 chain ok, 1 tampered or torn tail, 2 missing or unsafe file
  ledger repair  0 repaired or nothing to do, 1 tampered tail, 2 missing or unsafe file
  ledger show    0 records printed, 1 tampered, 2 missing or unsafe file
  latch clear    0 cleared or not latched, 1 tampered, 2 missing or unsafe file
  serve          0 client disconnected, 2 refused at start
  approve        0 approved, 1 expired, decided, unknown or not written, 2 bad config or ledger
  reject         0 rejected, 1 expired, decided, unknown or not written, 2 bad config or ledger
  pending        0 listed, 2 bad config or ledger
  init           0 approved, 1 refused, timed out or cancelled, 2 bad config or refused input
  status         0 approved, 1 not approved, expired or an old agent listed, 2 bad config or record
  revoke         0 replaced, 1 refused, timed out, no agent or old one listed, 2 bad config or input
  any command    2 on a usage error or a HOME that does not resolve"""


def _positive_int(text: str) -> int:
    if not text.isdigit() or int(text) < 1:
        raise argparse.ArgumentTypeError(f"must be an integer >= 1, got {text!r}")
    return int(text)


def _port(text: str) -> int:
    if not text.isdigit() or int(text) > 65535:
        raise argparse.ArgumentTypeError(f"must be an integer from 0 to 65535, got {text!r}")
    return int(text)


def main(argv: Sequence[str] | None = None) -> None:
    try:
        home = Path.home()
    except RuntimeError:
        print(
            f"hl-guard refused: HOME={os.environ.get('HOME')!r} does not name a home "
            "directory; set HOME to an absolute path",
            file=sys.stderr,
        )
        sys.exit(2)
    parser = argparse.ArgumentParser(
        prog="hl-guard",
        description="A self-hosted MCP server that guards Hyperliquid orders.",
        epilog=_EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"hl-guard {__version__}")
    commands = parser.add_subparsers(dest="command")
    probe = commands.add_parser(
        "probe-sign", help="sign a zero-size order with a throwaway key and post it (testnet)"
    )
    probe.add_argument("--network", required=True, choices=["testnet", "mainnet"])
    probe.add_argument("--data-dir", type=Path, default=home / ".hl-guard")
    probe.add_argument("--url", help="exchange URL; refused unless it is the pinned one")
    policy = commands.add_parser("policy", help="check a policy file against the ceilings")
    policy.add_argument("action", choices=["check"])
    policy.add_argument("path", type=Path)
    gate = commands.add_parser("gate", help="run proposals through the risk gate offline")
    gate.add_argument("action", choices=["try"])
    gate.add_argument("--policy", type=Path, required=True)
    gate.add_argument("--account", type=Path, required=True, help="account snapshot JSON")
    gate.add_argument("proposals", type=Path, help="a JSON proposal, a JSON list or JSON Lines")
    ledger = commands.add_parser("ledger", help="verify, repair or show a ledger file")
    ledger.add_argument("action", choices=["verify", "repair", "show"])
    ledger.add_argument("path", type=Path)
    ledger.add_argument("--last", type=_positive_int, default=20, help="show: number of records")
    latch = commands.add_parser(
        "latch", help="operator only: clear a daily-loss or loss-streak latch (never over MCP)"
    )
    latch.add_argument("action", choices=["clear"])
    latch.add_argument("ledger", type=Path)
    serve = commands.add_parser("serve", help="serve the read and guarded write tools over MCP")
    serve.add_argument("--config", type=Path, required=True, help="serve config YAML")
    serve.add_argument(
        "--expect-network",
        choices=["testnet", "mainnet"],
        help="must match the data dir mode file; mainnet needs both",
    )
    serve.add_argument(
        "--i-understand-autonomous",
        action="store_true",
        dest="autonomous",
        help="required when the policy says approval: never",
    )
    for verb, what in (("approve", "place"), ("reject", "drop")):
        answer = commands.add_parser(
            verb, help=f"operator only: {what} a proposal that waits for approval (never over MCP)"
        )
        answer.add_argument("id", help="the proposal id that hl-guard pending lists")
        answer.add_argument("--config", type=Path, required=True, help="serve config YAML")
    waiting = commands.add_parser("pending", help="list the proposals that wait for approval")
    waiting.add_argument("--config", type=Path, required=True, help="serve config YAML")
    for verb, what in (
        ("init", "create the agent key and approve it with your master wallet in the browser"),
        ("status", "show the agent address and when its approval expires"),
        ("revoke", "replace the agent with a new key that your master wallet approves"),
    ):
        keys = commands.add_parser(verb, help=what)
        keys.add_argument("--config", type=Path, required=True, help="serve config YAML")
        keys.add_argument(
            "--expect-network",
            choices=["testnet", "mainnet"],
            help="must match the data dir mode file; mainnet needs both",
        )
        if verb != "status":
            keys.add_argument("--days", type=int, default=30, help="approval lifetime, 1 to 180")
            keys.add_argument("--host", default="127.0.0.1", help="loopback address of the page")
            keys.add_argument("--port", type=_port, default=0, help="page port; 0 picks a free one")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        sys.exit(2)
    if args.command == "probe-sign":
        from hl_guard.exchange.probe import probe_sign

        code, report = probe_sign(args.network, args.data_dir, args.url)
        print(json.dumps(report, indent=2))
        sys.exit(code)
    if args.command == "policy":
        from hl_guard.policy.cli import policy_check

        _finish(*policy_check(args.path))
    if args.command == "gate":
        from hl_guard.policy.cli import gate_try

        _finish(*gate_try(args.policy, args.account, args.proposals))
    if args.command == "ledger":
        from hl_guard.ledger.cli import ledger_repair, ledger_show, ledger_verify

        if args.action == "verify":
            _finish(*ledger_verify(args.path))
        if args.action == "repair":
            _finish(*ledger_repair(args.path))
        _finish(*ledger_show(args.path, args.last))
    if args.command == "latch":
        from hl_guard.ledger.cli import latch_clear

        _finish(*latch_clear(args.ledger))
    if args.command == "serve":
        from hl_guard.server.app import serve as run_server

        sys.exit(run_server(args.config, args.expect_network, args.autonomous))
    if args.command in ("approve", "reject"):
        from hl_guard.approval.cli import decide, typed_yes

        approving = args.command == "approve"
        if approving and not sys.stdin.isatty():
            _finish(1, ["approve refused: run it in a terminal, where you type yes to confirm"])
        try:
            decide(args.config, args.id, approving, typed_yes if approving else None, _finish)
        except KeyboardInterrupt:
            _finish(1, [f"{args.command} cancelled: nothing written"])
    if args.command == "pending":
        from hl_guard.approval.cli import pending

        _finish(*pending(args.config))
    if args.command == "status":
        from hl_guard.cli.init import status

        _finish(*status(args.config, args.expect_network))
    if args.command in ("init", "revoke"):
        from hl_guard.cli import init

        run = init.init if args.command == "init" else init.revoke
        try:
            _finish(*run(args.config, args.days, args.host, args.port, args.expect_network))
        except KeyboardInterrupt:
            verb = args.command
            _finish(1, [f"{verb} cancelled: the key stays; run hl-guard {verb} again"])


def _finish(code: int, lines: list[str]) -> None:
    print("\n".join(lines), flush=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
