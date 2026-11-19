"""The answers a normalTpsl entry and a batchModify get, in the shapes testnet
gave on 2026-11-19 (runs T1, T7 and T8). Ids, prices and cloids are fresh."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from hl_guard.server.parse import (
    Filled,
    Refused,
    Resting,
    ShapeError,
    parse_entry_acks,
    parse_modify_acks,
    parse_open_orders,
    parse_order_status,
)

ENTRY, TP, SL = "0x" + "1" * 32, "0x" + "2" * 32, "0x" + "3" * 32


def _ok(kind: str, statuses: list[Any]) -> dict[str, Any]:
    return {"status": "ok", "response": {"type": kind, "data": {"statuses": statuses}}}


RESTING_ACK = _ok(
    "order", [{"resting": {"oid": 900001, "cloid": ENTRY}}, "waitingForFill", "waitingForFill"]
)
FILLED_ACK = _ok(
    "order",
    [
        {"filled": {"totalSz": "0.0064", "avgPx": "1700.1", "oid": 900010, "cloid": ENTRY}},
        "waitingForTrigger",
        "waitingForTrigger",
    ],
)
MODIFY_ACK = _ok(
    "order",
    [{"resting": {"oid": 900020, "cloid": TP}}, {"resting": {"oid": 900021, "cloid": SL}}],
)


def _row(**over: Any) -> dict[str, Any]:
    row = {
        "coin": "ETH",
        "side": "A",
        "limitPx": "800.1",
        "sz": "0.0105",
        "oid": 900012,
        "timestamp": 1_700_000_000_000,
        "triggerCondition": "Price below 1600.2",
        "isTrigger": True,
        "triggerPx": "1600.2",
        "children": [],
        "isPositionTpsl": False,
        "reduceOnly": True,
        "orderType": "Stop Market",
        "origSz": "0.0105",
        "tif": None,
        "cloid": SL,
    }
    return row | over


def _status(word: str, **over: Any) -> dict[str, Any]:
    return {"status": "order", "order": {"order": _row(**over), "status": word}}


def test_a_resting_entry_answers_waiting_for_fill_per_child() -> None:
    entry, slots = parse_entry_acks(RESTING_ACK, (ENTRY, TP, SL))

    assert entry == Resting(900001)
    assert slots == ("waitingForFill", "waitingForFill")


def test_a_filled_entry_answers_waiting_for_trigger_per_child() -> None:
    entry, slots = parse_entry_acks(FILLED_ACK, (ENTRY, TP, SL))

    assert entry == Filled(900010, Decimal("0.0064"), Decimal("1700.1"))
    assert slots == ("waitingForTrigger", "waitingForTrigger")


@pytest.mark.parametrize(
    "slot",
    [
        "waitingForFill",
        {"resting": {"oid": 900011}},
        {"error": "no stop"},
        "success",
        None,
    ],
    ids=["slot of a resting entry", "child with an oid", "child error", "other word", "null"],
)
def test_any_other_child_slot_after_a_fill_is_an_unknown_shape(slot: Any) -> None:
    ack = _ok("order", [FILLED_ACK["response"]["data"]["statuses"][0], slot])

    with pytest.raises(ShapeError):
        parse_entry_acks(ack, (ENTRY, SL))


def test_a_refused_entry_with_child_slots_is_an_unknown_shape() -> None:
    """No run showed what a grouped entry that does not fill answers."""
    ack = _ok("order", [{"error": "Order could not immediately match"}, "waitingForFill"])

    with pytest.raises(ShapeError):
        parse_entry_acks(ack, (ENTRY, SL))


def test_a_modify_answers_a_new_oid_under_the_same_cloid() -> None:
    assert parse_modify_acks(MODIFY_ACK, (TP, SL)) == (Resting(900020), Resting(900021))


def test_a_modify_answer_for_another_cloid_or_a_fill_is_an_unknown_shape() -> None:
    with pytest.raises(ShapeError):
        parse_modify_acks(MODIFY_ACK, (SL, TP))
    filled = _ok("order", [{"filled": {"totalSz": "1", "avgPx": "1", "oid": 7}}])
    with pytest.raises(ShapeError):
        parse_modify_acks(filled, (TP,))


def test_a_refused_modify_is_read_as_refused() -> None:
    ack = _ok("order", [{"error": "Cannot modify canceled or filled order"}])

    assert parse_modify_acks(ack, (TP,)) == (Refused("Cannot modify canceled or filled order"),)


def test_a_child_row_reads_as_a_reduce_only_trigger_with_its_cloid() -> None:
    (row,) = parse_open_orders([_row()])

    assert (row.reduce_only, row.trigger_px, row.cloid, row.order_type) == (
        True,
        Decimal("1600.2"),
        SL,
        "Stop Market",
    )


@pytest.mark.parametrize(
    ("word", "over", "remaining"),
    [
        ("open", {}, "0.0105"),
        ("canceled", {}, "0.0105"),
        ("siblingFilledCanceled", {}, "0.0105"),
        ("reduceOnlyCanceled", {}, "0.0105"),
        (
            "filled",
            {
                "sz": "0.0",
                "isTrigger": False,
                "triggerPx": "0.0",
                "triggerCondition": "Triggered",
                "orderType": "Take Profit Market",
                "tif": "Gtc",
            },
            "0",
        ),
    ],
)
def test_child_states_from_order_status(word: str, over: dict[str, Any], remaining: str) -> None:
    state = parse_order_status(_status(word, **over))

    assert state is not None
    assert state.status == word.lower()
    assert state.remaining == Decimal(remaining)
    assert state.resting is (word == "open")
