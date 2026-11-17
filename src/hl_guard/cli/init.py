from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from hl_guard.exchange.network import Network, bound_network
from hl_guard.keys.agent_key import (
    AGENT_NAME,
    Agent,
    AgentKeyError,
    KeySlot,
    Record,
    Saved,
    key_address,
    key_mode,
    peek,
    promote,
    save,
    settle,
    stage_key,
    usable_key,
    write_key,
)
from hl_guard.keys.agents import Listed, listed_agents
from hl_guard.keys.approve_page import (
    DAY_MS,
    MAX_VALID_DAYS,
    Approval,
    ApproveAgentError,
    ApprovePage,
    Refused,
    approval,
    check_days,
    check_host,
    check_port,
    expiry_text,
)
from hl_guard.policy.load import PolicyError, load_policy
from hl_guard.server.config import (
    ConfigError,
    ServeConfig,
    load_config,
    read_mode,
    resolve_network,
)
from hl_guard.server.info import InfoClient, WeightBudget

PAGE_TIMEOUT_S = 900.0
_OUR_NAME = re.compile(re.escape(AGENT_NAME) + r"( valid_until [0-9]+)?")
CONFIRM_READS = 5
CONFIRM_GAP_S = 1.0
Result = tuple[int, list[str]]
Clock = Callable[[], int]
Announce = Callable[[str], None]
Sleep = Callable[[float], None]
Listing = dict[str, Listed]


class _Refused(Exception):
    pass


class _Stop(Exception):
    def __init__(self, code: int, *lines: str) -> None:
        super().__init__(*lines)
        self.code = code
        self.lines = list(lines)


@dataclass(frozen=True)
class _Account:
    master: str
    network: Network
    slot: KeySlot
    info: InfoClient


@dataclass(frozen=True)
class _Request:
    verb: str
    days: int
    host: str
    port: int
    now_ms: Clock
    announce: Announce
    timeout_s: float
    sleep: Sleep

    @property
    def again(self) -> str:
        return f"run hl-guard {self.verb} again"

    @property
    def kept(self) -> str:
        key = "the replacement key" if self.verb == "revoke" else "the key"
        return f"{key} stays; {self.again}"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _announce(line: str) -> None:
    print(line, flush=True)


def _load(config_path: Path, expect_network: str | None) -> tuple[ServeConfig, str, KeySlot]:
    try:
        config = load_config(config_path)
        policy = load_policy(config.policy_path)
        network = resolve_network(policy.network, read_mode(config.data_dir), expect_network)
    except (ConfigError, PolicyError) as exc:
        raise _Refused(str(exc)) from None
    if config.agent_key_path is None:
        raise _Refused(
            f"config {config_path} has no agent_key_file; add this line to it: "
            f"agent_key_file: {config.data_dir / 'agent.key'}"
        )
    return config, network, KeySlot(config.agent_key_path)


def _check_request(request: _Request) -> None:
    try:
        check_days(request.days)
        check_host(request.host)
        check_port(request.port)
    except ApproveAgentError as exc:
        raise _Refused(str(exc)) from None


@contextmanager
def _opened(config_path: Path, expect_network: str | None) -> Iterator[_Account]:
    config, name, slot = _load(config_path, expect_network)
    with bound_network(name, allow_mainnet=name == "mainnet") as network:
        budget = WeightBudget(config.budget_path, config.info_weight_per_min)
        info = InfoClient(name, budget, timeout_s=config.info_timeout_s)
        yield _Account(config.account, network, slot, info)


def _settled(verb: str, run: Callable[[], Result]) -> Result:
    try:
        return run()
    except _Stop as exc:
        return exc.code, exc.lines
    except (_Refused, AgentKeyError, OSError) as exc:
        return 2, [f"{verb} refused: {exc}"]


def _approved_line(valid_until_ms: int) -> str:
    return f"agent approved, expires {expiry_text(valid_until_ms)}"


def _usable_until(listed: Listing, address: str, now_ms: int) -> int | None:
    row = listed.get(address.lower())
    return row.valid_until_ms if row is not None and row.valid_until_ms > now_ms else None


def _replaces(name: str | None) -> str:
    if not name:
        return "approving a new unnamed agent from the master wallet replaces it"
    if name == AGENT_NAME:
        return f"the next hl-guard revoke approves an agent named {name!r}, which replaces it"
    return f"approving an agent named {name!r} from the master wallet replaces it"


def _still_listed(address: str, row: Listed) -> str:
    remedies = "; ".join(_replaces(name) for name in row.names)
    return (
        f"the exchange still lists the old agent {address} until "
        f"{expiry_text(row.valid_until_ms)}, so its key can still trade until then; {remedies}"
    )


def _signed_by_hl_guard(name: str | None) -> bool:
    return name is not None and _OUR_NAME.fullmatch(name) is not None


def _unheld(address: str, row: Listed) -> str:
    names = ", ".join(repr(name) for name in row.names if _signed_by_hl_guard(name))
    remedies = "; ".join(_replaces(name) for name in row.names)
    return (
        f"the exchange lists an agent named {names}, {address}, until "
        f"{expiry_text(row.valid_until_ms)} that this key slot does not hold, so another "
        f"hl-guard setup or an old approval page approved it; its key can trade until then; "
        f"{remedies}"
    )


def _stopped(address: str) -> str:
    return (
        f"a revoke stopped after the exchange approved the replacement {address}; "
        "run hl-guard revoke to finish it"
    )


def _unrecorded(what: str, address: str, row: Listed, remedy: str) -> str:
    return (
        f"the exchange lists the {what} {address} until {expiry_text(row.valid_until_ms)}, "
        f"but hl-guard has not recorded its approval; {remedy}"
    )


def _read(account: _Account, record: Record) -> Listing | AgentKeyError:
    try:
        return listed_agents(account.info, account.master, record.held)
    except AgentKeyError as exc:
        return exc


def _pruned(slot: KeySlot, record: Saved, listed: Listing, now_ms: int) -> Saved:
    pruned = record.pruned(listed, now_ms)
    return record if pruned == record else save(slot, pruned)


def _key_trouble(slot: KeySlot, record: Record) -> str | None:
    instead = "run hl-guard revoke to approve a new key in its place"
    try:
        address = key_address(slot.key_path)
    except AgentKeyError as exc:
        return f"{exc}; {instead}"
    staged = record.staged
    if address == record.current.address or (staged is not None and address == staged.address):
        return None
    agent = record.current.address
    return f"the agent key {slot.key_path} does not hold the agent {agent}; {instead}"


@dataclass(frozen=True)
class _Verdict:
    problems: list[str]
    approved_until: int | None
    waiting: list[str]


def _current_problem(current: Agent, row: Listed | None, checked: bool, now_ms: int) -> str | None:
    until = current.valid_until_ms
    if current.refused:
        return (
            f"hl-guard will not use the key of the agent {current.address}, which the exchange "
            "listed past the expiry hl-guard signs; run hl-guard revoke for a new key"
        )
    if until is not None and row is not None and row.valid_until_ms != until:
        return (
            f"the exchange lists the agent {current.address} until "
            f"{expiry_text(row.valid_until_ms)}, but hl-guard recorded {expiry_text(until)}; "
            "another approval for it landed, so run hl-guard revoke for a new key"
        )
    if until is not None and until <= now_ms:
        return f"agent expired {expiry_text(until)}; run hl-guard init"
    if until is None and row is None:
        return "agent not approved; run hl-guard init"
    if until is None and row is not None:
        return _unrecorded("agent", current.address, row, "run hl-guard init")
    if row is None and checked:
        return (
            f"the exchange does not list the agent {current.address}; it was removed or "
            "replaced, so run hl-guard revoke for a new agent"
        )
    return None


def _verdict(slot: KeySlot, record: Record, read: Listing | AgentKeyError, now_ms: int) -> _Verdict:
    checked = not isinstance(read, AgentKeyError)
    listed: Listing = {} if isinstance(read, AgentKeyError) else read
    problems = [] if checked else [f"{read}; the exchange was not checked"]
    current, staged, waiting = record.current, record.staged, []
    trouble = _key_trouble(slot, record)
    problem = _current_problem(current, listed.get(current.address.lower()), checked, now_ms)
    problems += [line for line in (trouble, problem) if line]
    approved = None if problem else current.valid_until_ms
    for address in record.held:
        if address.lower() == current.address.lower():
            continue
        row = listed.get(address.lower())
        if staged is not None and address.lower() == staged.address.lower():
            if staged.valid_until_ms is not None:
                problems.append(_stopped(address))
            elif row is not None:
                remedy = "run hl-guard revoke to finish it"
                problems.append(_unrecorded("replacement agent", address, row, remedy))
            else:
                waiting.append("a replacement agent waits for approval; run hl-guard revoke")
        elif row is not None:
            problems.append(_still_listed(address, row))
    for address, row in listed.items():
        if not record.holds(address) and any(_signed_by_hl_guard(name) for name in row.names):
            problems.append(_unheld(address, row))
    return _Verdict(problems, approved, waiting)


def _lines(verdict: _Verdict, where: list[str]) -> Result:
    until = verdict.approved_until
    if until is not None and not verdict.problems:
        return 0, [_approved_line(until), *where, *verdict.waiting]
    kept = [f"the current agent stays approved, expires {expiry_text(until)}"] if until else []
    return 1, [*verdict.problems, *kept, *where, *verdict.waiting]


def _crowded(account: _Account, record: Record) -> list[str]:
    try:
        listed = listed_agents(account.info, account.master, record.held)
    except AgentKeyError as exc:
        return [f"{exc}; the master wallet's other agents were not checked"]
    others = {address: row for address, row in listed.items() if not record.holds(address)}
    if not others:
        return [
            "the exchange lists no other agent for the master wallet, so a full agent list "
            "is not why it refused"
        ]
    address, first = min(others.items(), key=lambda item: item[1].valid_until_ms)
    names = ", ".join(repr(name) if name else "unnamed" for name in first.names)
    remedies = "; ".join(_replaces(name) for name in first.names)
    return [
        "if the exchange refused because the master wallet holds as many agents as it allows, "
        f"one must end first: it lists {len(others)} other agents, and the first to expire is "
        f"{names} until {expiry_text(first.valid_until_ms)} ({address}); to end it now, "
        f"{remedies}"
    ]


def _ask_wallet(account: _Account, record: Saved, asked: Approval, request: _Request) -> None:
    master, network, address = account.master, account.network, asked.agent_address
    page = ApprovePage(asked, request.host, request.port)
    try:
        try:
            page.start()
        except OSError as exc:
            raise _Stop(
                2,
                f"cannot serve the page on {request.host}:{request.port}: {exc.strerror}",
                request.kept,
            ) from None
        request.announce(f"open this link in the browser that holds the master wallet {master}:")
        request.announce(page.url)
        request.announce(
            f"agent {address} on {network.name}, expires "
            f"{expiry_text(asked.valid_until_ms)} ({request.days} days); "
            f"waiting up to {request.timeout_s:g} s, Ctrl-C cancels"
        )
        page.wait(request.timeout_s)
    finally:
        page.close()
    outcome = page.wait(0)
    if outcome is None:
        raise _Stop(1, f"no answer from the page within {request.timeout_s:g} s", request.kept)
    if isinstance(outcome, Refused):
        if not outcome.by_exchange:
            raise _Stop(1, f"approval refused: {outcome.reason}", request.kept)
        raise _Stop(
            1,
            f"approval refused: {outcome.reason}",
            *_crowded(account, record),
            f"{request.kept} once the cause is fixed",
        )


def _not_signed(
    slot: KeySlot, record: Saved, address: str, until: int, limit: str, request: _Request
) -> _Stop:
    staged = record.staged is not None and record.staged.address == address
    save(slot, record.refusing(address))
    return _Stop(
        1,
        f"the exchange lists the agent {address} until {expiry_text(until)}, past {limit}, "
        "so hl-guard will not use its key and reports the agent while the exchange lists it",
        f"run hl-guard revoke{' again' if staged else ''} for a new key",
    )


def _confirmed(
    account: _Account, record: Saved, address: str, request: _Request, signed_until: int
) -> tuple[int, Listing]:
    for read in range(CONFIRM_READS):
        if read:
            request.sleep(CONFIRM_GAP_S)
        listed = _read(account, record)
        if isinstance(listed, AgentKeyError):
            raise _Stop(1, f"{listed}; the approval was not recorded", request.again)
        until = _usable_until(listed, address, request.now_ms())
        if until is None:
            continue
        if until > signed_until:
            limit = f"the {expiry_text(signed_until)} it was signed for"
            raise _not_signed(account.slot, record, address, until, limit, request)
        return until, listed
    raise _Stop(
        1,
        f"the exchange does not list the agent {address}; the approval was not recorded",
        request.again,
    )


def _approve(
    account: _Account, record: Saved, staged: bool, request: _Request, fresh: bool
) -> tuple[Saved, Listing]:
    slot, agent = account.slot, record.subject(staged)
    pre = None if fresh else _read(account, record)
    if pre is not None and not isinstance(pre, AgentKeyError):
        now = request.now_ms()
        until = _usable_until(pre, agent.address, now)
        if until is not None and until > now + MAX_VALID_DAYS * DAY_MS:
            cap = f"the {MAX_VALID_DAYS}-day cap hl-guard signs"
            raise _not_signed(slot, record, agent.address, until, cap, request)
        if until is not None:
            return save(slot, record.confirmed(agent.address, until)), pre
    master, network = account.master, account.network
    asked = approval(network, master, agent.address, days=request.days, now_ms=request.now_ms())
    record = save(slot, record.serving(agent.address, asked.nonce))
    _ask_wallet(account, record, asked, request)
    until, listed = _confirmed(account, record, agent.address, request, asked.valid_until_ms)
    return save(slot, record.confirmed(agent.address, until)), listed


def init(
    config_path: Path,
    days: int,
    host: str,
    port: int,
    expect_network: str | None,
    *,
    now_ms: Clock = _now_ms,
    announce: Announce = _announce,
    timeout_s: float = PAGE_TIMEOUT_S,
    sleep: Sleep = time.sleep,
) -> Result:
    request = _Request("init", days, host, port, now_ms, announce, timeout_s, sleep)

    def run() -> Result:
        _check_request(request)
        with _opened(config_path, expect_network) as account:
            slot, network = account.slot, account.network.name
            record = settle(slot, network).record
            fresh = record is None
            if record is None:
                record = save(
                    slot, Record.first(network, write_key(slot.key_path, overwrite=False))
                )
            try:
                address = key_address(slot.key_path)
            except AgentKeyError as exc:
                instead = "or run hl-guard revoke to approve a new key in its place"
                raise _Refused(f"{exc}; {instead}") from None
            if address != record.current.address or record.current.refused:
                return _replace(account, record, request, None)
            where = [f"agent {address} on {network}"]
            until = record.current.valid_until_ms
            if until is not None and until > now_ms():
                verdict = _verdict(slot, record, _read(account, record), now_ms())
                code, lines = _lines(verdict, where)
                again = ["run hl-guard revoke to replace it with a new agent"]
                return code, lines + again if code == 0 else lines
            record, listed = _approve(account, record, False, request, fresh)
            record = _pruned(slot, record, listed, now_ms())
            return _lines(_verdict(slot, record, listed, now_ms()), where)

    return _settled(request.verb, run)


def status(config_path: Path, expect_network: str | None, *, now_ms: Clock = _now_ms) -> Result:
    def run() -> Result:
        with _opened(config_path, expect_network) as account:
            slot, network = account.slot, account.network.name
            record = peek(slot, network)
            if record is None:
                return 1, [f"no agent key at {slot.key_path}; run hl-guard init"]
            where = [f"agent {record.current.address} on {network}"]
            if os.path.lexists(slot.key_path):
                where.append(f"key {slot.key_path} mode {key_mode(slot)}")
            return _lines(_verdict(slot, record, _read(account, record), now_ms()), where)

    return _settled("status", run)


def revoke(
    config_path: Path,
    days: int,
    host: str,
    port: int,
    expect_network: str | None,
    *,
    now_ms: Clock = _now_ms,
    announce: Announce = _announce,
    timeout_s: float = PAGE_TIMEOUT_S,
    sleep: Sleep = time.sleep,
) -> Result:
    request = _Request("revoke", days, host, port, now_ms, announce, timeout_s, sleep)

    def run() -> Result:
        _check_request(request)
        with _opened(config_path, expect_network) as account:
            settled = settle(account.slot, account.network.name)
            if settled.record is None:
                return 1, ["no agent to revoke; run hl-guard init"]
            return _replace(account, settled.record, request, settled.replaced)

    return _settled(request.verb, run)


def _replace(account: _Account, record: Saved, request: _Request, replaced: str | None) -> Result:
    slot, listed = account.slot, None
    old = replaced or record.current.address
    if replaced is None:
        staged, fresh = record.staged, False
        if staged is None or usable_key(slot.staged_path) != staged.address:
            record, staged = stage_key(slot, record)
            fresh = True
        if staged.valid_until_ms is None:
            record, listed = _approve(account, record, True, request, fresh)
        try:
            record = promote(slot, record)
        except (OSError, AgentKeyError) as exc:
            raise _Stop(
                1, f"the exchange approved {staged.address} but {exc}", request.again
            ) from None
    new = record.current.address
    read = _read(account, record) if listed is None else listed
    if isinstance(read, AgentKeyError):
        return 1, [f"{read}; {new} replaced {old} here but the exchange was not checked"]
    record = _pruned(slot, record, read, request.now_ms())
    verdict = _verdict(slot, record, read, request.now_ms())
    if verdict.problems or verdict.approved_until is None:
        return 1, verdict.problems
    return 0, [
        f"agent replaced: {old} -> {new}, expires {expiry_text(verdict.approved_until)}; "
        "restart hl-guard serve to trade with the new agent"
    ]
