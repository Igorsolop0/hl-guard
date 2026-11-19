"""The stop children of hl-guard's entries, folded from the ledger.

A ``child`` record is written before the entry goes out, one per child, with
the wire fields a shrink must keep. A ``child_resize`` record follows each
modify ack with the new oid and size. Decisions that name a ``child`` move its
state on; a placed decision of the parent's cloid gives the filled size.

The exchange gives a modified child a new oid under the same cloid, and
orderStatus by that cloid then answers with the old, cancelled order. So a
child is looked up by cloid in frontendOpenOrders, and by its latest oid in
orderStatus; by cloid only while it was never modified.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from hl_guard.approval.model import Status
from hl_guard.exchange.actions import RecordedChild
from hl_guard.ledger.ledger import Kind, Record

WAITING = "waiting"
LIVE = "live"
SHRUNK = "shrunk"
FIRED = "fired"
CANCELLED = "cancelled"
CLOSED = "closed: modify failed"
OPEN_STATES = frozenset({WAITING, LIVE, SHRUNK})
FILLED_WORDS = frozenset({"filled", "triggered"})
SETTLED_WORDS = frozenset(
    {"siblingfilledcanceled", "canceled", "reduceonlycanceled", "margincanceled"}
)


@dataclass(frozen=True)
class Child:
    pid: str
    parent: str
    coin: str
    cloid: str
    wire: RecordedChild
    ordered: Decimal
    size: Decimal
    oid: int | None
    resized: bool
    state: str
    seq: int

    @property
    def open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def tpsl(self) -> str:
        return self.wire.tpsl

    def view(self) -> dict[str, Any]:
        return {
            "cloid": self.cloid,
            "trigger_px": self.wire.trigger_px,
            "size": f"{self.size.normalize():f}",
            "state": self.state,
        }


def _decimal(v: Any) -> Decimal | None:
    try:
        return Decimal(v) if isinstance(v, str) else None
    except ArithmeticError:
        return None


class ChildFold:
    def __init__(self) -> None:
        self.children: dict[str, Child] = {}
        self.parent_filled: dict[str, Decimal] = {}
        self._by_oid: dict[int, str] = {}

    def apply(self, record: Record) -> None:
        p = record.payload
        if record.kind is Kind.CHILD:
            self._child(record)
        elif record.kind is Kind.CHILD_RESIZE:
            child = self.children.get(p.get("cloid", ""))
            size, oid = _decimal(p.get("size")), p.get("oid")
            if child is not None and size is not None and type(oid) is int:
                self._set(child, size=size, oid=oid, resized=True, state=SHRUNK)
        elif record.kind is Kind.DECISION:
            self._decision(p)

    def _child(self, record: Record) -> None:
        p = record.payload
        size = _decimal(p.get("size"))
        try:
            wire = RecordedChild(p["a"], p["b"], p["p"], p["trigger_px"], p["tpsl"], p["size"])
            cloid, parent, coin, pid = p["cloid"], p["parent"], p["coin"], p["id"]
        except (KeyError, TypeError):
            return
        if size is None or not all(isinstance(v, str) for v in (cloid, parent, coin, pid)):
            return
        self.children[cloid] = Child(
            pid, parent, coin, cloid, wire, size, size, None, False, WAITING, record.seq
        )

    def _set(self, child: Child, **changes: Any) -> None:
        if changes.get("oid") is not None:
            self._by_oid[changes["oid"]] = child.cloid
        self.children[child.cloid] = dataclasses.replace(child, **changes)

    def _decision(self, p: Mapping[str, Any]) -> None:
        outcome, oid = p.get("outcome"), p.get("oid")
        named = p.get("child")
        if isinstance(named, str) and named in self.children:
            self._child_decision(self.children[named], p)
            return
        if outcome == Status.PLACED.value and isinstance(p.get("cloid"), str):
            size = _decimal(p.get("size"))
            if size is not None and p.get("status") == "filled":
                self.parent_filled[p["cloid"]] = size
        if outcome == Status.FAILED.value:
            for cloid in p.get("cloids", []):
                if not isinstance(cloid, str):
                    continue
                if cloid in self.children and self.children[cloid].open:
                    detail = str(p.get("detail") or p.get("rule") or "the exchange refused it")
                    self._set(self.children[cloid], state=f"refused: {detail}")
                elif any(c.parent == cloid for c in self.children.values()):
                    self.parent_filled.setdefault(cloid, Decimal(0))
        if type(oid) is int and oid in self._by_oid:
            child = self.children[self._by_oid[oid]]
            if child.oid != oid or not child.open:
                return
            if outcome == Status.PLACED.value and p.get("status") == "filled":
                self._set(child, state=FIRED)
            elif outcome == Status.CANCELLED.value and p.get("settled") is True:
                self._set(child, state=CANCELLED)

    def _child_decision(self, child: Child, p: Mapping[str, Any]) -> None:
        outcome, oid = p.get("outcome"), p.get("oid")
        oid = oid if type(oid) is int else None
        if outcome == Status.PLACED.value:
            filled = p.get("status") == "filled"
            state = FIRED if filled else SHRUNK if child.resized else LIVE
            self._set(child, oid=oid or child.oid, state=state)
        elif outcome == Status.CANCELLED.value:
            closed = p.get("state") == CLOSED
            self._set(child, state=CLOSED if closed else CANCELLED)
        elif outcome == Status.FAILED.value:
            self._set(child, state=f"refused: {p.get('detail', '')}")

    def of(self, pid: str) -> list[Child]:
        return sorted((c for c in self.children.values() if c.pid == pid), key=lambda c: c.seq)

    def open_children(self, coin: str | None = None) -> list[Child]:
        return [c for c in self.children.values() if c.open and coin in (None, c.coin)]

    def recorded(self) -> dict[str, RecordedChild]:
        """What the signer lets a batchModify shrink: each open child at its latest size."""
        return {
            c.cloid: dataclasses.replace(c.wire, s=f"{c.size.normalize():f}")
            for c in self.children.values()
            if c.open
        }

    def target(self, child: Child) -> Decimal | None:
        """The size the child should have: never more than its parent filled."""
        filled = self.parent_filled.get(child.parent)
        return None if filled is None else min(filled, child.size)


def fold_children(records: Iterable[Record]) -> ChildFold:
    fold = ChildFold()
    for record in records:
        fold.apply(record)
    return fold


def protection(fold: ChildFold, pid: str) -> dict[str, Any] | None:
    found = fold.of(pid)
    if not found:
        return None
    return {c.tpsl: c.view() for c in found}


def own_lot(records: Iterable[Record], coin: str) -> Decimal:
    """The signed net of hl-guard's own recorded fills on ``coin``."""
    net = Decimal(0)
    for r in records:
        if r.kind is Kind.FILL and r.payload.get("coin") == coin:
            size = _decimal(r.payload.get("size")) or Decimal(0)
            net += size if r.payload.get("side") == "buy" else -size
    return net


def stop_warnings(
    fold: ChildFold, records: Iterable[Record], coin: str, position: Decimal
) -> list[str]:
    """Open own children larger than hl-guard's lot on the coin: its own net
    fills, capped by the position. It cannot tell the user's coins from its own
    after a close in the app."""
    open_children = fold.open_children(coin)
    if not open_children:
        return []
    net = own_lot(records, coin)
    same_side = (net > 0) == (position > 0) and net != 0
    lot = min(abs(net), abs(position)) if same_side else Decimal(0)
    return [
        f"the hl-guard {c.tpsl} {c.cloid} is {c.size.normalize():f} {coin}, larger than "
        f"hl-guard's own {coin} lot of {lot.normalize():f}; if you closed it in the app, "
        "cancel the stop there"
        for c in open_children
        if c.size > lot
    ]
