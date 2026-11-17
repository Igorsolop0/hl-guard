import dataclasses
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from policy_fixtures import account, entry, policy

from hl_guard.policy.model import (
    AddMargin,
    ApproveAboveUsd,
    AssetState,
    Close,
    EntryRecord,
    OpenOrder,
    Policy,
    ScheduleCancel,
    SetLeverage,
)

NAN = Decimal("NaN")
INF = Decimal("Infinity")


def _asset(**overrides: Any) -> AssetState:
    fields: dict[str, Any] = {
        "asset_id": 0,
        "margin_mode": "isolated",
        "leverage": 1,
        "mark_px": Decimal(100),
        "mark_at_ms": 0,
        "max_leverage": 50,
    }
    return AssetState(**{**fields, **overrides})


def _policy(**overrides: Any) -> Policy:
    return dataclasses.replace(policy(), **overrides)


BAD: list[tuple[str, Callable[[], object]]] = [
    ("entry size negative", lambda: entry(size_coins=Decimal(-1000))),
    ("entry size zero", lambda: entry(size_coins=Decimal(0))),
    ("entry size NaN", lambda: entry(size_coins=NAN)),
    ("entry limit infinite", lambda: entry(limit_px=INF)),
    ("entry stop negative", lambda: entry(stop_loss_px=Decimal(-1))),
    ("entry target zero", lambda: entry(take_profit_px=Decimal(0))),
    ("entry size a float", lambda: entry(size_coins=0.1)),
    ("close size negative", lambda: Close("BTC", Decimal(-5), Decimal(100))),
    ("close price zero", lambda: Close("BTC", Decimal(1), Decimal(0))),
    ("leverage zero", lambda: SetLeverage("BTC", 0)),
    ("leverage a bool", lambda: SetLeverage("BTC", True)),
    ("scheduleCancel negative", lambda: ScheduleCancel(-1)),
    ("margin zero", lambda: AddMargin("BTC", Decimal(0))),
    ("margin negative", lambda: AddMargin("BTC", Decimal(-10))),
    ("margin NaN", lambda: AddMargin("BTC", NAN)),
    ("asset mark zero", lambda: _asset(mark_px=Decimal(0))),
    ("asset leverage zero", lambda: _asset(leverage=0)),
    ("asset max leverage zero", lambda: _asset(max_leverage=0)),
    ("asset max leverage a bool", lambda: _asset(max_leverage=True)),
    ("asset mode unknown", lambda: _asset(margin_mode="portfolio")),
    ("asset mark time negative", lambda: _asset(mark_at_ms=-1)),
    ("order notional negative", lambda: OpenOrder("BTC", Decimal(-1), False)),
    ("entry record negative", lambda: EntryRecord("BTC", -1)),
    (
        "account equity zero",
        lambda: dataclasses.replace(account(), day_start_equity_usd=Decimal(0)),
    ),
    (
        "account equity negative",
        lambda: dataclasses.replace(account(), day_start_equity_usd=Decimal(-100)),
    ),
    ("account streak negative", lambda: dataclasses.replace(account(), loss_streak=-1)),
    ("account pnl NaN", lambda: dataclasses.replace(account(), realized_pnl_today_usd=NAN)),
    ("account snapshot negative", lambda: dataclasses.replace(account(), snapshot_at_ms=-1)),
    (
        "account position infinite",
        lambda: dataclasses.replace(account(), positions={"BTC": INF}),
    ),
    (
        "account position without a mark",
        lambda: dataclasses.replace(account(), positions={"DOGE": Decimal(1)}),
    ),
    (
        "account margin time negative",
        lambda: dataclasses.replace(account(), margin_added_at_ms=(-1,)),
    ),
    (
        "policy exposure above its ceiling",
        lambda: _policy(total_exposure_usd_max=Decimal(20001)),
    ),
    ("policy cooldown under its floor", lambda: _policy(cooldown_s=59)),
    ("policy leverage above 5x", lambda: _policy(leverage_max=6)),
    ("policy sell floor under 0.1", lambda: _policy(close_sell_floor=Decimal("0.09"))),
    ("policy sell floor above 0.95", lambda: _policy(close_sell_floor=Decimal("0.96"))),
    ("policy buy cap under 1.05", lambda: _policy(close_buy_cap=Decimal("1.04"))),
    ("policy buy cap above 10", lambda: _policy(close_buy_cap=Decimal("10.01"))),
    ("policy cancel window under 5 s", lambda: _policy(schedule_cancel_max_s=4)),
    ("policy cancel window above 7 days", lambda: _policy(schedule_cancel_max_s=604801)),
    ("policy margin above the order cap", lambda: _policy(add_margin_usd_max=Decimal(21))),
    ("policy sell floor NaN", lambda: _policy(close_sell_floor=NAN)),
    ("policy cancel window not an integer", lambda: _policy(schedule_cancel_max_s=Decimal(60))),
    (
        "policy leverage not an integer",
        lambda: _policy(leverage_max=Decimal(1)),
    ),
    ("policy band NaN", lambda: _policy(price_band_pct_max=NAN)),
    (
        "policy order cap above the exposure cap",
        lambda: _policy(per_order_usd_max=Decimal(101)),
    ),
    (
        "policy approval at the order cap",
        lambda: _policy(approval=ApproveAboveUsd(Decimal(20))),
    ),
    ("policy approval at 0 USD", lambda: _policy(approval=ApproveAboveUsd(Decimal(0)))),
    ("policy without coins", lambda: _policy(coins=frozenset())),
    ("policy on another network", lambda: _policy(network="devnet")),
    ("policy approval None", lambda: _policy(approval=None)),
    ("policy approval a string", lambda: _policy(approval="always")),
    ("policy coins a string", lambda: _policy(coins="BTCETH")),
    ("policy coins None", lambda: _policy(coins=None)),
    ("policy coins a list", lambda: _policy(coins=["BTC"])),
    ("policy coin not a string", lambda: _policy(coins=frozenset({1}))),
    ("policy coin non-ASCII digit", lambda: _policy(coins=frozenset({"BTC\u0661"}))),
    ("policy coin too long", lambda: _policy(coins=frozenset({"A" * 17}))),
]


@pytest.mark.parametrize("build", [b for _, b in BAD], ids=[n for n, _ in BAD])
def test_direct_construction_refuses_illegal_values(build: Callable[[], object]) -> None:
    with pytest.raises(ValueError):
        build()


def test_legal_values_construct() -> None:
    assert _policy(leverage_max=5, schedule_cancel_max_s=604800).leverage_max == 5
    assert _policy(add_margin_usd_max=Decimal(20)).add_margin_usd_max == 20
    edges = _policy(close_sell_floor=Decimal("0.1"), close_buy_cap=Decimal(10))
    assert (edges.close_sell_floor, edges.close_buy_cap) == (Decimal("0.1"), Decimal(10))
    assert entry().size_coins == Decimal("0.1")
    assert AddMargin("BTC", Decimal(10)).usd == 10
    assert account().latched is False


DECIMAL_FIELDS: list[tuple[str, Callable[[Decimal], object]]] = [
    *(
        (f"policy {name}", lambda d, name=name: _policy(**{name: d}))
        for name in (
            "daily_loss_pct_max",
            "stop_loss_max_pct",
            "take_profit_max_pct",
            "price_band_pct_max",
        )
    ),
    (
        "policy per_order_usd_max",
        lambda d: _policy(per_order_usd_max=d, add_margin_usd_max=min(d, Decimal(10))),
    ),
    (
        "policy total_exposure_usd_max",
        lambda d: _policy(
            per_order_usd_max=Decimal("1e-30"),
            add_margin_usd_max=Decimal("1e-30"),
            total_exposure_usd_max=d,
        ),
    ),
    ("policy add_margin_usd_max", lambda d: _policy(add_margin_usd_max=d)),
    ("policy approval", lambda d: _policy(approval=ApproveAboveUsd(d))),
    ("account equity", lambda d: dataclasses.replace(account(), day_start_equity_usd=d)),
    ("account pnl", lambda d: dataclasses.replace(account(), realized_pnl_today_usd=d)),
    ("account position", lambda d: dataclasses.replace(account(), positions={"BTC": d})),
    ("asset mark", lambda d: _asset(mark_px=d)),
    ("order notional", lambda d: OpenOrder("BTC", d, False)),
    ("entry size", lambda d: entry(size_coins=d)),
    ("entry limit", lambda d: entry(limit_px=d)),
    ("entry stop", lambda d: entry(stop_loss_px=d)),
    ("entry target", lambda d: entry(take_profit_px=d)),
    ("close size", lambda d: Close("BTC", d, Decimal(100))),
    ("close limit", lambda d: Close("BTC", Decimal(1), d)),
    ("margin", lambda d: AddMargin("BTC", d)),
]
OUT_OF_SIZE = ["1e31", "1.0000000000000000000000000000001e30", "9.9e-31", "1e999999", "1e-999999"]


@pytest.mark.parametrize("value", OUT_OF_SIZE)
@pytest.mark.parametrize(
    "build", [b for _, b in DECIMAL_FIELDS], ids=[n for n, _ in DECIMAL_FIELDS]
)
def test_every_decimal_field_refuses_a_size_outside_1e_30_to_1e30(
    build: Callable[[Decimal], object], value: str
) -> None:
    with pytest.raises(ValueError, match="between 1e-30 and 1e30"):
        build(Decimal(value))


@pytest.mark.parametrize(
    "build", [b for _, b in DECIMAL_FIELDS], ids=[n for n, _ in DECIMAL_FIELDS]
)
def test_every_decimal_field_takes_1e_30(build: Callable[[Decimal], object]) -> None:
    build(Decimal("1e-30"))


@pytest.mark.parametrize(
    "build",
    [b for n, b in DECIMAL_FIELDS if not n.startswith("policy")],
    ids=[n for n, _ in DECIMAL_FIELDS if not n.startswith("policy")],
)
def test_every_input_decimal_field_takes_1e30(build: Callable[[Decimal], object]) -> None:
    build(Decimal("1e30"))


@pytest.mark.parametrize("name", ["account pnl", "account position"])
def test_signed_fields_bound_the_size_of_a_negative_value(name: str) -> None:
    build = dict(DECIMAL_FIELDS)[name]
    build(Decimal("-1e30"))
    build(Decimal(0))
    with pytest.raises(ValueError, match="between 1e-30 and 1e30"):
        build(Decimal("-1e31"))


@pytest.mark.parametrize(
    "field",
    [f.name for f in dataclasses.fields(Policy) if f.type == "int"],
)
def test_policy_int_limits_refuse_0(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be an integer from 1 to 2\\*\\*63-1"):
        _policy(**{field: 0})


INT_FIELDS: list[tuple[str, Callable[[int], object]]] = [
    ("entry record time", lambda v: EntryRecord("BTC", v)),
    ("scheduleCancel time", lambda v: ScheduleCancel(v)),
    ("asset mark time", lambda v: _asset(mark_at_ms=v)),
    ("account now", lambda v: dataclasses.replace(account(), now_ms=v)),
    ("account margin time", lambda v: dataclasses.replace(account(), margin_added_at_ms=(v,))),
]


@pytest.mark.parametrize("build", [b for _, b in INT_FIELDS], ids=[n for n, _ in INT_FIELDS])
def test_integer_fields_take_up_to_2_63_minus_1(build: Callable[[int], object]) -> None:
    build(2**63 - 1)
    with pytest.raises(ValueError, match=re.escape("to 2**63-1")):
        build(2**63)
