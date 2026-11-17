from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from hl_guard.policy.ceilings import AT_MOST, CEILING_BY_FIELD, SCHEDULE_CANCEL_MIN_S
from hl_guard.policy.gate import check
from hl_guard.policy.load import PolicyError, load_policy, parse_account, parse_proposal
from hl_guard.policy.model import (
    Account,
    AddMargin,
    Allowed,
    ApproveAboveUsd,
    ApproveAlways,
    Close,
    Entry,
    EntryRecord,
    OpenOrder,
    Policy,
    Proposal,
    SetLeverage,
)

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_BAD_INPUT = 2


def describe(policy: Policy) -> list[str]:
    match policy.approval:
        case ApproveAlways():
            approval = "always"
        case ApproveAboveUsd(usd):
            approval = f"above_usd {usd}"
        case _:
            approval = "never"
    rows = [
        ("network", policy.network, "testnet by default"),
        ("coins", ",".join(sorted(policy.coins)), "perp asset ids below 10000"),
        ("stop_loss_required", "true", "always required"),
    ]
    values = dataclasses.asdict(policy)
    linked = dict(AT_MOST)
    for field, bound in CEILING_BY_FIELD.items():
        default = "" if bound.default is None else f"default {bound.default}, "
        ceiling = f"{field} <= {linked[field]}" if field in linked else bound
        rows.append((field, str(values[field]), f"{default}ceiling {ceiling}"))
    schedule_min = f"now + {SCHEDULE_CANCEL_MIN_S} s"
    rows.append(("schedule_cancel_min", schedule_min, "fixed by the exchange"))
    rows.append(("approval", approval, "below per_order_usd_max"))
    name_w = max(len(r[0]) for r in rows)
    value_w = max(len(r[1]) for r in rows)
    return [f"{name:<{name_w}}  {value:<{value_w}}  {note}" for name, value, note in rows]


def policy_check(path: Path) -> tuple[int, list[str]]:
    try:
        policy = load_policy(path)
    except PolicyError as exc:
        return EXIT_BAD_INPUT, [f"policy refused: {exc}"]
    return EXIT_OK, [f"policy {path} ok", *describe(policy)]


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes().decode("utf-8"), parse_float=Decimal)


def _read_proposals(path: Path) -> list[Any]:
    text = path.read_bytes().decode("utf-8")
    try:
        whole = json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError:
        return [json.loads(line, parse_float=Decimal) for line in text.splitlines() if line.strip()]
    return whole if isinstance(whole, list) else [whole]


def _assume_executed(account: Account, proposal: Proposal) -> Account:
    match proposal:
        case Entry():
            px = max(proposal.limit_px, account.assets[proposal.coin].mark_px)
            order = OpenOrder(proposal.coin, proposal.size_coins * px, reduce_only=False)
            record = EntryRecord(proposal.coin, account.now_ms)
            return dataclasses.replace(
                account,
                open_orders=(*account.open_orders, order),
                entries=(*account.entries, record),
            )
        case Close():
            held = account.positions[proposal.coin]
            left = held - proposal.size_coins if held > 0 else held + proposal.size_coins
            positions = {**account.positions, proposal.coin: left}
            return dataclasses.replace(account, positions=positions)
        case SetLeverage():
            asset = dataclasses.replace(account.assets[proposal.coin], leverage=proposal.leverage)
            return dataclasses.replace(account, assets={**account.assets, proposal.coin: asset})
        case AddMargin():
            added = (*account.margin_added_at_ms, account.now_ms)
            return dataclasses.replace(account, margin_added_at_ms=added)
        case _:
            return account


def gate_try(policy_path: Path, account_path: Path, proposals_path: Path) -> tuple[int, list[str]]:
    try:
        policy = load_policy(policy_path)
        account = parse_account(_read_json(account_path))
        proposals = [parse_proposal(p) for p in _read_proposals(proposals_path)]
    except (OSError, ValueError, RecursionError) as exc:
        return EXIT_BAD_INPUT, [json.dumps({"error": str(exc)})]
    lines = []
    code = EXIT_OK
    for n, proposal in enumerate(proposals, start=1):
        decision = check(policy, account, proposal)
        row: dict[str, Any] = {"n": n, "proposal": type(proposal).__name__}
        if not isinstance(decision, Allowed):
            row |= {"decision": "rejected", "rule": decision.rule.value, "detail": decision.detail}
            code = EXIT_REJECTED
            lines.append(json.dumps(row))
            continue
        lines.append(json.dumps(row | {"decision": "allowed"}))
        try:
            account = _assume_executed(account, proposal)
        except ValueError as exc:
            return EXIT_BAD_INPUT, [*lines, json.dumps({"error": f"after proposal {n}: {exc}"})]
    return code, lines
