from __future__ import annotations

import json
from pathlib import Path

from hl_guard.ledger.ledger import (
    Ledger,
    LedgerAccessError,
    LedgerError,
    Tail,
    TornTail,
    clock_warning,
)
from hl_guard.policy.model import Rule

EXIT_OK = 0
EXIT_TAMPERED = 1
EXIT_BAD_INPUT = 2

_TAIL_ONLY = "repair checks only the tail; run hl-guard ledger verify to check every line"

DRAWDOWN_RELATCH = (
    "note: the day start stays, so the next entry latches again while today's loss is at or "
    "past the daily loss limit, until the UTC day ends"
)


def _describe(torn: TornTail) -> str:
    if torn.is_record:
        return f"the last record ({torn.size} bytes) lacks its newline"
    return f"{torn.size} torn bytes follow the last record"


def _repaired(torn: TornTail) -> str:
    if torn.is_record:
        return f"the last record ({torn.size} bytes) lacked its newline; added it"
    return f"dropped {torn.size} torn bytes after the last record"


def _missing(path: Path) -> tuple[int, list[str]] | None:
    if path.is_dir():
        return EXIT_BAD_INPUT, [f"ledger {path} is a directory"]
    if not path.exists():
        return EXIT_BAD_INPUT, [f"ledger {path} does not exist"]
    return None


def _failed(verb: str, exc: LedgerError) -> tuple[int, list[str]]:
    code = EXIT_BAD_INPUT if isinstance(exc, LedgerAccessError) else EXIT_TAMPERED
    return code, [f"{verb} FAILED: {exc}"]


def ledger_verify(path: Path) -> tuple[int, list[str]]:
    if missing := _missing(path):
        return missing
    ledger = Ledger(path)
    try:
        records = ledger.replay(verify=True)
        torn = ledger.torn_tail()
    except LedgerError as exc:
        return _failed("verify", exc)
    head = records[-1] if records else None
    anchor = f"{len(records)} records, head {head.hash if head else 'empty'}"
    warning = [w] if (w := clock_warning(head)) else []
    if torn is not None:
        return EXIT_TAMPERED, [
            f"verify FAILED: {_describe(torn)}; run hl-guard ledger repair",
            f"committed: {anchor}",
            *warning,
        ]
    return EXIT_OK, [f"verify ok: {anchor}", *warning]


def ledger_repair(path: Path) -> tuple[int, list[str]]:
    if missing := _missing(path):
        return missing
    try:
        torn = Ledger(path).repair()
    except LedgerError as exc:
        return _failed("repair", exc)
    done = "repair: nothing to do" if torn is None else f"repaired: {_repaired(torn)}"
    return EXIT_OK, [done, _TAIL_ONLY]


def ledger_show(path: Path, last: int) -> tuple[int, list[str]]:
    if missing := _missing(path):
        return missing
    try:
        records = list(Ledger(path).records())
    except LedgerError as exc:
        return _failed("show", exc)
    return EXIT_OK, [
        json.dumps({"seq": r.seq, "ts_ms": r.ts_ms, "kind": r.kind.value, "payload": r.payload})
        for r in records[-last:]
    ]


def latch_clear(path: Path) -> tuple[int, list[str]]:
    if missing := _missing(path):
        return missing
    try:
        tail = Tail(Ledger(path))
        tail.records()
        cleared, torn = tail.unlatch()
    except LedgerError as exc:
        return _failed("latch clear", exc)
    lines = [] if torn is None else [f"repaired first: {_repaired(torn)}"]
    if cleared is None:
        return EXIT_OK, [*lines, "not latched: no unlatch record written"]
    latch = cleared.latch
    why = f"{latch.payload.get('rule')}: {latch.payload.get('detail')}"
    done = f"unlatched: wrote record {cleared.record.seq}, which clears record {latch.seq} ({why})"
    if latch.payload.get("rule") == Rule.DAILY_LOSS_PCT_MAX.value:
        return EXIT_OK, [*lines, done, DRAWDOWN_RELATCH]
    return EXIT_OK, [*lines, done]
