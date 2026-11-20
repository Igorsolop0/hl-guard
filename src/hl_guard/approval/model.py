"""Proposals and their outcomes, folded from the ledger.

The server writes a ``proposal`` record, then ``decision`` records whose
``outcome`` moves the proposal on. Only ``hl-guard approve`` and ``hl-guard
reject`` write ``approval`` records. An approval names the proposal's hash,
so it approves exactly the order the user was shown.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any, Literal, NamedTuple

from hl_guard.ledger.ledger import Kind, Record
from hl_guard.policy.model import Policy, Rule
from hl_guard.server.snapshot import GATE_CONTEXT

PLACED_WITH_ENTRY = (
    "placed on the exchange with the Ioc entry as reduce-only market triggers, "
    "sized to the filled lot"
)
PROTECTION = f"stop-loss and optional take-profit {PLACED_WITH_ENTRY}"
MIN_NOTIONAL = "min_notional"
IOC_ONLY = "ioc_only"

Tif = Literal["Gtc", "Ioc", "Alo"]
TIFS: tuple[Tif, ...] = ("Gtc", "Ioc", "Alo")


def wire(value: Decimal) -> str:
    return f"{value.normalize():f}"


@dataclass(frozen=True)
class Order:
    coin: str
    is_buy: bool
    size: Decimal
    limit_px: Decimal
    tif: Tif
    stop_loss_px: Decimal | None
    take_profit_px: Decimal | None
    leverage: int

    def as_json(self) -> dict[str, Any]:
        return {
            "coin": self.coin,
            "side": "buy" if self.is_buy else "sell",
            "size": wire(self.size),
            "limit_px": wire(self.limit_px),
            "tif": self.tif,
            "stop_loss_px": None if self.stop_loss_px is None else wire(self.stop_loss_px),
            "take_profit_px": None if self.take_profit_px is None else wire(self.take_profit_px),
            "leverage": self.leverage,
        }


MIN_NOTIONAL_USD = Decimal(10)
_DECIMAL = re.compile(r"[0-9]{1,30}(\.[0-9]{1,30})?")
_COIN = re.compile(r"[A-Za-z0-9]{1,16}")


def _positive(raw: Any) -> Decimal:
    if not isinstance(raw, str) or not _DECIMAL.fullmatch(raw) or Decimal(raw) == 0:
        raise ValueError(raw)
    return Decimal(raw)


def order_from_json(raw: Mapping[str, Any]) -> Order | None:
    try:
        stop, target = raw.get("stop_loss_px"), raw.get("take_profit_px")
        coin, side, tif, leverage = raw["coin"], raw["side"], raw["tif"], raw["leverage"]
        if not isinstance(coin, str) or not _COIN.fullmatch(coin) or side not in ("buy", "sell"):
            return None
        if tif not in TIFS or type(leverage) is not int or leverage < 1:
            return None
        return Order(
            coin,
            side == "buy",
            _positive(raw["size"]),
            _positive(raw["limit_px"]),
            tif,
            None if stop is None else _positive(stop),
            None if target is None else _positive(target),
            leverage,
        )
    except (KeyError, ValueError, TypeError):
        return None


class Refusal(NamedTuple):
    rule: str
    detail: str


def offline_problem(policy: Policy, order: Order) -> Refusal | None:
    """The first policy check that needs no exchange read and that ``order`` fails."""
    if order.coin not in policy.coins:
        return Refusal(
            Rule.COINS, f"{order.coin} is not in the policy coins {sorted(policy.coins)}"
        )
    if order.stop_loss_px is None:
        return Refusal(Rule.STOP_LOSS_REQUIRED, "every entry needs a stop-loss price")
    if order.tif != "Ioc":
        detail = f"tif {order.tif}: an entry carries its stops, so it is Ioc only"
        return Refusal(IOC_ONLY, detail)
    if order.leverage > policy.leverage_max:
        return Refusal(
            Rule.LEVERAGE_MAX, f"leverage {order.leverage}x is above {policy.leverage_max}x"
        )
    with localcontext(GATE_CONTEXT):
        notional = order.size * order.limit_px
    if notional > policy.per_order_usd_max:
        detail = f"order notional {notional} USD is above {policy.per_order_usd_max} USD"
        return Refusal(Rule.PER_ORDER_USD_MAX, detail)
    if notional < MIN_NOTIONAL_USD:
        detail = f"order notional {notional} USD is below the exchange minimum of 10 USD"
        return Refusal(MIN_NOTIONAL, detail)
    return None


def proposal_hash(proposal_id: str, network: str, account: str, order: Mapping[str, Any]) -> str:
    body = {"id": proposal_id, "network": network, "account": account, "order": order}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


class Status(StrEnum):
    PROPOSED = "proposed"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REFUSED = "refused"
    ALLOWED = "allowed"
    PLACED = "placed"
    CANCELLED = "cancelled"
    FAILED = "failed"


_OPEN = frozenset({Status.PENDING, Status.APPROVED})


@dataclass(frozen=True)
class Proposal:
    id: str
    tool: str
    order: Mapping[str, Any]
    hash: str
    status: Status
    expires_ms: int | None
    detail: str
    oid: int | None
    ttl_s: int | None


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    return value if isinstance(value, str) else ""


def _decided(p: Proposal, record: Record) -> Proposal:
    """A decision that names a ``child`` is about a stop child, not the entry."""
    payload = record.payload
    if "child" in payload:
        return p
    raw = payload.get("outcome")
    if not isinstance(raw, str) or raw not in Status.__members__.values():
        return p
    outcome = Status(raw)
    if outcome is Status.REFUSED and p.status not in _OPEN:
        return p
    rule, detail = _text(payload, "rule"), _text(payload, "detail")
    expires, oid, ttl = payload.get("expires_ms"), payload.get("oid"), payload.get("ttl_s")
    return Proposal(
        p.id,
        p.tool,
        p.order,
        p.hash,
        outcome,
        expires if type(expires) is int else p.expires_ms,
        f"{rule}: {detail}" if rule else detail,
        oid if type(oid) is int else p.oid,
        ttl if type(ttl) is int else p.ttl_s,
    )


def _approved(p: Proposal, record: Record) -> Proposal:
    if p.status is not Status.PENDING or record.payload.get("hash") != p.hash:
        return p
    if record.payload.get("approved") is True:
        return dataclasses.replace(p, status=Status.APPROVED, detail="")
    return dataclasses.replace(p, status=Status.REJECTED, detail="the user rejected it")


def fold(
    records: Iterable[Record], folded_before: Mapping[str, Proposal] | None = None
) -> dict[str, Proposal]:
    proposals = dict(folded_before or {})
    for record in records:
        pid = record.payload.get("id")
        if not isinstance(pid, str):
            continue
        if record.kind is Kind.PROPOSAL and pid not in proposals:
            order = record.payload.get("order")
            proposals[pid] = Proposal(
                pid,
                _text(record.payload, "tool"),
                order if isinstance(order, Mapping) else {},
                _text(record.payload, "hash"),
                Status.PROPOSED,
                None,
                "",
                None,
                None,
            )
        elif pid in proposals and record.kind is Kind.DECISION:
            proposals[pid] = _decided(proposals[pid], record)
        elif pid in proposals and record.kind is Kind.APPROVAL:
            proposals[pid] = _approved(proposals[pid], record)
    return proposals


def approval_problem(proposal: Proposal | None, proposal_id: str, now_ms: int) -> str | None:
    if proposal is None:
        return f"no proposal {proposal_id} in the ledger"
    if proposal.status is not Status.PENDING:
        return f"proposal {proposal_id} is {proposal.status.value}, not pending"
    if proposal.ttl_s is None:
        return f"proposal {proposal_id} has no recorded approval ttl"
    if proposal.expires_ms is None or now_ms > proposal.expires_ms:
        return f"proposal {proposal_id} expired at {proposal.expires_ms} (now {now_ms})"
    return None


def summary(order: Mapping[str, Any]) -> str:
    stop, target = order.get("stop_loss_px"), order.get("take_profit_px")
    return (
        f"{order.get('side')} {order.get('size')} {order.get('coin')} limit "
        f"{order.get('limit_px')} {order.get('tif')} at {order.get('leverage')}x isolated, "
        f"stop-loss {stop}"
        + (f", take-profit {target}" if target is not None else "")
        + f" ({PLACED_WITH_ENTRY})"
    )
