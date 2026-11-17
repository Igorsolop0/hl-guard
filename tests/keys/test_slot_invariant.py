from __future__ import annotations

import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount

from hl_guard.cli.init import Result, init, revoke, status
from hl_guard.keys.agent_key import AGENT_NAME, RECORD_BYTES_MAX, KeySlot
from hl_guard.keys.approve_page import DAY_MS, expiry_text

from .conftest import FakeExchange, MakeConfig
from .page_client import claim_refused, embedded_typed_data, get, post, result, sending

HOUR_MS = 3_600_000
NONCE_PAST_MS, NONCE_AHEAD_MS = 2 * DAY_MS, DAY_MS
FORBIDDEN_REMEDY = re.compile(r"\b(move|delete|remove|rm)\b.*(agent\.key|hl-guard)|\baway\b")


def key_slot(config: Path) -> KeySlot:
    return KeySlot(config.parent / "data" / "agent.key")


def now_ms() -> int:
    return int(time.time() * 1000)


class Crash(BaseException):
    pass


class Browser:
    def __init__(self, wallet: LocalAccount, exchange: FakeExchange) -> None:
        self.wallet = wallet
        self.exchange = exchange
        self.signed: set[str] = set()
        self.mode = "approve"
        self.pages = 0
        self.tabs: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, line: str) -> None:
        if not line.startswith("http://"):
            return
        self.pages += 1
        if self.mode == "ignore":
            return
        _, _, body = get(line)
        td = embedded_typed_data(body)
        assert sending(line)
        if self.mode == "tab":
            self.tabs.append((line, self._signed(td)))
            return
        if self.mode == "refuse":
            answer: object = {"status": "err", "response": "refused by the test"}
        else:
            answer = self._land(self._signed(td))
        if self.mode != "sign-only":
            post(line.replace("/approve?", "/result?"), result(self.wallet.address, answer))

    def land_late(self, index: int, at_ms: int) -> object:
        url, signed = self.tabs.pop(index)
        assert claim_refused(url), "a closed page grants no claim"
        nonce = signed["nonce"]
        if not at_ms - NONCE_PAST_MS < nonce < at_ms + NONCE_AHEAD_MS:
            return {"status": "err", "response": "nonce outside the accept window"}
        return self._land(signed)

    def _signed(self, td: dict[str, Any]) -> dict[str, Any]:
        from eth_account.messages import encode_typed_data

        sig = self.wallet.sign_message(encode_typed_data(full_message=td))
        return {
            "action": td["message"] | {"type": "approveAgent"},
            "nonce": td["message"]["nonce"],
            "signature": {"r": hex(sig.r), "s": hex(sig.s), "v": sig.v},
            "vaultAddress": None,
        }

    def _land(self, signed: dict[str, Any]) -> object:
        self.signed.add(signed["action"]["agentAddress"].lower())
        status_code, raw, _ = self.exchange.approve_agent(signed)
        assert status_code == 200
        return json.loads(raw)


def current_address(config: Path) -> str | None:
    try:
        key = key_slot(config).key_path.read_text().strip()
        return str(Account.from_key(key).address).lower()
    except (OSError, ValueError):
        return None


def listed(exchange: FakeExchange, master: str) -> set[str]:
    rows = {a.lower() for (u, _), (a, _) in exchange.agents.items() if u == master.lower()}
    for row in exchange.extra_rows:
        if isinstance(row, dict) and isinstance(row.get("address"), str):
            rows.add(row["address"].lower())
    return rows


def leaked(config: Path, exchange: FakeExchange, browser: Browser) -> set[str]:
    return (listed(exchange, browser.wallet.address) & browser.signed) - {current_address(config)}


def phrased(outcome: Result) -> bool:
    code, lines = outcome
    bare = code == 1 and any(line.startswith("agent approved") for line in lines)
    return not bare and not any(FORBIDDEN_REMEDY.search(line) for line in lines)


def holds(config: Path, exchange: FakeExchange, browser: Browser, outcome: Result) -> bool:
    assert phrased(outcome), outcome
    return outcome[0] != 0 or not leaked(config, exchange, browser)


def run(
    verb: Callable[..., Result], config: Path, browser: Browser, clock: int = 0, days: int = 30
) -> Result:
    if verb is status:
        return status(config, None, now_ms=lambda: now_ms() + clock)
    return verb(
        config,
        days,
        "127.0.0.1",
        0,
        None,
        now_ms=lambda: now_ms() + clock,
        announce=browser,
        timeout_s=0.05,
        sleep=lambda s: None,
    )


@pytest.fixture()
def browser(wallet: LocalAccount, exchange: FakeExchange) -> Browser:
    return Browser(wallet, exchange)


@pytest.fixture()
def wallet() -> LocalAccount:
    created: LocalAccount = Account.create()
    return created


def rename_ours(exchange: FakeExchange, wallet: LocalAccount, name: str) -> None:
    user = wallet.address.lower()
    exchange.agents[(user, name)] = exchange.agents.pop((user, AGENT_NAME))


def rename(exchange: FakeExchange, address: str | None, name: str) -> None:
    key = next(key for key, row in exchange.agents.items() if row[0].lower() == address)
    exchange.agents[(key[0], name)] = exchange.agents.pop(key)


def staged_address(config: Path) -> str:
    staged = key_slot(config).key_path.with_name("agent.key.next")
    return str(Account.from_key(staged.read_text().strip()).address)


@contextmanager
def crash_when_replacing(target: Path, browser: Browser) -> Iterator[None]:
    real, served = os.replace, browser.pages

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if Path(dst) == target and browser.pages > served:
            raise Crash
        real(src, dst, *args, **kwargs)

    os.replace = replace
    try:
        yield
    finally:
        os.replace = real


def test_n1_a_staged_agent_approved_while_the_confirmation_read_fails_is_reported(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser)[0] == 0
    rename_ours(exchange, wallet, "legacy")
    exchange.failed_reads = 1
    assert run(revoke, config, browser)[0] == 1
    staged = staged_address(config)
    assert staged.lower() in listed(exchange, wallet.address)
    for verb in (status, init):
        outcome = run(verb, config, browser)
        assert outcome[0] == 1 and any(staged in line for line in outcome[1]), outcome
        assert holds(config, exchange, browser, outcome)


def test_n2_the_remedy_after_a_listing_past_the_cap_never_forgets_the_agent(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser)[0] == 0
    rename_ours(exchange, wallet, "legacy")
    exchange.failed_reads = 1
    assert run(revoke, config, browser, clock=HOUR_MS, days=180)[0] == 1
    staged = staged_address(config)
    refused = run(revoke, config, browser)
    assert refused[0] == 1 and holds(config, exchange, browser, refused)
    browser.mode = "ignore"
    assert holds(config, exchange, browser, run(revoke, config, browser))
    assert staged.lower() in listed(exchange, wallet.address)
    outcome = run(status, config, browser)
    assert outcome[0] == 1 and any(staged in line for line in outcome[1]), outcome


def test_n3_the_remedy_after_init_refuses_a_listing_past_the_cap_reaches_a_clean_slot(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    exchange.until_shift_ms = 1
    refused = run(init, config, browser)
    assert refused[0] == 1 and "run hl-guard revoke" in refused[1][-1]
    exchange.until_shift_ms = 0
    for verb in (revoke, status):
        outcome = run(verb, config, browser)
        assert outcome[0] == 0 and holds(config, exchange, browser, outcome), outcome


def test_n5_a_lost_key_with_its_record_gets_a_new_key_and_keeps_every_held_agent(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser)[0] == 0
    lost = current_address(config)
    rename_ours(exchange, wallet, "legacy")
    key_slot(config).key_path.unlink()
    for verb in (init, status):
        outcome = run(verb, config, browser)
        assert outcome[0] == 1 and holds(config, exchange, browser, outcome), outcome
        assert any(lost and lost in line.lower() for line in outcome[1]), outcome
    assert current_address(config) not in (None, lost)
    del exchange.agents[(wallet.address.lower(), "legacy")]
    for verb in (revoke, status):
        outcome = run(verb, config, browser)
        assert outcome[0] == 0 and holds(config, exchange, browser, outcome), outcome


@pytest.mark.parametrize("skew", [5 * HOUR_MS, 3 * DAY_MS, -3 * DAY_MS])
def test_a1_r1_a_clock_ahead_or_behind_never_hides_a_live_old_agent(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    browser: Browser,
    skew: int,
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser, clock=-20 * HOUR_MS, days=1)[0] == 0
    rename_ours(exchange, wallet, "legacy")
    for clock in (0, 5 * HOUR_MS):
        assert holds(config, exchange, browser, run(revoke, config, browser, clock=clock))
    for clock in (0, skew):
        outcome = run(status, config, browser, clock=clock)
        assert outcome[0] == 1 and holds(config, exchange, browser, outcome)


def test_a2_r2_a_record_expiry_earlier_than_listed_never_hides_a_live_old_agent(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser, clock=-25 * HOUR_MS, days=1)[0] == 0
    exchange.failed_reads = 2
    assert holds(config, exchange, browser, run(init, config, browser))
    assert holds(config, exchange, browser, run(init, config, browser))
    rename_ours(exchange, wallet, "legacy")
    for verb in (revoke, revoke, status):
        outcome = run(verb, config, browser)
        assert outcome[0] == 1 and holds(config, exchange, browser, outcome), outcome


def pad(text: str) -> str:
    return text + " " * RECORD_BYTES_MAX


def bad_address(text: str) -> str:
    last = list(re.finditer(r"0x[0-9a-fA-F]{40}", text))[-1]
    return text[: last.start()] + "0xzz" + text[last.end() :]


def torn(text: str) -> str:
    return text[:-5]


@pytest.mark.parametrize("spoil", [pad, bad_address, torn], ids=["over 64 KiB", "bad row", "torn"])
def test_a3_r3_a_spoiled_record_naming_a_staged_agent_stops_every_command(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    browser: Browser,
    spoil: Callable[[str], str],
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser)[0] == 0
    browser.mode = "ignore"
    assert run(revoke, config, browser)[0] == 1
    slot = key_slot(config)
    staged_record = slot.key_path.with_name("agent.key.next.json")
    target = staged_record if staged_record.exists() else slot.record_path
    text = spoil(target.read_text())
    target.write_text(text)
    pages = browser.pages
    for verb in (status, init, revoke):
        code, lines = run(verb, config, browser)
        assert code == 2 and "restore it" in lines[0], (verb, lines)
    assert target.read_text() == text and browser.pages == pages


def test_a4_r4_an_address_listed_twice_is_named_under_every_name(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    real = now_ms()
    assert run(init, config, browser)[0] == 0
    original = current_address(config)
    rename_ours(exchange, wallet, "legacy")
    exchange.extra_rows += [
        {"name": "old", "address": original, "validUntil": real - DAY_MS},
        {"name": None, "address": original, "validUntil": real + 50 * DAY_MS},
    ]
    assert run(revoke, config, browser)[0] == 1
    code, lines = run(status, config, browser)
    text = " ".join(lines)
    assert code == 1 and "'legacy'" in text and "'old'" in text and "unnamed" in text


@pytest.mark.parametrize("shift", [1, 3650 * DAY_MS])
def test_c_a_listing_past_the_signed_expiry_is_never_clean(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    browser: Browser,
    shift: int,
) -> None:
    config = make_config(wallet.address)
    exchange.until_shift_ms = shift
    for verb in (init, status, init):
        outcome = run(verb, config, browser)
        assert outcome[0] == 1 and holds(config, exchange, browser, outcome), outcome


def test_c_a_pre_read_listing_past_the_cap_serves_no_page_and_is_never_clean(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    browser.mode = "ignore"
    assert run(init, config, browser)[0] == 1
    address = current_address(config)
    assert address is not None
    exchange.list_agent(wallet.address, "something-else", address, now_ms() + 3650 * DAY_MS)
    browser.signed.add(address)
    pages = browser.pages
    for verb in (init, status):
        outcome = run(verb, config, browser)
        assert outcome[0] == 1 and holds(config, exchange, browser, outcome), outcome
    assert browser.pages == pages


@pytest.mark.parametrize(
    "foreign",
    [
        {"name": "manual", "address": "0x" + "99" * 20, "validUntil": None},
        {"name": "manual", "address": "0x99", "validUntil": 1},
        ["manual", "0x" + "99" * 20, 1],
    ],
)
def test_c_a_malformed_row_for_another_agent_does_not_block_an_approval(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    browser: Browser,
    foreign: object,
) -> None:
    config = make_config(wallet.address)
    exchange.extra_rows.append(foreign)
    for verb in (init, status):
        outcome = run(verb, config, browser)
        assert outcome[0] == 0 and holds(config, exchange, browser, outcome), outcome


def test_c_a_revoke_while_the_old_agent_is_listed_is_never_clean(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser)[0] == 0
    old = current_address(config)
    rename_ours(exchange, wallet, "legacy")
    for verb in (revoke, revoke, status, init):
        outcome = run(verb, config, browser)
        assert outcome[0] == 1 and holds(config, exchange, browser, outcome), outcome
    assert current_address(config) != old


@pytest.mark.parametrize("cut", ["key", "record"])
def test_c_a_revoke_cut_before_its_promote_converges_without_a_page(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    browser: Browser,
    cut: str,
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser)[0] == 0
    slot = key_slot(config)
    target = slot.key_path if cut == "key" else slot.record_path
    with crash_when_replacing(target, browser), pytest.raises(Crash):
        run(revoke, config, browser)
    pages = browser.pages
    for verb in (init, status):
        assert holds(config, exchange, browser, run(verb, config, browser))
    for verb in (revoke, status):
        outcome = run(verb, config, browser)
        assert outcome[0] == 0 and holds(config, exchange, browser, outcome), outcome
    assert browser.pages == pages


def test_a_fifo_at_the_record_path_is_refused_without_hanging(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser)[0] == 0
    record = key_slot(config).record_path
    record.unlink()
    os.mkfifo(record, 0o600)
    outcomes: list[Result] = []
    worker = threading.Thread(target=lambda: outcomes.append(run(status, config, browser)))
    worker.start()
    worker.join(5)
    hung = worker.is_alive()
    if hung:
        os.close(os.open(record, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(5)
    assert not hung
    assert outcomes[0][0] == 2 and "regular file" in outcomes[0][1][0], outcomes


def test_a_refusal_while_the_master_holds_other_named_agents_names_when_one_ends(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    real = now_ms()
    for name, days in (("a", 9), ("b", 4), ("c", 7)):
        exchange.list_agent(wallet.address, name, Account.create().address, real + days * DAY_MS)
    browser.mode = "refuse"
    code, lines = run(init, config, browser)
    assert code == 1
    text = " ".join(lines)
    assert f"'b' until {expiry_text(real + 4 * DAY_MS)}" in text, lines
    assert "to end it now, approving an agent named 'b' from the master wallet replaces it" in text
    assert lines[-1] == "the key stays; run hl-guard init again once the cause is fixed", lines


def test_c1_a_refusal_names_how_to_end_the_first_unnamed_agent_now(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    soon = now_ms() + DAY_MS
    exchange.list_agent(wallet.address, "", Account.create().address, soon)
    for name in ("a", "b", "c"):
        exchange.list_agent(wallet.address, name, Account.create().address, soon + 90 * DAY_MS)
    browser.mode = "refuse"
    code, lines = run(init, config, browser)
    assert code == 1 and "first to expire is unnamed" in lines[1], lines
    assert lines[1].endswith(
        "to end it now, approving a new unnamed agent from the master wallet replaces it"
    )
    assert lines[-1] == "the key stays; run hl-guard init again once the cause is fixed"


def test_c2_a_refusal_with_no_other_agent_says_the_limit_is_not_the_cause(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    browser.mode = "refuse"
    code, lines = run(init, config, browser)
    assert code == 1 and lines[1:] == [
        "the exchange lists no other agent for the master wallet, so a full agent list is not "
        "why it refused",
        "the key stays; run hl-guard init again once the cause is fixed",
    ]


def test_the_record_names_each_agent_before_its_page_is_served(
    make_config: MakeConfig, wallet: LocalAccount, exchange: FakeExchange, browser: Browser
) -> None:
    config = make_config(wallet.address)
    record = key_slot(config).record_path
    unrecorded: list[str] = []

    def page(line: str) -> None:
        if line.startswith("http://"):
            address = embedded_typed_data(get(line)[2])["message"]["agentAddress"]
            if not record.exists() or address not in record.read_text():
                unrecorded.append(address)
        browser(line)

    steps = [(init, "ignore"), (init, "approve"), (revoke, "ignore"), (revoke, "approve")]
    for verb, mode in steps:
        browser.mode = mode
        verb(config, 30, "127.0.0.1", 0, None, announce=page, timeout_s=0.05, sleep=lambda s: None)
    assert browser.pages == 4 and unrecorded == []


@pytest.mark.parametrize("late_name", [None, "legacy"], ids=["as signed", "renamed"])
@pytest.mark.parametrize("names", ["base", "wire"])
def test_r2_a_tab_that_signs_after_its_agent_was_replaced_is_reported(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    browser: Browser,
    names: str,
    late_name: str | None,
) -> None:
    exchange.names = names
    config = make_config(wallet.address)
    browser.mode = "tab"
    assert run(init, config, browser)[0] == 1
    stale = current_address(config)
    browser.mode = "approve"
    assert run(revoke, config, browser)[0] == 0
    rename(exchange, current_address(config), "other")
    assert browser.land_late(0, now_ms()) == {"status": "ok", "response": {"type": "default"}}
    if late_name is not None:
        rename(exchange, stale, late_name)
    assert stale in listed(exchange, wallet.address)
    for verb in (status, init, status):
        outcome = run(verb, config, browser)
        assert outcome[0] == 1 and holds(config, exchange, browser, outcome), outcome
        assert any(stale and stale in line.lower() for line in outcome[1]), outcome


def recorded_until(config: Path) -> int:
    until: int = json.loads(key_slot(config).record_path.read_text())["current"]["valid_until_ms"]
    return until


@pytest.mark.parametrize("names", ["base", "wire"])
@pytest.mark.parametrize("late_days", [180, 1])
def test_r1_status_names_the_listed_expiry_when_a_late_tab_changed_it(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    browser: Browser,
    names: str,
    late_days: int,
) -> None:
    exchange.names = names
    config = make_config(wallet.address)
    browser.mode = "tab"
    assert run(init, config, browser, days=late_days)[0] == 1
    browser.mode = "approve"
    assert run(init, config, browser, days=31)[0] == 0
    assert browser.land_late(0, now_ms())
    address = current_address(config)
    latest = max(u for (_, _), (a, u) in exchange.agents.items() if a.lower() == address)
    changed = latest != recorded_until(config)
    assert changed == (names == "base" or late_days == 180)
    for clock in (0, 32 * DAY_MS):
        outcome = run(status, config, browser, clock=clock)
        clean = not changed and clock == 0
        assert outcome[0] == (0 if clean else 1), outcome
        if changed:
            line = outcome[1][0]
            assert line.lower().startswith(f"the exchange lists the agent {address} until ")
            assert f"until {expiry_text(latest)}, but hl-guard recorded" in line
        elif clean:
            assert outcome[1][0] == f"agent approved, expires {expiry_text(latest)}"
    assert run(revoke, config, browser)[0] == (0 if names == "base" else 1)


@pytest.mark.parametrize("names", ["base", "wire"])
def test_s4_an_agent_under_the_hl_guard_name_the_slot_does_not_hold_is_reported(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    browser: Browser,
    names: str,
) -> None:
    exchange.names = names
    config = make_config(wallet.address)
    assert run(init, config, browser)[0] == 0
    until = now_ms() + 9 * DAY_MS
    for name in ("hl-guard-other", "legacy", "", None, f"{AGENT_NAME} valid_until x"):
        row = {"name": name, "address": Account.create().address, "validUntil": until}
        exchange.extra_rows.append(row)
    assert run(status, config, browser)[0] == 0
    for name in (AGENT_NAME, f"{AGENT_NAME} valid_until {until}"):
        foreign = Account.create().address.lower()
        exchange.extra_rows.append({"name": name, "address": foreign, "validUntil": until})
        for verb in (status, init):
            code, lines = run(verb, config, browser)
            named = [line for line in lines if foreign in line and "does not hold" in line]
            assert code == 1 and len(named) == 1, lines
            assert f"{name!r}" in named[0] and f"until {expiry_text(until)}" in named[0]
        exchange.extra_rows.pop()


@pytest.mark.parametrize("where", ["key", "staged"])
def test_a_directory_at_a_key_path_is_refused_before_any_page(
    make_config: MakeConfig,
    wallet: LocalAccount,
    exchange: FakeExchange,
    browser: Browser,
    where: str,
) -> None:
    config = make_config(wallet.address)
    assert run(init, config, browser)[0] == 0
    slot = key_slot(config)
    path = slot.key_path if where == "key" else slot.staged_path
    path.unlink(missing_ok=True)
    path.mkdir(mode=0o700)
    pages = browser.pages
    for verb in (status, init, revoke, status):
        code, lines = run(verb, config, browser)
        assert code == 2 and lines == [
            f"{verb.__name__} refused: the agent key path {path} is a directory; "
            "point agent_key_file at a file path"
        ], lines
    assert browser.pages == pages and phrased((code, lines))
