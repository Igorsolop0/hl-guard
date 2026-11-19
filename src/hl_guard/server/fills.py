"""Fills of the orders hl-guard placed, recorded in the ledger.

An order's fate is resting, filled, or gone: gone means it left the book
without a known filled size, so orderStatus says how much filled before its
fills are read.

Each page of userFillsByTime read is recorded as a ``fills_read`` record with
the time span it covered. An order's fills are known up to its read mark: the
end of the last span that started at or before the mark while the order was
outstanding. A span that starts at the edge of the 7-day window raises every
older mark to its end. The next read starts at the lowest mark, so a read cut
short by the /info budget goes on from where it stopped on the next call.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from hl_guard.approval.model import Status, wire
from hl_guard.ledger.ledger import Kind, Record
from hl_guard.server.parse import (
    FILLS_MAX,
    FillRow,
    OrderRow,
    parse_fills,
    parse_order_status,
)
from hl_guard.server.snapshot import CLOCK_SKEW_MS, DAY_MS, Folded
from hl_guard.server.tools_read import Runtime

FILLS_PAGE_ROWS = FILLS_MAX
FILLS_WINDOW_MS = 7 * DAY_MS
FILLS_SLACK_MS = 60_000
FILLS_LAG_BEHIND_STATUS_MS = 300_000
EXCHANGE_RULE = "exchange"
FILLS_MISSING = "fills_missing"


class FillsUnread(Exception):
    pass


Fate = Literal["resting", "filled", "gone"]


@dataclass(frozen=True)
class _Outstanding:
    id: str
    coin: str
    placed_ms: int
    left_ms: int | None
    fate: Fate
    missing: Decimal
    read_from_ms: int
    child: str


@dataclass(frozen=True)
class _Sent:
    id: str
    ts_ms: int
    expires_after: int | None


@dataclass(frozen=True)
class _Placed:
    id: str
    coin: str
    size: Decimal
    at_ms: int
    left_ms: int | None
    fate: Fate
    child: str


class BookFold:
    """What the ledger says of the orders this server sent. ``unresolved``
    maps each cloid sent with no recorded outcome to its proposal, and
    ``outstanding`` each placed order whose recorded fills fall short of its
    size."""

    def __init__(self) -> None:
        self.sent: dict[str, _Sent] = {}
        self.unresolved: dict[str, str] = {}
        self.tids: set[Any] = set()
        self.outstanding: dict[int, _Outstanding] = {}
        self._decided: set[str] = set()
        self._placed: dict[int, _Placed] = {}
        self._got: dict[int, Decimal] = {}
        self._read_from: dict[int, int] = {}

    def apply(self, record: Record) -> None:
        p = record.payload
        oid = p.get("oid")
        if record.kind is Kind.ORDER:
            self._order(record)
        elif record.kind is Kind.FILL:
            self.tids.add(p.get("tid"))
            if type(oid) is int:
                self._got[oid] = self._got.get(oid, Decimal(0)) + Decimal(p["size"])
                self._touch(oid)
        elif record.kind is Kind.DECISION:
            for cloid in [p.get("cloid"), *p.get("cloids", [])]:
                if isinstance(cloid, str):
                    self._decided.add(cloid)
                    self.unresolved.pop(cloid, None)
            if type(oid) is int:
                self._decision(record, oid)
                self._touch(oid)
        elif record.kind is Kind.FILLS_READ:
            self._fills_read(p["from_ms"], p["through_ms"], p["window"] is True)

    def _order(self, record: Record) -> None:
        p = record.payload
        action, expires = p.get("action"), p.get("expires_after")
        if not isinstance(action, Mapping) or action.get("type") != "order":
            return
        sent = _Sent(p["id"], record.ts_ms, expires if type(expires) is int else None)
        orders = action["orders"]
        if action.get("grouping") == "normalTpsl":
            orders = orders[:1]
        for o in orders:
            self.sent.setdefault(o["c"], sent)
            if o["c"] not in self._decided:
                self.unresolved[o["c"]] = p["id"]

    def _decision(self, record: Record, oid: int) -> None:
        p = record.payload
        before = self._placed.get(oid)
        if p.get("outcome") == Status.PLACED.value and isinstance(p.get("size"), str):
            fate: Fate = "resting" if p.get("status") == "resting" else "filled"
            sent = self.sent.get(p.get("cloid", ""))
            at = before.at_ms if before else sent.ts_ms if sent else record.ts_ms
            left: int | None = None
            if fate != "resting":
                left = before.left_ms if before and before.left_ms is not None else record.ts_ms
            named = p.get("child")
            child = named if isinstance(named, str) else ""
            if before is not None:
                child = before.child
            placed = _Placed(p["id"], str(p["coin"]), Decimal(p["size"]), at, left, fate, child)
            self._placed[oid] = placed
            self._read_from.setdefault(oid, at - FILLS_SLACK_MS)
        elif p.get("outcome") == Status.CANCELLED.value and before is not None:
            if p.get("settled") is True:
                del self._placed[oid]
            else:
                left = record.ts_ms if before.left_ms is None else before.left_ms
                self._placed[oid] = dataclasses.replace(before, left_ms=left, fate="gone")

    def _fills_read(self, from_ms: int, through_ms: int, window: bool) -> None:
        for oid in list(self.outstanding):
            if window or self._read_from[oid] >= from_ms:
                self._read_from[oid] = max(self._read_from[oid], through_ms)
                self._touch(oid)

    def _touch(self, oid: int) -> None:
        placed, got = self._placed.get(oid), self._got.get(oid, Decimal(0))
        if placed is None or got >= placed.size:
            self.outstanding.pop(oid, None)
        else:
            self.outstanding[oid] = _Outstanding(
                placed.id,
                placed.coin,
                placed.at_ms,
                placed.left_ms,
                placed.fate,
                placed.size - got,
                self._read_from[oid],
                placed.child,
            )


class FillBook:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt
        self._folded: Folded[BookFold] = Folded(BookFold)

    def fold(self) -> BookFold:
        return self._folded.over(self.rt.records())

    def _decide(
        self, pid: str, outcome: Status, rule: str = "", detail: str = "", **extra: Any
    ) -> None:
        payload = {"id": pid, "outcome": outcome.value, "rule": rule, "detail": detail}
        self.rt.ledger.append(Kind.DECISION, payload | extra)

    def placed(
        self, pid: str, oid: int, status: str, size: str, coin: str, cloid: str, child: str = ""
    ) -> None:
        """``child`` names the stop child the order is, so the proposal keeps its own oid."""
        extra = {"child": child} if child else {}
        self._decide(
            pid, Status.PLACED, oid=oid, status=status, size=size, coin=coin, cloid=cloid, **extra
        )

    def _resolve(self) -> None:
        """An order the exchange does not know yet stays unresolved until its
        expiresAfter has passed by the most the gate lets this clock and the
        exchange's differ, since its POST may still land."""
        book = self.fold()
        skew_max_ms = max(CLOCK_SKEW_MS, self.rt.policy.snapshot_max_age_s * 1000)
        for cloid, pid in list(book.unresolved.items()):
            state = parse_order_status(self.rt.user_post("orderStatus", oid=cloid))
            expires = book.sent[cloid].expires_after
            if state is None and expires is not None and self.rt.now_ms() <= expires + skew_max_ms:
                continue
            if state is None:
                detail = "the exchange has no order with this cloid"
                self._decide(pid, Status.FAILED, EXCHANGE_RULE, detail, cloids=[cloid])
            elif not state.resting and state.orig_size > state.remaining:
                filled = wire(state.orig_size - state.remaining)
                self.placed(pid, state.oid, "filled", filled, state.coin, cloid)
            elif state.status.endswith("rejected"):
                detail = f"the exchange reports the order {state.status}"
                self._decide(pid, Status.FAILED, EXCHANGE_RULE, detail, cloids=[cloid])
            else:
                self.placed(pid, state.oid, "resting", wire(state.orig_size), state.coin, cloid)

    def _settle_gone(self, oid: int, o: _Outstanding) -> None:
        state = parse_order_status(self.rt.user_post("orderStatus", oid=oid))
        if state is not None and state.resting:
            return
        filled = Decimal(0) if state is None else state.orig_size - state.remaining
        if filled > 0 and state is not None:
            self.placed(o.id, oid, "filled", wire(filled), state.coin, "", o.child)
        else:
            extra = {"child": o.child} if o.child else {}
            self._decide(o.id, Status.CANCELLED, detail="no fill", oid=oid, settled=True, **extra)

    def reconcile(self, open_rows: Mapping[int, OrderRow] | None = None) -> None:
        """Record the fills of orders this server placed. ``open_rows`` holds the
        resting orders of a read just made. An order still resting with its whole
        unrecorded size has nothing new to fetch; one that left the book is asked
        about with orderStatus before its fills are read."""
        if self.fold().unresolved:
            self._resolve()
        gone = {
            oid: o
            for oid, o in self.fold().outstanding.items()
            if o.fate == "gone"
            or (o.fate == "resting" and open_rows is not None and oid not in open_rows)
        }
        for oid, o in gone.items():
            self._settle_gone(oid, o)
        book = self.fold()
        need = {
            oid: o
            for oid, o in book.outstanding.items()
            if o.fate == "filled"
            or (open_rows is not None and oid in open_rows and open_rows[oid].size != o.missing)
        }
        if not need:
            return
        now = self.rt.now_ms()
        floor = now - FILLS_WINDOW_MS
        start = min(o.read_from_ms for o in need.values())
        resumed = self._read_page(max(start, floor), window=start < floor)
        while resumed is not None:
            resumed = self._read_page(resumed, window=False)
        self._settle_missing(now, need)

    def _read_page(self, start: int, *, window: bool) -> int | None:
        outstanding = self.fold().outstanding
        page = self._page(start)
        for f in sorted(page, key=lambda f: (f.time_ms, f.tid)):
            if f.oid in outstanding and f.tid not in self.fold().tids:
                self._book(outstanding[f.oid].id, f)
        through = max((f.time_ms for f in page), default=start)
        more = len(page) >= FILLS_PAGE_ROWS
        if more and through <= start:
            raise FillsUnread(f"{len(page)} fills share the time {start} ms")
        if through > start:
            span = {"from_ms": start, "through_ms": through, "window": window}
            self.rt.ledger.append(Kind.FILLS_READ, span)
        return through if more else None

    def _page(self, start_ms: int) -> tuple[FillRow, ...]:
        body = {
            "type": "userFillsByTime",
            "user": self.rt.config.account,
            "startTime": start_ms,
            "aggregateByTime": False,
        }
        return parse_fills(self.rt.info.post(body, rows_max=FILLS_PAGE_ROWS), FILLS_PAGE_ROWS)

    def _book(self, pid: str, f: FillRow) -> None:
        fill = {
            "id": pid,
            "coin": f.coin,
            "side": f.side,
            "size": wire(f.size),
            "price": wire(f.px),
            "fee": wire(f.fee),
            "oid": f.oid,
            "tid": f.tid,
            "time_ms": f.time_ms,
        }
        self.rt.ledger.append(Kind.FILL, fill)

    def _settle_missing(self, now_ms: int, need: Mapping[int, _Outstanding]) -> None:
        old = [
            (oid, o)
            for oid, o in self.fold().outstanding.items()
            if oid in need
            and o.left_ms is not None
            and o.left_ms < now_ms - FILLS_LAG_BEHIND_STATUS_MS
            and o.placed_ms < now_ms - FILLS_WINDOW_MS
        ]
        for oid, o in old:
            detail = f"{o.missing} of order {oid} never showed in the fills"
            extra = {"child": o.child} if o.child else {}
            self._decide(
                o.id, Status.CANCELLED, FILLS_MISSING, detail, oid=oid, settled=True, **extra
            )
            self.rt.ledger.append(Kind.ERROR, {"id": o.id, "detail": detail})
        if old:
            detail = f"{len(old)} orders have fills the exchange never showed; check the account"
            self.rt.ledger.append(Kind.LATCH, {"rule": FILLS_MISSING, "detail": detail})
