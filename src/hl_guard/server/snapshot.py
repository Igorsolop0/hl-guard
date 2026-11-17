from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from typing import Generic, Protocol, TypeVar

from hl_guard.ledger.ledger import Fill, Kind, Record, _State
from hl_guard.policy.gate import gross_exposure_usd, trip
from hl_guard.policy.model import Account, AssetState, EntryRecord, OpenOrder, Policy, Rule
from hl_guard.server.parse import AccountState, Leverage, Market, OrderRow

GATE_CONTEXT = Context(
    prec=28,
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)
PNL_QUANTUM = Decimal("0.000001")
ACCOUNT_ROWS_MAX = 1000
DAY_MS = 24 * 60 * 60 * 1000
CLOCK_SKEW_MS = 2000


class SnapshotError(ValueError):
    pass


@dataclass(frozen=True)
class LedgerView:
    realized_pnl_today_usd: Decimal
    loss_streak: int
    latch: Record | None
    entries: tuple[EntryRecord, ...]


def _held(state: _State, coin: str) -> Decimal:
    return state.positions.get(coin, (Decimal(0), Decimal(0)))[0]


@dataclass(frozen=True)
class _Move:
    opens_cycle: bool
    ends_cycle: bool


def _move(before: Decimal, after: Decimal) -> _Move:
    flipped = before != 0 and after != 0 and (before > 0) != (after > 0)
    return _Move(
        opens_cycle=after != 0 and (before == 0 or flipped),
        ends_cycle=before != 0 and (after == 0 or flipped),
    )


def _floor_to_micro_dollar(usd: Decimal) -> Decimal:
    return usd.quantize(PNL_QUANTUM, rounding=ROUND_FLOOR) or Decimal(0)


def _fill_time(record: Record) -> int:
    at = record.payload.get("time_ms")
    return at if type(at) is int else record.ts_ms


def _entry_orders(record: Record, coins: Mapping[str, str]) -> list[tuple[str, EntryRecord]]:
    """(cloid, entry) of each order an ORDER record of propose_order sent."""
    action, pid = record.payload.get("action"), record.payload.get("id")
    coin = coins.get(pid) if isinstance(pid, str) else None
    if not isinstance(action, Mapping) or action.get("type") != "order" or coin is None:
        return []
    return [(o["c"], EntryRecord(coin, record.ts_ms)) for o in action["orders"]]


class Reducer(Protocol):
    def apply(self, record: Record) -> None: ...


R = TypeVar("R", bound=Reducer)


class Folded(Generic[R]):
    def __init__(self, make: Callable[[], R]) -> None:
        self._make = make
        self._reducer = make()
        self._seen = 0
        self._head = ""

    def over(self, records: Sequence[Record]) -> R:
        seen = self._seen
        if len(records) < seen or (seen and records[seen - 1].hash != self._head):
            self._reducer, seen = self._make(), 0
        for record in records[seen:]:
            self._reducer.apply(record)
        self._seen = len(records)
        self._head = records[-1].hash if records else ""
        return self._reducer


class RiskFold:
    """The ledger's risk history. An entry counts when its opening order is
    sent, at the ORDER record's time, whether it rests or fills, and it keeps
    counting when it is cancelled unfilled. Only an order the exchange refused
    stops counting. A fill that opens a cycle counts as an entry only for a
    proposal with no sent order. Day accounting uses the exchange fill time."""

    def __init__(self) -> None:
        self.state = _State(verify=False)
        self.last_ts_ms = 0
        self._pnl_by_day: dict[int, Decimal] = {}
        self._streak = 0
        self._cycle_pnl: dict[str, Decimal] = {}
        self._proposed: dict[str, str] = {}
        self._sent: dict[str, EntryRecord] = {}
        self._sent_ids: set[str] = set()
        self._opened: list[EntryRecord] = []

    def apply(self, record: Record) -> None:
        self.last_ts_ms = record.ts_ms
        with localcontext(GATE_CONTEXT):
            self._apply(record)

    def _apply(self, record: Record) -> None:
        p = record.payload
        if record.kind is Kind.PROPOSAL and p.get("tool") == "propose_order":
            order = p.get("order")
            coin = order.get("coin") if isinstance(order, Mapping) else None
            if isinstance(coin, str) and isinstance(p.get("id"), str):
                self._proposed.setdefault(p["id"], coin)
        elif record.kind is Kind.ORDER:
            for cloid, entry in _entry_orders(record, self._proposed):
                self._sent[cloid] = entry
                self._sent_ids.add(p["id"])
        elif record.kind is Kind.DECISION and p.get("outcome") == "failed":
            for cloid in p.get("cloids", []):
                if isinstance(cloid, str):
                    self._sent.pop(cloid, None)
        if record.kind is not Kind.FILL:
            latched = self.state.latch
            self.state.apply(record)
            if latched is not None and self.state.latch is None:
                self._streak = 0
            return
        coin = Fill.parse(record).coin
        at = _fill_time(record)
        before_held, before_pnl = _held(self.state, coin), self.state.realized
        self.state.apply(record)
        after_held, pnl = _held(self.state, coin), self.state.realized - before_pnl
        day = at // DAY_MS
        self._pnl_by_day[day] = self._pnl_by_day.get(day, Decimal(0)) + pnl
        move = _move(before_held, after_held)
        self._cycle_pnl[coin] = self._cycle_pnl.get(coin, Decimal(0)) + pnl
        if move.ends_cycle:
            self._streak = self._streak + 1 if self._cycle_pnl.pop(coin) < 0 else 0
        if move.opens_cycle:
            self._cycle_pnl.setdefault(coin, Decimal(0))
            if p.get("id") not in self._sent_ids:
                self._opened.append(EntryRecord(coin, at))

    def view(self, now_ms: int, cooldown_s: int) -> LedgerView:
        """Sent entries come in ORDER record order, whose times never go back,
        so the scan for recent ones stops at the first older one."""
        today = now_ms // DAY_MS
        since_ms = min(today * DAY_MS, now_ms - cooldown_s * 1000)
        with localcontext(GATE_CONTEXT):
            realized = sum(
                (pnl for day, pnl in self._pnl_by_day.items() if day >= today), Decimal(0)
            )
            rounded = _floor_to_micro_dollar(realized)
        sent: list[EntryRecord] = []
        for entry in reversed(self._sent.values()):
            if entry.at_ms < since_ms:
                break
            sent.append(entry)
        opened = [e for e in self._opened if e.at_ms >= since_ms]
        recent = sorted((*reversed(sent), *opened), key=lambda e: e.at_ms)
        return LedgerView(rounded, self._streak, self.state.latch, tuple(recent))


def fold_ledger(records: Iterable[Record], now_ms: int, cooldown_s: int) -> LedgerView:
    fold = RiskFold()
    for record in records:
        fold.apply(record)
    return fold.view(now_ms, cooldown_s)


@dataclass(frozen=True)
class Mark:
    px: Decimal
    at_ms: int


@dataclass(frozen=True)
class Exchange:
    """``markets`` gives asset ids and limits; ``marks`` the prices the gate trusts."""

    state: AccountState
    state_received_ms: int
    markets: Mapping[str, Market]
    marks: Mapping[str, Mark]
    orders: tuple[OrderRow, ...]
    leverages: Mapping[str, Leverage]


def perp_orders(exchange: Exchange) -> tuple[OrderRow, ...]:
    return tuple(o for o in exchange.orders if o.coin in exchange.markets)


def coins_needing_leverage(exchange: Exchange) -> list[str]:
    held = {p.coin for p in exchange.state.positions}
    return sorted({o.coin for o in perp_orders(exchange)} - held)


def snapshot_time_ms(received_ms: int, exchange_ms: int) -> int:
    if exchange_ms > received_ms + CLOCK_SKEW_MS:
        return exchange_ms
    return min(received_ms, exchange_ms)


def position_marks(state: AccountState, received_ms: int) -> dict[str, Mark]:
    with localcontext(GATE_CONTEXT):
        return {
            p.coin: Mark(p.position_value / abs(p.size), received_ms)
            for p in state.positions
            if p.size != 0
        }


def build_account(
    now_ms: int,
    day_start_equity_usd: Decimal,
    exchange: Exchange,
    view: LedgerView,
    extra_coins: Iterable[str] = (),
) -> Account:
    markets, marks = exchange.markets, exchange.marks
    leverage = {**exchange.leverages, **{p.coin: p.leverage for p in exchange.state.positions}}
    orders = perp_orders(exchange)
    coins = sorted(
        {p.coin for p in exchange.state.positions} | {o.coin for o in orders} | {*extra_coins}
    )
    if len(coins) > ACCOUNT_ROWS_MAX:
        raise SnapshotError(f"{len(coins)} coins held or ordered; the cap is {ACCOUNT_ROWS_MAX}")
    for what, known in (("market", markets), ("leverage", leverage), ("mark", marks)):
        unknown = [c for c in coins if c not in known]
        if unknown:
            raise SnapshotError(f"coins {unknown} have no {what} in this read")
    try:
        assets = {
            coin: AssetState(
                asset_id=markets[coin].asset_id,
                margin_mode=leverage[coin].mode,
                leverage=leverage[coin].value,
                mark_px=marks[coin].px,
                mark_at_ms=marks[coin].at_ms,
                max_leverage=markets[coin].max_leverage,
            )
            for coin in coins
        }
        with localcontext(GATE_CONTEXT):
            open_orders = tuple(
                OpenOrder(o.coin, o.size * max(o.limit_px, o.trigger_px or 0), o.reduce_only)
                for o in orders
            )
        return Account(
            now_ms=now_ms,
            snapshot_at_ms=snapshot_time_ms(exchange.state_received_ms, exchange.state.time_ms),
            day_start_equity_usd=day_start_equity_usd,
            realized_pnl_today_usd=view.realized_pnl_today_usd,
            loss_streak=view.loss_streak,
            latched=view.latch is not None,
            assets=assets,
            positions={p.coin: p.size for p in exchange.state.positions if p.size != 0},
            open_orders=open_orders,
            entries=view.entries,
            margin_added_at_ms=(),
        )
    except ValueError as exc:
        raise SnapshotError(f"account snapshot refused: {exc}") from None


def _fresh(policy: Policy, now_ms: int, at_ms: int) -> bool:
    return 0 <= now_ms - at_ms <= policy.snapshot_max_age_s * 1000


def _age(policy: Policy, now_ms: int, what: str, at_ms: int) -> str:
    age_ms = now_ms - at_ms
    when = f"{age_ms} ms before now" if age_ms >= 0 else f"{-age_ms} ms ahead of the local clock"
    cap = policy.snapshot_max_age_s
    return f"{Rule.SNAPSHOT_MAX_AGE_S.value}: {what} is {when}; the cap is {cap} s"


def held_blocks(halted: bool, latched: bool) -> list[str]:
    return (["halted: the HALT file is in the data dir"] if halted else []) + (
        ["latched: run hl-guard latch clear"] if latched else []
    )


def new_entry_blocks(policy: Policy, account: Account, *, halted: bool) -> list[str]:
    now = account.now_ms
    blocks = held_blocks(halted, account.latched)
    with localcontext(GATE_CONTEXT):
        tripped = trip(policy, account)
        gross = gross_exposure_usd(account)
    if tripped is not None:
        blocks.append(f"{tripped.rule.value}: {tripped.detail}")
    if not _fresh(policy, now, account.snapshot_at_ms):
        blocks.append(
            _age(policy, now, "the exchange time of the account read", account.snapshot_at_ms)
        )
    marks = sorted(
        {a.mark_at_ms for a in account.assets.values() if not _fresh(policy, now, a.mark_at_ms)}
    )
    if marks:
        blocks.append(_age(policy, now, "the mark price read", marks[0]))
    if gross >= policy.total_exposure_usd_max:
        blocks.append(
            f"{Rule.TOTAL_EXPOSURE_USD_MAX.value}: exposure is {gross} USD, at or above "
            f"{policy.total_exposure_usd_max} USD"
        )
    opening = sum(1 for o in account.open_orders if not o.reduce_only)
    if opening >= policy.open_orders_max:
        blocks.append(
            f"{Rule.OPEN_ORDERS_MAX.value}: {opening} opening orders rest; "
            f"the cap is {policy.open_orders_max}"
        )
    today = sum(1 for e in account.entries if e.at_ms // DAY_MS == now // DAY_MS)
    if today >= policy.entries_per_day_max:
        blocks.append(
            f"{Rule.ENTRIES_PER_DAY_MAX.value}: {today} entries today (UTC) reached the cap "
            f"{policy.entries_per_day_max}"
        )
    if any(e.at_ms > now for e in account.entries):
        blocks.append(f"{Rule.COOLDOWN_S.value}: an entry is later than now: clock moved back")
    cooling = sorted(
        {e.coin for e in account.entries if 0 <= now - e.at_ms < policy.cooldown_s * 1000}
        & policy.coins
    )
    if cooling and set(cooling) == policy.coins:
        blocks.append(f"{Rule.COOLDOWN_S.value}: every policy coin is in its cooldown")
    elif cooling:
        blocks.append(
            f"{Rule.COOLDOWN_S.value}: {', '.join(cooling)} entered less than "
            f"{policy.cooldown_s} s ago"
        )
    unfit = sorted(
        coin
        for coin, asset in account.assets.items()
        if coin in policy.coins
        and (asset.margin_mode != "isolated" or asset.leverage > policy.leverage_max)
    )
    leverage = f"cross or above {policy.leverage_max}x on the exchange"
    if set(unfit) == policy.coins:
        blocks.append(f"{Rule.LEVERAGE_MODE.value}: every policy coin is {leverage}")
    elif unfit:
        verb = "is" if len(unfit) == 1 else "are"
        blocks.append(f"{Rule.LEVERAGE_MODE.value}: {', '.join(unfit)} {verb} {leverage}")
    if policy.coins <= {*cooling, *unfit} and policy.coins not in (set(cooling), set(unfit)):
        blocks.append(
            f"{Rule.COOLDOWN_S.value}, {Rule.LEVERAGE_MODE.value}: every policy coin is in "
            f"its cooldown or {leverage}"
        )
    return blocks
