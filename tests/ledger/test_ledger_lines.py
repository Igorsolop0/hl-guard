import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from ledger_fixtures import _fill, _forge, _lines, _rewrite

from hl_guard.ledger.cli import ledger_verify
from hl_guard.ledger.ledger import Kind, Ledger, LedgerTamperError


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "ledger.jsonl"


def test_a_line_with_its_keys_reordered_still_verifies(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("BTC", "buy", "1", "10", "0.01"))
    led.append(Kind.FILL, _fill("BTC", "sell", "1", "11", "0.01"))
    original = path.read_text().splitlines()
    reordered = [
        {
            **{k: line[k] for k in reversed(list(line))},
            "payload": dict(reversed(line["payload"].items())),
        }
        for line in _lines(path)
    ]
    _rewrite(path, reordered)
    rewritten = path.read_text().splitlines()
    assert all(
        a != b and json.loads(a) == json.loads(b) for a, b in zip(original, rewritten, strict=True)
    )
    assert [r.seq for r in led.replay(verify=True)] == [1, 2]
    assert ledger_verify(path)[0] == 0


def test_a_negative_fee_is_a_rebate_that_lowers_fees_paid(path: Path) -> None:
    led = Ledger(path)
    led.append(Kind.FILL, _fill("BTC", "buy", "1", "100", "0.05"))
    led.append(Kind.FILL, _fill("BTC", "sell", "1", "100", "-0.02"))
    led.append(Kind.STATE_CHECKPOINT, led.reduce())
    state = led.reduce()
    assert Decimal(state["fees_paid"]) == Decimal("0.03")
    assert Decimal(state["realized_pnl"]) == Decimal("-0.03")
    assert len(led.replay(verify=True)) == 3


@pytest.mark.parametrize("payload", [["x"], "x", 5, None, [["coin", "BTC"]]])
@pytest.mark.parametrize("kind", [Kind.FILL, Kind.LATCH, Kind.UNLATCH, Kind.PROPOSAL])
def test_a_non_object_payload_on_disk_is_a_ledger_error(
    path: Path, kind: Kind, payload: Any
) -> None:
    _forge(path, kind, payload)
    with pytest.raises(LedgerTamperError, match="payload not an object"):
        Ledger(path).replay()
    code, out = ledger_verify(path)
    assert code != 0
    assert "payload not an object" in out[0]
