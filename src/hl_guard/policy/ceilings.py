from __future__ import annotations

from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass
from decimal import Decimal

MAX_LEVERAGE = 5
PER_ORDER_USD_CEILING = Decimal(5000)
SCHEDULE_CANCEL_MIN_S = 5


@dataclass(frozen=True)
class Bound:
    field: str
    _: KW_ONLY
    low: Decimal | None = None
    high: Decimal | None = None
    default: Decimal | int | None = None

    def allows(self, value: Decimal | int) -> bool:
        return (self.low is None or value >= self.low) and (self.high is None or value <= self.high)

    def __str__(self) -> str:
        text = self.field
        if self.low is not None:
            text = f"{self.low} <= {text}" if self.high is not None else f"{text} >= {self.low}"
        if self.high is not None:
            text = f"{text} <= {self.high}"
        return text


CEILINGS: tuple[Bound, ...] = (
    Bound("per_order_usd_max", high=PER_ORDER_USD_CEILING),
    Bound("total_exposure_usd_max", high=Decimal(20000)),
    Bound("leverage_max", high=Decimal(MAX_LEVERAGE), default=1),
    Bound("open_orders_max", high=Decimal(20)),
    Bound("entries_per_day_max", high=Decimal(50)),
    Bound("daily_loss_pct_max", high=Decimal(10)),
    Bound("loss_streak_max", high=Decimal(10)),
    Bound("cooldown_s", low=Decimal(60)),
    Bound("stop_loss_max_pct", high=Decimal(10)),
    Bound("take_profit_max_pct", high=Decimal(50)),
    Bound("price_band_pct_max", high=Decimal(5)),
    Bound("snapshot_max_age_s", high=Decimal(30)),
    Bound("margin_additions_per_day_max", high=Decimal(10)),
    Bound("close_sell_floor", low=Decimal("0.1"), high=Decimal("0.95"), default=Decimal("0.5")),
    Bound("close_buy_cap", low=Decimal("1.05"), high=Decimal(10), default=Decimal(2)),
    Bound(
        "schedule_cancel_max_s",
        low=Decimal(SCHEDULE_CANCEL_MIN_S),
        high=Decimal(7 * 24 * 60 * 60),
        default=24 * 60 * 60,
    ),
    Bound("add_margin_usd_max", default=Decimal(10)),
)

AT_MOST: tuple[tuple[str, str], ...] = (("add_margin_usd_max", "per_order_usd_max"),)

CEILING_BY_FIELD: dict[str, Bound] = {bound.field: bound for bound in CEILINGS}

DEFAULTS: Mapping[str, Decimal | int] = {
    bound.field: bound.default for bound in CEILINGS if bound.default is not None
}
