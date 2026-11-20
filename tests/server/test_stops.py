"""Stops placed with the entry: the normalTpsl entry, the shrink after a partial
fill and its fallback, reconcile after a restart, and close, halt and positions
with stops on the book. The fake venue follows the testnet runs of 2026-11-19."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hl_guard.ledger.ledger import Kind
from hl_guard.server import tools_write
from hl_guard.server.snapshot import fold_ledger
from hl_guard.server.tools_write import Writer
from server.conftest import Clock
from server.fake_info import Answer, FakeInfo
from server.test_tools_write import BTC, actions_sent, approve, records, session
from server.venue import Position, Venue, configure

WITH_TP = {**BTC, "take_profit_px": "84000"}


@pytest.fixture()
def never(tmp_path: Path, durable: None, venue: Venue) -> Path:
    venue.leverage["BTC"] = ("isolated", 1)
    return configure(tmp_path, policy={"approval": "never"})


def run(config: Path, *steps: Any) -> list[Any]:
    return session(config, *steps, autonomous=True)


def children(venue: Venue) -> list[dict[str, Any]]:
    return [o for o in venue.orders if o.get("trigger")]


def kinds_sent(config: Path) -> list[str]:
    return [a["type"] for a in actions_sent(config)]


def protection(out: Any) -> dict[str, Any]:
    found: dict[str, Any] = out[1]["protection"]
    return found


def test_an_entry_goes_out_as_one_ioc_normal_tpsl_action_with_its_stops(
    never: Path, venue: Venue
) -> None:
    ((_, placed),) = run(never, ("propose_order", WITH_TP))

    (action,) = [a for a in venue.actions if a["type"] == "order"]
    entry, sl, tp = action["orders"]
    assert action["grouping"] == "normalTpsl"
    assert entry["t"] == {"limit": {"tif": "Ioc"}}
    assert (sl["t"]["trigger"], tp["t"]["trigger"]) == (
        {"isMarket": True, "triggerPx": "80000", "tpsl": "sl"},
        {"isMarket": True, "triggerPx": "84000", "tpsl": "tp"},
    )
    assert {sl["s"], tp["s"]} == {entry["s"]} and sl["r"] is tp["r"] is True
    assert (sl["p"], tp["p"]) == ("76000", "79800")
    assert placed["status"] == "filled"
    assert {k: (v["size"], v["state"]) for k, v in placed["protection"].items()} == {
        "sl": ("0.0002", "live"),
        "tp": ("0.0002", "live"),
    }
    assert [o["s"] for o in children(venue)] == ["0.0002", "0.0002"]
    recorded = [r.payload for r in records(never) if r.kind is Kind.CHILD]
    assert [c["cloid"] for c in recorded] == [sl["c"], tp["c"]]
    assert recorded[0]["parent"] == entry["c"]


def test_a_grouped_entry_counts_once_toward_the_entry_caps(never: Path, venue: Venue) -> None:
    run(never, ("propose_order", WITH_TP))

    rs = records(never)
    assert [e.coin for e in fold_ledger(rs, rs[-1].ts_ms, 3600).entries] == ["BTC"]


def test_a_gtc_entry_is_refused_as_a_bad_argument(never: Path, venue: Venue) -> None:
    ((error, out),) = run(never, ("propose_order", {**BTC, "tif": "Gtc"}))

    assert error is True
    assert out["error"] == "bad_arguments"
    assert venue.actions == []


def test_an_entry_against_a_short_is_refused_by_opposite_position(
    never: Path, venue: Venue
) -> None:
    venue.positions["BTC"] = Position(Decimal("-0.0002"), Decimal(81171), 1)

    ((_, out),) = run(never, ("propose_order", BTC))

    assert (out["result"], out["rule"]) == ("rejected", "opposite_position")
    assert venue.actions == []


def test_a_partial_fill_shrinks_both_stops_to_the_filled_size(never: Path, venue: Venue) -> None:
    venue.liquidity["BTC"] = Decimal("0.0001")

    ((_, placed),) = run(never, ("propose_order", WITH_TP))

    assert placed["filled_size"] == "0.0001"
    assert placed["warning"] is None
    (modify,) = [a for a in venue.actions if a["type"] == "batchModify"]
    assert [m["order"]["s"] for m in modify["modifies"]] == ["0.0001", "0.0001"]
    assert [m["oid"] for m in modify["modifies"]] == [m["order"]["c"] for m in modify["modifies"]]
    assert [o["s"] for o in children(venue)] == ["0.0001", "0.0001"]
    assert {v["state"] for v in placed["protection"].values()} == {"shrunk"}
    resized = [r.payload for r in records(never) if r.kind is Kind.CHILD_RESIZE]
    assert [r["oid"] for r in resized] == [o["oid"] for o in children(venue)]


def _refuse_modify(action: dict[str, Any]) -> Answer:
    statuses = [{"error": "Cannot modify canceled or filled order"} for _ in action["modifies"]]
    body = {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}
    return 200, json.dumps(body).encode(), {}


def test_a_refused_shrink_cancels_the_stops_closes_the_lot_and_warns(
    never: Path, venue: Venue
) -> None:
    venue.liquidity["BTC"] = Decimal("0.0001")
    venue.modify_override = _refuse_modify

    ((_, placed),) = run(never, ("propose_order", WITH_TP))

    assert placed["warning"].startswith("WARNING: the stop could not be shrunk to the 0.0001 BTC")
    assert "hl-guard closed 0.0001 BTC reduce-only" in placed["warning"]
    assert "the BTC position is now 0" in placed["warning"]
    assert {v["state"] for v in placed["protection"].values()} == {"closed: modify failed"}
    assert children(venue) == [] and venue.positions == {}
    assert kinds_sent(never)[-3:] == ["batchModify", "cancelByCloid", "order"]
    close = actions_sent(never)[-1]["orders"][0]
    assert (close["r"], close["s"], close["t"]) == (True, "0.0001", {"limit": {"tif": "Ioc"}})


def test_a_shrink_with_no_usable_answer_halts_and_still_closes_the_lot(
    never: Path, venue: Venue
) -> None:
    venue.liquidity["BTC"] = Decimal("0.0001")
    venue.modify_override = lambda action: (500, b"upstream", {})

    ((_, placed),) = run(never, ("propose_order", BTC))

    assert (never.parent / "data" / "HALT").exists()
    assert placed["warning"].startswith("WARNING: ")
    assert children(venue) == [] and venue.positions == {}


def test_a_server_that_died_before_the_shrink_shrinks_on_its_next_start(
    never: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    venue.liquidity["BTC"] = Decimal("0.0001")
    with monkeypatch.context() as died:
        died.setattr(Writer, "_protect", lambda *args: None)
        run(never, ("propose_order", WITH_TP))
    assert [o["s"] for o in children(venue)] == ["0.0002", "0.0002"]
    (never.parent / "data" / "HALT").write_text("set by the user\n")

    run(never)

    assert [o["s"] for o in children(venue)] == ["0.0001", "0.0001"]
    assert kinds_sent(never)[-1] == "batchModify"


def test_a_stop_that_fired_books_its_fill_into_the_loss_streak(
    never: Path, venue: Venue, clock: Clock
) -> None:
    run(never, ("propose_order", BTC))
    venue.move("BTC", 79990)
    (fired,) = venue.trigger("BTC")
    clock.advance(10)

    ((_, status),) = run(never, lambda w, o: (False, w.status({"id": _entry_id(never)})))

    assert status["protection"]["sl"]["state"] == "fired"
    fills = [r.payload for r in records(never) if r.kind is Kind.FILL]
    assert [(f["side"], f["oid"]) for f in fills][-1] == ("sell", fired)
    rs = records(never)
    view = fold_ledger(rs, rs[-1].ts_ms, 3600)
    assert view.loss_streak == 1 and view.realized_pnl_today_usd < 0


def _entry_id(config: Path) -> str:
    return str(
        next(
            r.payload["id"]
            for r in records(config)
            if r.kind is Kind.PROPOSAL and r.payload["tool"] == "propose_order"
        )
    )


def test_a_shrunk_stop_is_asked_about_by_its_new_oid_not_its_cloid(
    never: Path, venue: Venue, fake: FakeInfo
) -> None:
    venue.liquidity["BTC"] = Decimal("0.0001")
    run(never, ("propose_order", WITH_TP))
    sl = next(o for o in children(venue) if o["trigger"]["tpsl"] == "sl")
    venue.move("BTC", 79990)
    venue.trigger("BTC")
    before = len(fake.requests)

    ((_, status),) = run(never, lambda w, o: (False, w.status({"id": _entry_id(never)})))

    asked = [r["oid"] for r in fake.requests[before:] if r["type"] == "orderStatus"]
    assert sl["oid"] in asked and sl["c"] not in asked
    assert (status["protection"]["sl"]["state"], status["protection"]["tp"]["state"]) == (
        "fired",
        "cancelled",
    )


def test_a_full_close_cancels_the_stops_of_the_lot(never: Path, venue: Venue) -> None:
    out = run(never, ("propose_order", WITH_TP), ("close_position", {"coin": "BTC"}))

    closed = out[1][1]
    assert closed["result"] == "placed"
    assert {v["state"] for v in closed["protection"].values()} == {"cancelled"}
    assert closed["warning"] is None
    assert children(venue) == [] and venue.positions == {}


def test_a_partial_close_shrinks_the_stops_to_the_lot_left(never: Path, venue: Venue) -> None:
    out = run(
        never,
        ("propose_order", WITH_TP),
        ("close_position", {"coin": "BTC", "size": "0.0001"}),
    )

    closed = out[1][1]
    assert {v["size"] for v in closed["protection"].values()} == {"0.0001"}
    assert [o["s"] for o in children(venue)] == ["0.0001", "0.0001"]
    assert venue.positions["BTC"].size == Decimal("0.0001")


def test_a_close_comes_out_of_the_own_lot_before_the_users_coins(never: Path, venue: Venue) -> None:
    out = run(
        never,
        ("propose_order", BTC),
        lambda w, o: venue.positions.__setitem__(
            "BTC", Position(Decimal("0.0005"), Decimal(81171), 1)
        ),
        ("close_position", {"coin": "BTC", "size": "0.0002"}),
    )

    assert {v["state"] for v in out[2][1]["protection"].values()} == {"cancelled"}
    assert children(venue) == []


def _refuse_reduce_only(venue: Venue) -> Any:
    def answer(body: dict[str, Any]) -> Answer:
        action = body["action"]
        if action["type"] == "order" and action["orders"][0]["r"]:
            text = "Reduce only order would increase position."
            statuses = [{"error": text} for _ in action["orders"]]
            data = {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}
            return 200, json.dumps(data).encode(), {}
        return venue._act(action)

    return answer


def test_halt_keeps_the_stops_of_a_coin_whose_close_failed(never: Path, venue: Venue) -> None:
    out = run(never, ("propose_order", WITH_TP))
    venue.exchange_override = _refuse_reduce_only(venue)

    ((_, halted),) = run(never, ("halt", {}))

    assert [c["coin"] for c in halted["closes"]] == ["BTC"] and "refused" in halted["closes"][0]
    assert "stops" not in halted
    assert len(children(venue)) == 2
    assert {v["state"] for v in protection(out[0]).values()} == {"live"}


def _refuse_far_from_oracle(venue: Venue) -> Any:
    def answer(body: dict[str, Any]) -> Answer:
        action = body["action"]
        if action["type"] == "order" and action["orders"][0]["r"]:
            text = "Price too far from oracle asset=0"
            statuses = [{"error": text} for _ in action["orders"]]
            data = {"status": "ok", "response": {"type": "order", "data": {"statuses": statuses}}}
            return 200, json.dumps(data).encode(), {}
        return venue._act(action)

    return answer


ORACLE_HINT = (
    "Price too far from oracle asset=0. The exchange refused the close price as too far "
    "from its oracle; nothing was closed and the stops stay"
)


def test_a_close_refused_as_too_far_from_the_oracle_says_so_and_keeps_the_stops(
    never: Path, venue: Venue
) -> None:
    run(never, ("propose_order", WITH_TP))
    venue.exchange_override = _refuse_far_from_oracle(venue)

    ((_, closed),) = run(never, ("close_position", {"coin": "BTC"}))

    assert (closed["result"], closed["rule"], closed["detail"]) == (
        "rejected",
        "exchange",
        ORACLE_HINT,
    )
    assert len(children(venue)) == 2
    assert venue.positions["BTC"].size == Decimal("0.0002")


def test_halt_with_a_close_refused_as_too_far_from_the_oracle_stays_halted_and_keeps_the_stops(
    never: Path, venue: Venue
) -> None:
    run(never, ("propose_order", WITH_TP))
    venue.exchange_override = _refuse_far_from_oracle(venue)

    ((_, halted), (_, again)) = run(never, ("halt", {}), ("propose_order", WITH_TP))

    assert halted["closes"] == [{"coin": "BTC", "refused": ORACLE_HINT}]
    assert halted["unfinished"] == [f"close BTC: refused: {ORACLE_HINT}"]
    assert "stops" not in halted
    assert len(children(venue)) == 2
    assert venue.positions["BTC"].size == Decimal("0.0002")
    assert (again["result"], again["rule"]) == ("rejected", "halted")


def test_halt_lists_a_coin_refused_before_the_send_as_unfinished(never: Path, venue: Venue) -> None:
    run(never, ("propose_order", WITH_TP))
    venue.marks["ZZZ"] = Decimal(5)
    venue.positions["ZZZ"] = Position(Decimal(3), Decimal(5), 1)

    ((_, halted),) = run(never, ("halt", {}))

    refused = "exchange: ZZZ is not a listed perp"
    assert {"coin": "ZZZ", "refused": refused} in halted["closes"]
    assert halted["unfinished"] == [f"close ZZZ: refused: {refused}"]
    assert set(venue.positions) == {"ZZZ"}
    assert (never.parent / "data" / "HALT").exists()


def test_a_fallback_close_refused_as_too_far_from_the_oracle_never_says_the_stops_stay(
    never: Path, venue: Venue
) -> None:
    venue.liquidity["BTC"] = Decimal("0.0001")
    venue.modify_override = _refuse_modify
    venue.exchange_override = _refuse_far_from_oracle(venue)

    ((_, placed),) = run(never, ("propose_order", WITH_TP))

    warning = placed["warning"]
    assert (
        "the close was refused: Price too far from oracle asset=0. The exchange refused the "
        "close price as too far from its oracle; the BTC position is now 0.0001"
    ) in warning
    assert "stops stay" not in warning and "nothing was closed" not in warning
    assert venue.positions["BTC"].size == Decimal("0.0001")


def test_halt_cancels_the_stops_of_a_coin_it_closed(never: Path, venue: Venue) -> None:
    run(never, ("propose_order", WITH_TP))

    ((_, halted),) = run(never, ("halt", {}))

    assert {v["state"] for v in halted["stops"]["BTC"].values()} == {"cancelled"}
    assert children(venue) == [] and venue.positions == {}


def test_cancel_order_refuses_a_stop(never: Path, venue: Venue) -> None:
    run(never, ("propose_order", BTC))
    (sl,) = children(venue)

    ((_, out),) = run(never, ("cancel_order", {"coin": "BTC", "oid": sl["oid"]}))

    assert (out["result"], out["rule"]) == ("rejected", "protects_position")
    assert children(venue) == [sl]


def test_positions_warns_when_a_stop_is_larger_than_the_own_lot(never: Path, venue: Venue) -> None:
    run(never, ("propose_order", BTC))
    (sl,) = children(venue)
    venue.positions["BTC"] = Position(Decimal("0.0001"), Decimal(81171), 1)

    ((_, rows),) = run(never, ("positions", {}))

    (warning,) = rows[0]["stop_warnings"]
    assert warning.startswith(f"the hl-guard sl {sl['c']} is 0.0002 BTC, larger than")
    assert "own BTC lot of 0.0001" in warning
    assert children(venue) == [sl]


def test_positions_shows_no_warning_for_a_stop_that_fits(never: Path, venue: Venue) -> None:
    ((_, rows),) = run(never, ("propose_order", BTC), ("positions", {}))[1:]

    assert rows[0]["stop_warnings"] == []


def test_a_failed_reconcile_read_never_stops_a_close(
    never: Path, venue: Venue, fake: FakeInfo
) -> None:
    run(never, ("propose_order", BTC))
    reads = {"n": 0}
    rows = fake.answers["frontendOpenOrders"]

    def first_fails(body: dict[str, Any]) -> Answer:
        reads["n"] += 1
        return (500, b"", {}) if reads["n"] == 1 else rows(body)

    fake.answers["frontendOpenOrders"] = first_fails

    ((_, closed),) = run(never, ("close_position", {"coin": "BTC"}))

    assert closed["result"] == "placed"
    assert venue.positions == {}
    errors = [r.payload["detail"] for r in records(never) if r.kind is Kind.ERROR]
    assert any(e.startswith("stop children not read: ") for e in errors)


def test_no_open_stop_means_no_reconcile_read(never: Path, venue: Venue, fake: FakeInfo) -> None:
    run(never, ("propose_order", {**BTC, "size": "0.0001"}))

    assert [r["type"] for r in fake.requests] == []
    assert Decimal("0.05") == tools_write.CHILD_SLIPPAGE


def test_a_partial_close_whose_shrink_fails_cancels_the_stops_and_keeps_the_lot(
    never: Path, venue: Venue
) -> None:
    venue.modify_override = _refuse_modify

    out = run(
        never,
        ("propose_order", WITH_TP),
        ("close_position", {"coin": "BTC", "size": "0.0001"}),
    )

    closed = out[1][1]
    assert closed["result"] == "placed"
    assert closed["warning"].startswith("WARNING: the stop could not be shrunk to the 0.0001 BTC")
    assert "the lot left has no stop" in closed["warning"]
    assert children(venue) == []
    assert venue.positions["BTC"].size == Decimal("0.0001")


def test_an_entry_at_the_weight_floor_is_evaluated_with_a_stop_on_the_book(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock
) -> None:
    """The floor still buys one entry evaluation a minute with a live stop; the
    reconcile of an entry uses the entry's own book read."""
    from server.test_fills import ETH, FLOOR, _fresh_minute

    config = configure(tmp_path, weight=FLOOR, policy={"entries_per_day_max": "5"})
    _fresh_minute(config)

    def next_minute(writer: Writer, out: list[Any]) -> None:
        clock.advance(61)
        _fresh_minute(config)

    out = session(
        config,
        ("propose_order", BTC),
        approve(config),
        next_minute,
        ("propose_order", ETH),
        next_minute,
        ("propose_order", ETH),
    )

    assert len(children(venue)) == 1
    assert [out[i][1]["result"] for i in (3, 5)] == ["pending_approval", "pending_approval"]


def test_a_reconcile_that_cannot_shrink_a_stop_warns_in_the_answer_of_the_call(
    never: Path, venue: Venue
) -> None:
    """A stop enlarged in the app cannot be shrunk to the lot, since the signer
    only lets a stop get smaller than its recorded size: the next write call
    cancels it, closes the lot and says so in its own answer."""
    run(never, ("propose_order", BTC))
    venue.positions["BTC"] = Position(Decimal("0.0005"), Decimal(81171), 1)
    for o in children(venue):
        o["s"] = "0.0003"

    ((_, out),) = run(never, ("cancel_order", {"coin": "BTC", "oid": 1}))

    assert out["warning"].startswith("WARNING: the stop could not be shrunk to the 0.0002 BTC")
    assert children(venue) == []
    assert venue.positions["BTC"].size == Decimal("0.0003")


def test_a_propose_under_halt_still_shrinks_an_oversized_stop_first(
    never: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    venue.liquidity["BTC"] = Decimal("0.0001")
    with monkeypatch.context() as died:
        died.setattr(Writer, "_protect", lambda *args: None)
        died.setattr(Writer, "reconcile_children", lambda *a, **k: None)
        run(never, ("propose_order", WITH_TP))
    assert [o["s"] for o in children(venue)] == ["0.0002", "0.0002"]
    (never.parent / "data" / "HALT").write_text("set by the user\n")
    real_start = Writer.start

    def start_without_reconcile(rt: Any) -> Writer:
        with monkeypatch.context() as skipped:
            skipped.setattr(Writer, "reconcile_children", lambda *a, **k: None)
            return real_start(rt)

    monkeypatch.setattr(Writer, "start", staticmethod(start_without_reconcile))

    ((_, out),) = run(never, ("propose_order", BTC))

    assert out["rule"] == "halted"
    assert [o["s"] for o in children(venue)] == ["0.0001", "0.0001"]


def test_the_stops_of_a_grouped_entry_are_never_left_unresolved(
    never: Path, venue: Venue, fake: FakeInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the entry of a normalTpsl ORDER waits for an outcome; its children
    have no oid in the ack and are tracked by the child records."""
    monkeypatch.setattr(Writer, "_learn_children", lambda *args: None)

    out = run(
        never,
        ("propose_order", WITH_TP),
        lambda w, o: (False, w.status({"id": o[0][1]["id"]})),
    )

    placed, status = out[0][1], out[1][1]
    assert status["oid"] == placed["oid"]
    asked = [r["oid"] for r in fake.requests if r["type"] == "orderStatus"]
    assert not {o["c"] for o in children(venue)} & set(asked)
    assert not (never.parent / "data" / "HALT").exists()


def test_an_approved_entry_shrinks_an_oversized_stop_before_it_goes_out(
    config: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approval places the entry after a fresh read; that read's rows
    reconcile the stops first, even when no write call ran in between."""
    from server.test_fills import ETH

    venue.leverage["BTC"] = ("isolated", 1)
    venue.liquidity["BTC"] = Decimal("0.0001")
    with monkeypatch.context() as died:
        died.setattr(Writer, "_protect", lambda *args: None)
        session(config, ("propose_order", WITH_TP), approve(config))
    assert [o["s"] for o in children(venue)] == ["0.0002", "0.0002"]
    real = Writer.reconcile_children
    calls: list[Any] = []

    def only_with_rows(self: Writer, headroom: int = 0, rows: Any = None) -> Any:
        calls.append(rows is not None)
        return real(self, headroom, rows) if rows is not None else None

    monkeypatch.setattr(Writer, "reconcile_children", only_with_rows)

    session(config, ("propose_order", ETH), approve(config))

    kinds = [a["type"] for a in venue.actions]
    entries = [i for i, a in enumerate(venue.actions) if a.get("grouping") == "normalTpsl"]
    assert kinds.index("batchModify") < entries[-1]
    assert [o["s"] for o in children(venue) if o["coin"] == "BTC"] == ["0.0001", "0.0001"]
    assert True in calls
