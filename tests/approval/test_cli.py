from __future__ import annotations

import builtins
import fcntl
import os
import signal
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from server.fake_info import write_config

from hl_guard.approval import cli, model
from hl_guard.approval.cli import decide, pending
from hl_guard.approval.model import proposal_hash
from hl_guard.cli.__main__ import main
from hl_guard.ledger.ledger import Kind, Ledger

ORDER = {
    "coin": "BTC",
    "side": "buy",
    "size": "0.0002",
    "limit_px": "81171",
    "tif": "Gtc",
    "stop_loss_px": "80000",
    "take_profit_px": None,
    "leverage": 1,
}


@pytest.fixture()
def idle(tmp_path: Path, durable: None) -> Path:
    return write_config(tmp_path)


@pytest.fixture()
def config(idle: Path) -> Iterator[Path]:
    fd = os.open(idle.parent / "data" / "serve.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    yield idle
    os.close(fd)


@pytest.fixture()
def durable(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from hl_guard.exchange import halt

    monkeypatch.setattr(halt, "temp_dirs", lambda: [])
    yield


def ledger(config: Path) -> Ledger:
    return Ledger(config.parent / "data" / "ledger.jsonl")


def propose(config: Path, pid: str = "a1b2c3d4e5f6", ttl_ms: int = 60_000, ttl_s: int = 120) -> str:
    digest = proposal_hash(pid, "testnet", "0x" + "ab" * 20, ORDER)
    led = ledger(config)
    led.append(Kind.PROPOSAL, {"id": pid, "tool": "propose_order", "order": ORDER, "hash": digest})
    expires = time.time_ns() // 1_000_000 + ttl_ms
    pending = {"id": pid, "outcome": "pending", "rule": "", "detail": "", "expires_ms": expires}
    led.append(Kind.DECISION, pending | {"ttl_s": ttl_s})
    return digest


def approvals(config: Path) -> list[dict[str, Any]]:
    return [dict(r.payload) for r in ledger(config).records() if r.kind is Kind.APPROVAL]


def test_approve_writes_one_approval_bound_to_the_hash(config: Path) -> None:
    digest = propose(config)

    code, lines = decide(config, "a1b2c3d4e5f6", True)

    assert code == 0
    assert signal.SIGINT not in signal.pthread_sigmask(signal.SIG_BLOCK, ())
    assert lines[0].startswith("approved a1b2c3d4e5f6: buy 0.0002 BTC limit 81171 Gtc")
    assert lines[1:] == [f"account 0x{'ab' * 20} on testnet", f"hash {digest}"]
    assert approvals(config) == [{"id": "a1b2c3d4e5f6", "hash": digest, "approved": True}]


def test_reject_writes_a_refusal(config: Path) -> None:
    propose(config)

    assert decide(config, "a1b2c3d4e5f6", False)[0] == 0
    assert approvals(config)[0]["approved"] is False


def test_a_second_answer_is_refused(config: Path) -> None:
    propose(config)
    decide(config, "a1b2c3d4e5f6", True)

    code, lines = decide(config, "a1b2c3d4e5f6", False)

    assert code == 1
    assert lines == ["reject refused: proposal a1b2c3d4e5f6 is approved, not pending"]
    assert len(approvals(config)) == 1


def test_an_unknown_id_is_refused(config: Path) -> None:
    propose(config)

    assert decide(config, "ffffffffffff", True) == (
        1,
        ["approve refused: no proposal ffffffffffff in the ledger"],
    )


def test_an_expired_proposal_is_refused(config: Path) -> None:
    propose(config, ttl_ms=-1)

    code, lines = decide(config, "a1b2c3d4e5f6", True)

    assert code == 1
    assert "expired" in lines[0]
    assert approvals(config) == []


def test_a_proposal_decided_while_the_command_runs_is_refused(
    config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    propose(config)
    real = model.fold
    calls: list[int] = []

    def racing(records: Any, folded_before: Any = None) -> Any:
        folded = real(records, folded_before)
        if not calls:
            ledger(config).append(
                Kind.DECISION,
                {"id": "a1b2c3d4e5f6", "outcome": "expired", "rule": "", "detail": ""},
            )
        calls.append(1)
        return folded

    monkeypatch.setattr("hl_guard.approval.cli.fold", racing)

    code, lines = decide(config, "a1b2c3d4e5f6", True)

    assert code == 1
    assert "is expired, not pending" in lines[0]
    assert approvals(config) == []


def test_a_bad_config_exits_2(tmp_path: Path) -> None:
    code, lines = decide(tmp_path / "missing.yaml", "a1b2c3d4e5f6", True)

    assert code == 2
    assert lines[0].startswith("approve FAILED: cannot read config")


def test_a_tampered_ledger_exits_2(config: Path) -> None:
    propose(config)
    path = config.parent / "data" / "ledger.jsonl"
    path.write_text(path.read_text().replace("81171", "91171"))

    assert decide(config, "a1b2c3d4e5f6", True)[0] == 2


def test_pending_lists_only_open_proposals(config: Path) -> None:
    assert pending(config) == (0, ["no proposal waits for approval"])
    propose(config)
    propose(config, pid="0123456789ab", ttl_ms=-1)

    code, lines = pending(config)

    assert code == 0
    assert len(lines) == 1
    assert lines[0].startswith("a1b2c3d4e5f6: buy 0.0002 BTC")


def run_in_a_terminal(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    typed: str | BaseException,
) -> tuple[int | str | None, str]:
    prompts: list[str] = []

    def answer(text: str) -> str:
        prompts.append(text)
        if isinstance(typed, BaseException):
            raise typed
        return typed

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", answer)
    with pytest.raises(SystemExit) as done:
        main(["approve", "a1b2c3d4e5f6", "--config", str(config)])
    return done.value.code, capsys.readouterr().out + "".join(prompts)


def test_the_cli_approves_in_a_terminal_after_a_typed_yes(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    propose(config)

    code, out = run_in_a_terminal(config, monkeypatch, capsys, "yes")

    assert code == 0
    assert out.index("buy 0.0002 BTC") < out.index("approved a1b2c3d4e5f6")
    assert out.endswith("Type yes to approve this order: ")
    assert len(approvals(config)) == 1


@pytest.mark.parametrize("typed", ["y", "", "YES please"])
def test_the_cli_writes_nothing_without_a_typed_yes(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], typed: str
) -> None:
    propose(config)

    code, out = run_in_a_terminal(config, monkeypatch, capsys, typed)

    assert code == 1
    assert "approve cancelled: nothing written" in out
    assert approvals(config) == []


@pytest.mark.parametrize("closed", [EOFError(), KeyboardInterrupt()], ids=["eof", "ctrl-c"])
def test_a_prompt_closed_without_an_answer_writes_nothing_in_one_line(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    closed: BaseException,
) -> None:
    propose(config)

    code, out = run_in_a_terminal(config, monkeypatch, capsys, closed)

    assert code == 1
    assert "approve cancelled: nothing written" in out.splitlines()
    assert approvals(config) == []


def _interrupt_the_lock_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    real = fcntl.flock

    def interrupted(fd: int, op: int) -> None:
        if op == fcntl.LOCK_EX:
            raise KeyboardInterrupt
        real(fd, op)

    monkeypatch.setattr(fcntl, "flock", interrupted)


def _interrupt_the_cold_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from hl_guard.ledger import ledger as ledger_module

    def interrupted(raw: bytes, line: int, before: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(ledger_module, "_parse_line", interrupted)


@pytest.mark.parametrize("verb", ["approve", "reject"])
@pytest.mark.parametrize(
    "interrupt", [_interrupt_the_lock_wait, _interrupt_the_cold_read], ids=["lock", "read"]
)
def test_ctrl_c_before_the_answer_is_written_says_so_in_one_line(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    interrupt: Any,
) -> None:
    propose(config)
    before = ledger(config).path.read_bytes()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda text: "yes")
    interrupt(monkeypatch)

    with pytest.raises(SystemExit) as done:
        main([verb, "a1b2c3d4e5f6", "--config", str(config)])

    assert done.value.code == 1
    assert capsys.readouterr().out.splitlines()[-1:] == [f"{verb} cancelled: nothing written"]
    assert ledger(config).path.read_bytes() == before


def test_ctrl_c_once_the_answer_is_being_written_lets_it_finish(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    propose(config)
    real = os.fsync

    def fsync_then_ctrl_c(fd: int) -> None:
        real(fd)
        os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(os, "fsync", fsync_then_ctrl_c)

    with pytest.raises(SystemExit) as done:
        main(["reject", "a1b2c3d4e5f6", "--config", str(config)])

    assert done.value.code == 0
    assert capsys.readouterr().out.startswith("rejected a1b2c3d4e5f6")
    assert [a["approved"] for a in approvals(config)] == [False]


def _exit_code(argv: list[str]) -> int | str | None:
    try:
        main(argv)
    except SystemExit as done:
        return done.code
    except KeyboardInterrupt:
        return "KeyboardInterrupt escaped main"
    return "main returned"


def _after_the_write(monkeypatch: pytest.MonkeyPatch, interrupt: Callable[[], None]) -> None:
    real, calls = cli._serving, []

    def serving(data_dir: Path) -> bool:
        calls.append(data_dir)
        if len(calls) == 2:
            interrupt()
        return real(data_dir)

    monkeypatch.setattr(cli, "_serving", serving)


def _sigint() -> None:
    os.kill(os.getpid(), signal.SIGINT)


def _typed_yes_in_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda text: "yes")


def test_ctrl_c_after_the_answer_is_written_still_reports_it_approved(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    propose(config)
    _typed_yes_in_a_terminal(monkeypatch)
    _after_the_write(monkeypatch, _sigint)

    code = _exit_code(["approve", "a1b2c3d4e5f6", "--config", str(config)])

    out = capsys.readouterr().out.splitlines()
    assert code == 0, out
    assert out[-3].startswith("approved a1b2c3d4e5f6: buy 0.0002 BTC"), out
    assert not any("nothing written" in line for line in out), out
    assert [a["approved"] for a in approvals(config)] == [True]


def test_an_interrupt_after_the_write_reads_the_answer_back_from_the_ledger(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = propose(config, pid="0123456789ab")
    ledger(config).append(Kind.APPROVAL, {"id": "0123456789ab", "hash": other, "approved": False})
    digest = propose(config)
    _typed_yes_in_a_terminal(monkeypatch)

    def a_later_answer_then_interrupt() -> None:
        late = {"id": "a1b2c3d4e5f6", "hash": digest, "approved": False}
        ledger(config).append(Kind.APPROVAL, late)
        raise KeyboardInterrupt

    _after_the_write(monkeypatch, a_later_answer_then_interrupt)

    code = _exit_code(["approve", "a1b2c3d4e5f6", "--config", str(config)])

    assert code == 0
    assert capsys.readouterr().out.splitlines()[-1:] == [
        "approve interrupted: the ledger records proposal a1b2c3d4e5f6 as approved"
    ]
    assert [a["approved"] for a in approvals(config)] == [False, True, False]


@pytest.mark.parametrize(
    ("verb", "line"),
    [
        ("approve", "approve interrupted: the ledger records proposal a1b2c3d4e5f6 as rejected"),
        ("reject", "reject interrupted: the ledger records proposal a1b2c3d4e5f6 as rejected"),
    ],
)
def test_an_interrupt_after_the_check_reports_the_answer_the_ledger_holds(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    line: str,
) -> None:
    from hl_guard.ledger import ledger as ledger_module

    propose(config)
    _typed_yes_in_a_terminal(monkeypatch)
    real = ledger_module._write

    def rejected_first(fd: int, prev: Any, kind: Kind, payload: Any, *rest: Any) -> Any:
        real(fd, prev, kind, {**payload, "approved": False}, *rest)
        raise KeyboardInterrupt

    monkeypatch.setattr(ledger_module, "_write", rejected_first)

    code = _exit_code([verb, "a1b2c3d4e5f6", "--config", str(config)])

    assert (code, capsys.readouterr().out.splitlines()[-1]) == (int(verb == "approve"), line)


def test_an_interrupt_after_the_check_but_before_the_write_says_nothing_written(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hl_guard.ledger import ledger as ledger_module

    propose(config)
    before = ledger(config).path.read_bytes()
    _typed_yes_in_a_terminal(monkeypatch)

    def interrupted(*args: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(ledger_module, "_write", interrupted)

    code = _exit_code(["approve", "a1b2c3d4e5f6", "--config", str(config)])

    assert code == 1
    assert capsys.readouterr().out.splitlines()[-1:] == ["approve cancelled: nothing written"]
    assert ledger(config).path.read_bytes() == before


def test_an_unreadable_ledger_after_the_check_says_the_answer_may_be_written(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hl_guard.ledger import ledger as ledger_module

    propose(config)
    _typed_yes_in_a_terminal(monkeypatch)
    real = ledger_module._write

    def written_then_opened_up(*args: Any) -> Any:
        real(*args)
        ledger(config).path.chmod(0o644)
        raise KeyboardInterrupt

    monkeypatch.setattr(ledger_module, "_write", written_then_opened_up)

    code = _exit_code(["approve", "a1b2c3d4e5f6", "--config", str(config)])

    (last,) = capsys.readouterr().out.splitlines()[-1:]
    assert code == 2
    assert last.startswith("approve interrupted: the answer may be written; reading the"), last


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_ctrl_c_while_a_refusal_prints_keeps_it(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    real = builtins.print

    def ctrl_c_then_print(*args: Any, **kwargs: Any) -> None:
        _sigint()
        real(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", ctrl_c_then_print)

    code = _exit_code([verb, "ffffffffffff", "--config", str(config)])

    assert (code, capsys.readouterr().out.splitlines()) == (
        1,
        [f"{verb} refused: no proposal ffffffffffff in the ledger"],
    )


@pytest.mark.parametrize(("verb", "done"), [("approve", "approved"), ("reject", "rejected")])
def test_ctrl_c_while_the_result_prints_keeps_every_line(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    done: str,
) -> None:
    propose(config)
    _typed_yes_in_a_terminal(monkeypatch)
    real = builtins.print

    def ctrl_c_then_print(*args: Any, **kwargs: Any) -> None:
        if args and str(args[0]).startswith(done):
            _sigint()
        real(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", ctrl_c_then_print)

    code = _exit_code([verb, "a1b2c3d4e5f6", "--config", str(config)])

    out = capsys.readouterr().out.splitlines()
    (answer,) = approvals(config)
    assert code == 0, out
    assert out[-3].startswith(f"{done} a1b2c3d4e5f6: buy 0.0002 BTC"), out
    assert out[-2:] == [f"account 0x{'ab' * 20} on testnet", f"hash {answer['hash']}"]


@pytest.mark.parametrize("home", ["~", "~nosuchuser-hl-guard"])
@pytest.mark.parametrize(
    "command",
    [["approve", "a1b2c3d4e5f6"], ["serve"], ["pending"]],
    ids=["approve", "serve", "pending"],
)
def test_a_home_that_does_not_resolve_is_refused_in_one_line(
    config: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    home: str,
    command: list[str],
) -> None:
    propose(config)
    before = ledger(config).path.read_bytes()
    monkeypatch.setenv("HOME", home)

    with pytest.raises(SystemExit) as done:
        main([*command, "--config", str(config)])

    captured = capsys.readouterr()
    assert done.value.code == 2
    assert captured.out == ""
    assert captured.err.splitlines() == [
        f"hl-guard refused: HOME={home!r} does not name a home directory; set HOME to an "
        "absolute path"
    ]
    assert ledger(config).path.read_bytes() == before


def test_reject_needs_no_terminal(config: Path, capsys: pytest.CaptureFixture[str]) -> None:
    propose(config)
    with pytest.raises(SystemExit) as done:
        main(["reject", "a1b2c3d4e5f6", "--config", str(config)])

    assert done.value.code == 0
    assert approvals(config)[0]["approved"] is False


def test_approve_shows_the_order_before_it_writes_and_can_be_cancelled(config: Path) -> None:
    digest = propose(config)
    seen: list[list[str]] = []

    def no(shown: list[str]) -> bool:
        seen.append(shown)
        assert approvals(config) == []
        return False

    assert decide(config, "a1b2c3d4e5f6", True, no) == (1, ["approve cancelled: nothing written"])
    assert seen[0][0].startswith("a1b2c3d4e5f6: buy 0.0002 BTC limit 81171")
    assert seen[0][1:] == [f"account 0x{'ab' * 20} on testnet", f"hash {digest}"]
    assert approvals(config) == []
    assert decide(config, "a1b2c3d4e5f6", True, lambda shown: True)[0] == 0


def test_approve_without_a_terminal_is_refused(
    config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    propose(config)
    with pytest.raises(SystemExit) as refused:
        main(["approve", "a1b2c3d4e5f6", "--config", str(config)])

    assert refused.value.code == 1
    assert "run it in a terminal" in capsys.readouterr().out
    assert approvals(config) == []


def test_a_proposal_whose_order_does_not_match_its_hash_is_refused(config: Path) -> None:
    led = ledger(config)
    led.append(
        Kind.PROPOSAL,
        {"id": "a1b2c3d4e5f6", "tool": "propose_order", "order": ORDER, "hash": "0" * 64},
    )
    expires = time.time_ns() // 1_000_000 + 60_000
    led.append(
        Kind.DECISION,
        {
            "id": "a1b2c3d4e5f6",
            "outcome": "pending",
            "rule": "",
            "detail": "",
            "expires_ms": expires,
            "ttl_s": 120,
        },
    )

    code, lines = decide(config, "a1b2c3d4e5f6", True)

    assert code == 1
    assert "does not match its hash" in lines[0]


NOT_PLACED = (
    "(recorded and checked by the gate, NOT placed: hl-guard places no stop orders, "
    "so you or the client must place them)"
)


def test_approve_says_the_stop_loss_is_not_placed(config: Path) -> None:
    propose(config)
    shown: list[str] = []

    def confirm(lines: list[str]) -> bool:
        shown.extend(lines)
        return True

    code, lines = decide(config, "a1b2c3d4e5f6", True, confirm)

    assert code == 0
    words = "buy 0.0002 BTC limit 81171 Gtc at 1x isolated, stop-loss 80000"
    assert f"{words} {NOT_PLACED}" in shown[0]
    assert lines[0].startswith(f"approved a1b2c3d4e5f6: {words} {NOT_PLACED}, ")


def test_the_summary_names_both_prices_as_not_placed() -> None:
    order = {**ORDER, "take_profit_px": "84000"}

    assert model.summary(order) == (
        "buy 0.0002 BTC limit 81171 Gtc at 1x isolated, stop-loss 80000, take-profit 84000 "
        + NOT_PLACED
    )


def test_approve_with_no_server_running_is_refused_and_says_why(idle: Path) -> None:
    propose(idle)

    code, lines = decide(idle, "a1b2c3d4e5f6", True)

    assert code == 1
    lock = idle.parent / "data" / "serve.lock"
    assert lines == [
        f"approve refused: no hl-guard serve with write tools holds {lock}; start it first, "
        "since only a running server places the order"
    ]
    assert approvals(idle) == []


def test_approve_after_the_server_exited_is_refused(idle: Path) -> None:
    lock = idle.parent / "data" / "serve.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.close(fd)
    propose(idle)

    code, lines = decide(idle, "a1b2c3d4e5f6", True)

    assert code == 1 and "no hl-guard serve with write tools holds" in lines[0]
    assert approvals(idle) == []


def test_approve_says_so_when_the_server_exits_before_the_approval_lands(idle: Path) -> None:
    lock = idle.parent / "data" / "serve.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    propose(idle, ttl_s=300)

    def server_exits(shown: list[str]) -> bool:
        os.close(fd)
        return True

    code, lines = decide(idle, "a1b2c3d4e5f6", True, server_exits)

    assert code == 0 and lines[0].startswith("approved a1b2c3d4e5f6")
    assert lines[-1] == (
        f"no hl-guard serve holds {lock} now: a serve that starts within 300 s runs the gate "
        "again and places it if it passes; after that it expires"
    )
    assert len(approvals(idle)) == 1


def test_reject_with_no_server_running_is_written(idle: Path) -> None:
    propose(idle)

    assert decide(idle, "a1b2c3d4e5f6", False)[0] == 0
    assert approvals(idle)[0]["approved"] is False


def _forged(config: Path, order: dict[str, Any], pid: str = "f0f0f0f0f0f0") -> None:
    digest = proposal_hash(pid, "testnet", "0x" + "ab" * 20, order)
    led = ledger(config)
    led.append(Kind.PROPOSAL, {"id": pid, "tool": "propose_order", "order": order, "hash": digest})
    expires = time.time_ns() // 1_000_000 + 60_000
    pending = {"id": pid, "outcome": "pending", "rule": "", "detail": "", "expires_ms": expires}
    led.append(Kind.DECISION, pending | {"ttl_s": 120})


def test_a_pending_proposal_without_a_recorded_ttl_is_refused(config: Path) -> None:
    digest = proposal_hash("a1b2c3d4e5f6", "testnet", "0x" + "ab" * 20, ORDER)
    led = ledger(config)
    led.append(
        Kind.PROPOSAL,
        {"id": "a1b2c3d4e5f6", "tool": "propose_order", "order": ORDER, "hash": digest},
    )
    expires = time.time_ns() // 1_000_000 + 60_000
    led.append(
        Kind.DECISION,
        {
            "id": "a1b2c3d4e5f6",
            "outcome": "pending",
            "rule": "",
            "detail": "",
            "expires_ms": expires,
        },
    )

    code, lines = decide(config, "a1b2c3d4e5f6", True)

    assert (code, lines) == (
        1,
        ["approve refused: proposal a1b2c3d4e5f6 has no recorded approval ttl"],
    )
    assert approvals(config) == []


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        ({"size": "0.0024"}, "per_order_usd_max"),
        ({"coin": "DOGE"}, "coins"),
        ({"stop_loss_px": None}, "stop_loss_required"),
        ({"leverage": 3}, "leverage_max"),
        ({"size": "0.0001"}, "min_notional"),
        ({"size": "lots"}, "bad_order"),
    ],
)
def test_approve_of_a_rechained_order_outside_the_policy_is_refused(
    config: Path, change: dict[str, Any], rule: str
) -> None:
    _forged(config, {**ORDER, **change})

    code, lines = decide(config, "f0f0f0f0f0f0", True)

    assert code == 1
    assert lines[0].startswith(f"approve refused: the order fails the policy offline: {rule}: ")
    assert approvals(config) == []


def test_an_approval_naming_another_hash_leaves_the_proposal_pending(config: Path) -> None:
    propose(config)
    ledger(config).append(Kind.APPROVAL, {"id": "a1b2c3d4e5f6", "hash": "0" * 64, "approved": True})

    proposal = model.fold(ledger(config).records())["a1b2c3d4e5f6"]

    assert proposal.status is model.Status.PENDING
