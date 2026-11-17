import decimal
import fcntl
import json
import multiprocessing
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ledger_fixtures import TS0, _fill, _forge, _lines, _rehash, _rewrite

from hl_guard.ledger.cli import latch_clear, ledger_repair, ledger_show, ledger_verify
from hl_guard.ledger.ledger import (
    GENESIS,
    Fill,
    Kind,
    Ledger,
    LedgerAccessError,
    LedgerError,
    LedgerTamperError,
    Record,
    Tail,
    TornTail,
    _canonical,
    _digest,
    active_latch,
    clock_warning,
    day_start_equity,
)

RELATCH = (
    "note: the day start stays, so the next entry latches again while today's loss is at or "
    "past the daily loss limit, until the UTC day ends"
)
OUT_OF_RANGE = "an integer is outside the range -2**63 to 2**64-1"
TAIL_ONLY = "repair checks only the tail; run hl-guard ledger verify to check every line"


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.jsonl"


def test_append_and_replay_roundtrip_preserves_order(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.PROPOSAL, {"coin": "BTC", "side": "buy", "size": "0.0002"})
    led.append(Kind.APPROVAL, {"proposal_seq": 1})
    led.append(Kind.ORDER, {"cloid": "0xabc", "action_type": "order"})
    records = Ledger(path).replay()
    assert [r.seq for r in records] == [1, 2, 3]
    assert [r.kind for r in records] == [Kind.PROPOSAL, Kind.APPROVAL, Kind.ORDER]
    assert records[2].payload["cloid"] == "0xabc"


def test_each_record_chains_to_the_one_before(path: Path) -> None:
    led = Ledger(path)
    first = led.append(Kind.HALT, {"reason": "a"}, ts_ms=TS0)
    second = led.append(Kind.UNHALT, {}, ts_ms=TS0 + 1)
    assert first.prev == GENESIS
    assert second.prev == first.hash
    assert [r.hash for r in led.replay()] == [first.hash, second.hash]


def test_reducer_computes_position_average_price_and_realized_pnl(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("BTC", "buy", "0.001", "80000", "0.0000030"))
    led.append(Kind.FILL, _fill("BTC", "buy", "0.001", "80200", "0.0000030"))
    led.append(Kind.FILL, _fill("BTC", "sell", "0.001", "81000", "0.0000045"))
    led.append(Kind.STATE_CHECKPOINT, led.reduce())
    state = led.reduce()
    assert Decimal(state["positions"]["BTC"]["size"]) == Decimal("0.001")
    assert Decimal(state["positions"]["BTC"]["avg_price"]) == Decimal(80100)
    assert Decimal(state["fees_paid"]) == Decimal("0.0000105")
    assert Decimal(state["realized_pnl"]) == Decimal("0.9") - Decimal("0.0000105")
    assert len(led.replay(verify=True)) == 4


def test_short_position_realizes_on_buy_back(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("ETH", "sell", "0.2", "2500", "0.0002250"))
    led.append(Kind.FILL, _fill("ETH", "buy", "0.2", "2450", "0.0002205"))
    state = led.reduce()
    assert state["positions"] == {}
    assert Decimal(state["realized_pnl"]) == Decimal(10) - Decimal("0.0004455")


def test_a_flip_opens_the_rest_at_the_fill_price(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("SOL", "buy", "1", "100", "0"))
    led.append(Kind.FILL, _fill("SOL", "sell", "3", "110", "0"))
    state = led.reduce()
    assert state["positions"]["SOL"] == {"size": "-2", "avg_price": "110"}
    assert Decimal(state["realized_pnl"]) == Decimal(10)


def test_funding_is_signed_and_stays_out_of_realized_pnl(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.FUNDING, {"coin": "BTC", "amount": "0.0125"})
    led.append(Kind.FUNDING, {"coin": "BTC", "amount": "-0.0025"})
    state = led.reduce()
    assert Decimal(state["funding_paid"]) == Decimal("0.0100")
    assert Decimal(state["realized_pnl"]) == 0


def test_verify_replay_detects_checkpoint_mismatch(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("BTC", "buy", "0.001", "80000", "0"))
    _forge(path, Kind.STATE_CHECKPOINT, {**led.reduce(), "realized_pnl": "1000"})
    with pytest.raises(LedgerTamperError, match="checkpoint does not match replay"):
        led.replay(verify=True)
    assert len(led.replay(verify=False)) == 2


def test_verify_replay_passes_on_honest_ledger(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("BTC", "buy", "0.001", "80000", "0.000003"))
    led.append(Kind.STATE_CHECKPOINT, led.reduce())
    led.append(Kind.FILL, _fill("BTC", "sell", "0.001", "80100", "0.000003"))
    led.append(Kind.STATE_CHECKPOINT, led.reduce())
    assert len(led.replay(verify=True)) == 4


def test_a_line_with_an_unknown_kind_fails_replay(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.HALT, {})
    lines = _lines(path)
    lines[0]["kind"] = "withdraw"
    _rewrite(path, lines)
    with pytest.raises(LedgerTamperError, match="line 1: unknown kind"):
        led.replay()
    with pytest.raises(ValueError):
        Kind("withdraw")


def test_replay_of_a_missing_or_empty_ledger_is_empty(path: Path) -> None:
    assert Ledger(path).replay() == []
    assert not path.exists()
    path.write_bytes(b"")
    path.chmod(0o600)
    assert Ledger(path).replay() == []


def _three(path: Path) -> Ledger:
    led = Ledger(path)
    for n in range(3):
        led.append(Kind.DECISION, {"n": n, "rule": "coins"}, ts_ms=TS0 + n)
    return led


def test_an_edited_payload_fails_on_that_line(path: Path) -> None:
    led = _three(path)
    lines = _lines(path)
    lines[1]["payload"]["rule"] = "none"
    _rewrite(path, lines)
    with pytest.raises(LedgerTamperError, match="line 2: hash does not match") as exc:
        led.replay()
    assert exc.value.line == 2


def test_an_edit_with_a_recomputed_hash_breaks_the_next_line(path: Path) -> None:
    led = _three(path)
    lines = _lines(path)
    lines[1]["payload"]["rule"] = "none"
    lines[1]["hash"] = _digest(2, TS0 + 1, "decision", lines[1]["payload"], lines[1]["prev"])
    _rewrite(path, lines)
    with pytest.raises(LedgerTamperError, match="line 3: prev does not match"):
        led.replay()


@pytest.mark.parametrize("edit", ["drop", "swap", "blank"])
def test_dropped_swapped_or_blank_lines_fail(path: Path, edit: str) -> None:
    led = _three(path)
    raw = path.read_text().splitlines(keepends=True)
    if edit == "drop":
        del raw[1]
    elif edit == "swap":
        raw[0], raw[1] = raw[1], raw[0]
    else:
        raw.insert(1, "\n")
    path.write_text("".join(raw))
    with pytest.raises(LedgerTamperError):
        led.replay()


def test_a_torn_last_line_is_skipped_by_reads_and_dropped_by_repair(path: Path) -> None:
    led = _three(path)
    whole = path.read_bytes()
    path.write_bytes(whole + b'{"seq":4,"ts_ms":9,"ki')
    assert len(led.replay()) == 3
    assert led.repair() == TornTail(len(b'{"seq":4,"ts_ms":9,"ki'), is_record=False)
    assert path.read_bytes() == whole
    assert led.repair() is None


def test_append_repairs_a_torn_tail_and_continues_the_chain(path: Path) -> None:
    led = _three(path)
    with path.open("ab") as f:
        f.write(b'{"seq":4,"ts')
    record = led.append(Kind.HALT, {})
    assert record.seq == 4
    assert len(led.replay()) == 4


def test_a_corrupt_complete_last_line_stops_appends(path: Path) -> None:
    led = _three(path)
    with path.open("ab") as f:
        f.write(b"not a record\n")
    with pytest.raises(LedgerTamperError, match="last line is not a ledger record"):
        led.append(Kind.HALT, {})
    with pytest.raises(LedgerTamperError, match="line 4"):
        led.replay()


def test_the_ledger_file_is_created_owner_only(path: Path) -> None:
    Ledger(path).append(Kind.HALT, {})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_a_payload_that_is_not_json_is_refused(path: Path) -> None:
    led = Ledger(path)
    with pytest.raises(ValueError):
        led.append(Kind.FILL, {"size": float("nan")})
    with pytest.raises(TypeError):
        led.append(Kind.FILL, {"size": Decimal(1)})
    assert led.replay() == []


def _append_many(path: str, n: int) -> None:
    led = Ledger(Path(path))
    for i in range(n):
        led.append(Kind.DECISION, {"pid": os.getpid(), "i": i})


def test_concurrent_writers_keep_one_chain(path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_append_many, args=(str(path), 50)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    records = Ledger(path).replay()
    assert [r.seq for r in records] == list(range(1, 201))


def test_cli_verify_reports_ok_torn_and_tamper(path: Path) -> None:
    led = _three(path)
    code, out = ledger_verify(path)
    assert code == 0
    head = led.replay()[-1].hash
    assert out == [f"verify ok: 3 records, head {head}"]
    with path.open("ab") as f:
        f.write(b'{"seq"')
    torn = path.read_bytes()
    code, out = ledger_verify(path)
    assert (code, out) == (
        1,
        [
            "verify FAILED: 6 torn bytes follow the last record; run hl-guard ledger repair",
            f"committed: 3 records, head {head}",
        ],
    )
    assert path.read_bytes() == torn
    assert ledger_repair(path) == (
        0,
        ["repaired: dropped 6 torn bytes after the last record", TAIL_ONLY],
    )
    assert ledger_repair(path) == (0, ["repair: nothing to do", TAIL_ONLY])
    lines = _lines(path)
    lines[0]["ts_ms"] = 99
    _rewrite(path, lines)
    code, out = ledger_verify(path)
    assert (code, out) == (1, ["verify FAILED: line 1: hash does not match the line's contents"])
    assert ledger_verify(path.with_name("absent"))[0] == 2


def test_cli_show_prints_the_last_records_without_repairing(path: Path) -> None:
    _three(path)
    with path.open("ab") as f:
        f.write(b"{")
    code, out = ledger_show(path, 2)
    assert code == 0
    assert [json.loads(line)["payload"]["n"] for line in out] == [1, 2]
    assert path.read_bytes().endswith(b"{")


def test_the_hash_covers_prev_so_the_head_commits_to_the_history(path: Path) -> None:
    led = _three(path)
    lines = _lines(path)
    lines[0]["payload"]["rule"] = "none"
    lines[0]["hash"] = _digest(1, TS0, "decision", lines[0]["payload"], GENESIS)
    lines[1]["prev"] = lines[0]["hash"]
    _rewrite(path, lines)
    with pytest.raises(LedgerTamperError, match="line 2: hash does not match"):
        led.replay()


def test_a_large_payload_is_written_whole(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.ERROR, {"text": "x" * 200_000})
    led.append(Kind.HALT, {})
    assert [r.seq for r in led.replay()] == [1, 2]


def test_an_edited_seq_fails_on_that_line(path: Path) -> None:
    led = _three(path)
    lines = _lines(path)
    lines[1] = _rehash({**lines[1], "seq": 77})
    lines[2]["prev"] = lines[1]["hash"]
    lines[2] = _rehash(lines[2])
    _rewrite(path, lines)
    with pytest.raises(LedgerTamperError, match="line 2: seq 77 is not the line number 2"):
        led.replay()


@pytest.mark.parametrize("field", ["seq", "ts_ms"])
@pytest.mark.parametrize("value", [True, "1", 1.0])
def test_seq_and_ts_ms_must_be_real_integers(path: Path, field: str, value: Any) -> None:
    led = Ledger(path)
    led.append(Kind.HALT, {}, ts_ms=TS0)
    lines = _lines(path)
    lines[0] = _rehash({**lines[0], field: value})
    _rewrite(path, lines)
    with pytest.raises(LedgerTamperError, match="line 1: seq and ts_ms must be integers"):
        led.replay()
    with pytest.raises(LedgerError):
        led.append(Kind.HALT, {})
    assert _lines(path) == lines


@pytest.mark.parametrize("rehash", [False, True], ids=["hash left", "hash recomputed"])
def test_an_edited_last_seq_stops_appends_without_writing(path: Path, rehash: bool) -> None:
    led = _three(path)
    lines = _lines(path)
    lines[2]["seq"] = 999
    if rehash:
        lines[2] = _rehash(lines[2])
    _rewrite(path, lines)
    before = path.read_bytes()
    with pytest.raises(LedgerTamperError, match="last line"):
        led.append(Kind.HALT, {})
    assert path.read_bytes() == before


def test_a_last_line_whose_prev_skips_its_neighbour_stops_appends(path: Path) -> None:
    led = _three(path)
    lines = _lines(path)
    lines[2] = _rehash({**lines[2], "prev": lines[0]["hash"]})
    _rewrite(path, lines)
    with pytest.raises(LedgerTamperError, match="does not follow the line before it"):
        led.append(Kind.HALT, {})


def test_verify_is_read_only_and_repair_completes_a_whole_record(path: Path) -> None:
    led = _three(path)
    led.append(Kind.HALT, {}, ts_ms=TS0 + 7)
    whole = path.read_bytes()
    path.write_bytes(whole[:-1])
    path.chmod(0o600)
    code, out = ledger_verify(path)
    assert code == 1
    assert out[0].startswith("verify FAILED: the last record (")
    assert out[1].startswith("committed: 3 records")
    assert path.read_bytes() == whole[:-1]
    assert ledger_repair(path)[1][0].startswith("repaired: the last record")
    assert path.read_bytes() == whole
    assert ledger_verify(path)[0] == 0


def test_append_completes_a_whole_last_record_instead_of_dropping_it(path: Path) -> None:
    led = _three(path)
    path.write_bytes(path.read_bytes()[:-1])
    record = led.append(Kind.HALT, {})
    assert record.seq == 4
    assert [r.seq for r in led.replay()] == [1, 2, 3, 4]


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o606])
def test_a_ledger_with_group_or_other_bits_is_refused(path: Path, mode: int) -> None:
    led = _three(path)
    path.chmod(mode)
    before = path.read_bytes()
    with pytest.raises(LedgerAccessError, match=f"has mode {mode:04o}"):
        led.append(Kind.HALT, {})
    with pytest.raises(LedgerAccessError):
        led.replay()
    assert ledger_verify(path)[0] == 2
    assert ledger_show(path, 1)[0] == 2
    assert path.read_bytes() == before


def test_a_ledger_owned_by_another_user_is_refused(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    led = _three(path)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(LedgerAccessError, match="belongs to uid"):
        led.replay()


def test_verify_reads_a_read_only_ledger_and_reports_no_access_with_exit_2(path: Path) -> None:
    _three(path)
    path.chmod(0o400)
    assert ledger_verify(path)[0] == 0
    path.chmod(0o000)
    code, out = ledger_verify(path)
    assert code == 2
    assert out[0].startswith("verify FAILED: cannot open ledger")
    path.chmod(0o600)


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        (b"[" * 100_000 + b"]" * 100_000, "the payload nests deeper than 32 levels"),
        (b'{"seq": ' + b"9" * 5000 + b"}", re.escape(OUT_OF_RANGE)),
        (b"\xff\xfe", "not valid JSON"),
    ],
    ids=["deep nesting", "huge int", "not utf-8"],
)
def test_an_unparseable_line_is_a_typed_error(path: Path, line: bytes, reason: str) -> None:
    led = _three(path)
    with path.open("ab") as f:
        f.write(line + b"\n")
    with pytest.raises(LedgerTamperError, match=f"line 4: {reason}"):
        led.replay()
    with pytest.raises(LedgerTamperError, match="last line is not a ledger record"):
        led.append(Kind.HALT, {})
    assert ledger_verify(path)[0] == 1


def test_the_latch_folds_from_latch_and_unlatch_records(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.DECISION, {})
    assert active_latch(led.replay()) is None
    led.append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    led.append(Kind.DECISION, {})
    assert active_latch(led.replay()) is not None
    Tail(led).unlatch()
    assert active_latch(led.replay()) is None
    led.append(Kind.LATCH, {"rule": "loss_streak_max", "detail": "y"})
    assert active_latch(led.replay()) is not None


def test_latch_clear_writes_one_unlatch_only_when_latched(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    assert latch_clear(path) == (
        0,
        ["unlatched: wrote record 2, which clears record 1 (daily_loss_pct_max: x)", RELATCH],
    )
    assert [r.kind for r in led.replay()] == [Kind.LATCH, Kind.UNLATCH]
    assert latch_clear(path) == (0, ["not latched: no unlatch record written"])
    assert len(led.replay()) == 2


def test_latch_clear_refuses_a_tampered_ledger(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    led.append(Kind.DECISION, {"n": 1})
    lines = _lines(path)
    lines[1]["payload"]["n"] = 2
    _rewrite(path, lines)
    before = path.read_bytes()
    code, out = latch_clear(path)
    assert code == 1
    assert out[0].startswith("latch clear FAILED: ")
    assert out[0].endswith("line 2: hash does not match the line's contents")
    assert path.read_bytes() == before
    assert latch_clear(path.with_name("absent"))[0] == 2


def test_latch_clear_refuses_a_forged_checkpoint(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    _forge(path, Kind.STATE_CHECKPOINT, {**led.reduce(), "realized_pnl": "1000"})
    code, out = latch_clear(path)
    assert code == 1
    assert "checkpoint does not match replay" in out[0]
    assert len(led.replay(verify=False)) == 2


def test_append_refuses_an_unlatch_record(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    before = path.read_bytes()
    with pytest.raises(ValueError, match=r"only by Tail\.unlatch"):
        led.append(Kind.UNLATCH, {})
    assert path.read_bytes() == before


def test_latch_clear_reads_shared_then_checks_and_writes_under_one_exclusive_lock(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import fcntl

    Ledger(path).append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    taken: list[int] = []
    real = fcntl.flock

    def spy(fd: int, op: int) -> None:
        taken.append(op)
        real(fd, op)

    monkeypatch.setattr(fcntl, "flock", spy)
    assert latch_clear(path)[0] == 0
    assert taken == [fcntl.LOCK_SH, fcntl.LOCK_EX]


def test_an_unlatch_clears_a_latch_recorded_after_its_tail_last_read(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "first"})
    tail = Tail(led)
    tail.records()
    led.append(Kind.LATCH, {"rule": "loss_streak_max", "detail": "second"})

    cleared, _ = tail.unlatch()

    assert cleared is not None and cleared.latch.payload["detail"] == "second"
    assert active_latch(led.replay()) is None


def test_a_day_start_through_a_tail_refuses_a_second_one_for_the_day(path: Path) -> None:
    led = Ledger(path)
    day = time.time_ns() // 1_000_000 // 86_400_000
    led.append(Kind.DAY_START, {"day": day, "equity_usd": "100"})
    tail = Tail(led)
    tail.records()
    led.append(Kind.HALT, {})

    with pytest.raises(ValueError, match=f"day {day} already has a day_start"):
        tail.append(Kind.DAY_START, {"day": day, "equity_usd": "90"})
    assert day_start_equity(led.replay(), day) == Decimal(100)


def test_latch_clear_names_the_latest_latch_it_clears(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    led.append(Kind.LATCH, {"rule": "loss_streak_max", "detail": "5 losses"})
    assert latch_clear(path) == (
        0,
        ["unlatched: wrote record 3, which clears record 2 (loss_streak_max: 5 losses)"],
    )


def test_latch_clear_reports_the_torn_tail_it_drops(path: Path) -> None:
    Ledger(path).append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    with path.open("ab") as f:
        f.write(b'{"se')
    code, out = latch_clear(path)
    assert code == 0
    assert out[0] == "repaired first: dropped 4 torn bytes after the last record"
    assert out[1].startswith("unlatched: wrote record 2")


def test_latch_clear_completes_a_latch_that_lacks_its_newline(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.DECISION, {})
    led.append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    path.write_bytes(path.read_bytes()[:-1])
    code, out = latch_clear(path)
    assert code == 0
    assert out[0].startswith("repaired first: the last record (")
    assert out[1].startswith("unlatched: wrote record 3, which clears record 2")


def _rechain(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prev = GENESIS
    out = []
    for line in lines:
        line = _rehash({**line, "prev": prev})
        prev = line["hash"]
        out.append(line)
    return out


@pytest.mark.parametrize(
    ("ts", "match"),
    [
        (TS0 - 1, "before the previous record"),
        (0, "before 2020-01-01"),
        (-1, "before 2020-01-01"),
        (1_577_836_799_999, "before 2020-01-01"),
    ],
    ids=["back in time", "zero", "negative", "before 2020"],
)
def test_a_ts_ms_out_of_order_or_range_fails_verify(path: Path, ts: int, match: str) -> None:
    _three(path)
    lines = _lines(path)
    lines[1]["ts_ms"] = ts
    _rewrite(path, _rechain(lines))
    with pytest.raises(LedgerTamperError, match=f"line 2: ts_ms {ts} .*{match}"):
        Ledger(path).replay()
    assert ledger_verify(path)[0] == 1


def test_a_ts_ms_up_to_a_day_ahead_verifies(path: Path) -> None:
    ahead = time.time_ns() // 1_000_000 + 23 * 3_600_000
    Ledger(path).append(Kind.HALT, {}, ts_ms=ahead)
    assert ledger_verify(path)[0] == 0
    assert Ledger(path).append(Kind.HALT, {}).ts_ms == ahead
    with pytest.raises(ValueError, match="more than one day ahead of the clock"):
        Ledger(path).append(Kind.HALT, {}, ts_ms=ahead + 2 * 3_600_000)


def test_a_tampered_last_ts_ms_stops_appends(path: Path) -> None:
    _three(path)
    lines = _lines(path)
    lines[2]["ts_ms"] = TS0
    _rewrite(path, _rechain(lines))
    with pytest.raises(LedgerTamperError, match="ts_ms"):
        Ledger(path).append(Kind.HALT, {})


@pytest.mark.parametrize("ts", [TS0 + 1, 0, 10**30])
def test_append_refuses_a_ts_ms_out_of_order_or_range(path: Path, ts: int) -> None:
    _three(path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="ts_ms"):
        Ledger(path).append(Kind.HALT, {}, ts_ms=ts)
    assert path.read_bytes() == before


def test_a_raw_carriage_return_is_tamper_for_reads_and_appends(path: Path) -> None:
    _three(path)
    raw = path.read_bytes().split(b"\n")
    raw[2] = raw[2].replace(b'"kind"', b'\r"kind"', 1)
    path.write_bytes(b"\n".join(raw))
    with pytest.raises(LedgerTamperError, match=r"line 3: .*carriage return"):
        Ledger(path).replay()
    before = path.read_bytes()
    with pytest.raises(LedgerTamperError, match="carriage return"):
        Ledger(path).append(Kind.HALT, {})
    assert path.read_bytes() == before


def test_repair_says_it_checks_only_the_tail(path: Path) -> None:
    _three(path)
    lines = _lines(path)
    lines[0]["payload"]["rule"] = "none"
    _rewrite(path, lines)
    code, out = ledger_repair(path)
    assert code == 0
    assert out[-1] == TAIL_ONLY
    assert ledger_verify(path)[0] == 1


def test_show_of_a_missing_ledger_exits_2(path: Path) -> None:
    assert ledger_show(path, 5) == (2, [f"ledger {path} does not exist"])


def test_a_record_with_an_extra_field_is_tamper(path: Path) -> None:
    _three(path)
    lines = _lines(path)
    lines[1]["note"] = "added"
    _rewrite(path, lines)
    with pytest.raises(LedgerTamperError, match="line 2: not a ledger record"):
        Ledger(path).replay()


def test_a_torn_tail_must_be_the_next_record_not_just_hash_valid(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.HALT, {}, ts_ms=TS0)
    led.append(Kind.HALT, {}, ts_ms=TS0 + 1)
    first = path.read_bytes().split(b"\n")[0]
    with path.open("ab") as f:
        f.write(first)
    assert led.repair() == TornTail(len(first), is_record=False)
    assert [r.seq for r in led.replay()] == [1, 2]


HOUR_MS = 3_600_000
LATCH = {"rule": "daily_loss_pct_max", "detail": "x"}
BAD_PAYLOADS: list[tuple[Kind, dict[str, Any]]] = [
    (Kind.FILL, {"coin": "BTC", "side": "buy", "size": 1, "price": "10", "fee": "0"}),
    (Kind.FILL, _fill("BTC", "buy", "x", "10", "0")),
    (Kind.FILL, _fill("BTC", "buy", "0", "10", "0")),
    (Kind.FILL, _fill("BTC", "short", "1", "10", "0")),
    (Kind.FILL, _fill("BTC", "buy", "1", "NaN", "0")),
    (Kind.FILL, _fill("BTC", "buy", "1e999999", "1e999999", "0")),
    (Kind.FILL, _fill("BTC", "buy", "1", "10", "1e-999999")),
    (Kind.FUNDING, {"coin": "BTC"}),
    (Kind.FUNDING, {"coin": "BTC", "amount": 0.5}),
    (Kind.LATCH, {}),
    (Kind.LATCH, {**LATCH, "extra": 1}),
    (Kind.LATCH, {"rule": 1, "detail": "x"}),
    (Kind.DAY_START, {}),
    (Kind.DAY_START, {"day": "20000", "equity_usd": "100"}),
    (Kind.DAY_START, {"day": 20000, "equity_usd": 100}),
    (Kind.DAY_START, {"day": 20000, "equity_usd": "0"}),
    (Kind.DAY_START, {"day": 20000, "equity_usd": "-5"}),
    (Kind.DAY_START, {"day": 20000, "equity_usd": "100", "extra": 1}),
    (Kind.STATE_CHECKPOINT, {}),
    (
        Kind.STATE_CHECKPOINT,
        {"positions": {}, "realized_pnl": "1000", "fees_paid": "0", "funding_paid": "0"},
    ),
]


@pytest.mark.parametrize(("kind", "payload"), BAD_PAYLOADS)
def test_append_refuses_a_payload_that_replay_would_reject(
    path: Path, kind: Kind, payload: dict[str, Any]
) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("BTC", "buy", "1", "10", "0"))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        led.append(kind, payload)
    assert path.read_bytes() == before
    assert ledger_verify(path)[0] == 0


@pytest.mark.parametrize(("kind", "payload"), BAD_PAYLOADS)
def test_replay_rejects_the_payloads_append_refuses(
    path: Path, kind: Kind, payload: dict[str, Any]
) -> None:
    Ledger(path).append(Kind.FILL, _fill("BTC", "buy", "1", "10", "0"))
    _forge(path, kind, payload)
    code, out = ledger_verify(path)
    assert code == 1
    assert out[0].startswith("verify FAILED: line 2: ")


def _float_now() -> Any:
    return float(time.time_ns() // 10**6)


@pytest.mark.parametrize("kind", [Kind.HALT, Kind.FILL])
def test_append_refuses_a_float_ts_ms_before_writing(path: Path, kind: Kind) -> None:
    led = Ledger(path)
    led.append(Kind.HALT, {})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="integers"):
        led.append(kind, _fill("BTC", "buy", "1", "10", "0"), ts_ms=_float_now())
    assert path.read_bytes() == before
    assert led.append(Kind.LATCH, LATCH).seq == 2


def test_a_latch_takes_its_ts_ms_from_the_clock(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.HALT, {})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="clock"):
        led.append(Kind.LATCH, LATCH, ts_ms=_float_now())
    assert path.read_bytes() == before


def test_a_checkpoint_that_matches_the_replay_is_accepted(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("BTC", "buy", "1", "10", "0.01"))
    led.append(Kind.FUNDING, {"coin": "BTC", "amount": "0.5"})
    led.append(Kind.STATE_CHECKPOINT, led.reduce())
    assert ledger_verify(path)[0] == 0


def test_a_latch_records_when_the_clock_steps_back_25_hours(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hl_guard.ledger import ledger as lg

    led = Ledger(path)
    head = led.append(Kind.PROPOSAL, {})
    real = lg._now_ms
    monkeypatch.setattr(lg, "_now_ms", lambda: real() - 25 * HOUR_MS)
    latch = led.append(Kind.LATCH, LATCH)
    assert latch.ts_ms == head.ts_ms
    code, out = ledger_verify(path)
    assert code == 0
    assert out[0].startswith("verify ok: 2 records")
    assert out[1].startswith("clock warning: the newest record is 25.0 h ahead of this clock")
    assert latch_clear(path)[1][0].startswith("unlatched: wrote record 3, which clears record 2")
    assert active_latch(led.replay()) is None
    assert ledger_verify(path)[0] == 0


def test_a_latch_records_when_the_head_is_far_ahead_of_the_clock(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hl_guard.ledger import ledger as lg

    led = Ledger(path)
    ahead = led.append(Kind.PROPOSAL, {}, ts_ms=lg._now_ms() + 23 * HOUR_MS)
    real = lg._now_ms
    monkeypatch.setattr(lg, "_now_ms", lambda: real() - 2 * HOUR_MS)
    assert led.append(Kind.LATCH, LATCH).ts_ms == ahead.ts_ms
    assert active_latch(led.replay()) is not None


def test_reads_bound_ts_ms_only_by_2020_and_order(path: Path) -> None:
    _three(path)
    lines = _lines(path)
    lines[2]["ts_ms"] = 10**15
    _rewrite(path, _rechain(lines))
    code, out = ledger_verify(path)
    assert code == 0
    assert out[1].startswith("clock warning: the newest record is ")
    assert Ledger(path).append(Kind.HALT, {}).ts_ms == 10**15


def test_an_unlatch_names_the_latch_it_clears(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, LATCH)
    led.append(Kind.LATCH, {"rule": "loss_streak_max", "detail": "5 losses"})
    latch_clear(path)
    assert led.replay()[-1].payload == {"clears_seq": 2}


@pytest.mark.parametrize(
    "payload",
    [{}, {"clears_seq": 1}, {"clears_seq": "2"}, {"clears_seq": 2, "by": "x"}],
    ids=["empty", "an older latch", "a string seq", "an extra key"],
)
def test_an_unlatch_that_does_not_name_the_active_latch_is_tamper(
    path: Path, payload: dict[str, Any]
) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, LATCH)
    led.append(Kind.LATCH, LATCH)
    _forge(path, Kind.UNLATCH, payload)
    code, out = ledger_verify(path)
    assert code == 1
    assert out[0].startswith("verify FAILED: line 3: unlatch")


def test_an_unlatch_without_an_active_latch_is_tamper(path: Path) -> None:
    Ledger(path).append(Kind.HALT, {})
    _forge(path, Kind.UNLATCH, {"clears_seq": 1})
    assert ledger_verify(path)[0] == 1


def _latch_when_told(path: str, ready: Any, go: Any) -> None:
    ready.set()
    go.wait(30)
    Ledger(Path(path)).append(Kind.LATCH, {"rule": "loss_streak_max", "detail": "new trip"})


def test_a_latch_written_during_latch_clear_stays_active(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hl_guard.ledger import ledger as lg

    Ledger(path).append(Kind.LATCH, LATCH)
    ctx = multiprocessing.get_context("spawn")
    ready, go = ctx.Event(), ctx.Event()
    child = ctx.Process(target=_latch_when_told, args=(str(path), ready, go))
    child.start()
    assert ready.wait(30)
    real = lg._settle_tail

    def slow_settle(fd: int, path: Path) -> Any:
        go.set()
        time.sleep(1.0)
        return real(fd, path)

    monkeypatch.setattr(lg, "_settle_tail", slow_settle)
    assert latch_clear(path)[0] == 0
    monkeypatch.undo()
    child.join(30)
    assert child.exitcode == 0
    records = Ledger(path).replay()
    assert [r.kind for r in records] == [Kind.LATCH, Kind.UNLATCH, Kind.LATCH]
    latch = active_latch(records)
    assert latch is not None
    assert latch.seq == 3


@pytest.mark.parametrize("mode", [0o711, 0o701, 0o710])
def test_a_ledger_with_group_or_other_execute_bits_is_refused(path: Path, mode: int) -> None:
    Ledger(path).append(Kind.HALT, {})
    path.chmod(mode)
    with pytest.raises(LedgerAccessError, match=f"mode {mode:04o}"):
        Ledger(path).append(Kind.HALT, {})


def test_a_directory_is_named_as_one(tmp_path: Path) -> None:
    for command in (ledger_verify, ledger_repair, latch_clear):
        assert command(tmp_path) == (2, [f"ledger {tmp_path} is a directory"])
    assert ledger_show(tmp_path, 5) == (2, [f"ledger {tmp_path} is a directory"])


DEPTH_MAX = 32
LINE_MAX = 262_144
DAY_MS = 24 * HOUR_MS


def _nest(levels: int) -> Any:
    value: Any = []
    for _ in range(levels - 1):
        value = [value]
    return value


def _from_depth(frames: int, call: Callable[[], Any]) -> Any:
    return _from_depth(frames - 1, call) if frames else call()


def test_a_payload_nested_to_the_cap_reads_back_from_a_deep_stack(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, LATCH)
    led.append(Kind.PROPOSAL, {"args": _nest(DEPTH_MAX - 1), "note": '"' + "[{" * 40})
    assert _from_depth(300, lambda: ledger_verify(path))[0] == 0
    assert _from_depth(300, lambda: latch_clear(path))[0] == 0
    assert _from_depth(300, lambda: [r.seq for r in led.replay()]) == [1, 2, 3]


@pytest.mark.parametrize("levels", [DEPTH_MAX, 985, 100_000])
def test_append_refuses_a_payload_nested_past_the_cap(path: Path, levels: int) -> None:
    led = Ledger(path)
    led.append(Kind.LATCH, LATCH)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="the payload nests deeper than 32 levels"):
        led.append(Kind.PROPOSAL, {"args": _nest(levels)})
    assert path.read_bytes() == before
    assert ledger_verify(path)[0] == 0
    assert latch_clear(path)[1][0].startswith("unlatched: wrote record 2")


def test_a_line_nested_past_the_cap_is_tamper_at_any_stack_depth(path: Path) -> None:
    Ledger(path).append(Kind.HALT, {})
    _forge(path, Kind.PROPOSAL, {"args": _nest(DEPTH_MAX)})
    expected = (1, ["verify FAILED: line 2: the payload nests deeper than 32 levels"])
    assert ledger_verify(path) == expected
    assert _from_depth(300, lambda: ledger_verify(path)) == expected


def test_a_line_at_the_size_cap_is_written_and_one_byte_more_is_refused(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.ERROR, {"text": ""}, ts_ms=TS0)
    room = LINE_MAX - (len(path.read_bytes()) - 1)
    path.unlink()
    led.append(Kind.ERROR, {"text": "x" * room}, ts_ms=TS0)
    assert len(path.read_bytes()) == LINE_MAX + 1
    before = path.read_bytes()
    with pytest.raises(ValueError, match="the line is longer than 262144 bytes"):
        led.append(Kind.ERROR, {"text": "x" * (room + 1)}, ts_ms=TS0)
    assert path.read_bytes() == before
    assert ledger_verify(path)[0] == 0


def test_a_line_past_the_size_cap_is_tamper(path: Path) -> None:
    Ledger(path).append(Kind.HALT, {})
    _forge(path, Kind.ERROR, {"text": "x" * LINE_MAX})
    assert ledger_verify(path) == (
        1,
        ["verify FAILED: line 2: the line is longer than 262144 bytes"],
    )


def test_an_over_long_latch_is_recorded_with_its_text_cut_to_a_fixed_length(path: Path) -> None:
    led = Ledger(path)
    latch = led.append(Kind.LATCH, {"rule": "r" * 5000, "detail": "\U0001f600" * 1_000_000})
    for text in latch.payload.values():
        assert len(text) == 1000
        assert text.endswith(" [truncated]")
    assert active_latch(led.replay()) == latch
    exact = {"rule": "daily_loss_pct_max", "detail": "d" * 1000}
    assert led.append(Kind.LATCH, exact).payload == exact
    assert ledger_verify(path)[0] == 0


@pytest.mark.parametrize(
    "payload",
    [{"oid": 2**64 - 1}, {"oid": -(2**63)}, {"statuses": [{"resting": {"oid": 2**64 - 1}}]}],
    ids=["u64 max", "i64 min", "order ack"],
)
def test_an_int_in_the_u64_or_i64_range_is_written_and_read(
    path: Path, payload: dict[str, Any]
) -> None:
    Ledger(path).append(Kind.ACK, payload)
    assert Ledger(path).replay()[0].payload == payload
    assert ledger_verify(path)[0] == 0


@pytest.mark.parametrize(
    "value", [2**64, -(2**63) - 1, 10**5000], ids=["2**64", "-2**63-1", "5001 digits"]
)
def test_append_refuses_an_int_outside_the_range_whatever_the_int_limit(
    path: Path, value: int
) -> None:
    Ledger(path).append(Kind.HALT, {})
    before = path.read_bytes()
    limit = sys.get_int_max_str_digits()
    for setting in (limit, 0):
        sys.set_int_max_str_digits(setting)
        try:
            with pytest.raises(ValueError, match=re.escape(OUT_OF_RANGE)):
                Ledger(path).append(Kind.ACK, {"oid": value})
        finally:
            sys.set_int_max_str_digits(limit)
    assert path.read_bytes() == before


@pytest.mark.parametrize("value", [2**64, -(2**63) - 1], ids=["2**64", "-2**63-1"])
def test_an_int_outside_the_range_is_tamper_whatever_the_int_limit(path: Path, value: int) -> None:
    Ledger(path).append(Kind.HALT, {})
    _forge(path, Kind.ACK, {"oid": value})
    expected = (1, [f"verify FAILED: line 2: {OUT_OF_RANGE}"])
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)
    try:
        assert ledger_verify(path) == expected
    finally:
        sys.set_int_max_str_digits(limit)
    assert ledger_verify(path) == expected


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b'"payload":{"a":2}', b'"payload":{"a":1,"a":2}'),
        (b'"kind":"ack"', b'"kind":"ack","kind":"ack"'),
    ],
    ids=["in the payload", "in the record"],
)
def test_a_repeated_key_is_tamper(path: Path, old: bytes, new: bytes) -> None:
    Ledger(path).append(Kind.ACK, {"a": 2})
    line = path.read_bytes()
    assert old in line
    path.write_bytes(line.replace(old, new))
    assert ledger_verify(path) == (1, ["verify FAILED: line 1: an object repeats a key"])


class _Twin(str):
    def __eq__(self, other: object) -> bool:
        return self is other

    def __hash__(self) -> int:
        return id(self)


def test_append_refuses_a_payload_that_repeats_a_key(path: Path) -> None:
    with pytest.raises(ValueError, match="an object repeats a key"):
        Ledger(path).append(Kind.ACK, {_Twin("a"): 1, _Twin("a"): 2})
    assert path.read_bytes() == b""


def test_replay_arithmetic_ignores_the_callers_decimal_context(path: Path) -> None:
    led = Ledger(path)
    with decimal.localcontext(prec=50, capitals=0):
        led.append(Kind.FILL, _fill("BTC", "buy", "1", "1", "0"))
        led.append(Kind.FILL, _fill("BTC", "buy", "2", "2", "0"))
        led.append(Kind.FUNDING, {"coin": "BTC", "amount": "1e-7"})
        led.append(Kind.STATE_CHECKPOINT, led.reduce())
    assert led.reduce()["positions"]["BTC"]["avg_price"] == "1.666666666666666666666666667"
    assert led.reduce()["funding_paid"] == "1E-7"
    assert ledger_verify(path)[0] == 0
    with decimal.localcontext(decimal.Context(prec=6, traps=[decimal.Inexact, decimal.Rounded])):
        assert ledger_verify(path)[0] == 0
        assert led.reduce()["positions"]["BTC"]["avg_price"] == "1.666666666666666666666666667"


@pytest.mark.parametrize("amount", ["1e30", "-1e30", "1e-30", "-1e-30", "0", "9.99e29"])
def test_a_decimal_from_1e_30_to_1e30_in_size_is_accepted(path: Path, amount: str) -> None:
    Ledger(path).append(Kind.FUNDING, {"coin": "BTC", "amount": amount})
    assert ledger_verify(path)[0] == 0


@pytest.mark.parametrize(
    ("amount", "reason"),
    [
        ("1.0000001e30", "must be 0 or between 1e-30 and 1e30 in size"),
        ("1.0000000000000000000000000000001e30", "must be 0 or between 1e-30 and 1e30 in size"),
        ("-1.0000001e30", "must be 0 or between 1e-30 and 1e30 in size"),
        ("1e31", "must be 0 or between 1e-30 and 1e30 in size"),
        ("9.99e-31", "must be 0 or between 1e-30 and 1e30 in size"),
        ("x", "is not a finite decimal"),
    ],
)
def test_a_decimal_outside_1e_30_to_1e30_in_size_is_refused(
    path: Path, amount: str, reason: str
) -> None:
    with pytest.raises(ValueError, match=f"funding amount '{amount}' {reason}"):
        Ledger(path).append(Kind.FUNDING, {"coin": "BTC", "amount": amount})
    assert path.read_bytes() == b""


def test_the_clock_warning_survives_an_absurd_head_ts_ms() -> None:
    head = Record(1, 10**400, Kind.HALT, {}, GENESIS, GENESIS)
    warning = clock_warning(head)
    assert warning is not None
    assert " h ahead of this clock" in warning


def test_append_refuses_a_kind_that_is_not_a_kind_before_touching_the_file(
    path: Path, tmp_path: Path
) -> None:
    led = Ledger(path)
    led.append(Kind.HALT, {})
    with path.open("ab") as f:
        f.write(b'{"torn')
    before = path.read_bytes()
    untyped: Any = "halt"
    with pytest.raises(ValueError, match="is not a Kind"):
        led.append(untyped, {})
    assert path.read_bytes() == before
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(ValueError, match="is not a Kind"):
        Ledger(missing).append(untyped, {})
    assert not missing.exists()


def test_an_unlatch_with_clears_seq_true_is_tamper(path: Path) -> None:
    Ledger(path).append(Kind.LATCH, LATCH)
    _forge(path, Kind.UNLATCH, {"clears_seq": True})
    code, out = ledger_verify(path)
    assert code == 1
    assert out[0].startswith("verify FAILED: line 2: unlatch payload must be")


def test_an_explicit_ts_ms_may_be_exactly_one_day_ahead(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hl_guard.ledger import ledger as lg

    monkeypatch.setattr(lg, "_now_ms", lambda: TS0)
    led = Ledger(path)
    assert led.append(Kind.HALT, {}, ts_ms=TS0 + DAY_MS).ts_ms == TS0 + DAY_MS
    with pytest.raises(ValueError, match="more than one day ahead of the clock"):
        led.append(Kind.HALT, {}, ts_ms=TS0 + DAY_MS + 1)


@pytest.mark.parametrize(
    "context",
    [decimal.DefaultContext, decimal.Context(traps=list(decimal.Context().flags))],
    ids=["default", "every trap"],
)
@pytest.mark.parametrize("fee", ["abc", "NaN", "1e999999999"])
def test_fill_parse_raises_tamper_under_any_decimal_context(
    context: decimal.Context, fee: str
) -> None:
    record = Record(1, TS0, Kind.FILL, _fill("BTC", "buy", "1", "10", fee), GENESIS, "")
    with decimal.localcontext(context), pytest.raises(LedgerTamperError, match="fill fee"):
        Fill.parse(record)


@pytest.mark.parametrize(
    ("ahead_ms", "hours"), [(25 * HOUR_MS - 1, "25.0"), (25 * HOUR_MS - 180_001, "24.9")]
)
def test_the_clock_warning_rounds_to_the_nearest_tenth_of_an_hour(
    monkeypatch: pytest.MonkeyPatch, ahead_ms: int, hours: str
) -> None:
    from hl_guard.ledger import ledger as lg

    monkeypatch.setattr(lg, "_now_ms", lambda: TS0)
    warning = clock_warning(Record(1, TS0 + ahead_ms, Kind.HALT, {}, GENESIS, GENESIS))
    assert warning is not None
    assert warning.startswith(f"clock warning: the newest record is {hours} h ahead")


@pytest.mark.parametrize("setting", [640, 0])
def test_a_1000_digit_int_is_out_of_range_under_any_int_limit(path: Path, setting: int) -> None:
    led = _three(path)
    with path.open("ab") as f:
        f.write(b'{"seq": ' + b"9" * 1000 + b"}\n")
    limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(setting)
    try:
        with pytest.raises(LedgerTamperError, match=re.escape(f"line 4: {OUT_OF_RANGE}")):
            led.replay()
    finally:
        sys.set_int_max_str_digits(limit)


@pytest.mark.parametrize("payload", [["rule", "detail"], "rule", 7, None])
@pytest.mark.parametrize("kind", [Kind.LATCH, Kind.PROPOSAL])
def test_append_refuses_a_payload_that_is_not_a_mapping(
    path: Path, kind: Kind, payload: Any
) -> None:
    with pytest.raises(ValueError, match="payload must be a mapping"):
        Ledger(path).append(kind, payload)
    assert not path.exists() or path.read_bytes() == b""


DAY = 86_400_000


def test_the_day_start_equity_of_each_day_reads_back(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.DAY_START, {"day": 20000, "equity_usd": "1000.5"}, ts_ms=20000 * DAY + 5)
    led.append(Kind.FILL, _fill("BTC", "buy", "1", "10", "0"), ts_ms=20000 * DAY + 9)
    led.append(Kind.DAY_START, {"day": 20001, "equity_usd": "900"}, ts_ms=20001 * DAY)
    records = led.replay()
    assert day_start_equity(records, 20000) == Decimal("1000.5")
    assert day_start_equity(records, 20001) == Decimal(900)
    assert day_start_equity(records, 20002) is None


def test_a_second_day_start_for_one_day_is_refused_and_tamper(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.DAY_START, {"day": 20000, "equity_usd": "1000"}, ts_ms=20000 * DAY)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="day 20000 already has a day_start"):
        led.append(Kind.DAY_START, {"day": 20000, "equity_usd": "5000"}, ts_ms=20000 * DAY + 1)
    assert path.read_bytes() == before
    _forge(path, Kind.DAY_START, {"day": 20000, "equity_usd": "5000"})
    code, out = ledger_verify(path)
    assert code == 1
    assert "day 20000 already has a day_start" in out[0]


@pytest.mark.parametrize("day_off", [-1, 1, 30])
def test_a_day_start_for_another_day_than_its_time_is_refused_and_tamper(
    path: Path, day_off: int
) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("BTC", "buy", "1", "10", "0"))
    head_ts = list(led.records())[-1].ts_ms
    before = path.read_bytes()
    day = head_ts // DAY + day_off
    with pytest.raises(ValueError, match="is not the UTC day of its ts_ms"):
        led.append(Kind.DAY_START, {"day": day, "equity_usd": "1000"})
    assert path.read_bytes() == before
    _forge(path, Kind.DAY_START, {"day": day, "equity_usd": "1000"})
    code, out = ledger_verify(path)
    assert code == 1
    assert "is not the UTC day of its ts_ms" in out[0]


def test_a_tail_parses_what_it_read_after_it_let_go_of_the_lock(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hl_guard.ledger import ledger as ledger_module
    from hl_guard.ledger.ledger import Tail

    led = Ledger(path)
    led.append(Kind.HALT, {"n": 1})
    real = ledger_module._parse_line
    writable: list[bool] = []

    def parse_and_try_the_lock(raw: bytes, line: int, before: Record | None) -> Record:
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            writable.append(True)
        except BlockingIOError:
            writable.append(False)
        finally:
            os.close(fd)
        return real(raw, line, before)

    monkeypatch.setattr(ledger_module, "_parse_line", parse_and_try_the_lock)

    assert [r.payload["n"] for r in Tail(led).records()] == [1]
    assert writable == [True]


def test_a_checked_append_sees_every_record_after_the_named_seq(path: Path) -> None:
    from hl_guard.ledger.ledger import Tail

    led = Ledger(path)
    led.append(Kind.PROPOSAL, {"id": "a"})
    led.append(Kind.PROPOSAL, {"id": "b"})
    tail = Tail(led)
    tail.records()
    led.append(Kind.PROPOSAL, {"id": "c"})
    seen: list[list[str]] = []

    def check(records: list[Record], ts_ms: int) -> None:
        seen.append([r.payload.get("id", "") for r in records])
        if any(r.kind is Kind.APPROVAL for r in records):
            raise ValueError("already decided")

    tail.append(Kind.APPROVAL, {"id": "a"}, after_seq=1, check=check)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="already decided"):
        tail.append(Kind.APPROVAL, {"id": "a"}, after_seq=1, check=check)
    assert path.read_bytes() == before
    assert seen == [["b", "c"], ["b", "c", "a"]]
    assert tail.records() == led.replay()


def test_a_checked_append_parses_only_the_lines_its_tail_has_not_read(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hl_guard.ledger import ledger as ledger_module
    from hl_guard.ledger.ledger import Tail

    led = Ledger(path)
    for n in range(50):
        led.append(Kind.HALT, {"n": n})
    tail = Tail(led)
    tail.records()
    led.append(Kind.HALT, {"n": 50})
    real = ledger_module._decode_and_check_hash
    parsed: list[int | None] = []

    def counted(raw: bytes, line: int | None) -> Record:
        parsed.append(line)
        return real(raw, line)

    monkeypatch.setattr(ledger_module, "_decode_and_check_hash", counted)

    tail.append(Kind.HALT, {"n": 51})

    assert len(parsed) <= 6, parsed
    assert [r.payload["n"] for r in tail.records()] == list(range(52))


def test_a_checked_append_refuses_a_ledger_rewritten_under_its_tail(path: Path) -> None:
    from hl_guard.ledger.ledger import Tail

    led = Ledger(path)
    led.append(Kind.HALT, {"n": 1})
    led.append(Kind.HALT, {"n": 2})
    tail = Tail(led)
    tail.records()
    path.unlink()
    Ledger(path).append(Kind.HALT, {"n": 9})
    before = path.read_bytes()

    with pytest.raises(LedgerTamperError, match="changed under its reader"):
        tail.append(Kind.HALT, {"n": 11})
    assert path.read_bytes() == before


def test_a_checked_append_refuses_a_last_line_rewritten_under_its_tail(path: Path) -> None:
    from hl_guard.ledger.ledger import Tail

    led = Ledger(path)
    led.append(Kind.HALT, {"n": 1})
    led.append(Kind.HALT, {"n": 2})
    tail = Tail(led)
    tail.records()
    lines = _lines(path)
    lines[-1] = _rehash({**lines[-1], "payload": {"n": 3}})
    path.write_text("".join(_canonical(line).decode() + "\n" for line in lines))
    before = path.read_bytes()

    with pytest.raises(LedgerTamperError, match="changed under its reader"):
        tail.append(Kind.HALT, {"n": 4})
    assert path.read_bytes() == before


def test_a_tail_that_rereads_a_shrunk_ledger_folds_it_afresh(path: Path) -> None:
    day = time.time_ns() // 1_000_000 // 86_400_000
    Ledger(path).append(Kind.DAY_START, {"day": day, "equity_usd": "100"})
    tail = Tail(Ledger(path))
    tail.records()
    assert tail.unlatch() == (None, None)
    path.unlink()
    Ledger(path).append(Kind.HALT, {})
    tail.records()

    tail.append(Kind.DAY_START, {"day": day, "equity_usd": "90"})

    assert day_start_equity(Ledger(path).replay(), day) == Decimal(90)


def test_a_checked_append_refuses_a_checkpoint(path: Path) -> None:
    with pytest.raises(ValueError, match="needs a verified replay"):
        Tail(Ledger(path)).append(Kind.STATE_CHECKPOINT, {})


def test_a_tail_reads_each_appended_line_once_and_keeps_the_chain(path: Path) -> None:
    from hl_guard.ledger.ledger import Tail

    led = Ledger(path)
    tail = Tail(led)
    assert tail.records() == []
    led.append(Kind.HALT, {"n": 1})
    led.append(Kind.HALT, {"n": 2})
    assert [r.payload["n"] for r in tail.records()] == [1, 2]
    with path.open("ab") as f:
        f.write(b'{"seq": 3')
    led_records = tail.records()
    assert len(led_records) == 2
    led.append(Kind.HALT, {"n": 3})
    assert [r.payload["n"] for r in tail.records()] == [1, 2, 3]
    assert tail.records() == led.replay()


def test_a_tail_refuses_a_line_that_breaks_the_chain(path: Path) -> None:
    from hl_guard.ledger.ledger import Tail

    led = Ledger(path)
    tail = Tail(led)
    led.append(Kind.HALT, {"n": 1})
    tail.records()
    head = _lines(path)[-1]
    line = {"seq": 2, "ts_ms": head["ts_ms"], "kind": "halt", "payload": {}, "prev": "0" * 64}
    with path.open("a") as f:
        f.write(json.dumps(_rehash(line)) + "\n")
    with pytest.raises(LedgerTamperError, match="prev does not match"):
        tail.records()


def test_a_tail_rereads_a_file_that_shrank(path: Path) -> None:
    from hl_guard.ledger.ledger import Tail

    led = Ledger(path)
    tail = Tail(led)
    led.append(Kind.HALT, {"n": 1})
    led.append(Kind.HALT, {"n": 2})
    tail.records()
    path.unlink()
    Ledger(path).append(Kind.HALT, {"n": 9})
    assert [r.payload["n"] for r in tail.records()] == [9]
