from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_utils.address import is_checksum_address

from hl_guard.keys.agent_key import (
    Agent,
    AgentKeyError,
    KeySlot,
    Record,
    Saved,
    Served,
    key_address,
    key_mode,
    load,
    peek,
    promote,
    save,
    settle,
    stage_key,
    usable_key,
    write_key,
)

NOW = 1_757_300_000_000
DAY = 86_400_000
OLD = "0x" + "11" * 20
NEW = "0x" + "22" * 20


def slot_in(root: Path) -> KeySlot:
    return KeySlot(root / "hl" / "agent.key")


def key_text(path: Path) -> str:
    return path.read_text().strip()


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def first(slot: KeySlot) -> Saved:
    return save(slot, Record.first("testnet", write_key(slot.key_path, overwrite=False)))


def names(slot: KeySlot) -> list[str]:
    return sorted(p.name for p in slot.key_path.parent.iterdir())


def test_write_key_writes_a_0600_key_in_a_0700_dir(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    address = write_key(slot.key_path, overwrite=False)
    assert mode(slot.key_path.parent) == 0o700 and mode(slot.key_path) == 0o600
    assert key_mode(slot) == "0600"
    assert address == Account.from_key(key_text(slot.key_path)).address
    assert is_checksum_address(address)
    assert key_address(slot.key_path) == address


def test_write_key_never_overwrites_without_leave(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    write_key(slot.key_path, overwrite=False)
    before = key_text(slot.key_path)
    with pytest.raises(AgentKeyError, match="key already exists"):
        write_key(slot.key_path, overwrite=False)
    assert key_text(slot.key_path) == before
    assert names(slot) == ["agent.key"]


def test_write_key_with_overwrite_replaces_the_file(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    write_key(slot.staged_path, overwrite=False)
    before = key_text(slot.staged_path)
    write_key(slot.staged_path, overwrite=True)
    assert key_text(slot.staged_path) != before and mode(slot.staged_path) == 0o600


def test_a_failed_key_link_leaves_no_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def full(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "link", full)
    slot = slot_in(tmp_path)
    with pytest.raises(AgentKeyError, match="No space left"):
        write_key(slot.key_path, overwrite=False)
    assert names(slot) == []


def dir_syncs(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    synced: list[int] = []
    real = os.fsync

    def noted(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            synced.append(os.fstat(fd).st_ino)
        real(fd)

    monkeypatch.setattr(os, "fsync", noted)
    return synced


def test_every_write_syncs_the_key_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    slot = slot_in(tmp_path)
    slot.key_path.parent.mkdir(mode=0o700)
    synced = dir_syncs(monkeypatch)
    record = first(slot)
    assert len(synced) == 2
    record, staged = stage_key(slot, record)
    assert len(synced) == 4
    record = save(slot, record.confirmed(staged.address, NOW + DAY))
    assert len(synced) == 5
    promote(slot, record)
    assert len(synced) == 7 and set(synced) == {os.stat(slot.key_path.parent).st_ino}


def test_a_derivation_failure_leaves_no_file_and_no_key_in_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(key: str) -> Any:
        raise ValueError(f"bad key {key}")

    monkeypatch.setattr(Account, "from_key", boom)
    slot = slot_in(tmp_path)
    with pytest.raises(AgentKeyError) as caught:
        write_key(slot.key_path, overwrite=False)
    assert "bad key" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
    assert names(slot) == []


@pytest.mark.parametrize("problem", ["another uid", "group-writable"])
def test_write_key_refuses_an_unsafe_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    slot = slot_in(tmp_path)
    slot.key_path.parent.mkdir(mode=0o700)
    if problem == "another uid":
        uid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    else:
        slot.key_path.parent.chmod(0o770)
    with pytest.raises(AgentKeyError, match=r"uid|chmod 700"):
        write_key(slot.key_path, overwrite=False)
    assert names(slot) == []


@pytest.mark.parametrize("dir_mode", [0o750, 0o705, 0o755, 0o711, 0o701, 0o500], ids=oct)
def test_write_key_refuses_a_dir_that_is_not_0700(tmp_path: Path, dir_mode: int) -> None:
    slot = slot_in(tmp_path)
    slot.key_path.parent.mkdir()
    slot.key_path.parent.chmod(dir_mode)
    with pytest.raises(AgentKeyError, match=f"mode {dir_mode:04o}.*chmod 700"):
        write_key(slot.key_path, overwrite=False)
    slot.key_path.parent.chmod(0o700)
    assert names(slot) == []


def test_write_key_makes_each_missing_parent_0700(tmp_path: Path) -> None:
    slot = KeySlot(tmp_path / "a" / "b" / "hl" / "agent.key")
    previous = os.umask(0o022)
    try:
        write_key(slot.key_path, overwrite=False)
    finally:
        os.umask(previous)
    for made in (tmp_path / "a", tmp_path / "a" / "b", tmp_path / "a" / "b" / "hl"):
        assert mode(made) == 0o700, made


@pytest.mark.parametrize(
    ("spoil", "match"),
    [
        (lambda path: path.chmod(0o644), "chmod 600"),
        (lambda path: path.write_text("not a key"), "64 hex digits"),
    ],
    ids=["readable by others", "not a key"],
)
def test_an_unusable_key_is_refused_by_key_address_and_none_for_usable_key(
    tmp_path: Path, spoil: Any, match: str
) -> None:
    slot = slot_in(tmp_path)
    write_key(slot.key_path, overwrite=False)
    spoil(slot.key_path)
    with pytest.raises(AgentKeyError, match=match):
        key_address(slot.key_path)
    assert usable_key(slot.key_path) is None
    assert key_address(slot.staged_path) is None


def test_a_key_owned_by_another_uid_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = slot_in(tmp_path)
    write_key(slot.key_path, overwrite=False)
    uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    with pytest.raises(AgentKeyError, match="you own"):
        key_address(slot.key_path)


def test_the_record_holds_no_key_material_and_reads_back(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    record = first(slot)
    secret = key_text(slot.key_path)
    text = slot.record_path.read_text()
    assert secret not in text and secret.removeprefix("0x") not in text
    assert mode(slot.record_path) == 0o600
    assert json.loads(text) == {
        "network": "testnet",
        "current": {"address": record.current.address, "valid_until_ms": None, "refused": False},
        "staged": None,
        "held": [record.current.address],
        "served": [],
    }
    assert load(slot, "testnet") == record


def test_the_record_invariant_names_current_and_staged_in_held() -> None:
    with pytest.raises(ValueError, match="held must list"):
        Record("testnet", Agent(OLD), None, ())
    with pytest.raises(ValueError, match="held must list"):
        Record("testnet", Agent(OLD), Agent(NEW), (OLD,))
    with pytest.raises(ValueError, match="held must list"):
        Record("testnet", Agent(OLD), None, (OLD, OLD.upper().replace("0X", "0x")))
    with pytest.raises(ValueError, match="no approval"):
        Agent(OLD, NOW, refused=True)


def test_each_transition_keeps_every_held_agent() -> None:
    record = Record.first("testnet", OLD).confirmed(OLD, NOW + DAY)
    staged = record.staging(NEW)
    assert staged.held == (OLD, NEW) and staged.staged == Agent(NEW)
    assert staged.staging(NEW).held == (OLD, NEW)
    assert staged.refusing(NEW) == Record("testnet", Agent(OLD, NOW + DAY), None, (OLD, NEW))
    assert staged.refusing(OLD).current == Agent(OLD, refused=True)
    with pytest.raises(ValueError, match="confirmed staged"):
        staged.promoted()
    with pytest.raises(ValueError, match="confirmed staged"):
        record.promoted()
    done = staged.confirmed(NEW, NOW + 2 * DAY).promoted()
    assert done == Record("testnet", Agent(NEW, NOW + 2 * DAY), None, (OLD, NEW))
    assert done.pruned({NEW.lower()}, NOW).held == (NEW,)
    assert done.pruned({OLD.lower()}, NOW).held == (OLD, NEW)
    assert done.pruned(set(), NOW).held == (NEW,)
    assert staged.pruned(set(), NOW).held == (OLD, NEW)


def test_a_served_agent_stays_held_while_the_exchange_could_take_a_late_signature() -> None:
    record = Record.first("testnet", OLD).serving(OLD, NOW).staging(NEW).serving(NEW, NOW + DAY)
    done = record.confirmed(NEW, NOW + 30 * DAY).promoted()
    assert done.served == (Served(OLD, NOW), Served(NEW, NOW + DAY))
    assert done.pruned(set(), NOW + 3 * DAY - 1).held == (OLD, NEW)
    later = done.pruned(set(), NOW + 3 * DAY)
    assert later.held == (NEW,) and later.served == (Served(NEW, NOW + DAY),)
    assert done.pruned(set(), NOW - 10 * DAY).held == (OLD, NEW)
    assert later.pruned(set(), NOW + 4 * DAY).served == ()
    assert done.serving(OLD, NOW - DAY).served[-1] == Served(OLD, NOW)
    assert done.serving(OLD, NOW + 2 * DAY).served[-1] == Served(OLD, NOW + 2 * DAY)
    with pytest.raises(ValueError, match="held agent"):
        done.serving("0x" + "99" * 20, NOW)
    with pytest.raises(ValueError, match="at most once"):
        Record("testnet", Agent(OLD), None, (OLD,), (Served(OLD, NOW), Served(OLD, NOW)))
    with pytest.raises(ValueError, match="at most once"):
        Record("testnet", Agent(OLD), None, (OLD,), (Served(NEW, NOW),))


@pytest.mark.parametrize(
    "agent",
    [
        {"address": "0xzz", "valid_until_ms": None, "refused": False},
        {"address": OLD, "valid_until_ms": 10**20, "refused": False},
        {"address": OLD, "valid_until_ms": 2**53, "refused": False},
        {"address": OLD, "valid_until_ms": 0, "refused": False},
        {"address": OLD, "valid_until_ms": -1, "refused": False},
        {"address": OLD, "valid_until_ms": True, "refused": False},
        {"address": OLD, "valid_until_ms": None, "refused": 1},
        {"address": OLD, "valid_until_ms": NOW, "refused": True},
        {"address": OLD, "valid_until_ms": None},
        {"address": OLD, "valid_until_ms": None, "refused": False, "x": 1},
        [OLD],
    ],
)
@pytest.mark.parametrize("where", ["current", "staged"])
def test_a_bad_agent_entry_is_refused(tmp_path: Path, agent: object, where: str) -> None:
    slot = slot_in(tmp_path)
    first(slot)
    raw = json.loads(slot.record_path.read_text())
    raw[where] = agent
    raw["held"].append(OLD)
    text = json.dumps(raw)
    slot.record_path.write_text(text)
    with pytest.raises(AgentKeyError, match="is malformed; restore it"):
        load(slot, "testnet")
    assert slot.record_path.read_text() == text


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("{ not json", "is not JSON"),
        ("", "is not JSON"),
        ("[]", "is malformed"),
        ('{"network": "testnet"}', "is malformed"),
        (" " * (64 * 1024) + "{}", "is over 65536 bytes"),
    ],
    ids=["torn", "empty", "a list", "missing keys", "oversize"],
)
def test_a_corrupt_record_is_refused_and_left_as_it_is(tmp_path: Path, text: str, why: str) -> None:
    slot = slot_in(tmp_path)
    first(slot)
    slot.record_path.write_text(text)
    for read in (load, peek, settle):
        with pytest.raises(AgentKeyError, match=f"agent record .* {why}; restore it"):
            read(slot, "testnet")
    assert slot.record_path.read_text() == text


@pytest.mark.parametrize("staged", [{}, [], "", 0, False])
def test_a_staged_entry_that_is_neither_null_nor_an_agent_is_refused(
    tmp_path: Path, staged: object
) -> None:
    slot = slot_in(tmp_path)
    first(slot)
    raw = json.loads(slot.record_path.read_text())
    raw["staged"] = staged
    slot.record_path.write_text(json.dumps(raw))
    with pytest.raises(AgentKeyError, match="is malformed"):
        load(slot, "testnet")


@pytest.mark.parametrize(
    "served",
    [
        "x",
        [None],
        [{"address": OLD, "nonce": 1}],
        [{"address": "0xzz", "nonce": 1}],
        [{"address": None, "nonce": 1}],
        [{"nonce": 1}],
    ],
)
def test_a_bad_served_list_is_refused(tmp_path: Path, served: object) -> None:
    slot = slot_in(tmp_path)
    record = first(slot)
    raw = json.loads(slot.record_path.read_text())
    good = {"address": record.current.address, "nonce": NOW}
    raw["served"] = served
    slot.record_path.write_text(json.dumps(raw))
    with pytest.raises(AgentKeyError, match="is malformed"):
        load(slot, "testnet")
    for nonce in (0, True, 2**53, "1"):
        raw["served"] = [good | {"nonce": nonce}]
        slot.record_path.write_text(json.dumps(raw))
        with pytest.raises(AgentKeyError, match="is malformed"):
            load(slot, "testnet")
    raw["served"] = [good]
    slot.record_path.write_text(json.dumps(raw))
    assert load(slot, "testnet") == record.serving(record.current.address, NOW)


@pytest.mark.parametrize(
    ("held", "network"),
    [
        ("x", "testnet"),
        ([1], "testnet"),
        (["0xzz"], "testnet"),
        ([], "testnet"),
        (None, 1),
    ],
)
def test_a_bad_held_list_or_network_is_refused(
    tmp_path: Path, held: object, network: object
) -> None:
    slot = slot_in(tmp_path)
    record = first(slot)
    raw = json.loads(slot.record_path.read_text())
    raw["held"] = [record.current.address] if held is None else held
    raw["network"] = network
    slot.record_path.write_text(json.dumps(raw))
    with pytest.raises(AgentKeyError, match="is malformed"):
        load(slot, "testnet")


def test_a_record_over_the_limit_is_never_written(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    record = first(slot)
    before = slot.record_path.read_text()
    many = Record(
        "testnet",
        record.current,
        None,
        (record.current.address, *("0x" + f"{i:040x}" for i in range(2000))),
    )
    with pytest.raises(AgentKeyError, match="over 65536 bytes; nothing was written"):
        save(slot, many)
    assert slot.record_path.read_text() == before
    assert names(slot) == ["agent.key", "agent.key.json"]


def test_a_failed_record_replace_keeps_the_old_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slot = slot_in(tmp_path)
    record = first(slot)
    before = slot.record_path.read_text()

    def crash(src: str, dst: str) -> None:
        raise OSError("crash before the rename")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(AgentKeyError, match="crash before the rename"):
        save(slot, record.confirmed(record.current.address, NOW + DAY))
    assert slot.record_path.read_text() == before
    assert names(slot) == ["agent.key", "agent.key.json"]


def test_a_record_for_another_network_is_refused(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    first(slot)
    with pytest.raises(AgentKeyError, match="belongs to testnet, not mainnet"):
        load(slot, "mainnet")


@pytest.mark.parametrize(("swap", "why"), [("symlink", "unreadable: "), ("dir", "not a regular")])
def test_a_record_that_is_not_a_regular_file_is_refused(
    tmp_path: Path, swap: str, why: str
) -> None:
    slot = slot_in(tmp_path)
    first(slot)
    real = slot.record_path.with_name("elsewhere.json")
    os.replace(slot.record_path, real)
    if swap == "symlink":
        slot.record_path.symlink_to(real)
    else:
        slot.record_path.mkdir()
    with pytest.raises(AgentKeyError, match=f"is {why}.*; restore it"):
        load(slot, "testnet")


def test_a_fifo_record_is_refused_without_blocking(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    slot.key_path.parent.mkdir(mode=0o700)
    os.mkfifo(slot.record_path, 0o600)
    with pytest.raises(AgentKeyError, match="is not a regular file; restore it"):
        load(slot, "testnet")


def test_settle_adopts_a_key_without_a_record(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    assert settle(slot, "testnet").record is None and peek(slot, "testnet") is None
    address = write_key(slot.key_path, overwrite=False)
    assert peek(slot, "testnet") == Record.first("testnet", address)
    assert not slot.record_path.exists()
    assert settle(slot, "testnet").record == Record.first("testnet", address)
    assert load(slot, "testnet") == Record.first("testnet", address)


def test_settle_refuses_an_unreadable_key_without_a_record(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    write_key(slot.key_path, overwrite=False)
    slot.key_path.write_text("not a key")
    with pytest.raises(AgentKeyError, match="64 hex digits"):
        settle(slot, "testnet")
    assert not slot.record_path.exists()


def test_settle_adopts_a_staged_key_the_record_does_not_hold(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    record = first(slot)
    loose = write_key(slot.staged_path, overwrite=False)
    settled = settle(slot, "testnet")
    assert settled == settle(slot, "testnet")
    assert settled.record == record.staging(loose) and settled.replaced is None


def test_settle_leaves_a_refused_staged_key_alone(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    record, staged = stage_key(slot, first(slot))
    refused = save(slot, record.refusing(staged.address))
    assert settle(slot, "testnet").record == refused
    assert refused.staged is None and refused.holds(staged.address)


def test_settle_finishes_a_promote_cut_after_the_key_moved(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    record = first(slot)
    old = record.current.address
    record, staged = stage_key(slot, record)
    record = save(slot, record.confirmed(staged.address, NOW + DAY))
    os.replace(slot.staged_path, slot.key_path)
    settled = settle(slot, "testnet")
    assert settled.replaced == old
    assert settled.record == Record(
        "testnet", Agent(staged.address, NOW + DAY), None, (old, staged.address)
    )
    assert settle(slot, "testnet").replaced is None


def test_settle_never_promotes_an_unconfirmed_staged_agent(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    record, _ = stage_key(slot, first(slot))
    os.replace(slot.staged_path, slot.key_path)
    assert settle(slot, "testnet") == settle(slot, "testnet")
    assert load(slot, "testnet") == record


def test_promote_moves_the_staged_key_then_the_record(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    record, staged = stage_key(slot, first(slot))
    secret = key_text(slot.staged_path)
    record = save(slot, record.confirmed(staged.address, NOW + DAY))
    done = promote(slot, record)
    assert done.current == Agent(staged.address, NOW + DAY) and done.staged is None
    assert key_text(slot.key_path) == secret and not slot.staged_path.exists()
    assert load(slot, "testnet") == done


def test_pruning_matches_addresses_whatever_their_case() -> None:
    mixed = "0x" + "aB" * 20
    record = Record("testnet", Agent(mixed), None, (mixed.lower(), OLD))
    assert record.pruned(set(), NOW).held == (mixed.lower(),)
    assert record.pruned({OLD}, NOW).held == (mixed.lower(), OLD)
    served = record.serving(OLD.upper().replace("0X", "0x"), NOW)
    assert served.pruned(set(), NOW).held == (mixed.lower(), OLD)
    assert record.holds(mixed.upper().replace("0X", "0x"))


def test_the_page_subject_is_always_a_held_agent() -> None:
    record = Record.first("testnet", OLD)
    assert record.subject(False) == Agent(OLD)
    assert record.staging(NEW).subject(True) == Agent(NEW)
    with pytest.raises(ValueError, match="no staged agent"):
        record.subject(True)


def test_settle_clears_the_temp_files_a_killed_write_left(tmp_path: Path) -> None:
    slot = slot_in(tmp_path)
    record = first(slot)
    torn = [slot.key_path.with_name(f"agent.key{part}.k1l9.tmp") for part in ("", ".json", ".next")]
    kept = [slot.key_path.with_name("agent.keyring.tmp"), slot.key_path.with_name("other.tmp")]
    for path in torn + kept:
        path.write_text("0x" + "ab" * 32)
    assert peek(slot, "testnet") == record and all(path.exists() for path in torn)
    assert settle(slot, "testnet").record == record
    assert not any(path.exists() for path in torn) and all(path.exists() for path in kept)
