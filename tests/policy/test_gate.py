import random
from decimal import Decimal
from typing import Any

import pytest
from policy_fixtures import ACCOUNT, HOUR_MS, NOW_MS, account, entry, policy

from hl_guard.policy.gate import check, trip
from hl_guard.policy.load import PolicyError, parse_account, parse_proposal
from hl_guard.policy.model import (
    AddMargin,
    Allowed,
    Close,
    Latch,
    Proposal,
    Rejected,
    Rule,
    ScheduleCancel,
    SetLeverage,
)

DAY_MS = 24 * HOUR_MS


def _asset(**overrides: Any) -> dict[str, Any]:
    return {"BTC": {**ACCOUNT["assets"]["BTC"], **overrides}}


def _resting(n: int, notional: int = 10, reduce_only: bool = False) -> list[dict[str, Any]]:
    return [{"coin": "ETH", "notional_usd": notional, "reduce_only": reduce_only}] * n


FIVE_X: dict[str, Any] = {"leverage_max": 5, "stop_loss_max_pct": 10}
AT_LIQUIDATION = Decimal("109.090909090909090909090909091")

DOGE = {**ACCOUNT["assets"], "DOGE": {**ACCOUNT["assets"]["BTC"], "asset_id": 9}}

OPEN_BTC: dict[str, Any] = {"positions": {"BTC": Decimal("0.3")}}
LONG_BTC: dict[str, Any] = {"positions": {"BTC": Decimal("0.1")}}
SHORT_BTC: dict[str, Any] = {"positions": {"BTC": Decimal("-0.1")}}
STALE_ETH = {
    **ACCOUNT["assets"],
    "ETH": {**ACCOUNT["assets"]["ETH"], "mark_px": 50, "mark_at_ms": 0},
}

HALTED: dict[str, Any] = {
    "positions": {"BTC": Decimal("0.3")},
    "realized_pnl_today_usd": -500,
    "loss_streak": 9,
    "entries": [{"coin": "BTC", "at_ms": NOW_MS}] * 5,
}

REJECTIONS: list[tuple[str, dict[str, Any], dict[str, Any], Proposal, Rule]] = [
    ("coin outside the policy", {}, {"assets": DOGE}, entry(coin="DOGE"), Rule.COINS),
    ("coin unknown to the exchange", {"coins": ["BTC", "XRP"]}, {}, entry(coin="XRP"), Rule.COINS),
    ("cross margin", {}, {"assets": _asset(margin_mode="cross")}, entry(), Rule.LEVERAGE_MODE),
    ("isolated 3x", {}, {"assets": _asset(leverage=3)}, entry(), Rule.LEVERAGE_MODE),
    ("no stop-loss", {}, {}, entry(stop_loss_px=None), Rule.STOP_LOSS_REQUIRED),
    ("stop above a buy", {}, {}, entry(stop_loss_px=Decimal(101)), Rule.STOP_LOSS_REQUIRED),
    (
        "a buy stop-loss on the profit side of limit and mark",
        {},
        {},
        entry(stop_loss_px=Decimal("100.5"), take_profit_px=None),
        Rule.STOP_LOSS_REQUIRED,
    ),
    ("a buy against a short", {}, SHORT_BTC, entry(), Rule.OPPOSITE_POSITION),
    (
        "a sell against a long",
        {},
        LONG_BTC,
        entry(is_buy=False, stop_loss_px=Decimal(101), take_profit_px=None),
        Rule.OPPOSITE_POSITION,
    ),
    (
        "stop above the mark of a marketable buy",
        {"price_band_pct_max": 5},
        {"assets": _asset(mark_px=98)},
        entry(stop_loss_px=Decimal("98.5")),
        Rule.STOP_LOSS_REQUIRED,
    ),
    (
        "stop below a sell",
        {},
        {},
        entry(is_buy=False, stop_loss_px=Decimal(99), take_profit_px=None),
        Rule.STOP_LOSS_REQUIRED,
    ),
    ("stop too far", {}, {}, entry(stop_loss_px=Decimal("97.99")), Rule.STOP_LOSS_MAX_PCT),
    (
        "a short stop past the liquidation price at 5x on a 5x coin",
        FIVE_X,
        {"assets": _asset(leverage=5, max_leverage=5)},
        entry(is_buy=False, stop_loss_px=Decimal("109.1"), take_profit_px=None),
        Rule.STOP_LOSS_LIQUIDATION,
    ),
    (
        "a short stop exactly at the liquidation distance",
        FIVE_X,
        {"assets": _asset(leverage=5, max_leverage=5)},
        entry(is_buy=False, stop_loss_px=AT_LIQUIDATION, take_profit_px=None),
        Rule.STOP_LOSS_LIQUIDATION,
    ),
    ("target too far", {}, {}, entry(take_profit_px=Decimal("105.01")), Rule.TAKE_PROFIT_MAX_PCT),
    (
        "target past the cap from the limit, inside it from the mark",
        {"price_band_pct_max": 5, "take_profit_max_pct": 50},
        {"assets": _asset(mark_px=101)},
        entry(take_profit_px=Decimal(151)),
        Rule.TAKE_PROFIT_MAX_PCT,
    ),
    (
        "target on the loss side",
        {},
        {},
        entry(take_profit_px=Decimal("98.5")),
        Rule.TAKE_PROFIT_MAX_PCT,
    ),
    (
        "buy target at the limit",
        {},
        {},
        entry(take_profit_px=Decimal(100)),
        Rule.TAKE_PROFIT_MAX_PCT,
    ),
    (
        "buy target at a mark above the limit",
        {},
        {"assets": _asset(mark_px=Decimal("100.3"))},
        entry(take_profit_px=Decimal("100.3")),
        Rule.TAKE_PROFIT_MAX_PCT,
    ),
    (
        "sell target at the limit",
        {},
        {},
        entry(is_buy=False, stop_loss_px=Decimal(101), take_profit_px=Decimal(100)),
        Rule.TAKE_PROFIT_MAX_PCT,
    ),
    (
        "sell target at a mark below the limit",
        {},
        {"assets": _asset(mark_px=Decimal("99.7"))},
        entry(is_buy=False, stop_loss_px=Decimal(101), take_profit_px=Decimal("99.7")),
        Rule.TAKE_PROFIT_MAX_PCT,
    ),
    ("order above the cap", {}, {}, entry(size_coins=Decimal("0.2001")), Rule.PER_ORDER_USD_MAX),
    (
        "a sell limit under the mark is sized at the mark",
        {"stop_loss_max_pct": 10, "price_band_pct_max": 5},
        {"assets": _asset(mark_px=105)},
        entry(
            is_buy=False,
            size_coins=Decimal("0.195"),
            limit_px=Decimal(100),
            stop_loss_px=Decimal(106),
            take_profit_px=None,
        ),
        Rule.PER_ORDER_USD_MAX,
    ),
    (
        "exposure from positions and resting entries",
        {},
        {"positions": {"BTC": Decimal("0.5")}, "open_orders": _resting(1, 41)},
        entry(),
        Rule.TOTAL_EXPOSURE_USD_MAX,
    ),
    (
        "a short counts as exposure",
        {},
        {"positions": {"SOL": -1}},
        entry(),
        Rule.TOTAL_EXPOSURE_USD_MAX,
    ),
    ("three opening orders rest", {}, {"open_orders": _resting(3)}, entry(), Rule.OPEN_ORDERS_MAX),
    (
        "daily loss at the limit",
        {},
        {"realized_pnl_today_usd": -30},
        entry(),
        Rule.DAILY_LOSS_PCT_MAX,
    ),
    ("five losses in a row", {}, {"loss_streak": 5}, entry(), Rule.LOSS_STREAK_MAX),
    (
        "same coin inside the cooldown",
        {},
        {"entries": [{"coin": "BTC", "at_ms": NOW_MS - 3_599_999}]},
        entry(),
        Rule.COOLDOWN_S,
    ),
    (
        "an entry later than now",
        {},
        {"entries": [{"coin": "ETH", "at_ms": NOW_MS + 1}]},
        entry(),
        Rule.COOLDOWN_S,
    ),
    (
        "two entries today",
        {},
        {"entries": [{"coin": "ETH", "at_ms": NOW_MS - 2 * HOUR_MS}] * 2},
        entry(),
        Rule.ENTRIES_PER_DAY_MAX,
    ),
    ("close without a position", {}, {}, Close("BTC", Decimal(1), Decimal(100)), Rule.REDUCE_ONLY),
    (
        "close above the position",
        {},
        {"positions": {"BTC": Decimal("-0.1")}},
        Close("BTC", Decimal("0.11"), Decimal(100)),
        Rule.REDUCE_ONLY,
    ),
    ("leverage above the cap", {}, {}, SetLeverage("BTC", 2), Rule.LEVERAGE_MAX),
    (
        "leverage on a coin outside the policy",
        {},
        {"assets": DOGE},
        SetLeverage("DOGE", 1),
        Rule.COINS,
    ),
    (
        "leverage on a coin the exchange lacks",
        {"coins": ["BTC", "XRP"]},
        {},
        SetLeverage("XRP", 1),
        Rule.COINS,
    ),
    ("scheduleCancel now", {}, {}, ScheduleCancel(NOW_MS), Rule.SCHEDULE_CANCEL),
    (
        "scheduleCancel past 24 h",
        {},
        {},
        ScheduleCancel(NOW_MS + DAY_MS + 1),
        Rule.SCHEDULE_CANCEL,
    ),
    (
        "scheduleCancel under 5 s ahead",
        {},
        {},
        ScheduleCancel(NOW_MS + 4999),
        Rule.SCHEDULE_CANCEL,
    ),
    ("an entry while latched", {}, {"latched": True}, entry(), Rule.LATCHED),
    ("leverage while latched", {}, {"latched": True}, SetLeverage("BTC", 1), Rule.LATCHED),
    (
        "leverage at the daily loss limit",
        {},
        HALTED,
        SetLeverage("BTC", 1),
        Rule.DAILY_LOSS_PCT_MAX,
    ),
    (
        "a marketable buy far above the mark",
        {"stop_loss_max_pct": 10},
        {},
        entry(limit_px=Decimal(111), stop_loss_px=Decimal("99.9"), take_profit_px=None),
        Rule.PRICE_BAND_PCT_MAX,
    ),
    (
        "a limit just outside the band",
        {},
        {},
        entry(limit_px=Decimal("100.51")),
        Rule.PRICE_BAND_PCT_MAX,
    ),
    (
        "a sell limit far under the mark",
        {},
        {},
        entry(is_buy=False, limit_px=Decimal(99), stop_loss_px=Decimal(101), take_profit_px=None),
        Rule.PRICE_BAND_PCT_MAX,
    ),
    (
        "a sell close under half the mark",
        {},
        LONG_BTC,
        Close("BTC", Decimal("0.1"), Decimal("49.99")),
        Rule.EXIT_PRICE_CAP,
    ),
    (
        "a sell close under half a stale mark",
        {},
        {**LONG_BTC, "assets": _asset(mark_at_ms=NOW_MS - DAY_MS)},
        Close("BTC", Decimal("0.1"), Decimal(40)),
        Rule.EXIT_PRICE_CAP,
    ),
    (
        "a buy close over twice the mark",
        {},
        SHORT_BTC,
        Close("BTC", Decimal("0.1"), Decimal("200.01")),
        Rule.EXIT_PRICE_CAP,
    ),
    (
        "a buy close over twice a mark from the future",
        {},
        {**SHORT_BTC, "assets": _asset(mark_at_ms=NOW_MS + 1000)},
        Close("BTC", Decimal("0.1"), Decimal(201)),
        Rule.EXIT_PRICE_CAP,
    ),
    (
        "an entry while a held coin has a stale mark",
        {},
        {"positions": {"ETH": Decimal("0.85")}, "assets": STALE_ETH},
        entry(size_coins=Decimal("0.2")),
        Rule.SNAPSHOT_MAX_AGE_S,
    ),
    (
        "an entry while a coin with a resting opening order has a stale mark",
        {},
        {"open_orders": _resting(1), "assets": STALE_ETH},
        entry(),
        Rule.SNAPSHOT_MAX_AGE_S,
    ),
    ("a stale snapshot", {}, {"snapshot_at_ms": NOW_MS - 5001}, entry(), Rule.SNAPSHOT_MAX_AGE_S),
    (
        "a snapshot from the future",
        {},
        {"snapshot_at_ms": NOW_MS + 1},
        entry(),
        Rule.SNAPSHOT_MAX_AGE_S,
    ),
    (
        "a stale mark",
        {},
        {"assets": _asset(mark_at_ms=NOW_MS - 5001)},
        entry(),
        Rule.SNAPSHOT_MAX_AGE_S,
    ),
    (
        "leverage on a stale snapshot",
        {},
        {"snapshot_at_ms": NOW_MS - 5001},
        SetLeverage("BTC", 1),
        Rule.SNAPSHOT_MAX_AGE_S,
    ),
    (
        "a buy target between the limit and the mark",
        {},
        {"assets": _asset(mark_px=Decimal("100.4"))},
        entry(take_profit_px=Decimal("100.3")),
        Rule.TAKE_PROFIT_MAX_PCT,
    ),
    (
        "a sell target between the mark and the limit",
        {},
        {"assets": _asset(mark_px=Decimal("99.6"))},
        entry(is_buy=False, stop_loss_px=Decimal(101), take_profit_px=Decimal("99.8")),
        Rule.TAKE_PROFIT_MAX_PCT,
    ),
    (
        "a sell target on the loss side",
        {},
        {},
        entry(is_buy=False, stop_loss_px=Decimal(101), take_profit_px=Decimal("100.5")),
        Rule.TAKE_PROFIT_MAX_PCT,
    ),
    ("margin above 10 USD", {}, OPEN_BTC, AddMargin("BTC", Decimal("10.01")), Rule.ADD_MARGIN),
    ("margin without a position", {}, {}, AddMargin("BTC", Decimal(1)), Rule.ADD_MARGIN),
    (
        "margin to a cross position",
        {},
        {**OPEN_BTC, "assets": _asset(margin_mode="cross")},
        AddMargin("BTC", Decimal(1)),
        Rule.ADD_MARGIN,
    ),
    ("margin on an unlisted coin", {}, {"assets": DOGE}, AddMargin("DOGE", Decimal(1)), Rule.COINS),
    (
        "margin while latched",
        {},
        {**OPEN_BTC, "latched": True},
        AddMargin("BTC", Decimal(1)),
        Rule.LATCHED,
    ),
    (
        "margin on a stale snapshot",
        {},
        {**OPEN_BTC, "snapshot_at_ms": NOW_MS - 5001},
        AddMargin("BTC", Decimal(1)),
        Rule.SNAPSHOT_MAX_AGE_S,
    ),
    (
        "margin additions recorded later than now",
        {},
        {**OPEN_BTC, "margin_added_at_ms": [NOW_MS + DAY_MS] * 3},
        AddMargin("BTC", Decimal(1)),
        Rule.MARGIN_ADDITIONS_PER_DAY_MAX,
    ),
    (
        "a fourth margin addition today",
        {},
        {**OPEN_BTC, "margin_added_at_ms": [NOW_MS - HOUR_MS] * 3},
        AddMargin("BTC", Decimal(1)),
        Rule.MARGIN_ADDITIONS_PER_DAY_MAX,
    ),
]


@pytest.mark.parametrize(
    ("policy_over", "account_over", "proposal", "rule"),
    [case[1:] for case in REJECTIONS],
    ids=[case[0] for case in REJECTIONS],
)
def test_each_rule_rejects_with_its_name(
    policy_over: dict[str, Any], account_over: dict[str, Any], proposal: Proposal, rule: Rule
) -> None:
    decision = check(policy(**policy_over), account(**account_over), proposal)
    assert isinstance(decision, Rejected)
    assert decision.rule is rule
    assert decision.detail


def test_every_rule_has_a_rejection_case() -> None:
    assert {case[4] for case in REJECTIONS} == set(Rule)


ALLOWED: list[tuple[str, dict[str, Any], dict[str, Any], Proposal]] = [
    ("a plain entry", {}, {}, entry()),
    ("no target", {}, {}, entry(take_profit_px=None)),
    ("a buy that adds to a long", {}, LONG_BTC, entry()),
    (
        "a sell that adds to a short",
        {},
        SHORT_BTC,
        entry(is_buy=False, stop_loss_px=Decimal(101), take_profit_px=None),
    ),
    ("stop exactly at the cap", {}, {}, entry(stop_loss_px=Decimal(98))),
    (
        "a short stop just inside the liquidation price at 5x on a 5x coin",
        FIVE_X,
        {"assets": _asset(leverage=5, max_leverage=5)},
        entry(is_buy=False, stop_loss_px=Decimal("109.09"), take_profit_px=None),
    ),
    (
        "a long 10% stop at 5x on a 5x coin, 11.1% from liquidation",
        FIVE_X,
        {"assets": _asset(leverage=5, max_leverage=5)},
        entry(stop_loss_px=Decimal(90), take_profit_px=None),
    ),
    (
        "a short stop one step inside the liquidation distance",
        FIVE_X,
        {"assets": _asset(leverage=5, max_leverage=5)},
        entry(
            is_buy=False,
            stop_loss_px=Decimal("109.090909090909090909090909090"),
            take_profit_px=None,
        ),
    ),
    (
        "a 10% stop at 5x on a 40x coin",
        FIVE_X,
        {"assets": _asset(leverage=5, max_leverage=40)},
        entry(is_buy=False, stop_loss_px=Decimal(110), take_profit_px=None),
    ),
    ("target exactly at the cap", {}, {}, entry(take_profit_px=Decimal(105))),
    ("order exactly at the cap", {}, {}, entry(size_coins=Decimal("0.2"))),
    (
        "a sell with the stop above",
        {},
        {},
        entry(is_buy=False, stop_loss_px=Decimal(101), take_profit_px=Decimal(97)),
    ),
    (
        "exposure exactly at the cap",
        {},
        {"positions": {"BTC": Decimal("0.5")}, "open_orders": _resting(1, 40)},
        entry(),
    ),
    (
        "reduce-only resting orders count for neither exposure nor the order cap",
        {},
        {"open_orders": _resting(2) + _resting(6, 500, reduce_only=True)},
        entry(),
    ),
    ("daily loss just under the limit", {}, {"realized_pnl_today_usd": Decimal("-29.99")}, entry()),
    ("four losses in a row", {}, {"loss_streak": 4}, entry()),
    (
        "same coin after the cooldown",
        {},
        {"entries": [{"coin": "BTC", "at_ms": NOW_MS - HOUR_MS}]},
        entry(),
    ),
    (
        "another coin inside the cooldown",
        {"entries_per_day_max": 3},
        {"entries": [{"coin": "ETH", "at_ms": NOW_MS - 1}]},
        entry(),
    ),
    (
        "yesterday's entries do not count today",
        {},
        {"entries": [{"coin": "ETH", "at_ms": NOW_MS - DAY_MS}] * 2},
        entry(),
    ),
    ("close at the daily loss limit", {}, HALTED, Close("BTC", Decimal("0.3"), Decimal(100))),
    (
        "close a coin the policy no longer lists",
        {"coins": ["ETH"]},
        {"positions": {"BTC": Decimal("-0.3")}},
        Close("BTC", Decimal("0.1"), Decimal(100)),
    ),
    ("leverage at the cap", {}, {}, SetLeverage("BTC", 1)),
    ("scheduleCancel at 24 h", {}, {}, ScheduleCancel(NOW_MS + DAY_MS)),
    ("scheduleCancel at the 5 s minimum", {}, {}, ScheduleCancel(NOW_MS + 5000)),
    ("scheduleCancel while latched", {}, {"latched": True}, ScheduleCancel(NOW_MS + 5000)),
    ("limit at the band edge", {}, {}, entry(limit_px=Decimal("100.5"))),
    (
        "the stop distance is measured from the limit, not the mark",
        {},
        {"assets": _asset(mark_px=Decimal("100.4"))},
        entry(stop_loss_px=Decimal("98.05")),
    ),
    ("a snapshot exactly at the age cap", {}, {"snapshot_at_ms": NOW_MS - 5000}, entry()),
    ("a mark exactly at the age cap", {}, {"assets": _asset(mark_at_ms=NOW_MS - 5000)}, entry()),
    ("close at the band edge", {}, OPEN_BTC, Close("BTC", Decimal("0.1"), Decimal("99.5"))),
    (
        "close outside the band while the mark is stale",
        {},
        {**OPEN_BTC, "assets": _asset(mark_at_ms=NOW_MS - 5001)},
        Close("BTC", Decimal("0.1"), Decimal(90)),
    ),
    (
        "a sell close 3 % under a fresh mark",
        {},
        LONG_BTC,
        Close("BTC", Decimal("0.1"), Decimal(97)),
    ),
    (
        "a sell close 3 % under a stale mark",
        {},
        {**LONG_BTC, "assets": _asset(mark_at_ms=NOW_MS - DAY_MS)},
        Close("BTC", Decimal("0.1"), Decimal(97)),
    ),
    ("a sell close at half the mark", {}, LONG_BTC, Close("BTC", Decimal("0.1"), Decimal(50))),
    ("a buy close at twice the mark", {}, SHORT_BTC, Close("BTC", Decimal("0.1"), Decimal(200))),
    (
        "a passive sell close above the band",
        {},
        LONG_BTC,
        Close("BTC", Decimal("0.1"), Decimal("100.51")),
    ),
    (
        "a passive buy close far under the mark",
        {},
        SHORT_BTC,
        Close("BTC", Decimal("0.1"), Decimal(1)),
    ),
    (
        "a stale mark on a coin with no position and no opening order",
        {},
        {
            "assets": STALE_ETH,
            "positions": {"ETH": Decimal(0)},
            "open_orders": _resting(1, reduce_only=True),
        },
        entry(),
    ),
    (
        "leverage needs a fresh snapshot, not a fresh mark",
        {},
        {"assets": _asset(mark_at_ms=NOW_MS - DAY_MS)},
        SetLeverage("BTC", 1),
    ),
    (
        "close while latched and stale",
        {},
        {**OPEN_BTC, "latched": True, "snapshot_at_ms": NOW_MS - DAY_MS},
        Close("BTC", Decimal("0.3"), Decimal(100)),
    ),
    (
        "margin to an open isolated position, third today",
        {},
        {**OPEN_BTC, "margin_added_at_ms": [NOW_MS - HOUR_MS] * 2},
        AddMargin("BTC", Decimal(10)),
    ),
    (
        "yesterday's margin additions do not count today",
        {},
        {**OPEN_BTC, "margin_added_at_ms": [NOW_MS - DAY_MS] * 3},
        AddMargin("BTC", Decimal(1)),
    ),
    (
        "margin to a short",
        {},
        {"positions": {"BTC": Decimal("-0.1")}},
        AddMargin("BTC", Decimal(1)),
    ),
]


@pytest.mark.parametrize(
    ("policy_over", "account_over", "proposal"),
    [case[1:] for case in ALLOWED],
    ids=[case[0] for case in ALLOWED],
)
def test_boundaries_and_risk_reduction_are_allowed(
    policy_over: dict[str, Any], account_over: dict[str, Any], proposal: Proposal
) -> None:
    assert check(policy(**policy_over), account(**account_over), proposal) == Allowed()


CONFIGURED: list[tuple[str, dict[str, Any], dict[str, Any], Proposal, Rule | None]] = [
    ("leverage 3x under a 3x cap", {"leverage_max": 3}, {}, SetLeverage("BTC", 3), None),
    (
        "leverage 4x over a 3x cap",
        {"leverage_max": 3},
        {},
        SetLeverage("BTC", 4),
        Rule.LEVERAGE_MAX,
    ),
    ("leverage 5x under a 5x cap", {"leverage_max": 5}, {}, SetLeverage("BTC", 5), None),
    (
        "an entry on isolated 3x under a 3x cap",
        {"leverage_max": 3},
        {"assets": _asset(leverage=3)},
        entry(),
        None,
    ),
    (
        "an entry on isolated 4x over a 3x cap",
        {"leverage_max": 3},
        {"assets": _asset(leverage=4)},
        entry(),
        Rule.LEVERAGE_MODE,
    ),
    (
        "an entry on cross 3x under a 3x cap",
        {"leverage_max": 3},
        {"assets": _asset(margin_mode="cross", leverage=3)},
        entry(),
        Rule.LEVERAGE_MODE,
    ),
    (
        "a sell close at 0.8 of the mark with a 0.8 floor",
        {"close_sell_floor": Decimal("0.8")},
        LONG_BTC,
        Close("BTC", Decimal("0.1"), Decimal(80)),
        None,
    ),
    (
        "a sell close at 0.79 of the mark with a 0.8 floor",
        {"close_sell_floor": Decimal("0.8")},
        LONG_BTC,
        Close("BTC", Decimal("0.1"), Decimal(79)),
        Rule.EXIT_PRICE_CAP,
    ),
    (
        "a sell close at 0.4 of the mark with a 0.1 floor",
        {"close_sell_floor": Decimal("0.1")},
        LONG_BTC,
        Close("BTC", Decimal("0.1"), Decimal(40)),
        None,
    ),
    (
        "a buy close at 1.5 of the mark with a 1.5 cap",
        {"close_buy_cap": Decimal("1.5")},
        SHORT_BTC,
        Close("BTC", Decimal("0.1"), Decimal(150)),
        None,
    ),
    (
        "a buy close at 1.51 of the mark with a 1.5 cap",
        {"close_buy_cap": Decimal("1.5")},
        SHORT_BTC,
        Close("BTC", Decimal("0.1"), Decimal(151)),
        Rule.EXIT_PRICE_CAP,
    ),
    (
        "a buy close at 5 times the mark with a 10 cap",
        {"close_buy_cap": Decimal(10)},
        SHORT_BTC,
        Close("BTC", Decimal("0.1"), Decimal(500)),
        None,
    ),
    (
        "scheduleCancel at a 1 h window",
        {"schedule_cancel_max_s": 3600},
        {},
        ScheduleCancel(NOW_MS + HOUR_MS),
        None,
    ),
    (
        "scheduleCancel 1 s past a 1 h window",
        {"schedule_cancel_max_s": 3600},
        {},
        ScheduleCancel(NOW_MS + HOUR_MS + 1000),
        Rule.SCHEDULE_CANCEL,
    ),
    (
        "scheduleCancel at a 7 day window",
        {"schedule_cancel_max_s": 604800},
        {},
        ScheduleCancel(NOW_MS + 7 * DAY_MS),
        None,
    ),
    (
        "scheduleCancel under 5 s with a 7 day window",
        {"schedule_cancel_max_s": 604800},
        {},
        ScheduleCancel(NOW_MS + 4999),
        Rule.SCHEDULE_CANCEL,
    ),
    (
        "margin at a 15 USD cap",
        {"add_margin_usd_max": 15},
        OPEN_BTC,
        AddMargin("BTC", Decimal(15)),
        None,
    ),
    (
        "margin over a 15 USD cap",
        {"add_margin_usd_max": 15},
        OPEN_BTC,
        AddMargin("BTC", Decimal("15.01")),
        Rule.ADD_MARGIN,
    ),
    (
        "margin over the 10 USD default",
        {},
        OPEN_BTC,
        AddMargin("BTC", Decimal("10.01")),
        Rule.ADD_MARGIN,
    ),
    (
        "margin under a 5 USD cap",
        {"add_margin_usd_max": 5},
        OPEN_BTC,
        AddMargin("BTC", Decimal(6)),
        Rule.ADD_MARGIN,
    ),
]


@pytest.mark.parametrize(
    ("policy_over", "account_over", "proposal", "rule"),
    [case[1:] for case in CONFIGURED],
    ids=[case[0] for case in CONFIGURED],
)
def test_the_gate_uses_the_configured_rules(
    policy_over: dict[str, Any], account_over: dict[str, Any], proposal: Proposal, rule: Rule | None
) -> None:
    decision = check(policy(**policy_over), account(**account_over), proposal)
    if rule is None:
        assert decision == Allowed()
    else:
        assert isinstance(decision, Rejected)
        assert decision.rule is rule


def test_rejections_name_the_configured_values() -> None:
    tight = policy(
        close_sell_floor=Decimal("0.8"),
        close_buy_cap=Decimal("1.5"),
        schedule_cancel_max_s=3600,
        add_margin_usd_max=15,
        leverage_max=3,
    )
    details = [
        check(tight, account(**LONG_BTC), Close("BTC", Decimal("0.1"), Decimal(79))),
        check(tight, account(**SHORT_BTC), Close("BTC", Decimal("0.1"), Decimal(151))),
        check(tight, account(), ScheduleCancel(NOW_MS + DAY_MS)),
        check(tight, account(**OPEN_BTC), AddMargin("BTC", Decimal(16))),
        check(tight, account(), SetLeverage("BTC", 4)),
    ]
    assert [d.detail for d in details if isinstance(d, Rejected)] == [
        "sell close limit 79 is below 0.8x the last mark 100",
        "buy close limit 151 is above 1.5x the last mark 100",
        f"scheduleCancel time {NOW_MS + DAY_MS} must be between now + 5 s ({NOW_MS + 5000}) "
        f"and now + 3600 s ({NOW_MS + HOUR_MS})",
        "margin addition 16 USD is above 15 USD",
        "leverage 4x is above 3x",
    ]


def test_a_sell_whose_stop_sits_between_the_mark_and_the_limit_is_rejected() -> None:
    above_limit = account(assets=_asset(mark_px=Decimal("100.4")))
    between = entry(is_buy=False, stop_loss_px=Decimal("100.2"), take_profit_px=None)
    decision = check(policy(), above_limit, between)
    assert isinstance(decision, Rejected)
    assert decision.rule is Rule.STOP_LOSS_REQUIRED
    above_both = entry(is_buy=False, stop_loss_px=Decimal("100.5"), take_profit_px=None)
    assert check(policy(), above_limit, above_both) == Allowed()


def test_a_short_with_a_stale_mark_blocks_an_entry() -> None:
    short_eth = account(assets=STALE_ETH, positions={"ETH": Decimal("-0.1")})
    decision = check(policy(), short_eth, entry())
    assert isinstance(decision, Rejected)
    assert decision.rule is Rule.SNAPSHOT_MAX_AGE_S
    assert "ETH mark" in decision.detail


def test_an_open_order_with_notional_0_counts_for_the_order_cap_only() -> None:
    zero = [{"coin": "ETH", "notional_usd": 0, "reduce_only": False}]
    near_cap = {"positions": {"BTC": Decimal("0.8")}}
    assert check(policy(), account(**near_cap, open_orders=zero * 2), entry()) == Allowed()
    full = check(policy(), account(open_orders=zero * 3), entry())
    assert isinstance(full, Rejected)
    assert full.rule is Rule.OPEN_ORDERS_MAX


def test_the_cooldown_follows_the_policy() -> None:
    recent = account(entries=[{"coin": "BTC", "at_ms": NOW_MS - 120_000}])
    assert check(policy(cooldown_s=60), recent, entry()) == Allowed()
    assert isinstance(check(policy(cooldown_s=121), recent, entry()), Rejected)


def test_trip_names_the_limit_that_latches() -> None:
    assert trip(policy(), account()) is None
    daily = trip(policy(), account(realized_pnl_today_usd=-30))
    assert isinstance(daily, Latch) and daily.rule is Rule.DAILY_LOSS_PCT_MAX
    streak = trip(policy(), account(loss_streak=5))
    assert isinstance(streak, Latch) and streak.rule is Rule.LOSS_STREAK_MAX
    assert trip(policy(), account(realized_pnl_today_usd=Decimal("-29.99"), loss_streak=4)) is None


def test_the_latch_outlives_the_utc_day() -> None:
    next_day = NOW_MS + DAY_MS
    fresh = {"now_ms": next_day, "snapshot_at_ms": next_day, "realized_pnl_today_usd": 0}
    assets = {c: {**a, "mark_at_ms": next_day} for c, a in ACCOUNT["assets"].items()}
    assert check(policy(), account(**fresh, assets=assets), entry()) == Allowed()
    latched = account(**fresh, assets=assets, latched=True)
    decision = check(policy(), latched, entry())
    assert isinstance(decision, Rejected) and decision.rule is Rule.LATCHED


@pytest.mark.parametrize(
    ("policy_over", "account_over", "coin", "detail"),
    [
        ({}, {"assets": DOGE}, "DOGE", "DOGE is not in the policy coins ['BTC', 'ETH', 'SOL']"),
        ({"coins": ["BTC", "XRP"]}, {}, "XRP", "XRP is not in the account snapshot assets"),
    ],
    ids=["policy", "snapshot"],
)
def test_the_coins_rule_says_which_list_lacks_the_coin(
    policy_over: dict[str, Any], account_over: dict[str, Any], coin: str, detail: str
) -> None:
    decision = check(policy(**policy_over), account(**account_over), entry(coin=coin))
    assert decision == Rejected(Rule.COINS, detail)


def test_entries_per_day_count_the_utc_day_not_the_last_24_h() -> None:
    midnight = NOW_MS - NOW_MS % DAY_MS
    now = midnight + 60_000
    fresh = {coin: {**a, "mark_at_ms": now - 1000} for coin, a in ACCOUNT["assets"].items()}
    clock = {"now_ms": now, "snapshot_at_ms": now - 1000, "assets": fresh}
    late = [{"coin": "ETH", "at_ms": midnight - 120_000}, {"coin": "SOL", "at_ms": midnight - 1}]
    assert check(policy(), account(**clock, entries=late), entry()) == Allowed()
    early = [{"coin": "ETH", "at_ms": midnight}, {"coin": "SOL", "at_ms": midnight + 1}]
    decision = check(policy(), account(**clock, entries=early), entry())
    assert isinstance(decision, Rejected)
    assert decision.rule is Rule.ENTRIES_PER_DAY_MAX


def _magnitude(rng: random.Random) -> Decimal:
    if rng.random() < 0.95:
        exponent = rng.randint(-25, 36)
    else:
        exponent = rng.choice([1, -1]) * rng.randint(999_000, 999_999)
    return Decimal(rng.randint(1, 10**6)).scaleb(exponent - 6)


def test_check_returns_a_decision_for_every_parsed_input_whatever_the_magnitudes() -> None:
    rng = random.Random(20261020)
    parsed = 0
    for _ in range(4000):
        m = [_magnitude(rng) for _ in range(9)]
        mark = m[2]
        limit, stop, target = (
            (mark, mark - mark.scaleb(-2), mark + mark.scaleb(-2))
            if rng.random() < 0.5
            else (m[6], m[7], m[8])
        )
        raw_account = {
            **ACCOUNT,
            "day_start_equity_usd": m[0],
            "realized_pnl_today_usd": -m[1],
            "assets": {**ACCOUNT["assets"], "BTC": {**ACCOUNT["assets"]["BTC"], "mark_px": mark}},
            "positions": {"BTC": m[3] * rng.choice([1, -1])},
            "open_orders": [{"coin": "ETH", "notional_usd": m[4], "reduce_only": False}],
        }
        raw_proposals = [
            {
                "kind": "entry",
                "coin": "BTC",
                "is_buy": True,
                "size_coins": m[5],
                "limit_px": limit,
                "stop_loss_px": stop,
                "take_profit_px": target,
            },
            {"kind": "close", "coin": "BTC", "size_coins": m[5], "limit_px": limit},
            {"kind": "add_margin", "coin": "BTC", "usd": m[5]},
        ]
        try:
            acct = parse_account(raw_account)
        except PolicyError:
            continue
        for raw in raw_proposals:
            try:
                proposal = parse_proposal(raw)
            except PolicyError:
                continue
            parsed += 1
            assert isinstance(check(policy(), acct, proposal), Allowed | Rejected)
    assert parsed > 3000
