from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from hl_guard.exchange import halt, network
from hl_guard.ledger import ledger as ledger_module
from hl_guard.ledger.ledger import Record
from hl_guard.server import info
from server.fake_info import FakeInfo, point_testnet_at, serving
from server.venue import Venue, configure


@pytest.fixture(autouse=True)
def _restore_network() -> Iterator[None]:
    rows, enabled = network.NETWORKS, network._enabled
    network._enabled = None
    yield
    network.NETWORKS, network._enabled = rows, enabled


@pytest.fixture(autouse=True)
def _lock_dir_per_test(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def _info_worker_ends() -> Iterator[None]:
    yield
    if info._worker is not None:
        info._worker.join(5)
        assert not info._worker.is_alive()


@pytest.fixture()
def durable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(halt, "temp_dirs", lambda: [])


@pytest.fixture()
def fake() -> Iterator[FakeInfo]:
    with serving(FakeInfo()) as running:
        point_testnet_at(running.url)
        yield running


@pytest.fixture()
def venue(fake: FakeInfo) -> Venue:
    simulated = Venue()
    simulated.install(fake)
    point_testnet_at(fake.url, fake.url.removesuffix("/info") + "/exchange")
    return simulated


@dataclass
class Clock:
    wall_offset_ns: int = 0
    sleep_counting_ns: int = 0
    monotonic_ns: int = 0

    def advance(self, seconds: float) -> None:
        step = int(seconds * 1_000_000_000)
        self.wall_offset_ns += step
        self.sleep_counting_ns += max(step, 0)
        self.monotonic_ns += max(step, 0)

    def sleep(self, seconds: float) -> None:
        step = int(seconds * 1_000_000_000)
        self.wall_offset_ns += step
        self.sleep_counting_ns += step

    def step(self, seconds: float) -> None:
        self.wall_offset_ns += int(seconds * 1_000_000_000)


DAY_NS = 86_400 * 1_000_000_000


def _offset_to_noon_utc_ns(now_ns: int) -> int:
    return DAY_NS // 2 - now_ns % DAY_NS


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    real = time.time_ns
    moved = Clock(_offset_to_noon_utc_ns(real()))
    monotonic, clock_gettime = time.monotonic_ns, time.clock_gettime_ns

    def elapsed(clock_id: int) -> int:
        return clock_gettime(clock_id) + moved.sleep_counting_ns * (clock_id != time.CLOCK_REALTIME)

    monkeypatch.setattr(time, "time_ns", lambda: real() + moved.wall_offset_ns)
    monkeypatch.setattr(time, "monotonic_ns", lambda: monotonic() + moved.monotonic_ns)
    monkeypatch.setattr(time, "clock_gettime_ns", elapsed)
    return moved


@pytest.fixture()
def config(tmp_path: Path, durable: None, venue: Venue) -> Path:
    return configure(tmp_path)


def work_under_exclusive_lock(monkeypatch: pytest.MonkeyPatch, path: Path) -> list[str]:
    seen: list[str] = []
    decode, apply = ledger_module._decode_and_check_hash, ledger_module._State.apply

    def note(what: str) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            seen.append(what)
        finally:
            os.close(fd)

    def decoded(raw: bytes, line: int | None) -> Record:
        note("decode")
        return decode(raw, line)

    def applied(state: ledger_module._State, record: Record) -> None:
        note("fold")
        apply(state, record)

    monkeypatch.setattr(ledger_module, "_decode_and_check_hash", decoded)
    monkeypatch.setattr(ledger_module._State, "apply", applied)
    return seen
