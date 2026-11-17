from decimal import Decimal
from typing import Any

from hl_guard.policy.load import parse_account, parse_policy
from hl_guard.policy.model import Account, Entry, Policy

NOW_MS = 1_760_000_000_000
HOUR_MS = 3_600_000

POLICY: dict[str, Any] = {
    "network": "testnet",
    "coins": ["BTC", "ETH", "SOL"],
    "per_order_usd_max": 20,
    "total_exposure_usd_max": 100,
    "leverage_max": 1,
    "open_orders_max": 3,
    "entries_per_day_max": 2,
    "daily_loss_pct_max": 3,
    "loss_streak_max": 5,
    "cooldown_s": 3600,
    "stop_loss_required": True,
    "stop_loss_max_pct": 2,
    "take_profit_max_pct": 5,
    "price_band_pct_max": 0.5,
    "snapshot_max_age_s": 5,
    "margin_additions_per_day_max": 3,
    "approval": "always",
}


def _asset(asset_id: int, mark: int) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "margin_mode": "isolated",
        "leverage": 1,
        "mark_px": mark,
        "mark_at_ms": NOW_MS - 1000,
    }


ACCOUNT: dict[str, Any] = {
    "now_ms": NOW_MS,
    "snapshot_at_ms": NOW_MS - 1000,
    "day_start_equity_usd": 1000,
    "realized_pnl_today_usd": 0,
    "loss_streak": 0,
    "latched": False,
    "assets": {"BTC": _asset(0, 100), "ETH": _asset(1, 100), "SOL": _asset(5, 100)},
    "positions": {},
    "open_orders": [],
    "entries": [],
    "margin_added_at_ms": [],
}


def policy(**overrides: Any) -> Policy:
    return parse_policy({**POLICY, **overrides})


def account(**overrides: Any) -> Account:
    return parse_account({**ACCOUNT, **overrides})


def entry(**overrides: Any) -> Entry:
    fields: dict[str, Any] = {
        "coin": "BTC",
        "is_buy": True,
        "size_coins": Decimal("0.1"),
        "limit_px": Decimal(100),
        "stop_loss_px": Decimal(99),
        "take_profit_px": Decimal(102),
    }
    fields.update(overrides)
    return Entry(**fields)
