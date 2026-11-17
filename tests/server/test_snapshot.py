from __future__ import annotations

import dataclasses
import itertools
import json
from collections.abc import Mapping
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
import yaml

from hl_guard.ledger.ledger import Kind, Ledger, Record, Tail
from hl_guard.policy import gate
from hl_guard.policy.load import parse_policy
from hl_guard.policy.model import (
    Account,
    AssetState,
    Entry,
    EntryRecord,
    MarginMode,
    OpenOrder,
    Rejected,
)
from hl_guard.server.parse import (
    AccountState,
    Leverage,
    Market,
    OrderRow,
    parse_clearinghouse_state,
    parse_markets,
    parse_open_orders,
)
from hl_guard.server.snapshot import (
    DAY_MS,
    Exchange,
    Folded,
    LedgerView,
    Mark,
    RiskFold,
    SnapshotError,
    build_account,
    coins_needing_leverage,
    fold_ledger,
    new_entry_blocks,
    position_marks,
)
from server.fake_info import ROOT, recorded

DAY = 20_380
MIDNIGHT = DAY * DAY_MS
NOW = MIDNIGHT + 12 * 3_600_000
HOUR = 3_600_000


def fill(coin: str, side: str, size: str, price: str, fee: str = "0") -> dict[str, str]:
    return {"coin": coin, "side": side, "size": size, "price": price, "fee": fee}


def ledger(tmp_path: Path, *rows: tuple[int, Kind, Mapping[str, object]]) -> list[Record]:
    led = Ledger(tmp_path / "ledger.jsonl")
    for ts_ms, kind, payload in rows:
        if kind in (Kind.LATCH,):
            led.append(kind, payload)
        elif kind is Kind.UNLATCH:
            Tail(led).unlatch()
        else:
            led.append(kind, payload, ts_ms=ts_ms)
    return list(led.records())


def test_realized_pnl_counts_only_today(tmp_path: Path) -> None:
    records = ledger(
        tmp_path,
        (NOW - DAY_MS, Kind.FILL, fill("BTC", "buy", "1", "100")),
        (NOW - DAY_MS + 1, Kind.FILL, fill("BTC", "sell", "1", "90")),
        (NOW - HOUR, Kind.FILL, fill("ETH", "buy", "2", "10")),
        (NOW - 1, Kind.FILL, fill("ETH", "sell", "2", "9", fee="0.5")),
    )

    view = fold_ledger(records, NOW, cooldown_s=60)

    assert view.realized_pnl_today_usd == Decimal("-2.5")


def test_dust_rounds_down_to_the_micro_dollar(tmp_path: Path) -> None:
    records = ledger(
        tmp_path,
        (NOW - 2, Kind.FILL, fill("BTC", "buy", "3", "1")),
        (NOW - 1, Kind.FILL, fill("BTC", "sell", "1", "1.0000000001")),
    )

    view = fold_ledger(records, NOW, cooldown_s=60)

    assert view.realized_pnl_today_usd == 0
    records = ledger(tmp_path, (NOW, Kind.FILL, fill("BTC", "sell", "1", "0.9999999998")))
    assert fold_ledger(records, NOW, 60).realized_pnl_today_usd == Decimal("-0.000001")


def test_loss_streak_counts_losing_cycles_once(tmp_path: Path) -> None:
    records = ledger(
        tmp_path,
        (NOW - 9, Kind.FILL, fill("BTC", "buy", "2", "100")),
        (NOW - 8, Kind.FILL, fill("BTC", "sell", "1", "99")),
        (NOW - 7, Kind.FILL, fill("BTC", "sell", "1", "99")),
        (NOW - 6, Kind.FILL, fill("ETH", "sell", "1", "10")),
        (NOW - 5, Kind.FILL, fill("ETH", "buy", "1", "11")),
    )

    assert fold_ledger(records, NOW, 60).loss_streak == 2


def test_a_winning_cycle_ends_the_streak(tmp_path: Path) -> None:
    records = ledger(
        tmp_path,
        (NOW - 4, Kind.FILL, fill("BTC", "buy", "1", "100")),
        (NOW - 3, Kind.FILL, fill("BTC", "sell", "1", "99")),
        (NOW - 2, Kind.FILL, fill("BTC", "buy", "1", "100")),
        (NOW - 1, Kind.FILL, fill("BTC", "sell", "1", "101")),
    )

    assert fold_ledger(records, NOW, 60).loss_streak == 0


def test_fees_can_make_a_flat_cycle_a_loss(tmp_path: Path) -> None:
    records = ledger(
        tmp_path,
        (NOW - 2, Kind.FILL, fill("BTC", "buy", "1", "100", fee="0.01")),
        (NOW - 1, Kind.FILL, fill("BTC", "sell", "1", "100", fee="0.01")),
    )

    assert fold_ledger(records, NOW, 60).loss_streak == 1


def test_a_losing_flip_ends_a_cycle_and_opens_an_entry(tmp_path: Path) -> None:
    records = ledger(
        tmp_path,
        (NOW - 3 * HOUR, Kind.FILL, fill("BTC", "buy", "1", "100")),
        (NOW - 2 * HOUR, Kind.FILL, fill("BTC", "sell", "2", "90")),
    )

    view = fold_ledger(records, NOW, 60)

    assert view.loss_streak == 1
    assert view.entries == (EntryRecord("BTC", NOW - 3 * HOUR), EntryRecord("BTC", NOW - 2 * HOUR))


def test_unlatch_clears_the_latch_and_the_streak(tmp_path: Path) -> None:
    led_rows: list[tuple[int, Kind, Mapping[str, object]]] = [
        (NOW - 4, Kind.FILL, fill("BTC", "buy", "1", "100")),
        (NOW - 3, Kind.FILL, fill("BTC", "sell", "1", "99")),
        (0, Kind.LATCH, {"rule": "loss_streak_max", "detail": "1 loss"}),
    ]
    latched = fold_ledger(ledger(tmp_path, *led_rows), NOW, 60)
    assert latched.latch is not None and latched.loss_streak == 1

    cleared = fold_ledger(ledger(tmp_path, (0, Kind.UNLATCH, {})), NOW, 60)

    assert cleared.latch is None
    assert cleared.loss_streak == 0


def test_entries_keep_today_and_the_cooldown_window(tmp_path: Path) -> None:
    just_after_midnight = MIDNIGHT + 10 * 60_000
    records = ledger(
        tmp_path,
        (MIDNIGHT - 5 * HOUR, Kind.FILL, fill("SOL", "buy", "1", "10")),
        (MIDNIGHT - 4 * HOUR, Kind.FILL, fill("SOL", "sell", "1", "10")),
        (MIDNIGHT - 30 * 60_000, Kind.FILL, fill("BTC", "buy", "1", "100")),
        (MIDNIGHT - 29 * 60_000, Kind.FILL, fill("BTC", "buy", "1", "100")),
        (MIDNIGHT + 60_000, Kind.FILL, fill("ETH", "sell", "1", "10")),
        (MIDNIGHT + 2 * 60_000, Kind.FILL, fill("ETH", "buy", "3", "10")),
    )

    view = fold_ledger(records, just_after_midnight, cooldown_s=3600)

    assert view.entries == (
        EntryRecord("BTC", MIDNIGHT - 30 * 60_000),
        EntryRecord("ETH", MIDNIGHT + 60_000),
        EntryRecord("ETH", MIDNIGHT + 2 * 60_000),
    )


def test_an_old_entry_still_in_its_cooldown_survives_a_thousand_newer_ones(
    tmp_path: Path,
) -> None:
    policy = dataclasses.replace(POLICY, cooldown_s=21 * DAY_MS // 1000, entries_per_day_max=50)
    rows = [
        (NOW - 20 * DAY_MS - 2, Kind.FILL, fill("SOL", "buy", "1", "10")),
        (NOW - 20 * DAY_MS - 1, Kind.FILL, fill("SOL", "sell", "1", "10")),
    ]
    btc = [NOW - 20 * DAY_MS + i * (20 * DAY_MS // 1000) for i in range(1000)]
    for at in btc:
        rows += [
            (at, Kind.FILL, fill("BTC", "buy", "1", "100")),
            (at + 1, Kind.FILL, fill("BTC", "sell", "1", "100")),
        ]

    view = fold_ledger(ledger(tmp_path, *rows), NOW, policy.cooldown_s)
    fit = {coin: asset() for coin in policy.coins}
    acct = account(assets=fit, positions={}, entries=view.entries)
    sol = gate.check(
        policy, acct, Entry("SOL", True, Decimal("0.1"), Decimal(100), Decimal(99), None)
    )

    assert isinstance(sol, Rejected) and sol.rule.value == "cooldown_s"
    assert any(
        b.startswith("cooldown_s") and "SOL" in b
        for b in new_entry_blocks(policy, acct, halted=False)
    )
    today = [e.at_ms for e in view.entries if e.at_ms >= MIDNIGHT]
    assert today == [at for at in btc if at >= MIDNIGHT]


def test_fold_ignores_the_callers_decimal_context(tmp_path: Path) -> None:
    records = ledger(
        tmp_path,
        (NOW - 2, Kind.FILL, fill("BTC", "buy", "0.123456789", "81234.56789")),
        (NOW - 1, Kind.FILL, fill("BTC", "sell", "0.123456789", "81111.11111", fee="0.0123")),
    )
    expected = fold_ledger(records, NOW, 60)

    with localcontext() as ctx:
        ctx.prec = 3
        assert fold_ledger(records, NOW, 60) == expected
    assert expected.realized_pnl_today_usd == Decimal("-15.253878")


def exchange(
    *,
    state: AccountState | None = None,
    markets: Mapping[str, Market] | None = None,
    orders: tuple[OrderRow, ...] | None = None,
) -> Exchange:
    return Exchange(
        state=state
        or dataclasses.replace(
            parse_clearinghouse_state(json.loads(recorded("clearinghouseState"))),
            time_ms=NOW - 250,
        ),
        state_received_ms=NOW - 200,
        markets=(read := markets or parse_markets(json.loads(recorded("metaAndAssetCtxs")))),
        marks={coin: Mark(m.mark_px, NOW - 300) for coin, m in read.items()},
        orders=parse_open_orders(json.loads(recorded("frontendOpenOrders")))
        if orders is None
        else orders,
        leverages={},
    )


VIEW = LedgerView(Decimal("-1.5"), 2, None, (EntryRecord("BTC", NOW - HOUR),))


def test_account_takes_times_from_the_reads() -> None:
    account = build_account(NOW, Decimal(1000), exchange(), VIEW)

    assert account.snapshot_at_ms == NOW - 250
    assert {a.mark_at_ms for a in account.assets.values()} == {NOW - 300}
    assert account.assets["BTC"].asset_id == 3
    assert account.assets["BTC"].margin_mode == "cross"
    assert account.positions == {"BTC": Decimal("0.25"), "ETH": Decimal("4.2")}
    assert account.realized_pnl_today_usd == Decimal("-1.5")
    assert account.loss_streak == 2
    assert account.entries == VIEW.entries


def test_trigger_orders_count_at_the_higher_price() -> None:
    raw = json.loads(recorded("frontendOpenOrders"))
    raw[0] |= {"isTrigger": True, "triggerPx": "9000.0", "limitPx": "2390.0", "reduceOnly": True}

    account = build_account(NOW, Decimal(1000), exchange(orders=parse_open_orders(raw)), VIEW)

    assert account.open_orders[0].notional_usd == Decimal("36000.0")
    assert account.open_orders[0].reduce_only is True


def test_spot_orders_stay_out_of_the_account() -> None:
    raw = json.loads(recorded("frontendOpenOrders"))
    raw[0]["coin"] = "@107"

    account = build_account(NOW, Decimal(1000), exchange(orders=parse_open_orders(raw)), VIEW)

    assert len(account.open_orders) == 2


def test_order_only_coins_need_a_leverage_read() -> None:
    empty = parse_clearinghouse_state(
        json.loads(recorded("clearinghouseState")) | {"assetPositions": []}
    )
    read = exchange(state=empty)

    assert coins_needing_leverage(read) == ["BTC", "ETH"]
    with pytest.raises(SnapshotError, match="no leverage"):
        build_account(NOW, Decimal(1000), read, VIEW)
    leverages = {"BTC": Leverage("isolated", 1), "ETH": Leverage("cross", 3)}
    account = build_account(
        NOW, Decimal(1000), dataclasses.replace(read, leverages=leverages), VIEW
    )
    assert account.assets["BTC"].margin_mode == "isolated"


def test_more_than_a_thousand_coins_is_refused() -> None:
    raw = json.loads(recorded("frontendOpenOrders"))
    many = [raw[0] | {"coin": f"C{i}"} for i in range(999)]
    btc = parse_markets(json.loads(recorded("metaAndAssetCtxs")))["BTC"]
    markets = {f"C{i}": btc for i in range(999)} | {"BTC": btc, "ETH": btc}
    read = exchange(orders=parse_open_orders(many), markets=markets)

    with pytest.raises(SnapshotError, match="1000"):
        build_account(NOW, Decimal(1000), read, VIEW)


def test_a_position_without_a_market_is_refused() -> None:
    markets = parse_markets(json.loads(recorded("metaAndAssetCtxs")))
    del markets["ETH"]

    with pytest.raises(SnapshotError, match="no market"):
        build_account(NOW, Decimal(1000), exchange(markets=markets), VIEW)


@pytest.mark.parametrize(
    ("exchange_ms", "snapshot_ms"),
    [
        (NOW - 150, NOW - 200),
        (NOW - 9_000, NOW - 9_000),
        (NOW - 200 + 2_000, NOW - 200),
        (NOW - 200 + 2_001, NOW - 200 + 2_001),
        (1, 1),
    ],
    ids=["later-than-receipt", "old", "skew", "ahead", "epoch"],
)
def test_the_snapshot_takes_the_older_time_unless_the_exchange_runs_ahead(
    exchange_ms: int, snapshot_ms: int
) -> None:
    read = exchange()
    read = dataclasses.replace(read, state=dataclasses.replace(read.state, time_ms=exchange_ms))

    assert build_account(NOW, Decimal(1000), read, VIEW).snapshot_at_ms == snapshot_ms


@pytest.mark.parametrize("px", ["0.00000000000000000001", "9" * 20])
def test_an_order_notional_out_of_range_is_a_snapshot_error(px: str) -> None:
    raw = json.loads(recorded("frontendOpenOrders"))
    raw[0] |= {"sz": px, "limitPx": px}

    with pytest.raises(SnapshotError, match="notional_usd"):
        build_account(NOW, Decimal(1000), exchange(orders=parse_open_orders(raw)), VIEW)


POLICY = parse_policy(yaml.safe_load((ROOT / "examples" / "policy.yaml").read_text()))


def account(**changes: Any) -> Account:
    return dataclasses.replace(
        build_account(NOW, Decimal(1000), exchange(orders=()), VIEW), **changes
    )


def asset(
    mode: MarginMode = "isolated", leverage: int = 1, mark_at_ms: int = NOW - 300
) -> AssetState:
    return AssetState(1, mode, leverage, Decimal(100), mark_at_ms, 50)


def rules(blocks: list[str]) -> list[str]:
    return [line.split(":", 1)[0] for line in blocks]


def test_a_flat_account_blocks_only_on_exposure() -> None:
    fit = {coin: asset() for coin in ("BTC", "ETH")}

    blocks = new_entry_blocks(POLICY, account(assets=fit), halted=False)

    assert rules(blocks) == ["total_exposure_usd_max"]


def test_halt_and_latch_come_first() -> None:
    blocks = new_entry_blocks(POLICY, account(latched=True), halted=True)

    assert rules(blocks)[:2] == ["halted", "latched"]


def test_a_stale_mark_blocks_entries() -> None:
    stale = {coin: asset(mark_at_ms=NOW - 6000) for coin in ("BTC", "ETH")}

    blocks = new_entry_blocks(POLICY, account(assets=stale), halted=False)

    assert "snapshot_max_age_s: the mark price read is 6000 ms before now; the cap is 5 s" in blocks


def test_an_entry_later_than_now_blocks_entries() -> None:
    later = (EntryRecord("ETH", NOW + 1000),)

    blocks = new_entry_blocks(POLICY, account(entries=later), halted=False)

    assert "cooldown_s: an entry is later than now: clock moved back" in blocks


@pytest.mark.parametrize(("reduce_only", "blocked"), [(True, False), (False, True)])
def test_only_opening_orders_count_against_the_cap(reduce_only: bool, blocked: bool) -> None:
    orders = tuple(OpenOrder("BTC", Decimal(1), reduce_only) for _ in range(3))

    blocks = new_entry_blocks(POLICY, account(open_orders=orders), halted=False)

    assert ("open_orders_max" in rules(blocks)) is blocked


@pytest.mark.parametrize(
    ("sol", "summary"),
    [(asset("cross"), True), (asset(leverage=2), True), (asset(), False), (None, False)],
    ids=["cross", "above-max", "isolated", "unknown"],
)
def test_leverage_on_every_policy_coin_sums_up(sol: AssetState | None, summary: bool) -> None:
    assets = {"BTC": asset("cross"), "ETH": asset(leverage=5)} | ({"SOL": sol} if sol else {})

    blocks = new_entry_blocks(POLICY, account(assets=assets), halted=False)

    line = "leverage_mode: every policy coin is cross or above 1x on the exchange"
    assert (line in blocks) is summary


def test_leverage_names_each_unfit_coin_next_to_the_cooldown() -> None:
    assets = {
        "BTC": asset(),
        "ETH": asset("cross"),
        "SOL": asset(leverage=3),
        "DOGE": asset("cross"),
    }
    entries = (EntryRecord("BTC", NOW - 10_000),)

    blocks = new_entry_blocks(POLICY, account(assets=assets, entries=entries), halted=False)

    assert blocks[-3:] == [
        "cooldown_s: BTC entered less than 3600 s ago",
        "leverage_mode: ETH, SOL are cross or above 1x on the exchange",
        "cooldown_s, leverage_mode: every policy coin is in its cooldown or cross or above 1x "
        "on the exchange",
    ]


STATES = {
    "fit": asset(),
    "cooling": asset(),
    "cross": asset("cross"),
    "above": asset(leverage=3),
    "unknown": None,
}


@pytest.mark.parametrize("states", list(itertools.product(STATES, repeat=3)), ids="-".join)
def test_risk_names_every_coin_the_gate_refuses_for_cooldown_or_leverage(
    states: tuple[str, ...],
) -> None:
    coins = sorted(POLICY.coins)
    assets = {coin: a for coin, s in zip(coins, states, strict=True) if (a := STATES[s])}
    entries = tuple(
        EntryRecord(coin, NOW - 10_000)
        for coin, s in zip(coins, states, strict=True)
        if s == "cooling"
    )
    acct = account(assets=assets, positions={}, entries=entries)
    refused = {}
    for coin in coins:
        decision = gate.check(
            POLICY, acct, Entry(coin, True, Decimal("0.1"), Decimal(100), Decimal(99), None)
        )
        if isinstance(decision, Rejected):
            refused[coin] = decision.rule.value

    blocks = new_entry_blocks(POLICY, acct, halted=False)

    per_coin = {coin for coin, rule in refused.items() if rule in ("cooldown_s", "leverage_mode")}
    every = [line for line in blocks if "every policy coin" in line]
    for coin in per_coin:
        named = [line for line in blocks if line.startswith(f"{refused[coin]}: ")]
        assert every or any(coin in line for line in named), (coin, blocks)
    assert bool(every) is (per_coin == POLICY.coins)


def test_a_position_mark_is_its_value_per_coin_at_the_read_time() -> None:
    state = parse_clearinghouse_state(json.loads(recorded("clearinghouseState")))
    marks = position_marks(state, NOW - 5)
    held = {p.coin: p for p in state.positions}
    assert set(marks) == set(held)
    for coin, mark in marks.items():
        assert mark.at_ms == NOW - 5
        assert mark.px * abs(held[coin].size) == held[coin].position_value


def test_an_extra_coin_is_priced_from_its_own_mark_and_leverage() -> None:
    read = exchange(orders=())
    with pytest.raises(SnapshotError, match=r"\['SOL'\] have no leverage"):
        build_account(NOW, Decimal(1000), read, VIEW, extra_coins=("SOL",))
    read = dataclasses.replace(
        read,
        leverages={"SOL": Leverage("isolated", 2)},
        marks={**read.marks, "SOL": Mark(Decimal(150), NOW - 7)},
    )
    sol = build_account(NOW, Decimal(1000), read, VIEW, extra_coins=("SOL",)).assets["SOL"]
    assert (sol.margin_mode, sol.leverage, sol.mark_px, sol.mark_at_ms) == (
        "isolated",
        2,
        Decimal(150),
        NOW - 7,
    )
    marks = {c: m for c, m in read.marks.items() if c != "SOL"}
    with pytest.raises(SnapshotError, match=r"\['SOL'\] have no mark"):
        build_account(
            NOW, Decimal(1000), dataclasses.replace(read, marks=marks), VIEW, extra_coins=("SOL",)
        )


class _Seen:
    def __init__(self) -> None:
        self.details: list[str] = []

    def apply(self, record: Record) -> None:
        self.details.append(record.payload["detail"])


def errors(led: Ledger, n: int) -> list[Record]:
    start = len(list(led.records())) if led.path.exists() else 0
    for i in range(start, start + n):
        led.append(Kind.ERROR, {"detail": f"{led.path.stem}{i}"})
    return list(led.records())


def test_a_folded_reducer_applies_each_new_record_once(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "a.jsonl")
    folded = Folded(_Seen)
    for _ in range(3):
        folded.over(errors(led, 1))
    folded.over(list(led.records()))

    assert folded.over(list(led.records())).details == ["a0", "a1", "a2"]


def test_a_folded_reducer_starts_over_when_its_records_are_gone(tmp_path: Path) -> None:
    first, second = errors(Ledger(tmp_path / "a.jsonl"), 3), errors(Ledger(tmp_path / "b.jsonl"), 4)
    folded = Folded(_Seen)
    folded.over(first)

    assert folded.over(second).details == ["b0", "b1", "b2", "b3"]
    assert folded.over(first[:2]).details == ["a0", "a1"]


def test_the_risk_fold_fed_record_by_record_matches_a_full_fold(tmp_path: Path) -> None:
    rows = [
        (NOW - DAY_MS, Kind.FILL, fill("BTC", "buy", "1", "100")),
        (NOW - DAY_MS + 1, Kind.FILL, fill("BTC", "sell", "1", "90")),
        (NOW - HOUR, Kind.FILL, fill("ETH", "buy", "2", "10")),
        (NOW - 3, Kind.FILL, fill("ETH", "sell", "1", "9", fee="0.5")),
        (NOW - 1, Kind.FILL, fill("ETH", "sell", "1", "8")),
        (NOW, Kind.LATCH, {"rule": "r", "detail": "d"}),
    ]
    records = ledger(tmp_path, *rows)
    fold = RiskFold()
    views = []
    for record in records:
        fold.apply(record)
        views.append(fold.view(NOW, 60))

    assert views == [fold_ledger(records[: i + 1], NOW, 60) for i in range(len(records))]
    assert views[-1].realized_pnl_today_usd == Decimal("-3.5")
