from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from decimal import Decimal
from enum import StrEnum
from typing import Literal, TypeGuard

from hl_guard.policy.ceilings import AT_MOST, CEILINGS

Network = Literal["testnet", "mainnet"]
MarginMode = Literal["isolated", "cross"]


DECIMAL_MIN = Decimal("1e-30")
DECIMAL_MAX = Decimal("1e30")
INT_LIMIT = 2**63
SIZE_RULE = "must be 0 or between 1e-30 and 1e30 in size"


def in_size(value: Decimal) -> bool:
    return not value or DECIMAL_MIN <= value.copy_abs() <= DECIMAL_MAX


def _finite(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal, got {value!r}")
    if not in_size(value):
        raise ValueError(f"{name} {SIZE_RULE}, got {value!r}")


def _positive(name: str, value: Decimal) -> None:
    _finite(name, value)
    if value <= 0:
        raise ValueError(f"{name} must be a finite Decimal above 0, got {value!r}")


def is_coin_name(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and value.isascii() and value.isalnum() and len(value) <= 16


def _count(name: str, value: int, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value < INT_LIMIT:
        raise ValueError(f"{name} must be an integer from {minimum} to 2**63-1, got {value!r}")


@dataclass(frozen=True)
class ApproveAlways:
    pass


@dataclass(frozen=True)
class ApproveAboveUsd:
    usd: Decimal


@dataclass(frozen=True)
class ApproveNever:
    pass


Approval = ApproveAlways | ApproveAboveUsd | ApproveNever


@dataclass(frozen=True)
class Policy:
    network: Network
    coins: frozenset[str]
    per_order_usd_max: Decimal
    total_exposure_usd_max: Decimal
    leverage_max: int
    open_orders_max: int
    entries_per_day_max: int
    daily_loss_pct_max: Decimal
    loss_streak_max: int
    cooldown_s: int
    stop_loss_max_pct: Decimal
    take_profit_max_pct: Decimal
    price_band_pct_max: Decimal
    snapshot_max_age_s: int
    margin_additions_per_day_max: int
    close_sell_floor: Decimal
    close_buy_cap: Decimal
    schedule_cancel_max_s: int
    add_margin_usd_max: Decimal
    approval: Approval

    def __post_init__(self) -> None:
        if self.network not in ("testnet", "mainnet"):
            raise ValueError(f"network must be testnet or mainnet, got {self.network!r}")
        if not isinstance(self.coins, frozenset) or not self.coins:
            raise ValueError(f"coins must be a non-empty frozenset, got {self.coins!r}")
        if not all(is_coin_name(c) for c in self.coins):
            raise ValueError(f"coins must be names of ASCII letters and digits, got {self.coins!r}")
        if not isinstance(self.approval, Approval):
            raise ValueError(f"approval must be an approval rule, got {self.approval!r}")
        values = vars(self)
        for bound in CEILINGS:
            value = values[bound.field]
            if bound.field in COUNT_FIELDS:
                _count(bound.field, value, minimum=1)
            else:
                _positive(bound.field, value)
            if not bound.allows(value):
                raise ValueError(f"{bound.field} = {value} breaks the product hard ceiling {bound}")
        for field, cap in AT_MOST:
            if values[field] > values[cap]:
                raise ValueError(
                    f"{field} = {values[field]} breaks the product hard ceiling "
                    f"{field} <= {cap} ({values[cap]})"
                )
        if self.per_order_usd_max > self.total_exposure_usd_max:
            raise ValueError(
                f"per_order_usd_max {self.per_order_usd_max} is above "
                f"total_exposure_usd_max {self.total_exposure_usd_max}"
            )
        if isinstance(self.approval, ApproveAboveUsd):
            _positive("approval.above_usd", self.approval.usd)
            if self.approval.usd >= self.per_order_usd_max:
                raise ValueError(
                    f"approval.above_usd {self.approval.usd} must be below per_order_usd_max "
                    f"{self.per_order_usd_max}: at or above it no order would ever ask for approval"
                )


COUNT_FIELDS = frozenset(f.name for f in fields(Policy) if f.type == "int")


@dataclass(frozen=True)
class AssetState:
    asset_id: int
    margin_mode: MarginMode
    leverage: int
    mark_px: Decimal
    mark_at_ms: int
    max_leverage: int

    def __post_init__(self) -> None:
        _count("asset_id", self.asset_id)
        _count("max_leverage", self.max_leverage, minimum=1)
        if self.margin_mode not in ("isolated", "cross"):
            raise ValueError(f"margin_mode must be isolated or cross, got {self.margin_mode!r}")
        _count("leverage", self.leverage, minimum=1)
        _positive("mark_px", self.mark_px)
        _count("mark_at_ms", self.mark_at_ms)


@dataclass(frozen=True)
class OpenOrder:
    coin: str
    notional_usd: Decimal
    reduce_only: bool

    def __post_init__(self) -> None:
        _finite("notional_usd", self.notional_usd)
        if self.notional_usd < 0:
            raise ValueError(f"notional_usd must not be negative, got {self.notional_usd}")


@dataclass(frozen=True)
class EntryRecord:
    coin: str
    at_ms: int

    def __post_init__(self) -> None:
        _count("at_ms", self.at_ms)


@dataclass(frozen=True)
class Account:
    """``snapshot_at_ms`` is when margin modes, leverage and positions were read.
    Each asset carries its own ``mark_at_ms``. ``latched`` is folded from the
    ledger's latch and unlatch records."""

    now_ms: int
    snapshot_at_ms: int
    day_start_equity_usd: Decimal
    realized_pnl_today_usd: Decimal
    loss_streak: int
    latched: bool
    assets: Mapping[str, AssetState]
    positions: Mapping[str, Decimal]
    open_orders: tuple[OpenOrder, ...]
    entries: tuple[EntryRecord, ...]
    margin_added_at_ms: tuple[int, ...]

    def __post_init__(self) -> None:
        _count("now_ms", self.now_ms)
        _count("snapshot_at_ms", self.snapshot_at_ms)
        _positive("day_start_equity_usd", self.day_start_equity_usd)
        _finite("realized_pnl_today_usd", self.realized_pnl_today_usd)
        _count("loss_streak", self.loss_streak)
        for coin, size in self.positions.items():
            _finite(f"positions.{coin}", size)
        unpriced = sorted(set(self.positions) - set(self.assets))
        if unpriced:
            raise ValueError(f"positions {unpriced} have no entry in assets")
        unmarked = sorted({o.coin for o in self.open_orders} - set(self.assets))
        if unmarked:
            raise ValueError(f"open orders {unmarked} have no entry in assets")
        for at_ms in self.margin_added_at_ms:
            _count("margin_added_at_ms", at_ms)


@dataclass(frozen=True)
class Entry:
    coin: str
    is_buy: bool
    size_coins: Decimal
    limit_px: Decimal
    stop_loss_px: Decimal | None
    take_profit_px: Decimal | None

    def __post_init__(self) -> None:
        _positive("size_coins", self.size_coins)
        _positive("limit_px", self.limit_px)
        if self.stop_loss_px is not None:
            _positive("stop_loss_px", self.stop_loss_px)
        if self.take_profit_px is not None:
            _positive("take_profit_px", self.take_profit_px)


@dataclass(frozen=True)
class Close:
    coin: str
    size_coins: Decimal
    limit_px: Decimal

    def __post_init__(self) -> None:
        _positive("size_coins", self.size_coins)
        _positive("limit_px", self.limit_px)


@dataclass(frozen=True)
class SetLeverage:
    coin: str
    leverage: int

    def __post_init__(self) -> None:
        _count("leverage", self.leverage, minimum=1)


@dataclass(frozen=True)
class ScheduleCancel:
    time_ms: int

    def __post_init__(self) -> None:
        _count("time_ms", self.time_ms)


@dataclass(frozen=True)
class AddMargin:
    coin: str
    usd: Decimal

    def __post_init__(self) -> None:
        _positive("usd", self.usd)


Proposal = Entry | Close | SetLeverage | ScheduleCancel | AddMargin


class Rule(StrEnum):
    LATCHED = "latched"
    COINS = "coins"
    SNAPSHOT_MAX_AGE_S = "snapshot_max_age_s"
    LEVERAGE_MODE = "leverage_mode"
    LEVERAGE_MAX = "leverage_max"
    PRICE_BAND_PCT_MAX = "price_band_pct_max"
    EXIT_PRICE_CAP = "exit_price_cap"
    STOP_LOSS_REQUIRED = "stop_loss_required"
    STOP_LOSS_MAX_PCT = "stop_loss_max_pct"
    STOP_LOSS_LIQUIDATION = "stop_loss_liquidation"
    TAKE_PROFIT_MAX_PCT = "take_profit_max_pct"
    PER_ORDER_USD_MAX = "per_order_usd_max"
    TOTAL_EXPOSURE_USD_MAX = "total_exposure_usd_max"
    OPEN_ORDERS_MAX = "open_orders_max"
    DAILY_LOSS_PCT_MAX = "daily_loss_pct_max"
    LOSS_STREAK_MAX = "loss_streak_max"
    COOLDOWN_S = "cooldown_s"
    ENTRIES_PER_DAY_MAX = "entries_per_day_max"
    REDUCE_ONLY = "reduce_only"
    SCHEDULE_CANCEL = "schedule_cancel"
    ADD_MARGIN = "add_margin"
    MARGIN_ADDITIONS_PER_DAY_MAX = "margin_additions_per_day_max"


@dataclass(frozen=True)
class Latch:
    rule: Literal[Rule.DAILY_LOSS_PCT_MAX, Rule.LOSS_STREAK_MAX]
    detail: str


@dataclass(frozen=True)
class Allowed:
    pass


@dataclass(frozen=True)
class Rejected:
    rule: Rule
    detail: str


Decision = Allowed | Rejected
