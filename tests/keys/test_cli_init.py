from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount

from hl_guard.cli import init as init_module
from hl_guard.cli.__main__ import main
from hl_guard.cli.init import Result, init, revoke, status
from hl_guard.keys.agent_key import (
    AGENT_NAME,
    LATE_SIGNATURE_MS,
    Agent,
    KeySlot,
    Record,
    Saved,
    load,
    save,
    stage_key,
    write_key,
)
from hl_guard.keys.approve_page import DAY_MS, DEFAULT_VALID_DAYS, expiry_text

from .conftest import FakeExchange, MakeConfig
from .page_client import embedded_typed_data, get, post, result, sending


class Run:
    def __init__(self, target: Callable[..., Result], *args: Any, **kwargs: Any) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.result: Result | None = None

        def run() -> None:
            self.result = target(*args, announce=self.lines.put, **kwargs)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def url(self) -> str:
        while True:
            line = self.lines.get(timeout=10)
            if line.startswith("http://"):
                return line

    def finish(self) -> Result:
        self.thread.join(10)
        assert not self.thread.is_alive() and self.result is not None
        return self.result


def approve_in_browser(url: str, wallet: LocalAccount, answer: object = None) -> dict[str, Any]:
    status, _, body = get(url)
    assert status == 200
    assert sending(url)
    td = embedded_typed_data(body)
    signable = encode_typed_data(full_message=td)
    sig = wallet.sign_message(signable)
    assert Account.recover_message(signable, signature=sig.signature) == wallet.address
    if answer is None:
        found = re.search(r"const exchangeUrl = (.*);\n", body)
        assert found
        signed = {
            "action": td["message"] | {"type": "approveAgent"},
            "nonce": td["message"]["nonce"],
            "signature": {"r": hex(sig.r), "s": hex(sig.s), "v": sig.v},
            "vaultAddress": None,
        }
        req = urllib.request.Request(
            json.loads(found.group(1)),
            data=json.dumps(signed).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            answer = json.loads(resp.read())
    assert post(url.replace("/approve?", "/result?"), result(wallet.address, answer)) == 200
    return td


def created(slot: KeySlot) -> Saved:
    return save(slot, Record.first("testnet", write_key(slot.key_path, overwrite=False)))


def approved(slot: KeySlot, until: int, held: tuple[str, ...] = ()) -> Saved:
    record = created(slot)
    address = record.current.address
    return save(slot, Record("testnet", Agent(address, until), None, (address, *held)))


def stage_replacement(slot: KeySlot, record: Saved, until: int) -> Agent:
    record, staged = stage_key(slot, record)
    save(slot, record.confirmed(staged.address, until))
    return Agent(staged.address, until)


def stopped(address: str) -> str:
    return (
        f"a revoke stopped after the exchange approved the replacement {address}; "
        "run hl-guard revoke to finish it"
    )


def held(config: Path) -> tuple[str, ...]:
    record = load(key_slot(config), "testnet")
    assert record is not None
    return record.held


def key_slot(config: Path) -> KeySlot:
    return KeySlot(config.parent / "data" / "agent.key")


def key_address_of(config: Path) -> str:
    address: str = Account.from_key(secret_of(config)).address
    return address


def secret_of(config: Path) -> str:
    return key_slot(config).key_path.read_text().strip()


def no_secret(secret: str, *texts: str) -> bool:
    return all(secret not in t and secret.removeprefix("0x") not in t for t in texts)


@pytest.fixture()
def wallet() -> LocalAccount:
    created: LocalAccount = Account.create()
    return created


def test_init_approves_with_the_wallet_and_status_shows_the_expiry(
    make_config: MakeConfig, wallet: LocalAccount, capsys: pytest.CaptureFixture[str]
) -> None:
    config = make_config(wallet.address)
    run = Run(init, config, 30, "127.0.0.1", 0, None)
    url = run.url()
    secret = secret_of(config)
    page_html = get(url)[2]
    td = approve_in_browser(url, wallet)
    code, lines = run.finish()
    valid_until = int(td["message"]["agentName"].rsplit(" ", 1)[1])
    address = Account.from_key(secret).address
    assert td["message"]["agentAddress"] == address
    assert td["message"]["agentName"].startswith(f"{AGENT_NAME} valid_until ")
    assert (code, lines[0]) == (0, f"agent approved, expires {expiry_text(valid_until)}")
    assert status(config, None) == (
        0,
        [
            f"agent approved, expires {expiry_text(valid_until)}",
            f"agent {address} on testnet",
            f"key {key_slot(config).key_path} mode 0600",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        main(["status", "--config", str(config)])
    assert exited.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"agent approved, expires {expiry_text(valid_until)}\n")
    announced = list(run.lines.queue)
    assert no_secret(secret, page_html, out, *lines, *announced)
    assert no_secret(secret, key_slot(config).record_path.read_text())


def test_init_on_an_approved_agent_serves_no_page(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot = key_slot(config)
    until = int(time.time() * 1000) + DAY_MS
    record = approved(slot, until).current
    exchange.list_agent(wallet.address, AGENT_NAME, record.address, until)
    announced: list[str] = []
    code, lines = init(config, 30, "127.0.0.1", 0, None, announce=announced.append)
    assert (code, announced) == (0, [])
    assert lines == [
        f"agent approved, expires {expiry_text(until)}",
        f"agent {record.address} on testnet",
        "run hl-guard revoke to replace it with a new agent",
    ]


def test_init_reapproves_the_same_key_once_expired(
    make_config: MakeConfig, wallet: LocalAccount
) -> None:
    config = make_config(wallet.address)
    slot = key_slot(config)
    record = approved(slot, 1_000).current
    assert status(config, None)[1][0] == f"agent expired {expiry_text(1_000)}; run hl-guard init"
    run = Run(init, config, 7, "127.0.0.1", 0, None)
    td = approve_in_browser(run.url(), wallet)
    assert run.finish()[0] == 0
    assert td["message"]["agentAddress"] == record.address
    assert status(config, None)[0] == 0


@pytest.mark.parametrize(
    ("days", "host", "port", "match"),
    [(200, "127.0.0.1", 0, "1 to 180"), (0, "127.0.0.1", 0, "always sets an expiry")]
    + [(30, host, 0, "loopback") for host in ("0.0.0.0", "192.168.1.2", "localhost")]
    + [(30, "127.0.0.1", port, "0 to 65535") for port in (70000, -1)],
)
def test_init_refuses_bad_input_before_creating_a_key(
    make_config: MakeConfig, wallet: LocalAccount, days: int, host: str, port: int, match: str
) -> None:
    config = make_config(wallet.address)
    for verb in (init, revoke):
        code, lines = verb(config, days, host, port, None, announce=pytest.fail)
        assert code == 2 and match in lines[0]
    assert sorted(p.name for p in (config.parent / "data").iterdir()) == []


def test_init_needs_agent_key_file_in_the_config(
    make_config: MakeConfig, wallet: LocalAccount
) -> None:
    config = make_config(wallet.address, key_line=False)
    data = config.parent / "data"
    code, lines = init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert code == 2
    assert lines[0].endswith(f"add this line to it: agent_key_file: {data / 'agent.key'}")
    assert status(config, None)[0] == 2


def test_init_refuses_mainnet_without_the_flag(
    make_config: MakeConfig, wallet: LocalAccount
) -> None:
    config = make_config(wallet.address)
    mode = config.parent / "data" / "mode"
    mode.write_text("mainnet\n")
    mode.chmod(0o600)
    code, lines = init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert code == 2 and "--expect-network mainnet" in lines[0]
    assert not key_slot(config).key_path.exists()


def test_init_follows_the_policy_network(make_config: MakeConfig, wallet: LocalAccount) -> None:
    config = make_config(wallet.address)
    policy = config.parent / "policy.yaml"
    policy.write_text(policy.read_text().replace("network: testnet", "network: mainnet"))
    for verb in (init, revoke):
        code, lines = verb(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
        assert code == 2 and "the policy says mainnet but the data dir mode is testnet" in lines[0]
    assert status(config, None)[0] == 2
    assert sorted(p.name for p in (config.parent / "data").iterdir()) == []


def test_init_refuses_an_unreadable_policy(make_config: MakeConfig, wallet: LocalAccount) -> None:
    config = make_config(wallet.address)
    (config.parent / "policy.yaml").chmod(0o666)
    code, lines = init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert code == 2 and "policy" in lines[0]
    assert not key_slot(config).key_path.exists()


def test_a_refused_approval_keeps_the_key_for_the_next_init(
    make_config: MakeConfig, wallet: LocalAccount
) -> None:
    config = make_config(wallet.address)
    run = Run(init, config, 30, "127.0.0.1", 0, None)
    url = run.url()
    approve_in_browser(url, wallet, {"status": "err", "response": "Extra agent already used."})
    code, lines = run.finish()
    assert code == 1
    assert lines == [
        'approval refused: the exchange answered {"status": "err", "response": '
        '"Extra agent already used."}',
        "the exchange lists no other agent for the master wallet, so a full agent list is not "
        "why it refused",
        "the key stays; run hl-guard init again once the cause is fixed",
    ]
    first = secret_of(config)
    assert status(config, None)[1][0] == "agent not approved; run hl-guard init"
    rerun = Run(init, config, 30, "127.0.0.1", 0, None)
    approve_in_browser(rerun.url(), wallet)
    assert rerun.finish()[0] == 0
    assert secret_of(config) == first


def test_init_times_out(make_config: MakeConfig, wallet: LocalAccount) -> None:
    config = make_config(wallet.address)
    code, lines = init(config, 30, "127.0.0.1", 0, None, announce=lambda _: None, timeout_s=0.2)
    assert (code, lines) == (
        1,
        ["no answer from the page within 0.2 s", "the key stays; run hl-guard init again"],
    )
    assert key_slot(config).key_path.exists()


def test_status_without_a_key_writes_nothing(make_config: MakeConfig, wallet: LocalAccount) -> None:
    config = make_config(wallet.address)
    before = sorted(p.name for p in config.parent.rglob("*"))
    slot = key_slot(config)
    assert status(config, None) == (1, [f"no agent key at {slot.key_path}; run hl-guard init"])
    assert sorted(p.name for p in config.parent.rglob("*")) == before


def test_revoke_needs_an_agent(make_config: MakeConfig, wallet: LocalAccount) -> None:
    config = make_config(wallet.address)
    code, lines = revoke(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert (code, lines) == (1, ["no agent to revoke; run hl-guard init"])
    assert sorted(p.name for p in (config.parent / "data").iterdir()) == []


def test_revoke_replaces_the_agent_under_the_same_name(
    make_config: MakeConfig, wallet: LocalAccount
) -> None:
    config = make_config(wallet.address)
    first = Run(init, config, 30, "127.0.0.1", 0, None)
    approve_in_browser(first.url(), wallet)
    assert first.finish()[0] == 0
    slot = key_slot(config)
    old = Account.from_key(secret_of(config)).address

    replace = Run(revoke, config, 30, "127.0.0.1", 0, None)
    url = replace.url()
    staged_secret = slot.staged_path.read_text().strip()
    td = approve_in_browser(url, wallet)
    code, lines = replace.finish()

    new = Account.from_key(secret_of(config)).address
    valid_until = int(td["message"]["agentName"].rsplit(" ", 1)[1])
    assert new != old and secret_of(config) == staged_secret
    assert td["message"]["agentAddress"] == new
    assert td["message"]["agentName"].startswith(f"{AGENT_NAME} valid_until ")
    assert (code, lines) == (
        0,
        [
            f"agent replaced: {old} -> {new}, expires {expiry_text(valid_until)}; "
            "restart hl-guard serve to trade with the new agent"
        ],
    )
    assert json.loads(slot.record_path.read_text())["current"]["address"] == new
    assert not slot.staged_path.exists()
    assert f"agent {new} on testnet" in status(config, None)[1]
    assert no_secret(staged_secret, *lines, *replace.lines.queue)


def test_a_refused_revoke_keeps_the_staged_key_for_the_rerun(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot = key_slot(config)
    until = int(time.time() * 1000) + DAY_MS
    record = approved(slot, until).current
    exchange.list_agent(wallet.address, AGENT_NAME, record.address, until)
    code, lines = revoke(config, 30, "127.0.0.1", 0, None, announce=lambda _: None, timeout_s=0.1)
    assert code == 1 and lines[-1] == "the replacement key stays; run hl-guard revoke again"
    staged = slot.staged_path.read_text()
    code, lines = status(config, None)
    assert code == 0 and lines[-1] == "a replacement agent waits for approval; run hl-guard revoke"

    rerun = Run(revoke, config, 30, "127.0.0.1", 0, None)
    td = approve_in_browser(rerun.url(), wallet)
    assert rerun.finish()[0] == 0
    assert td["message"]["agentAddress"] == Account.from_key(staged).address
    assert slot.key_path.read_text() == staged


def test_init_finishes_a_promote_cut_between_its_two_moves(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot, until = key_slot(config), int(time.time() * 1000) + DAY_MS
    old = approved(slot, until)
    new = stage_replacement(slot, old, until + DAY_MS)
    exchange.list_agent(wallet.address, AGENT_NAME, new.address, until + DAY_MS)
    os.replace(slot.staged_path, slot.key_path)
    code, lines = status(config, None)
    assert code == 1 and stopped(new.address) in lines
    code, lines = init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert (code, lines[0]) == (0, f"agent approved, expires {expiry_text(until + DAY_MS)}")
    assert status(config, None)[1][1] == f"agent {new.address} on testnet"


@pytest.mark.parametrize("verb", ["init", "revoke"])
def test_ctrl_c_keeps_the_key(
    verb: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(*args: object) -> Result:
        raise KeyboardInterrupt

    monkeypatch.setattr(init_module, verb, interrupted)
    with pytest.raises(SystemExit) as exited:
        main([verb, "--config", "unused.yaml"])
    assert exited.value.code == 1
    assert (
        capsys.readouterr().out == f"{verb} cancelled: the key stays; run hl-guard {verb} again\n"
    )


def test_ctrl_c_while_waiting_closes_the_page_and_keeps_the_key(
    make_config: MakeConfig, wallet: LocalAccount
) -> None:
    config = make_config(wallet.address)
    urls: list[str] = []

    def press_ctrl_c(line: str) -> None:
        if line.startswith("http://"):
            urls.append(line)
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        init(config, 30, "127.0.0.1", 0, None, announce=press_ctrl_c)
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(urls[0], timeout=2)
    assert key_slot(config).key_path.exists()


def test_revoke_promotes_a_replacement_the_exchange_already_approved(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot, until = key_slot(config), int(time.time() * 1000) + DAY_MS
    old = approved(slot, until)
    new = stage_replacement(slot, old, until + DAY_MS)
    exchange.list_agent(wallet.address, AGENT_NAME, new.address, until + DAY_MS)
    code, lines = revoke(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert (code, lines) == (
        0,
        [
            f"agent replaced: {old.current.address} -> {new.address}, expires "
            f"{expiry_text(until + DAY_MS)}; restart hl-guard serve to trade with the new agent"
        ],
    )


@pytest.mark.parametrize("verb", ["init", "revoke"])
def test_the_days_flag_defaults_to_the_page_default(
    verb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int] = []

    def record_days(config: Path, days: int, *args: object) -> Result:
        seen.append(days)
        return 0, []

    monkeypatch.setattr(init_module, verb, record_days)
    with pytest.raises(SystemExit):
        main([verb, "--config", "unused.yaml"])
    assert seen == [DEFAULT_VALID_DAYS]


@pytest.mark.parametrize("port", ["70000", "-1", "x"])
def test_the_port_flag_takes_0_to_65535(
    port: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(init_module, "init", pytest.fail)
    with pytest.raises(SystemExit) as exited:
        main(["init", "--config", "unused.yaml", "--port", port])
    assert exited.value.code == 2
    assert "0 to 65535" in capsys.readouterr().err


def test_status_reports_a_key_that_vanishes_mid_read(
    make_config: MakeConfig, wallet: LocalAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(wallet.address)
    slot = key_slot(config)
    approved(slot, int(time.time() * 1000) + DAY_MS)

    def vanished(slot: KeySlot) -> str:
        raise FileNotFoundError(2, "No such file or directory", str(slot.key_path))

    monkeypatch.setattr(init_module, "key_mode", vanished)
    code, lines = status(config, None)
    assert code == 2 and "No such file" in lines[0]


def no_sleep(seconds: float) -> None:
    pass


def now_ms() -> int:
    return int(time.time() * 1000)


def test_a_forged_ok_the_exchange_does_not_list_records_nothing(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    run = Run(init, config, 30, "127.0.0.1", 0, None, sleep=no_sleep)
    approve_in_browser(run.url(), wallet, {"status": "ok", "response": {"type": "default"}})
    code, lines = run.finish()
    address = Account.from_key(secret_of(config)).address
    assert (code, lines) == (
        1,
        [
            f"the exchange does not list the agent {address}; the approval was not recorded",
            "run hl-guard init again",
        ],
    )
    assert exchange.reads == 5
    assert status(config, None)[1][0] == "agent not approved; run hl-guard init"


def test_an_agent_the_exchange_lists_as_expired_is_not_recorded(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    exchange.until_shift_ms = -31 * DAY_MS
    run = Run(init, config, 30, "127.0.0.1", 0, None, sleep=no_sleep)
    approve_in_browser(run.url(), wallet)
    code, lines = run.finish()
    assert code == 1 and "the approval was not recorded" in lines[0]
    code, lines = status(config, None)
    assert code == 1 and lines[0].endswith("has not recorded its approval; run hl-guard init")


def test_the_polls_wait_a_second_between_reads(
    make_config: MakeConfig, wallet: LocalAccount
) -> None:
    config = make_config(wallet.address)
    slept: list[float] = []
    run = Run(init, config, 30, "127.0.0.1", 0, None, sleep=slept.append)
    approve_in_browser(run.url(), wallet, {"status": "ok", "response": {"type": "default"}})
    assert run.finish()[0] == 1
    assert slept == [1.0] * 4


def test_init_records_the_expiry_the_exchange_lists(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    exchange.until_shift_ms = -3_600_000
    run = Run(init, config, 30, "127.0.0.1", 0, None)
    td = approve_in_browser(run.url(), wallet)
    listed = int(td["message"]["agentName"].rsplit(" ", 1)[1]) - 3_600_000
    assert run.finish()[1][0] == f"agent approved, expires {expiry_text(listed)}"
    record = json.loads(key_slot(config).record_path.read_text())
    assert record["current"]["valid_until_ms"] == listed


def test_a_failed_confirmation_read_records_nothing(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    run = Run(init, config, 30, "127.0.0.1", 0, None, sleep=no_sleep)
    url = run.url()
    exchange.failed_reads = 1
    approve_in_browser(url, wallet)
    code, lines = run.finish()
    assert code == 1 and "cannot read the exchange's agent list" in lines[0]
    assert lines[-1] == "run hl-guard init again"
    code, lines = status(config, None)
    assert code == 1 and lines[0].endswith(
        "but hl-guard has not recorded its approval; run hl-guard init"
    )
    assert init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)[0] == 0


def test_init_converges_from_a_crash_after_the_exchange_approved(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    record = created(key_slot(config)).current
    until = now_ms() + DAY_MS
    exchange.list_agent(wallet.address, AGENT_NAME, record.address, until)
    code, lines = init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert (code, lines) == (
        0,
        [f"agent approved, expires {expiry_text(until)}", f"agent {record.address} on testnet"],
    )
    assert exchange.reads == 1
    assert status(config, None)[0] == 0


def test_init_serves_the_page_when_the_first_read_fails(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    record = created(key_slot(config)).current
    exchange.failed_reads = 1
    run = Run(init, config, 30, "127.0.0.1", 0, None)
    td = approve_in_browser(run.url(), wallet)
    assert run.finish()[0] == 0
    assert td["message"]["agentAddress"] == record.address
    assert exchange.reads == 2


def test_revoke_exits_1_while_the_exchange_still_lists_the_old_agent(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    first = Run(init, config, 30, "127.0.0.1", 0, None)
    approve_in_browser(first.url(), wallet)
    assert first.finish()[0] == 0
    old = Account.from_key(secret_of(config)).address
    user = wallet.address.lower()
    exchange.agents[(user, "legacy")] = exchange.agents.pop((user, AGENT_NAME))
    old_until = exchange.agents[(user, "legacy")][1]

    replace = Run(revoke, config, 30, "127.0.0.1", 0, None)
    approve_in_browser(replace.url(), wallet)
    code, lines = replace.finish()
    new = Account.from_key(secret_of(config)).address
    assert new != old
    assert (code, lines) == (1, [still_listed(old, "legacy", old_until)])
    assert f"agent {new} on testnet" in status(config, None)[1]


def test_a_revoke_rerun_after_a_cut_promote_reports_the_replacement(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot, until = key_slot(config), now_ms() + DAY_MS
    old = approved(slot, until)
    new = stage_replacement(slot, old, until + DAY_MS)
    os.replace(slot.staged_path, slot.key_path)
    exchange.list_agent(wallet.address, AGENT_NAME, new.address, until + DAY_MS)
    code, lines = revoke(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert (code, lines) == (
        0,
        [
            f"agent replaced: {old.current.address} -> {new.address}, expires "
            f"{expiry_text(until + DAY_MS)}; restart hl-guard serve to trade with the new agent"
        ],
    )
    assert not os.path.lexists(slot.staged_path)
    assert status(config, None)[1][1] == f"agent {new.address} on testnet"


@pytest.mark.parametrize("exchange_says", ["nothing", "error"])
def test_a_revoke_rerun_that_cannot_confirm_the_new_agent_exits_1(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, exchange_says: str
) -> None:
    config = make_config(wallet.address)
    slot, until = key_slot(config), now_ms() + DAY_MS
    old = approved(slot, until)
    new = stage_replacement(slot, old, until + DAY_MS)
    os.replace(slot.staged_path, slot.key_path)
    exchange.failed_reads = 1 if exchange_says == "error" else 0
    code, lines = revoke(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert code == 1
    if exchange_says == "nothing":
        assert lines == [
            f"the exchange does not list the agent {new.address}; it was removed or replaced, "
            "so run hl-guard revoke for a new agent"
        ]
    else:
        assert "cannot read the exchange's agent list" in lines[0]
        assert lines[0].endswith(
            f"{new.address} replaced {old.current.address} here but the exchange was not checked"
        )
    assert status(config, None)[1][1] == f"agent {new.address} on testnet"


@pytest.mark.parametrize(
    "foreign",
    [
        {"name": "manual", "address": "0x" + "99" * 20, "validUntil": None},
        {"name": "manual", "address": "0x99", "validUntil": 1},
        ["manual", "0x" + "99" * 20, 1],
    ],
)
def test_a_malformed_row_for_another_agent_does_not_block_the_approval(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, foreign: object
) -> None:
    config = make_config(wallet.address)
    exchange.extra_rows.append(foreign)
    run = Run(init, config, 30, "127.0.0.1", 0, None, sleep=no_sleep)
    td = approve_in_browser(run.url(), wallet)
    code, lines = run.finish()
    valid_until = int(td["message"]["agentName"].rsplit(" ", 1)[1])
    assert (code, lines[0]) == (0, f"agent approved, expires {expiry_text(valid_until)}")


@pytest.mark.parametrize("shift_ms", [1, 3650 * DAY_MS])
def test_a_listing_past_the_signed_expiry_is_not_recorded(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, shift_ms: int
) -> None:
    config = make_config(wallet.address)
    exchange.until_shift_ms = shift_ms
    run = Run(init, config, 30, "127.0.0.1", 0, None, sleep=no_sleep)
    td = approve_in_browser(run.url(), wallet)
    signed = int(td["message"]["agentName"].rsplit(" ", 1)[1])
    code, lines = run.finish()
    address = Account.from_key(secret_of(config)).address
    assert code == 1
    assert lines[0] == (
        f"the exchange lists the agent {address} until {expiry_text(signed + shift_ms)}, "
        f"past the {expiry_text(signed)} it was signed for, so hl-guard will not use its key "
        "and reports the agent while the exchange lists it"
    )
    assert status(config, None)[1][0] == (
        f"hl-guard will not use the key of the agent {address}, which the exchange listed past "
        "the expiry hl-guard signs; run hl-guard revoke for a new key"
    )


@pytest.mark.parametrize(("extra_ms", "recorded"), [(0, True), (1, False)])
def test_a_pre_read_listing_is_recorded_only_within_the_180_day_cap(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    extra_ms: int,
    recorded: bool,
) -> None:
    config = make_config(wallet.address)
    record = created(key_slot(config)).current
    now = now_ms()
    exchange.list_agent(wallet.address, "elsewhere", record.address, now + 180 * DAY_MS + extra_ms)
    code, lines = init(config, 30, "127.0.0.1", 0, None, now_ms=lambda: now, announce=pytest.fail)
    assert code == (0 if recorded else 1)
    if not recorded:
        assert lines[0].endswith(
            "past the 180-day cap hl-guard signs, so hl-guard will not use its key and reports "
            "the agent while the exchange lists it"
        )
    assert (status(config, None, now_ms=lambda: now)[0] == 0) is recorded


def still_listed(address: str, name: str, until: int) -> str:
    return (
        f"the exchange still lists the old agent {address} until {expiry_text(until)}, so its "
        f"key can still trade until then; approving an agent named {name!r} from the master "
        "wallet replaces it"
    )


def test_every_revoke_names_the_original_agent_while_the_exchange_lists_it(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    first = Run(init, config, 30, "127.0.0.1", 0, None)
    approve_in_browser(first.url(), wallet)
    assert first.finish()[0] == 0
    original = Account.from_key(secret_of(config)).address
    user = wallet.address.lower()
    exchange.agents[(user, "legacy")] = exchange.agents.pop((user, AGENT_NAME))
    original_until = exchange.agents[(user, "legacy")][1]
    expected = still_listed(original, "legacy", original_until)

    for _ in range(2):
        replace = Run(revoke, config, 30, "127.0.0.1", 0, None, sleep=no_sleep)
        approve_in_browser(replace.url(), wallet)
        code, lines = replace.finish()
        assert code == 1 and not any("agent replaced" in line for line in lines)
        assert lines == [expected]
        code, lines = status(config, None)
        assert code == 1 and lines[0] == expected

    del exchange.agents[(user, "legacy")]
    code, lines = status(config, None)
    assert code == 0 and lines[0].startswith("agent approved, expires ")


def test_a_revoke_cut_before_its_promote_is_reported_until_a_rerun_finishes_it(
    make_config: MakeConfig, wallet: LocalAccount, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(wallet.address)
    first = Run(init, config, 30, "127.0.0.1", 0, None)
    approve_in_browser(first.url(), wallet)
    assert first.finish()[0] == 0
    old = Account.from_key(secret_of(config)).address

    def cut(*args: object) -> None:
        raise OSError("cut before the promote")

    with monkeypatch.context() as patched:
        patched.setattr(init_module, "promote", cut)
        replace = Run(revoke, config, 30, "127.0.0.1", 0, None, sleep=no_sleep)
        td = approve_in_browser(replace.url(), wallet)
        assert replace.finish()[0] == 1
    new = td["message"]["agentAddress"]
    new_until = int(td["message"]["agentName"].rsplit(" ", 1)[1])
    stopped = (
        f"a revoke stopped after the exchange approved the replacement {new}; "
        "run hl-guard revoke to finish it"
    )
    gone = (
        f"the exchange does not list the agent {old}; it was removed or replaced, "
        "so run hl-guard revoke for a new agent"
    )

    assert init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail) == (
        1,
        [gone, stopped, f"agent {old} on testnet"],
    )
    code, lines = status(config, None)
    assert code == 1 and lines[0] == gone and stopped in lines
    assert not any("waits for approval" in line for line in lines)

    assert revoke(config, 30, "127.0.0.1", 0, None, announce=pytest.fail) == (
        0,
        [
            f"agent replaced: {old} -> {new}, expires {expiry_text(new_until)}; "
            "restart hl-guard serve to trade with the new agent"
        ],
    )
    assert status(config, None)[0] == 0


def test_status_reports_an_approved_agent_the_exchange_no_longer_lists(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    first = Run(init, config, 30, "127.0.0.1", 0, None)
    approve_in_browser(first.url(), wallet)
    assert first.finish()[0] == 0
    address = Account.from_key(secret_of(config)).address
    exchange.agents.clear()
    code, lines = status(config, None)
    assert (code, lines[0]) == (
        1,
        f"the exchange does not list the agent {address}; it was removed or replaced, "
        "so run hl-guard revoke for a new agent",
    )
    assert init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)[0] == 1


def test_status_says_when_the_exchange_was_not_checked(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    first = Run(init, config, 30, "127.0.0.1", 0, None)
    approve_in_browser(first.url(), wallet)
    assert first.finish()[0] == 0
    exchange.failed_reads = 1
    code, lines = status(config, None)
    assert code == 1 and lines[0].startswith("cannot read the exchange's agent list")
    assert lines[0].endswith("; the exchange was not checked")
    assert lines[1].startswith("the current agent stays approved, expires ")


def test_revoke_drops_only_the_superseded_agents_the_exchange_no_longer_lists(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot, now = key_slot(config), now_ms()
    gone, listed = "0x" + "11" * 20, "0x" + "22" * 20
    current = approved(slot, now + 2 * DAY_MS, (gone, listed)).current
    exchange.list_agent(wallet.address, AGENT_NAME, current.address, now + 2 * DAY_MS)
    exchange.list_agent(wallet.address, "legacy", listed, now - DAY_MS)
    code, _ = approve_run(revoke, config, wallet, now_ms=lambda: now)
    assert code == 1
    assert held(config) == (listed, key_address_of(config))
    del exchange.agents[(wallet.address.lower(), "legacy")]
    exchange.extra_rows.append({"address": listed, "validUntil": "unreadable"})
    unchecked = approve_run(revoke, config, wallet, now_ms=lambda: now)
    assert unchecked[0] == 1 and unchecked[1][0].startswith(
        "the exchange's agent list is malformed"
    )
    assert held(config)[0] == listed
    exchange.extra_rows.clear()
    assert (
        revoke(config, 30, "127.0.0.1", 0, None, announce=pytest.fail, now_ms=lambda: now)[0] == 0
    )
    assert len(held(config)) == 2 and held(config)[-1] == key_address_of(config)
    later = now + LATE_SIGNATURE_MS
    assert approve_run(revoke, config, wallet, now_ms=lambda: later)[0] == 0
    assert held(config) == (key_address_of(config),)


def test_revoking_an_unapproved_key_holds_it_until_the_exchange_stops_listing_it(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot, now = key_slot(config), now_ms()
    current = created(slot).current
    exchange.list_agent(wallet.address, "elsewhere", current.address, now + DAY_MS)
    code, lines = approve_run(revoke, config, wallet, now_ms=lambda: now)
    assert names_live(current.address, (code, lines))
    assert held(config) == (current.address, key_address_of(config))


def test_a_replacement_the_exchange_approved_is_reported_while_the_old_agent_is_listed(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot, until = key_slot(config), now_ms() + DAY_MS
    old = approved(slot, until)
    exchange.list_agent(wallet.address, "legacy", old.current.address, until)
    new = stage_replacement(slot, old, until + DAY_MS)
    exchange.list_agent(wallet.address, AGENT_NAME, new.address, until + DAY_MS)
    stopped = (
        f"a revoke stopped after the exchange approved the replacement {new.address}; "
        "run hl-guard revoke to finish it"
    )
    code, lines = init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)
    assert (code, lines) == (
        1,
        [
            stopped,
            f"the current agent stays approved, expires {expiry_text(until)}",
            f"agent {old.current.address} on testnet",
        ],
    )
    code, lines = status(config, None)
    assert code == 1 and lines[0] == stopped


def test_init_reapproving_an_expired_key_keeps_the_superseded_agents(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot, now = key_slot(config), now_ms()
    old = "0x" + "33" * 20
    exchange.list_agent(wallet.address, "legacy", old, now + DAY_MS)
    approved(slot, 1_000, (old,))
    code, lines = approve_run(init, config, wallet)
    expected = still_listed(old, "legacy", now + DAY_MS)
    assert code == 1 and lines[0] == expected
    assert lines[1].startswith("the current agent stays approved, expires ")
    assert old in held(config)
    code, lines = status(config, None)
    assert code == 1 and lines[0] == expected


HOUR_MS = 3_600_000


def approve_run(
    verb: Callable[..., Result], config: Path, wallet: LocalAccount, **kw: Any
) -> Result:
    run = Run(verb, config, kw.pop("days", 30), "127.0.0.1", 0, None, sleep=no_sleep, **kw)
    approve_in_browser(run.url(), wallet)
    return run.finish()


def names_live(address: str, result: Result) -> bool:
    code, lines = result
    return code == 1 and any(address in line and "still lists" in line for line in lines)


def rename_ours(exchange: FakeExchange, wallet: LocalAccount, name: str) -> None:
    user = wallet.address.lower()
    exchange.agents[(user, name)] = exchange.agents.pop((user, AGENT_NAME))


def test_a_clock_ahead_of_an_old_agents_listed_expiry_still_reports_it(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config, real = make_config(wallet.address), now_ms()
    first = approve_run(init, config, wallet, now_ms=lambda: real - 20 * HOUR_MS, days=1)
    assert first[0] == 0
    original = Account.from_key(secret_of(config)).address
    rename_ours(exchange, wallet, "legacy")
    assert names_live(original, approve_run(revoke, config, wallet, now_ms=lambda: real))
    ahead = approve_run(revoke, config, wallet, now_ms=lambda: real + 5 * HOUR_MS)
    assert names_live(original, ahead)
    assert names_live(original, status(config, None, now_ms=lambda: real))
    assert names_live(original, status(config, None, now_ms=lambda: real + 10 * DAY_MS))


def test_an_old_agent_recorded_with_an_earlier_expiry_than_listed_is_still_reported(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config, real = make_config(wallet.address), now_ms()
    first = approve_run(init, config, wallet, now_ms=lambda: real - 25 * HOUR_MS, days=1)
    assert first[0] == 0
    original = Account.from_key(secret_of(config)).address
    exchange.failed_reads = 2
    assert approve_run(init, config, wallet, now_ms=lambda: real)[0] == 1
    rename_ours(exchange, wallet, "legacy")
    for _ in range(2):
        assert names_live(original, approve_run(revoke, config, wallet, now_ms=lambda: real))
    assert names_live(original, status(config, None, now_ms=lambda: real))


@pytest.mark.parametrize(
    ("name", "remedy"),
    [
        ("legacy", "approving an agent named 'legacy' from the master wallet replaces it"),
        (
            AGENT_NAME,
            "the next hl-guard revoke approves an agent named 'hl-guard', which replaces it",
        ),
        (None, "approving a new unnamed agent from the master wallet replaces it"),
        ("", "approving a new unnamed agent from the master wallet replaces it"),
    ],
)
def test_the_remedy_for_a_live_old_agent_is_the_documented_replacement(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, name: str, remedy: str
) -> None:
    config = make_config(wallet.address)
    slot, now = key_slot(config), now_ms()
    old = "0x" + "44" * 20
    current = approved(slot, now + DAY_MS, (old,)).current
    exchange.list_agent(wallet.address, AGENT_NAME, current.address, now + DAY_MS)
    exchange.extra_rows.append({"name": name, "address": old, "validUntil": now + 2 * DAY_MS})
    code, lines = status(config, None)
    assert code == 1
    assert lines[0] == (
        f"the exchange still lists the old agent {old} until {expiry_text(now + 2 * DAY_MS)}, "
        f"so its key can still trade until then; {remedy}"
    )
    assert not any("API page" in line for line in lines)


def test_an_address_listed_twice_is_live_until_its_latest_row_under_every_name(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot, now = key_slot(config), now_ms()
    old = "0x" + "55" * 20
    current = approved(slot, now + DAY_MS, (old,)).current
    exchange.list_agent(wallet.address, AGENT_NAME, current.address, now + DAY_MS)
    exchange.extra_rows += [
        {"name": "legacy", "address": old, "validUntil": now + 10 * DAY_MS},
        {"name": "stale", "address": old, "validUntil": now - 1},
    ]
    code, lines = status(config, None)
    assert code == 1
    assert lines[0] == (
        f"the exchange still lists the old agent {old} until {expiry_text(now + 10 * DAY_MS)}, "
        "so its key can still trade until then; "
        "approving an agent named 'legacy' from the master wallet replaces it; "
        "approving an agent named 'stale' from the master wallet replaces it"
    )


def test_a_pre_read_checks_the_latest_of_two_rows_for_the_agent(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    record = created(key_slot(config)).current
    now = now_ms()
    exchange.extra_rows += [
        {"name": "far", "address": record.address, "validUntil": now + 181 * DAY_MS},
        {"name": "near", "address": record.address, "validUntil": now + DAY_MS},
    ]
    code, lines = init(config, 30, "127.0.0.1", 0, None, now_ms=lambda: now, announce=pytest.fail)
    assert code == 1
    assert lines[0].endswith(
        "past the 180-day cap hl-guard signs, so hl-guard will not use its key and reports "
        "the agent while the exchange lists it"
    )


def oversize_record(raw: dict[str, Any]) -> str:
    many = ["0x" + f"{i:040x}" for i in range(1500)]
    return json.dumps(raw | {"held": many})


@pytest.mark.parametrize(
    "spoil",
    [
        oversize_record,
        lambda raw: json.dumps(raw | {"held": [*raw["held"], "0xzz"]}),
        lambda raw: "{ torn",
    ],
    ids=["over 64 KiB", "one bad row", "not JSON"],
)
def test_a_spoiled_record_stops_every_command_and_is_left_as_it_is(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    spoil: Callable[[dict[str, Any]], str],
) -> None:
    config = make_config(wallet.address)
    slot, now = key_slot(config), now_ms()
    old = Account.create().address
    exchange.list_agent(wallet.address, "legacy", old, now + 10 * DAY_MS)
    current = approved(slot, now + DAY_MS, (old,)).current
    exchange.list_agent(wallet.address, AGENT_NAME, current.address, now + DAY_MS)
    text = spoil(json.loads(slot.record_path.read_text()))
    slot.record_path.write_text(text)
    for verb, (code, lines) in [
        ("status", status(config, None)),
        ("init", init(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)),
        ("revoke", revoke(config, 30, "127.0.0.1", 0, None, announce=pytest.fail)),
    ]:
        assert code == 2, (verb, lines)
        assert lines[0].startswith(f"{verb} refused: the agent record {slot.record_path} is ")
        assert lines[0].endswith("; restore it, because it lists every agent hl-guard held")
    assert slot.record_path.read_text() == text
    assert not slot.staged_path.exists()


def test_the_remedy_after_init_refuses_a_listing_past_the_signed_expiry_works(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    exchange.until_shift_ms = 1
    code, lines = approve_run(init, config, wallet)
    bad = Account.from_key(secret_of(config)).address
    assert code == 1
    assert lines[-1] == "run hl-guard revoke for a new key"
    exchange.until_shift_ms = 0
    code, lines = approve_run(revoke, config, wallet)
    assert code == 0 and lines[0].startswith(f"agent replaced: {bad} -> ")
    assert status(config, None)[0] == 0


def test_the_remedy_after_revoke_refuses_a_listing_past_the_signed_expiry_works(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    assert approve_run(init, config, wallet)[0] == 0
    exchange.until_shift_ms = 1
    code, lines = approve_run(revoke, config, wallet)
    bad = Account.from_key(key_slot(config).staged_path.read_text().strip()).address
    assert (code, lines[-1]) == (1, "run hl-guard revoke again for a new key")
    assert bad in held(config)
    exchange.until_shift_ms = 0
    assert approve_run(revoke, config, wallet)[0] == 0
    assert status(config, None)[0] == 0


def test_a_current_agent_listed_until_another_time_than_recorded_is_reported(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    slot, now = key_slot(config), now_ms()
    current = approved(slot, now + 2 * DAY_MS).current
    for listed in (now - HOUR_MS, now + 2 * DAY_MS + 1, now + 170 * DAY_MS):
        exchange.list_agent(wallet.address, AGENT_NAME, current.address, listed)
        code, lines = status(config, None, now_ms=lambda: now)
        assert code == 1 and lines[0] == (
            f"the exchange lists the agent {current.address} until {expiry_text(listed)}, but "
            f"hl-guard recorded {expiry_text(now + 2 * DAY_MS)}; another approval for it "
            "landed, so run hl-guard revoke for a new key"
        )
    exchange.list_agent(wallet.address, AGENT_NAME, current.address, now + 2 * DAY_MS)
    code, lines = status(config, None, now_ms=lambda: now)
    assert (code, lines[0]) == (0, f"agent approved, expires {expiry_text(now + 2 * DAY_MS)}")


@pytest.mark.parametrize(
    ("spoil", "trouble"),
    [
        (lambda path: path.unlink(), "does not hold the agent"),
        (lambda path: path.chmod(0o644), "has mode 0644; run chmod 600"),
    ],
    ids=["gone", "readable by others"],
)
def test_status_reports_a_key_it_cannot_use(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    spoil: Callable[[Path], None],
    trouble: str,
) -> None:
    config = make_config(wallet.address)
    assert approve_run(init, config, wallet)[0] == 0
    spoil(key_slot(config).key_path)
    code, lines = status(config, None)
    assert code == 1 and trouble in lines[0]
    assert lines[0].endswith("run hl-guard revoke to approve a new key in its place")
    assert lines[1].startswith("the current agent stays approved, expires ")


def test_revoke_replaces_a_staged_key_that_is_gone(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange
) -> None:
    config = make_config(wallet.address)
    assert approve_run(init, config, wallet)[0] == 0
    code, _ = revoke(config, 30, "127.0.0.1", 0, None, announce=lambda _: None, timeout_s=0.05)
    assert code == 1
    slot = key_slot(config)
    lost = Account.from_key(slot.staged_path.read_text().strip()).address
    slot.staged_path.unlink()
    code, lines = approve_run(revoke, config, wallet)
    assert code == 0 and lines[0].endswith("restart hl-guard serve to trade with the new agent")
    assert key_address_of(config) != lost
