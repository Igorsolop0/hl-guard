"""``hl-guard approve``, ``reject`` and ``pending``: the user's out-of-band answer.

These commands read and append the ledger only. They never load the agent key
and never reach the network, so approving needs nothing a tool can call.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

from hl_guard.approval.model import (
    Proposal,
    approval_problem,
    fold,
    offline_problem,
    order_from_json,
    proposal_hash,
    summary,
)
from hl_guard.ledger.ledger import Kind, Ledger, LedgerError, Record, Tail
from hl_guard.policy.load import PolicyError, load_policy
from hl_guard.policy.model import Policy
from hl_guard.server.config import SERVE_LOCK, ConfigError, load_config

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_BAD_INPUT = 2


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _line(p: Proposal, now_ms: int) -> str:
    left = "" if p.expires_ms is None else f", {max(0, p.expires_ms - now_ms) // 1000} s left"
    return f"{p.id}: {summary(p.order)}{left}"


Confirm = Callable[[list[str]], bool]


def _serving(data_dir: Path) -> bool:
    try:
        fd = os.open(data_dir / SERVE_LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)
    return False


def _approve_problem(data_dir: Path, policy: Policy, proposal: Proposal) -> str | None:
    """A rechained ledger can carry any order with a matching hash, so the
    order passes the policy checks that need no exchange read before it is
    approved; the server runs the whole gate again before it places it."""
    order = order_from_json(proposal.order)
    if order is None:
        return f"the order fails the policy offline: bad_order: {proposal.order!r:.200}"
    refusal = offline_problem(policy, order)
    if refusal is not None:
        return f"the order fails the policy offline: {refusal.rule}: {refusal.detail}"
    if not _serving(data_dir):
        return (
            f"no hl-guard serve with write tools holds {data_dir / SERVE_LOCK}; start it first, "
            "since only a running server places the order"
        )
    return None


Report = Callable[[int, list[str]], None]


def _unreported(code: int, lines: list[str]) -> None:
    pass


def _holding_ctrl_c() -> bool:
    return signal.SIGINT in signal.pthread_sigmask(signal.SIG_BLOCK, ())


def decide(
    config_path: Path,
    proposal_id: str,
    approved: bool,
    confirm: Confirm | None = None,
    report: Report = _unreported,
) -> tuple[int, list[str]]:
    """``confirm`` sees what the approval binds, before anything is written, and
    answers whether to go on. Without it the caller already confirmed. Ctrl-C
    is held off from the moment the answer passed its last check until
    ``report`` has shown the outcome, so an answer that was written is never
    reported as not written. A ``report`` that exits the process keeps it
    held through the exit."""
    verb = "approve" if approved else "reject"
    try:
        outcome = _answer(config_path, proposal_id, approved, confirm)
    except KeyboardInterrupt:
        if _holding_ctrl_c():
            outcome = _answer_in_ledger(config_path, proposal_id, approved)
        else:
            outcome = EXIT_REFUSED, [f"{verb} cancelled: nothing written"]
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    report(*outcome)
    with contextlib.suppress(KeyboardInterrupt):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
    return outcome


def _answer_in_ledger(config_path: Path, proposal_id: str, approved: bool) -> tuple[int, list[str]]:
    verb = "approve" if approved else "reject"
    try:
        records = Ledger(load_config(config_path).ledger_path).records()
        answers = [
            r.payload.get("approved") is True
            for r in records
            if r.kind is Kind.APPROVAL and r.payload.get("id") == proposal_id
        ]
    except (ConfigError, LedgerError) as exc:
        return EXIT_BAD_INPUT, [
            f"{verb} interrupted: the answer may be written; reading the ledger failed: {exc}"
        ]
    if not answers:
        return EXIT_REFUSED, [f"{verb} cancelled: nothing written"]
    recorded = "approved" if answers[0] else "rejected"
    code = EXIT_OK if answers[0] is approved else EXIT_REFUSED
    return code, [f"{verb} interrupted: the ledger records proposal {proposal_id} as {recorded}"]


def _answer(
    config_path: Path, proposal_id: str, approved: bool, confirm: Confirm | None
) -> tuple[int, list[str]]:
    verb = "approve" if approved else "reject"
    try:
        config = load_config(config_path)
        policy = load_policy(config.policy_path)
        network = policy.network
        tail = Tail(Ledger(config.ledger_path))
        read = tail.records()
        proposal = fold(read).get(proposal_id)
    except (ConfigError, PolicyError, LedgerError) as exc:
        return EXIT_BAD_INPUT, [f"{verb} FAILED: {exc}"]
    problem = approval_problem(proposal, proposal_id, _now_ms())
    if problem is not None or proposal is None:
        return EXIT_REFUSED, [f"{verb} refused: {problem}"]
    if proposal_hash(proposal_id, network, config.account, proposal.order) != proposal.hash:
        return EXIT_REFUSED, [f"{verb} refused: proposal {proposal_id} does not match its hash"]
    if approved:
        refused = _approve_problem(config.data_dir, policy, proposal)
        if refused is not None:
            return EXIT_REFUSED, [f"approve refused: {refused}"]
    shown = [_line(proposal, _now_ms()), f"account {config.account} on {network}"]
    if confirm is not None and not confirm([*shown, f"hash {proposal.hash}"]):
        return EXIT_REFUSED, [f"{verb} cancelled: nothing written"]

    def still_pending_then_hold_ctrl_c(fresh: list[Record], answer_ts_ms: int) -> None:
        current = fold(fresh, {proposal_id: proposal})[proposal_id]
        late = approval_problem(current, proposal_id, answer_ts_ms)
        if late is not None and answer_ts_ms > _now_ms():
            late += (
                "; the ledger's newest record is later than this clock and an answer takes "
                "that time, so check the system clock"
            )
        if late is not None:
            raise ValueError(late)
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})

    payload = {"id": proposal_id, "hash": proposal.hash, "approved": approved}
    try:
        tail.append(
            Kind.APPROVAL, payload, after_seq=len(read), check=still_pending_then_hold_ctrl_c
        )
    except LedgerError as exc:
        return EXIT_BAD_INPUT, [f"{verb} FAILED: {exc}"]
    except ValueError as exc:
        return EXIT_REFUSED, [f"{verb} refused: {exc}"]
    done = "approved" if approved else "rejected"
    lines = [f"{done} {shown[0]}", shown[1], f"hash {proposal.hash}"]
    if approved and not _serving(config.data_dir):
        lines.append(
            f"no hl-guard serve holds {config.data_dir / SERVE_LOCK} now: a serve that starts "
            f"within {proposal.ttl_s} s runs the gate again and places it if it passes; after "
            "that it expires"
        )
    return EXIT_OK, lines


def pending(config_path: Path) -> tuple[int, list[str]]:
    try:
        proposals = fold(Ledger(load_config(config_path).ledger_path).records())
    except (ConfigError, LedgerError) as exc:
        return EXIT_BAD_INPUT, [f"pending FAILED: {exc}"]
    now = _now_ms()
    waiting = [_line(p, now) for p in proposals.values() if approval_problem(p, p.id, now) is None]
    return EXIT_OK, waiting or ["no proposal waits for approval"]


def typed_yes(shown: list[str]) -> bool:
    print("\n".join(shown))
    try:
        return input("Type yes to approve this order: ").strip() == "yes"
    except (EOFError, KeyboardInterrupt):
        print()
        return False
