from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hl_guard.ledger.ledger import Kind, Ledger
from hl_guard.server.snapshot import fold_ledger
from server.conftest import Clock
from server.fake_info import Answer
from server.test_tools_write import BTC, records, rest, session
from server.venue import Venue, configure

RESTING_BTC = {**BTC, "limit_px": "81000"}
RESTING_ETH = {
    "coin": "ETH",
    "side": "buy",
    "size": "0.005",
    "limit_px": "2378",
    "stop_loss_px": "2350",
}
RESTING_SOL = {
    "coin": "SOL",
    "side": "buy",
    "size": "0.1",
    "limit_px": "103.5",
    "stop_loss_px": "102",
}


def never(tmp_path: Path, **policy: str) -> Path:
    return configure(tmp_path, policy={"approval": "never", **policy})


def entries(config: Path) -> list[str]:
    rs = records(config)
    return [e.coin for e in fold_ledger(rs, rs[-1].ts_ms, 3600).entries]


def test_resting_entries_count_toward_entries_per_day_when_placed(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = never(tmp_path)

    out = session(
        config,
        rest(RESTING_BTC),
        rest(RESTING_ETH),
        rest(RESTING_SOL),
        autonomous=True,
    )

    assert [r[1]["result"] for r in out] == ["placed", "placed", "rejected"]
    assert out[2][1]["rule"] == "entries_per_day_max"
    assert [o["coin"] for o in venue.orders] == ["BTC", "ETH"]
    assert entries(config) == ["BTC", "ETH"]


def test_a_second_resting_entry_on_a_coin_waits_for_its_cooldown(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock
) -> None:
    config = never(tmp_path, entries_per_day_max="5")

    out = session(
        config,
        rest(RESTING_BTC),
        lambda w, o: clock.advance(1800),
        rest(RESTING_BTC),
        lambda w, o: clock.advance(1801),
        rest(RESTING_BTC),
        autonomous=True,
    )

    assert out[2][1]["rule"] == "cooldown_s"
    assert out[4][1]["result"] == "placed"


def test_a_cancelled_unfilled_entry_still_counts(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = never(tmp_path, entries_per_day_max="5")

    out = session(
        config,
        rest(RESTING_BTC),
        lambda w, o: w.cancel({"coin": "BTC", "oid": venue.orders[0]["oid"]}),
        rest(RESTING_BTC),
        autonomous=True,
    )

    assert venue.orders == []
    assert out[2][1]["rule"] == "cooldown_s"


def test_an_entry_the_exchange_refused_does_not_count(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = never(tmp_path, entries_per_day_max="5")

    def refuse_once(body: dict[str, Any]) -> Answer | None:
        venue.answer_after = None
        return 200, json.dumps({"status": "err", "response": "Insufficient margin"}).encode(), {}

    venue.leverage["BTC"] = ("isolated", 1)
    venue.answer_after = refuse_once
    out = session(config, ("propose_order", BTC), ("propose_order", BTC), autonomous=True)

    assert out[0][1]["rule"] == "exchange"
    assert out[1][1]["result"] == "placed"


def test_risk_status_counts_a_resting_entry_and_its_cooldown(config: Path, venue: Venue) -> None:
    out = session(config, rest(RESTING_BTC), ("risk_status", {}))

    status = out[-1][1]
    assert status["entries_today"]["count"] == 1
    assert any(b.startswith("cooldown_s: BTC") for b in status["new_entries_blocked_by"])


@pytest.mark.parametrize("day_offset_ms", [0, -1])
def test_day_accounting_uses_the_exchange_fill_time(
    config: Path, venue: Venue, day_offset_ms: int
) -> None:
    ledger = Ledger(config.parent / "data" / "ledger.jsonl")
    now = records_now(ledger)
    day_start = now - now % 86_400_000
    fill = {"coin": "BTC", "size": "0.0002", "price": "80000", "fee": "0", "oid": 1, "tid": 1}
    ledger.append(Kind.FILL, {**fill, "id": "a", "side": "buy", "time_ms": day_start - 10})
    ledger.append(
        Kind.FILL,
        {
            **fill,
            "id": "a",
            "side": "sell",
            "price": "79000",
            "tid": 2,
            "time_ms": day_start + day_offset_ms,
        },
    )

    view = fold_ledger(list(ledger.records()), now, 3600)

    expected = -0.2 if day_offset_ms == 0 else 0
    assert float(view.realized_pnl_today_usd) == expected


def records_now(ledger: Ledger) -> int:
    ledger.append(Kind.ERROR, {"detail": "clock"})
    return list(ledger.records())[-1].ts_ms
