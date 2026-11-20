from __future__ import annotations

import math
import random
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hl_guard.exchange.nonces import NonceManager
from hl_guard.ledger.ledger import Kind
from hl_guard.policy.load import load_policy
from hl_guard.server import fills as fills_module
from hl_guard.server.app import StartupRefused, boot
from hl_guard.server.snapshot import fold_ledger
from hl_guard.server.tools_write import ENTRY_FIXED_WEIGHT, FILLS_PAGE_WEIGHT, write_weight_floor
from server.conftest import Clock
from server.fake_info import ROOT, Answer, write_config
from server.test_tools_write import BTC, _status_of, approve, records, rest, session
from server.venue import Venue, configure

ETH = {"coin": "ETH", "side": "buy", "size": "0.005", "limit_px": "2379.6", "stop_loss_px": "2350"}
RESTING_BTC = {**BTC, "limit_px": "81000"}
FLOOR = write_weight_floor(load_policy(ROOT / "examples" / "policy.yaml"))
GAPS = pytest.mark.parametrize("gap_s", [120, 7200], ids=["2 min", "2 h"])


def booked(config: Path) -> list[dict[str, Any]]:
    return [dict(r.payload) for r in records(config) if r.kind is Kind.FILL]


def unhalt(config: Path) -> Any:
    return lambda writer, out: (config.parent / "data" / "HALT").unlink()


@GAPS
def test_a_resting_fill_learned_later_is_booked(
    config: Path, venue: Venue, clock: Clock, gap_s: int
) -> None:
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(30)
    venue.fill_resting(order["oid"])
    clock.advance(gap_s)

    session(config, ("propose_order", ETH), ("propose_order", ETH))

    assert [f["oid"] for f in booked(config)] == [order["oid"]]


@GAPS
def test_the_entry_fill_learned_later_keeps_the_close_a_close(
    config: Path, venue: Venue, clock: Clock, gap_s: int
) -> None:
    session(config, rest(RESTING_BTC))
    clock.advance(30)
    venue.fill_resting(venue.resting()[0]["oid"])
    clock.advance(gap_s)

    session(config, ("propose_order", ETH), ("close_position", {"coin": "BTC"}))

    assert [(f["coin"], f["side"]) for f in booked(config)] == [("BTC", "buy"), ("BTC", "sell")]
    view = fold_ledger(records(config), records(config)[-1].ts_ms, 3600)
    assert [e.coin for e in view.entries] == ["BTC"]


@GAPS
def test_a_lost_answer_resolved_later_books_its_fill(
    config: Path, venue: Venue, clock: Clock, gap_s: int
) -> None:
    def lost(body: dict[str, Any]) -> Answer | None:
        return (500, b"upstream", {}) if body["action"]["type"] == "order" else None

    venue.answer_after = lost
    session(config, ("propose_order", BTC), approve(config))
    assert len(venue.fills) == 1
    venue.answer_after = None
    clock.advance(gap_s)

    session(config, unhalt(config), ("propose_order", ETH))

    assert [f["tid"] for f in booked(config)] == [venue.fills[0]["tid"]]


def test_a_fill_record_carries_the_exchange_fill_time(
    config: Path, venue: Venue, clock: Clock
) -> None:
    session(config, rest(RESTING_BTC))
    venue.fill_resting(venue.resting()[0]["oid"])
    filled_at = venue.fills[0]["time"]
    clock.advance(600)

    session(config, ("propose_order", ETH))

    (fill,) = booked(config)
    assert fill["time_ms"] == filled_at


def _ui_fills(venue: Venue, n: int, at_ms: int) -> None:
    for i in range(n):
        venue.fills.append({**venue.fills[-1], "oid": 1, "tid": 90_000 + i, "time": at_ms + i})


def test_fills_beyond_one_page_are_read_page_by_page(
    config: Path, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fills_module, "FILLS_PAGE_ROWS", 5)
    venue.page_rows = 5
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(60)
    venue.fill_resting(order["oid"], "0.0001")
    _ui_fills(venue, 12, venue.fills[-1]["time"] - 30_000)
    clock.advance(60)
    venue.fill_resting(order["oid"])
    clock.advance(60)

    session(config, ("propose_order", ETH))

    assert [f["oid"] for f in booked(config)] == [order["oid"], order["oid"]]
    assert len(venue.fills_asked) >= 3


def _others(venue: Venue, n: int, start_ms: int) -> None:
    template = venue.fills[-1]
    venue.fills += [
        {**template, "oid": 1, "tid": 900_000 + i, "time": start_ms + i * 10} for i in range(n)
    ]


def _fresh_minute(config: Path) -> None:
    boot(config, None).config.budget_path.unlink(missing_ok=True)


def _entry_in_a_fresh_minute(config: Path) -> dict[str, Any]:
    _fresh_minute(config)
    ((_, out),) = session(config, ("propose_order", ETH))
    assert isinstance(out, dict)
    return out


@pytest.mark.parametrize("others", [2500, 9999])
@pytest.mark.parametrize("per_min", [FLOOR, 300, 600])
def test_fills_beyond_a_minutes_budget_are_read_on_over_the_next_minutes(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock, per_min: int, others: int
) -> None:
    config = configure(tmp_path, weight=per_min)
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(60)
    venue.fill_resting(order["oid"], "0.0001")
    _others(venue, others, venue.fills[-1]["time"] + 1)
    clock.advance(others / 100 + 60)
    venue.fill_resting(order["oid"])
    clock.advance(60)
    pages = (others + 2) // fills_module.FILLS_PAGE_ROWS + 1
    minutes = math.ceil(pages / ((per_min - ENTRY_FIXED_WEIGHT) // FILLS_PAGE_WEIGHT))

    outs = [_entry_in_a_fresh_minute(config) for _ in range(minutes + 1)]

    results = [o.get("result") or o.get("error") for o in outs]
    assert results == ["rate_limited"] * (minutes - 1) + ["pending_approval"] * 2
    ours = [f["tid"] for f in venue.fills if f["oid"] == order["oid"]]
    assert [f["tid"] for f in booked(config)] == ours


def test_a_read_for_one_order_leaves_an_older_orders_unread_fills_to_find(
    config: Path, venue: Venue, clock: Clock
) -> None:
    session(config, ("propose_order", ETH), approve(config))
    session(config, rest(RESTING_BTC))
    (older,) = venue.resting()
    clock.advance(600)
    venue.fill_resting(older["oid"], "0.0001")
    clock.advance(600)

    session(config, ("close_position", {"coin": "ETH"}), ("propose_order", ETH))

    partial = venue.fills[-2]
    assert partial["oid"] == older["oid"]
    assert partial["tid"] in [f["tid"] for f in booked(config)]


RESTING_SOL = {
    "coin": "SOL",
    "side": "buy",
    "size": "0.1",
    "limit_px": "103.2",
    "stop_loss_px": "102",
}
UI_COINS = ["APT", "ATOM", "MATIC", "APE", "OP", "DYDX", "GMT", "AVAX"]


def _ui_resting(venue: Venue, coin: str) -> None:
    venue.next_oid += 1
    venue.orders.append(
        {
            "a": venue.names.index(coin),
            "b": True,
            "p": str(venue.marks[coin] / 2),
            "s": "1",
            "r": False,
            "t": {"limit": {"tif": "Gtc"}},
            "c": "0x" + "ab" * 16,
            "coin": coin,
            "oid": venue.next_oid,
            "tif": "Gtc",
            "at": time.time_ns() // 1_000_000,
        }
    )


def _answer(out: dict[str, Any]) -> str:
    return str(out.get("rule") or out.get("result") or out.get("error"))


@pytest.mark.parametrize(
    ("per_min", "ui", "answer"),
    [(FLOOR, 1, "pending_approval"), (184, 1, "pending_approval"), (300, 6, "open_orders_max")],
    ids=["floor + 1 UI order", "184 + 1 UI order", "300 + 6 UI orders"],
)
def test_an_entry_is_evaluated_once_its_fills_are_read_whatever_the_book_needs_after(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock, per_min: int, ui: int, answer: str
) -> None:
    config = configure(tmp_path, weight=per_min, policy={"entries_per_day_max": "5"})
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(60)
    venue.fill_resting(order["oid"], "0.0001")
    for coin in UI_COINS[:ui]:
        _ui_resting(venue, coin)
    clock.advance(60)

    outs = [_answer(_entry_in_a_fresh_minute(config)) for _ in range(2)]

    assert outs[-1] == answer, outs
    ours = [f["tid"] for f in venue.fills if f["oid"] == order["oid"]]
    assert [f["tid"] for f in booked(config)] == ours


def test_the_widest_book_the_policy_allows_is_evaluated_at_its_floor(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock
) -> None:
    policy = {"open_orders_max": "8", "entries_per_day_max": "5"}
    config = configure(tmp_path, weight=600, policy=policy)
    floor = write_weight_floor(boot(config, None).policy)
    config.write_text(
        config.read_text().replace("info_weight_per_min: 600", f"info_weight_per_min: {floor}")
    )
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(60)
    venue.fill_resting(order["oid"], "0.0001")
    for coin in UI_COINS[:6]:
        _ui_resting(venue, coin)
    clock.advance(60)

    out = _entry_in_a_fresh_minute(config)

    assert _answer(out) == "pending_approval", out
    assert len([f for f in booked(config)]) == 1


def test_a_book_wider_than_the_floor_is_priced_for_is_rejected_before_its_reads(
    config: Path, venue: Venue, fake: Any
) -> None:
    for coin in UI_COINS[:2]:
        _ui_resting(venue, coin)
        venue.orders[-1]["r"] = True
    _ui_resting(venue, UI_COINS[2])
    before = len(fake.requests)

    ((_, out),) = session(config, ("propose_order", ETH))

    assert out["rule"] == "wide_book", out
    assert "the leverage of 4 coins" in out["detail"]
    asked = [r["type"] for r in fake.requests[before:]]
    assert "activeAssetData" not in asked


def test_a_long_resting_order_that_part_fills_keeps_booking_its_fills(
    config: Path, venue: Venue, clock: Clock
) -> None:
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(8 * fills_module.FILLS_WINDOW_MS / 7000)
    venue.fill_resting(order["oid"], "0.0001")
    clock.advance(60)

    ((_, first),) = session(config, ("propose_order", ETH))
    clock.advance(60)
    venue.fill_resting(order["oid"])
    clock.advance(60)
    session(config, ("propose_order", ETH))

    assert first["result"] == "pending_approval", first
    assert [r for r in records(config) if r.kind is Kind.LATCH] == []
    ours = [f["tid"] for f in venue.fills if f["oid"] == order["oid"]]
    assert [f["tid"] for f in booked(config)] == ours


def test_a_close_between_entries_leaves_a_part_filled_entry_to_the_next_entry(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock
) -> None:
    config = configure(tmp_path, policy={"entries_per_day_max": "5"})
    venue.leverage["SOL"] = ("isolated", 1)
    session(config, rest(RESTING_BTC))
    session(config, rest({**RESTING_SOL, "limit_px": "103.62"}))
    (resting,) = venue.resting()
    clock.advance(60)
    venue.fill_resting(resting["oid"], "0.0001")
    clock.advance(60)

    ((_, closed), (_, entry)) = session(
        config, ("close_position", {"coin": "SOL"}), ("propose_order", ETH)
    )

    assert closed["result"] == "placed" and entry["result"] == "pending_approval", entry
    tids = [f["tid"] for f in booked(config)]
    assert sorted(tids) == sorted(f["tid"] for f in venue.fills)
    assert len(tids) == len(set(tids))


def _evaluated_within(config: Path, bound: int) -> str:
    answers: list[str] = []
    while not answers or answers[-1] == "rate_limited":
        assert len(answers) < bound, f"no evaluation within {bound} minutes: {answers}"
        answers.append(_answer(_entry_in_a_fresh_minute(config)))
    return answers[-1]


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("per_min", [FLOOR, 184, 300, 600])
def test_an_entry_is_evaluated_after_the_catch_up_and_each_own_fill_is_booked_once(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock, per_min: int, seed: int
) -> None:
    rng = random.Random(f"{per_min}-{seed}")
    config = configure(tmp_path, weight=per_min, policy={"entries_per_day_max": "20"})
    own = rng.randint(0, 2)
    for proposal in [RESTING_BTC, RESTING_SOL][:own]:
        _fresh_minute(config)
        session(config, rest(proposal))
    oids = [o["oid"] for o in venue.resting()]
    ui = rng.randint(0, 3 - own)
    for coin in UI_COINS[:ui]:
        _ui_resting(venue, coin)
    clock.advance(60)
    for oid in oids:
        size = next(o["s"] for o in venue.resting() if o["oid"] == oid)
        venue.fill_resting(oid, rng.choice([None, str(Decimal(size) / 2)]))
        clock.advance(rng.randint(1, 600))
    others = rng.choice([0, rng.randint(1, 9999), 9999])
    if venue.fills and others:
        _others(venue, others, venue.fills[-1]["time"] + 1)
    clock.advance(others / 100 + 60)
    pages = len(venue.fills) // fills_module.FILLS_PAGE_ROWS + 1
    bound = math.ceil(pages / ((per_min - ENTRY_FIXED_WEIGHT) // FILLS_PAGE_WEIGHT)) + 1
    opening = len(venue.resting())

    first = _evaluated_within(config, bound)
    for oid in [o["oid"] for o in venue.resting() if o["oid"] in oids]:
        venue.fill_resting(oid)
    clock.advance(61)
    second = _evaluated_within(config, bound)

    full = "open_orders_max"
    assert (first, second) == (
        full if opening >= 3 else "pending_approval",
        full if ui >= 3 else "pending_approval",
    )
    tids = [f["tid"] for f in booked(config)]
    assert sorted(tids) == sorted(f["tid"] for f in venue.fills if f["oid"] in oids)
    assert len(tids) == len(set(tids))
    assert [r for r in records(config) if r.kind is Kind.LATCH] == []


def test_write_tools_refuse_a_weight_below_one_entry_and_one_fills_page(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, weight=FLOOR - 1)

    with pytest.raises(StartupRefused, match="at least 164 for open_orders_max 3"):
        boot(config, None)
    (tmp_path / "wide").mkdir()
    wide = configure(tmp_path / "wide", weight=464, policy={"open_orders_max": "20"})
    with pytest.raises(StartupRefused, match="at least 465 for open_orders_max 20"):
        boot(wide, None)
    wide.write_text(
        wide.read_text().replace("info_weight_per_min: 464", "info_weight_per_min: 465")
    )
    assert boot(wide, None).agent is not None
    (tmp_path / "read").mkdir()
    read_only = write_config(tmp_path / "read")
    read_only.write_text(read_only.read_text() + "info_weight_per_min: 20\n")
    assert boot(read_only, None).agent is None


def test_an_order_whose_fills_never_show_is_given_up_with_a_latch(
    config: Path, venue: Venue, clock: Clock
) -> None:
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    venue.fill_resting(order["oid"])
    venue.fills.clear()
    clock.advance(fills_module.FILLS_WINDOW_MS / 1000 + 3600)
    ((_, learned),) = session(config, ("propose_order", ETH))
    assert learned["result"] == "pending_approval"
    clock.advance(fills_module.FILLS_LAG_BEHIND_STATUS_MS / 1000)

    ((_, first),) = session(config, ("propose_order", ETH))
    reads = len(venue.fills_asked)
    window_start = records(config)[-1].ts_ms - fills_module.FILLS_WINDOW_MS
    assert venue.fills_asked[-1]["startTime"] >= window_start - 60_000
    ((_, second),) = session(config, ("propose_order", ETH))

    kinds = [r.kind for r in records(config)]
    assert Kind.LATCH in kinds and Kind.ERROR in kinds
    assert first["rule"] == "latched" and second["rule"] == "latched"
    assert len(venue.fills_asked) == reads


@pytest.mark.parametrize("age_days", [6, 8], ids=["inside the window", "older than the window"])
def test_a_resting_order_whose_fill_row_lags_its_status_is_booked_not_given_up(
    config: Path, venue: Venue, clock: Clock, age_days: int
) -> None:
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(age_days * 86_400)
    venue.fill_resting(order["oid"])
    venue.fills_lag = 1
    clock.advance(1)
    session(config, ("propose_order", ETH))
    venue.fills_lag = 0
    clock.advance(60)

    session(config, ("propose_order", ETH))

    assert [r for r in records(config) if r.kind is Kind.LATCH] == []
    assert [f["tid"] for f in booked(config)] == [f["tid"] for f in venue.fills]


def test_an_order_inside_the_window_is_not_given_up_while_its_fills_stay_missing(
    config: Path, venue: Venue, clock: Clock
) -> None:
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(86_400)
    venue.fill_resting(order["oid"])
    venue.fills.clear()
    session(config, ("propose_order", ETH))
    clock.advance(fills_module.FILLS_LAG_BEHIND_STATUS_MS / 1000 + 60)

    ((_, out),) = session(config, ("propose_order", ETH))

    assert out["result"] == "pending_approval"
    assert [r for r in records(config) if r.kind is Kind.LATCH] == []


def test_an_old_order_cancelled_after_a_lost_part_fill_is_given_up_from_the_cancel(
    config: Path, venue: Venue, clock: Clock
) -> None:
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    venue.fill_resting(order["oid"], "0.0001")
    venue.fills.clear()
    clock.advance(fills_module.FILLS_WINDOW_MS / 1000 + 3600)
    session(config, ("cancel_order", {"coin": "BTC", "oid": order["oid"]}))
    clock.advance(fills_module.FILLS_LAG_BEHIND_STATUS_MS / 1000 + 60)

    ((_, out),) = session(config, ("propose_order", ETH))

    assert out["rule"] == "latched"
    assert [r.payload["rule"] for r in records(config) if r.kind is Kind.LATCH] == ["fills_missing"]


def test_an_order_whose_fills_never_show_is_given_up_after_pages_over_minutes(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock
) -> None:
    config = configure(tmp_path, weight=300)
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    venue.fill_resting(order["oid"])
    clock.advance(fills_module.FILLS_WINDOW_MS / 1000 + 3600)
    now = time.time_ns() // 1_000_000
    venue.fills[-1] |= {"oid": 1, "tid": 899_999, "time": now - 200_000}
    _others(venue, 9998, now - 120_000)

    pages = 9999 // fills_module.FILLS_PAGE_ROWS + 1
    minutes = math.ceil(pages / ((300 - ENTRY_FIXED_WEIGHT) // FILLS_PAGE_WEIGHT))

    outs = [_entry_in_a_fresh_minute(config) for _ in range(minutes)]
    clock.advance(fills_module.FILLS_LAG_BEHIND_STATUS_MS / 1000)
    outs += [_entry_in_a_fresh_minute(config) for _ in range(2)]

    results = [o.get("rule") or o.get("error") for o in outs]
    assert results == ["rate_limited"] * (minutes - 1) + [None] + ["latched"] * 2
    assert [r.payload["rule"] for r in records(config) if r.kind is Kind.LATCH] == ["fills_missing"]


def test_every_action_is_signed_to_expire_30_s_after_it_is_sent(config: Path, venue: Venue) -> None:
    before = time.time_ns() // 1_000_000
    session(config, ("propose_order", BTC), approve(config))
    after = time.time_ns() // 1_000_000

    orders = [r for r in records(config) if r.kind is Kind.ORDER]
    assert len(orders) == len(venue.bodies) == 2
    for record, body in zip(orders, venue.bodies, strict=True):
        assert record.payload["expires_after"] == body["expiresAfter"]
        assert before + 30_000 <= body["expiresAfter"] <= after + 30_000


def test_the_expiry_follows_the_clock_when_the_ledger_nonce_is_ahead(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configure(tmp_path, policy={"approval": "never"})
    clock.advance(3600)
    session(config, ("propose_order", BTC), autonomous=True)
    monkeypatch.setattr(NonceManager, "_by_agent", {})
    clock.step(-3600)
    before = time.time_ns() // 1_000_000

    session(config, ("close_position", {"coin": "BTC"}), autonomous=True)

    close = [r.payload for r in records(config) if r.kind is Kind.ORDER][-1]
    assert close["nonce"] > before + 3_000_000
    assert before + 30_000 <= close["expires_after"] <= time.time_ns() // 1_000_000 + 30_000


def _lost_then(config: Path, venue: Venue) -> None:
    venue.leverage["BTC"] = ("isolated", 1)
    venue.hold = True
    session(config, rest(RESTING_BTC))
    assert venue.held and not venue.resting()


def test_a_lost_order_is_not_failed_while_its_post_can_still_land(
    config: Path, venue: Venue, clock: Clock
) -> None:
    _lost_then(config, venue)
    clock.advance(5)
    session(config, unhalt(config), ("propose_order", ETH))
    clock.advance(5)
    venue.land()
    (order,) = venue.resting()

    session(config, unhalt(config), ("propose_order", ETH))

    placed = [r.payload for r in records(config) if r.payload.get("outcome") == "placed"]
    assert [p["oid"] for p in placed] == [order["oid"]]
    assert not [r for r in records(config) if r.payload.get("cloids")]


def test_a_lost_order_past_its_expiry_is_failed_and_cannot_land(
    config: Path, venue: Venue, clock: Clock
) -> None:
    _lost_then(config, venue)
    clock.advance(40)

    session(config, unhalt(config), ("propose_order", ETH))
    venue.land()

    failed = [r.payload for r in records(config) if r.payload.get("cloids")]
    assert [(f["outcome"], f["rule"]) for f in failed] == [("failed", "exchange")]
    assert venue.resting() == [] and venue.positions == {}


def test_an_order_the_book_read_misses_but_order_status_calls_resting_is_kept(
    config: Path, venue: Venue, clock: Clock
) -> None:
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    venue.hidden.add(order["oid"])
    session(config, ("propose_order", ETH))
    venue.hidden.clear()
    clock.advance(60)
    venue.fill_resting(order["oid"])
    clock.advance(600)

    session(config, ("propose_order", ETH))

    assert [f["oid"] for f in booked(config)] == [order["oid"]]


def _lost_entry(tmp_path: Path, venue: Venue) -> tuple[Path, dict[str, Any]]:
    config = configure(tmp_path, policy={"approval": "never", "entries_per_day_max": "1"})

    def lost(body: dict[str, Any]) -> Answer | None:
        return (500, b"upstream", {}) if body["action"]["type"] == "order" else None

    venue.answer_after = lost
    session(config, rest(RESTING_BTC), autonomous=True)
    venue.answer_after = None
    (order,) = venue.resting()
    return config, order


def test_a_lost_order_stays_unresolved_while_a_server_clock_ahead_could_still_see_it_land(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def ahead(*args: Any, **kwargs: Any) -> Any:
        rt = boot(*args, **kwargs)
        rt.now_ms = lambda: time.time_ns() // 1_000_000 + 3_000
        return rt

    monkeypatch.setattr("server.test_tools_write.boot", ahead)
    config = configure(tmp_path, policy={"approval": "never"})
    venue.leverage["BTC"] = ("isolated", 1)
    venue.hold = True
    session(config, ("propose_order", BTC), autonomous=True)
    clock.advance(32.5)

    session(config, unhalt(config), ("propose_order", ETH), autonomous=True)
    venue.land()

    failed = [r for r in records(config) if r.payload.get("outcome") == "failed"]
    assert [r.payload.get("cloids") for r in failed if "cloids" in r.payload] == []
    assert "BTC" in venue.positions


def test_a_lost_entry_that_landed_and_was_cancelled_unfilled_still_counts(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock
) -> None:
    config, order = _lost_entry(tmp_path, venue)
    clock.advance(60)

    out = session(
        config,
        ("cancel_order", {"coin": "BTC", "oid": order["oid"]}),
        unhalt(config),
        ("propose_order", ETH),
        autonomous=True,
    )

    assert venue.resting() == []
    assert out[2][1]["rule"] == "entries_per_day_max"
    assert not [r for r in records(config) if r.payload.get("cloids")]
    outcomes = [r.payload.get("outcome") for r in records(config) if r.kind is Kind.DECISION]
    assert outcomes[-3:-1] == ["placed", "cancelled"]


def test_a_lost_entry_the_exchange_reports_rejected_stops_counting(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock
) -> None:
    config, order = _lost_entry(tmp_path, venue)
    venue.resting().clear()
    venue.by_cloid[order["c"]]["status"] = "perpMarginRejected"
    clock.advance(60)

    out = session(config, unhalt(config), ("propose_order", ETH), autonomous=True)

    assert out[1][1]["result"] == "placed"
    failed = [r.payload for r in records(config) if r.payload.get("cloids")]
    assert [(f["outcome"], f["cloids"]) for f in failed] == [("failed", [order["c"]])]


def _more_fills_at_one_ms_than_a_page(
    venue: Venue, monkeypatch: pytest.MonkeyPatch, at_ms: int
) -> None:
    monkeypatch.setattr(fills_module, "FILLS_PAGE_ROWS", 5)
    venue.page_rows = 5
    row: dict[str, Any] = {
        "coin": "SOL",
        "side": "B",
        "px": "100",
        "sz": "1",
        "fee": "0",
        "feeToken": "USDC",
    }
    row |= {"startPosition": "0", "closedPnl": "0", "crossed": True, "dir": "Open Long"}
    row |= {"hash": "0x" + "0" * 64, "oid": 1, "time": at_ms}
    venue.fills += [{**row, "tid": 800_000 + i} for i in range(6)]


def test_a_filled_close_whose_fills_cannot_be_read_still_answers_placed(
    config: Path, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(60)
    venue.fill_resting(order["oid"], "0.0001")
    clock.advance(60)
    stuck_at = time.time_ns() // 1_000_000
    _more_fills_at_one_ms_than_a_page(venue, monkeypatch, stuck_at)

    ((error, closed), (_, entry)) = session(
        config, ("close_position", {"coin": "BTC"}), ("propose_order", ETH)
    )

    assert (error, closed["result"]) == (False, "placed")
    assert "BTC" not in venue.positions
    errors = [r.payload["detail"] for r in records(config) if r.kind is Kind.ERROR]
    assert errors == [f"fills not read: 5 fills share the time {stuck_at} ms"]
    assert (entry["result"], entry["rule"]) == ("rejected", "fills_unread")


def test_an_approved_entry_that_filled_reads_placed_when_its_fills_cannot_be_read(
    config: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stuck_then_approve(writer: Any, out: list[Any]) -> Any:
        _more_fills_at_one_ms_than_a_page(venue, monkeypatch, time.time_ns() // 1_000_000)
        return approve(config)(writer, out)

    out = session(config, ("propose_order", BTC), stuck_then_approve, _status_of())

    assert out[-1][1]["status"] == "placed"
    assert venue.positions["BTC"].size == Decimal("0.0002")


def test_a_lost_order_that_part_filled_before_it_was_resolved_books_every_fill(
    config: Path, venue: Venue, clock: Clock
) -> None:
    def lost(body: dict[str, Any]) -> Answer | None:
        return (500, b"upstream", {}) if body["action"]["type"] == "order" else None

    venue.answer_after = lost
    session(config, rest(RESTING_BTC))
    venue.answer_after = None
    (order,) = venue.resting()
    clock.advance(600)
    venue.fill_resting(order["oid"], "0.0001")
    clock.advance(7200)
    session(config, unhalt(config), ("propose_order", ETH))
    clock.advance(600)
    venue.fill_resting(order["oid"])
    clock.advance(7200)

    session(config, ("propose_order", ETH))

    assert sorted(f["tid"] for f in booked(config)) == sorted(f["tid"] for f in venue.fills)


def test_fills_at_one_ms_across_a_page_boundary_are_booked_once_each(
    config: Path, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fills_module, "FILLS_PAGE_ROWS", 5)
    venue.page_rows = 5
    session(config, rest(RESTING_BTC))
    (order,) = venue.resting()
    clock.advance(60)
    for _ in range(4):
        venue.fill_resting(order["oid"], "0.00001")
    at = venue.fills[-1]["time"]
    for f in venue.fills:
        f["time"] = at
    _others(venue, 3, at - 1)
    for f in venue.fills[-3:]:
        f["time"] = at - 1
    venue.fill_resting(order["oid"], "0.00016")
    venue.fills[-1]["time"] = at
    clock.advance(60)

    session(config, ("propose_order", ETH))

    ours = [f["tid"] for f in venue.fills if f["oid"] == order["oid"]]
    assert sorted(f["tid"] for f in booked(config)) == sorted(ours)
    assert len(venue.fills_asked) >= 2
