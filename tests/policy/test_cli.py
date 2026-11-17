import json
from pathlib import Path
from typing import Any

import pytest
from policy_fixtures import ACCOUNT

from hl_guard.cli.__main__ import main

EXAMPLE = Path(__file__).parents[2] / "examples" / "policy.yaml"


@pytest.fixture()
def policy_path(tmp_path: Path) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_bytes(EXAMPLE.read_bytes())
    path.chmod(0o600)
    return path


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, list[str]]:
    with pytest.raises(SystemExit) as exc:
        main(argv)
    code = exc.value.code
    assert isinstance(code, int)
    return code, capsys.readouterr().out.splitlines()


def _gate_try(
    policy: Path, account: Path, proposals: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[str]]:
    argv = ["gate", "try", "--policy", str(policy), "--account", str(account), str(proposals)]
    return _run(argv, capsys)


def test_policy_check_prints_every_limit_and_its_ceiling(
    policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(["policy", "check", str(policy_path)], capsys)
    assert code == 0
    rows = {line.split()[0]: line for line in out[1:]}
    assert "ceiling per_order_usd_max <= 5000" in rows["per_order_usd_max"]
    assert "ceiling cooldown_s >= 60" in rows["cooldown_s"]
    assert set(rows) >= {"coins", "stop_loss_required", "schedule_cancel_min", "approval"}


def test_policy_check_prints_the_configurable_rules_with_default_and_ceiling(
    policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = [
        line
        for line in policy_path.read_text().splitlines()
        if not line.startswith(
            (
                "leverage_max",
                "close_sell_floor",
                "close_buy_cap",
                "schedule_cancel_max_s",
                "add_margin_usd_max",
            )
        )
    ]
    policy_path.write_text("\n".join(lines))
    code, out = _run(["policy", "check", str(policy_path)], capsys)
    assert code == 0
    rows = {line.split()[0]: " ".join(line.split()) for line in out[1:]}
    assert rows["leverage_max"] == "leverage_max 1 default 1, ceiling leverage_max <= 5"
    assert rows["close_sell_floor"] == (
        "close_sell_floor 0.5 default 0.5, ceiling 0.1 <= close_sell_floor <= 0.95"
    )
    assert rows["close_buy_cap"] == (
        "close_buy_cap 2 default 2, ceiling 1.05 <= close_buy_cap <= 10"
    )
    assert rows["schedule_cancel_max_s"] == (
        "schedule_cancel_max_s 86400 default 86400, ceiling 5 <= schedule_cancel_max_s <= 604800"
    )
    assert rows["add_margin_usd_max"] == (
        "add_margin_usd_max 10 default 10, ceiling add_margin_usd_max <= per_order_usd_max"
    )


def test_policy_check_names_the_broken_ceiling(
    policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_path.write_text(
        policy_path.read_text().replace("per_order_usd_max: 20", "per_order_usd_max: 9000")
    )
    code, out = _run(["policy", "check", str(policy_path)], capsys)
    assert code == 2
    assert out == [
        "policy refused: per_order_usd_max = 9000 breaks the product hard ceiling "
        "per_order_usd_max <= 5000"
    ]


def _entry(coin: str, size: str = "0.1") -> dict[str, Any]:
    return {
        "kind": "entry",
        "coin": coin,
        "is_buy": True,
        "size_coins": float(size),
        "limit_px": 100,
        "stop_loss_px": 99,
        "take_profit_px": 102,
    }


def test_gate_try_folds_allowed_entries_into_the_account(
    tmp_path: Path, policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    account = tmp_path / "account.json"
    account.write_text(json.dumps(ACCOUNT))
    proposals = tmp_path / "proposals.jsonl"
    proposals.write_text(
        "\n".join(
            json.dumps(p) for p in [_entry("BTC"), _entry("BTC"), _entry("ETH"), _entry("SOL")]
        )
    )
    code, out = _run(
        ["gate", "try", "--policy", str(policy_path), "--account", str(account), str(proposals)],
        capsys,
    )
    rows = [json.loads(line) for line in out]
    assert code == 1
    assert [(r["decision"], r.get("rule")) for r in rows] == [
        ("allowed", None),
        ("rejected", "cooldown_s"),
        ("allowed", None),
        ("rejected", "entries_per_day_max"),
    ]


def test_gate_try_reports_bad_input_with_exit_2(
    tmp_path: Path, policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    account = tmp_path / "account.json"
    account.write_text(json.dumps(ACCOUNT))
    proposal = tmp_path / "p.json"
    proposal.write_text(json.dumps({"kind": "withdraw"}))
    code, out = _run(
        ["gate", "try", "--policy", str(policy_path), "--account", str(account), str(proposal)],
        capsys,
    )
    assert code == 2
    assert "proposal.kind" in json.loads(out[0])["error"]


def test_policy_check_aligns_the_ceiling_column(
    policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy_path.write_text(
        policy_path.read_text().replace(
            "coins: [BTC, ETH, SOL]", "coins: [AVAX, BTC, DOGE, ETH, SOL]"
        )
    )
    code, out = _run(["policy", "check", str(policy_path)], capsys)
    assert code == 0
    rows = out[1:]
    notes = {row.index(note) for row, note in zip(rows, _notes(rows), strict=True)}
    assert len(notes) == 1
    assert "add_margin_usd_max" in {row.split()[0] for row in rows}


def _notes(rows: list[str]) -> list[str]:
    return [row.split("  ")[-1].strip() for row in rows]


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe{}", b'{"kind": "add_margin", "coin": "BTC", "usd": ' + b"9" * 5000 + b"}"],
    ids=["not utf-8", "huge int"],
)
def test_gate_try_reports_unreadable_proposals_with_exit_2(
    tmp_path: Path, policy_path: Path, capsys: pytest.CaptureFixture[str], content: bytes
) -> None:
    account = tmp_path / "account.json"
    account.write_text(json.dumps(ACCOUNT))
    proposal = tmp_path / "p.json"
    proposal.write_bytes(content)
    code, out = _run(
        ["gate", "try", "--policy", str(policy_path), "--account", str(account), str(proposal)],
        capsys,
    )
    assert code == 2
    assert "error" in json.loads(out[0])


def test_gate_try_counts_margin_additions(
    tmp_path: Path, policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    account = tmp_path / "account.json"
    account.write_text(json.dumps({**ACCOUNT, "positions": {"BTC": 0.1}}))
    proposals = tmp_path / "p.jsonl"
    add = {"kind": "add_margin", "coin": "BTC", "usd": 5}
    proposals.write_text("\n".join(json.dumps(add) for _ in range(4)))
    code, out = _run(
        ["gate", "try", "--policy", str(policy_path), "--account", str(account), str(proposals)],
        capsys,
    )
    assert code == 1
    assert [json.loads(line).get("rule") for line in out] == [
        None,
        None,
        None,
        "margin_additions_per_day_max",
    ]


def test_ledger_repair_and_latch_clear_run_from_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from hl_guard.ledger.ledger import Kind, Ledger

    path = tmp_path / "ledger.jsonl"
    Ledger(path).append(Kind.LATCH, {"rule": "loss_streak_max", "detail": "5 losses"})
    with path.open("ab") as f:
        f.write(b'{"se')
    assert _run(["ledger", "verify", str(path)], capsys)[0] == 1
    assert _run(["ledger", "repair", str(path)], capsys)[0] == 0
    assert _run(["latch", "clear", str(path)], capsys) == (
        0,
        ["unlatched: wrote record 2, which clears record 1 (loss_streak_max: 5 losses)"],
    )
    assert _run(["latch", "clear", str(path)], capsys) == (
        0,
        ["not latched: no unlatch record written"],
    )
    assert _run(["ledger", "verify", str(path)], capsys)[0] == 0


def test_help_lists_the_exit_codes_of_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = _run(["--help"], capsys)
    assert code == 0
    codes = out[out.index("exit codes:") + 1 :]
    assert [line.split()[:2] for line in codes if line.strip()] == [
        ["probe-sign", "0"],
        ["policy", "check"],
        ["gate", "try"],
        ["ledger", "verify"],
        ["ledger", "repair"],
        ["ledger", "show"],
        ["latch", "clear"],
        ["serve", "0"],
        ["approve", "0"],
        ["reject", "0"],
        ["pending", "0"],
        ["init", "0"],
        ["status", "0"],
        ["revoke", "0"],
        ["any", "command"],
    ]
    assert "  gate try       0 every proposal allowed, 1 any rejected, 2 bad input" in out


def test_bare_hl_guard_prints_help_and_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    assert "exit codes:" in capsys.readouterr().err


@pytest.mark.parametrize("last", ["0", "-1", "x"])
def test_ledger_show_needs_a_positive_count(
    tmp_path: Path, last: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["ledger", "show", str(tmp_path / "l.jsonl"), "--last", last])
    assert exc.value.code == 2
    assert "--last" in capsys.readouterr().err


def test_policy_check_names_a_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(["policy", "check", str(tmp_path)], capsys) == (
        2,
        [f"policy refused: policy {tmp_path} is a directory"],
    )


@pytest.mark.parametrize(
    ("proposal", "account"),
    [
        (
            '{"kind":"entry","coin":"BTC","is_buy":true,"size_coins":1e999999,'
            '"limit_px":100,"stop_loss_px":99}',
            ACCOUNT,
        ),
        (
            json.dumps(_entry("BTC")),
            {
                **ACCOUNT,
                "assets": {
                    **ACCOUNT["assets"],
                    "BTC": {**ACCOUNT["assets"]["BTC"], "mark_px": "MARK"},
                },
            },
        ),
        (json.dumps(_entry("BTC")), {**ACCOUNT, "positions": {"BTC": "POSITION"}}),
    ],
    ids=["size 1e999999", "mark 1e-999999", "position 1e999999"],
)
def test_gate_try_reports_a_number_out_of_size_as_bad_input(
    tmp_path: Path,
    policy_path: Path,
    capsys: pytest.CaptureFixture[str],
    proposal: str,
    account: dict[str, Any],
) -> None:
    account_path = tmp_path / "account.json"
    account_path.write_text(
        json.dumps(account).replace('"MARK"', "1e-999999").replace('"POSITION"', "1e999999")
    )
    proposal_path = tmp_path / "p.json"
    proposal_path.write_text(proposal)
    code, out = _gate_try(policy_path, account_path, proposal_path, capsys)
    assert (code, len(out)) == (2, 1)
    assert "must be 0 or between 1e-30 and 1e30 in size" in json.loads(out[0])["error"]


def test_policy_check_prints_the_schedule_cancel_window(
    policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(["policy", "check", str(policy_path)], capsys)[1]
    row = next(line for line in out if line.startswith("schedule_cancel_min"))
    assert "  now + 5 s  " in row
    assert row.endswith("fixed by the exchange")


def test_gate_try_reports_an_integer_of_4300_digits_as_bad_input(
    tmp_path: Path, policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    account_path = tmp_path / "account.json"
    account_path.write_text(json.dumps(ACCOUNT).replace(str(ACCOUNT["now_ms"]), "9" * 4300, 1))
    proposal_path = tmp_path / "p.json"
    proposal_path.write_text(json.dumps({"kind": "schedule_cancel", "time_ms": 0}))
    code, out = _gate_try(policy_path, account_path, proposal_path, capsys)
    assert (code, len(out)) == (2, 1)
    assert "account.now_ms must be an integer from 0 to 2**63-1" in json.loads(out[0])["error"]


def test_gate_try_reports_a_close_that_leaves_a_dust_position_as_bad_input(
    tmp_path: Path, policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    account_path = tmp_path / "account.json"
    account_path.write_text(json.dumps({**ACCOUNT, "positions": {"BTC": 1}}))
    proposal_path = tmp_path / "p.jsonl"
    proposal_path.write_text(
        '{"kind":"close","coin":"BTC","size_coins":0.9999999999999999999999999999999,'
        '"limit_px":100}\n{"kind":"close","coin":"BTC","size_coins":0.5,"limit_px":100}\n'
    )
    code, out = _gate_try(policy_path, account_path, proposal_path, capsys)
    assert code == 2
    assert json.loads(out[0])["decision"] == "allowed"
    assert json.loads(out[1])["error"].startswith("after proposal 1: positions.BTC must be 0")
    assert len(out) == 2
