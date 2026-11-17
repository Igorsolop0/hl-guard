from __future__ import annotations

import glob
import itertools
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Container
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NewType, TypeGuard

from eth_account import Account

from hl_guard.server.config import ConfigError, load_agent_key

AGENT_NAME = "hl-guard"
RECORD_BYTES_MAX = 64 * 1024
VALID_UNTIL_LIMIT = 2**53
# The exchange takes an approval up to 2 days after its nonce; the third day covers a local
# clock that runs fast.
LATE_SIGNATURE_MS = 3 * 86_400_000
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


class AgentKeyError(RuntimeError):
    pass


@dataclass(frozen=True)
class KeySlot:
    key_path: Path

    @property
    def record_path(self) -> Path:
        return self.key_path.with_name(self.key_path.name + ".json")

    @property
    def staged_path(self) -> Path:
        return self.key_path.with_name(self.key_path.name + ".next")


@dataclass(frozen=True)
class Agent:
    address: str
    valid_until_ms: int | None = None
    refused: bool = False

    def __post_init__(self) -> None:
        if self.refused and self.valid_until_ms is not None:
            raise ValueError("a refused agent has no approval")


@dataclass(frozen=True)
class Served:
    address: str
    nonce: int


@dataclass(frozen=True)
class Record:
    network: str
    current: Agent
    staged: Agent | None
    held: tuple[str, ...]
    served: tuple[Served, ...] = ()

    def __post_init__(self) -> None:
        lowered = [address.lower() for address in self.held]
        named = [agent for agent in (self.current, self.staged) if agent is not None]
        if len(set(lowered)) != len(lowered) or any(
            agent.address.lower() not in lowered for agent in named
        ):
            raise ValueError("held must list each agent once, the current and staged ones too")
        pages = [entry.address.lower() for entry in self.served]
        if len(set(pages)) != len(pages) or not set(pages) <= set(lowered):
            raise ValueError("served must name each held agent at most once")

    @classmethod
    def first(cls, network: str, address: str) -> Record:
        return cls(network, Agent(address), None, (address,))

    def confirmed(self, address: str, valid_until_ms: int) -> Record:
        agent = Agent(address, valid_until_ms)
        if self.staged is not None and self.staged.address == address:
            return replace(self, staged=agent)
        return replace(self, current=agent)

    def staging(self, address: str) -> Record:
        held = self.held if self.holds(address) else (*self.held, address)
        return replace(self, staged=Agent(address), held=held)

    def refusing(self, address: str) -> Record:
        if self.staged is not None and self.staged.address == address:
            return replace(self, staged=None)
        return replace(self, current=Agent(address, refused=True))

    def promoted(self) -> Record:
        if self.staged is None or self.staged.valid_until_ms is None:
            raise ValueError("only a confirmed staged agent can become current")
        return replace(self, current=self.staged, staged=None)

    def serving(self, address: str, nonce: int) -> Record:
        if not self.holds(address):
            raise ValueError("a page is served only for a held agent")
        before = [entry.nonce for entry in self.served if entry.address.lower() == address.lower()]
        rest = tuple(entry for entry in self.served if entry.address.lower() != address.lower())
        return replace(self, served=(*rest, Served(address, max([nonce, *before]))))

    def pruned(self, listed: Container[str], now_ms: int) -> Record:
        served = tuple(e for e in self.served if now_ms < e.nonce + LATE_SIGNATURE_MS)
        keep = {agent.address.lower() for agent in (self.current, self.staged) if agent}
        keep |= {entry.address.lower() for entry in served}
        held = tuple(a for a in self.held if a.lower() in keep or a.lower() in listed)
        return replace(self, held=held, served=served)

    def subject(self, staged: bool) -> Agent:
        agent = self.staged if staged else self.current
        if agent is None:
            raise ValueError("no staged agent to approve")
        return agent

    def holds(self, address: str) -> bool:
        return address.lower() in (a.lower() for a in self.held)


Saved = NewType("Saved", Record)


def _private_dir(path: Path) -> None:
    missing = list(itertools.takewhile(lambda p: not os.path.lexists(p), (path, *path.parents)))
    try:
        for parent in reversed(missing):
            parent.mkdir(mode=0o700, exist_ok=True)
        st = os.stat(path)
    except OSError as exc:
        raise AgentKeyError(f"cannot create the key dir {path}: {exc.strerror}") from None
    if st.st_uid != os.geteuid():
        raise AgentKeyError(f"the key dir {path} belongs to uid {st.st_uid}, not you")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise AgentKeyError(
            f"the key dir {path} has mode {stat.S_IMODE(st.st_mode):04o}, not 0700; "
            f"run chmod 700 {path}"
        )


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _place(path: Path, text: str, what: str, *, overwrite: bool) -> None:
    fd, temp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite:
            os.replace(temp, path)
        else:
            os.link(temp, path)
        _sync_dir(path.parent)
    except FileExistsError:
        raise AgentKeyError(
            f"the agent {what} already exists at {path}; refusing to overwrite it"
        ) from None
    except OSError as exc:
        raise AgentKeyError(
            f"cannot write the agent {what} {path}: {exc.strerror or exc}"
        ) from None
    finally:
        Path(temp).unlink(missing_ok=True)


def write_key(path: Path, *, overwrite: bool) -> str:
    _private_dir(path.parent)
    private = "0x" + secrets.token_hex(32)
    try:
        address: str = Account.from_key(private).address
    except Exception:
        raise AgentKeyError("cannot derive the agent address from the new key") from None
    _place(path, private, "key", overwrite=overwrite)
    return address


def key_address(path: Path) -> str | None:
    if not os.path.lexists(path):
        return None
    try:
        address: str = load_agent_key(path).address
    except ConfigError as exc:
        raise AgentKeyError(str(exc)) from None
    return address


def usable_key(path: Path) -> str | None:
    try:
        return key_address(path)
    except AgentKeyError:
        return None


def _agent_json(agent: Agent | None) -> dict[str, object] | None:
    if agent is None:
        return None
    return {
        "address": agent.address,
        "valid_until_ms": agent.valid_until_ms,
        "refused": agent.refused,
    }


def save(slot: KeySlot, record: Record) -> Saved:
    text = json.dumps(
        {
            "network": record.network,
            "current": _agent_json(record.current),
            "staged": _agent_json(record.staged),
            "held": list(record.held),
            "served": [{"address": e.address, "nonce": e.nonce} for e in record.served],
        }
    )
    if len(text) > RECORD_BYTES_MAX:
        raise AgentKeyError(
            f"the agent record {slot.record_path} would be {len(text)} bytes, over "
            f"{RECORD_BYTES_MAX} bytes; nothing was written"
        )
    _place(slot.record_path, text, "record", overwrite=True)
    return Saved(record)


def valid_until_ok(value: object) -> TypeGuard[int]:
    return type(value) is int and 0 < value < VALID_UNTIL_LIMIT


def _spoiled(path: Path, why: str) -> AgentKeyError:
    return AgentKeyError(
        f"the agent record {path} is {why}; restore it, because it lists every agent hl-guard held"
    )


def load(slot: KeySlot, network: str) -> Saved | None:
    path = slot.record_path
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _spoiled(path, f"unreadable: {exc.strerror or exc}") from None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise _spoiled(path, "not a regular file")
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(RECORD_BYTES_MAX + 1)
    if len(data) > RECORD_BYTES_MAX:
        raise _spoiled(path, f"over {RECORD_BYTES_MAX} bytes")
    try:
        raw = json.loads(data)
    except ValueError:
        raise _spoiled(path, "not JSON") from None
    record = _parse(raw)
    if record is None:
        raise _spoiled(path, "malformed")
    if record.network != network:
        raise AgentKeyError(
            f"the agent key {slot.key_path} belongs to {record.network}, not {network}; "
            "point agent_key_file at a new path for this network"
        )
    return Saved(record)


def _parse_agent(raw: object) -> Agent | None:
    if not isinstance(raw, dict) or set(raw) != {"address", "valid_until_ms", "refused"}:
        return None
    address, until, refused = raw["address"], raw["valid_until_ms"], raw["refused"]
    if not _is_address(address) or (until is not None and not valid_until_ok(until)):
        return None
    if type(refused) is not bool or (refused and until is not None):
        return None
    return Agent(address, until, refused)


def _parse_served(raw: object) -> Served | None:
    if not isinstance(raw, dict) or set(raw) != {"address", "nonce"}:
        return None
    address, nonce = raw["address"], raw["nonce"]
    if not _is_address(address) or not valid_until_ok(nonce):
        return None
    return Served(address, nonce)


def _parse(raw: object) -> Record | None:
    if not isinstance(raw, dict) or set(raw) != {"network", "current", "staged", "held", "served"}:
        return None
    network, held, pages = raw["network"], raw["held"], raw["served"]
    current = _parse_agent(raw["current"])
    staged = None if raw["staged"] is None else _parse_agent(raw["staged"])
    served = [_parse_served(entry) for entry in pages] if isinstance(pages, list) else [None]
    lost_staged = raw["staged"] is not None and staged is None
    if not isinstance(network, str) or current is None or lost_staged or None in served:
        return None
    if not isinstance(held, list) or not all(_is_address(a) for a in held):
        return None
    try:
        return Record(network, current, staged, tuple(held), tuple(e for e in served if e))
    except ValueError:
        return None


def _is_address(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and ADDRESS.fullmatch(value) is not None


@dataclass(frozen=True)
class Settled:
    record: Saved | None
    replaced: str | None = None


def _refuse_key_directories(slot: KeySlot) -> None:
    for path in (slot.key_path, slot.staged_path):
        if os.path.isdir(path) and not os.path.islink(path):
            raise AgentKeyError(
                f"the agent key path {path} is a directory; point agent_key_file at a file path"
            )


def _clear_torn_writes(slot: KeySlot) -> None:
    torn = [
        path
        for path in slot.key_path.parent.glob(glob.escape(slot.key_path.name) + ".*.tmp")
        if stat.S_ISREG(os.lstat(path).st_mode)
    ]
    for path in torn:
        path.unlink(missing_ok=True)
    if torn:
        _sync_dir(slot.key_path.parent)


def settle(slot: KeySlot, network: str) -> Settled:
    _refuse_key_directories(slot)
    _clear_torn_writes(slot)
    record = load(slot, network)
    if record is None:
        address = key_address(slot.key_path)
        return Settled(None if address is None else save(slot, Record.first(network, address)))
    staged = record.staged
    if staged and staged.valid_until_ms and usable_key(slot.key_path) == staged.address:
        return Settled(save(slot, record.promoted()), record.current.address)
    loose = usable_key(slot.staged_path)
    if loose is not None and not record.holds(loose):
        record = save(slot, record.staging(loose))
    return Settled(record)


def peek(slot: KeySlot, network: str) -> Record | None:
    _refuse_key_directories(slot)
    record = load(slot, network)
    if record is not None:
        return record
    address = key_address(slot.key_path)
    return None if address is None else Record.first(network, address)


def stage_key(slot: KeySlot, record: Saved) -> tuple[Saved, Agent]:
    address = write_key(slot.staged_path, overwrite=True)
    return save(slot, record.staging(address)), Agent(address)


def promote(slot: KeySlot, record: Saved) -> Saved:
    os.replace(slot.staged_path, slot.key_path)
    _sync_dir(slot.key_path.parent)
    return save(slot, record.promoted())


def key_mode(slot: KeySlot) -> str:
    return f"{stat.S_IMODE(os.lstat(slot.key_path).st_mode):04o}"
