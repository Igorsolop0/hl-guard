import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from hl_guard.exchange.nonces import (
    NonceManager,
    NonceWindowError,
    nonce_in_window,
)

T0 = 1_788_700_000_000
AGENT = "0x" + "ab" * 20
OTHER_AGENT = "0x" + "cd" * 20


def test_first_nonce_is_now() -> None:
    m = NonceManager(AGENT)
    assert m.next_nonce(T0) == T0
    assert m.last_nonce == T0


def test_burst_in_one_ms_is_strictly_monotonic() -> None:
    m = NonceManager(AGENT)
    n1 = m.next_nonce(T0)
    n2 = m.next_nonce(T0)
    n3 = m.next_nonce(T0)
    assert (n1, n2, n3) == (T0, T0 + 1, T0 + 2)


def test_clock_still_monotonic_across_later_now() -> None:
    m = NonceManager(AGENT)
    assert m.next_nonce(T0) == T0
    assert m.next_nonce(T0 + 5) == T0 + 5
    assert m.next_nonce(T0 + 5) == T0 + 6


def test_small_rollback_is_monotonic_big_rollback_raises() -> None:
    m = NonceManager(AGENT)
    assert m.next_nonce(T0) == T0
    assert m.next_nonce(T0 - 1) == T0 + 1
    assert m.last_nonce == T0 + 1
    with pytest.raises(NonceWindowError):
        m.next_nonce(T0 - 3 * 24 * 3600 * 1000)
    assert m.last_nonce == T0 + 1


def test_forward_jump_outside_window_raises() -> None:
    m = NonceManager(AGENT, last_nonce=T0)
    with pytest.raises(NonceWindowError):
        m.next_nonce(T0 - 2 * 24 * 3600 * 1000 - 2)
    assert m.last_nonce == T0


def test_window_edges_are_inclusive() -> None:
    assert nonce_in_window(0, 2 * 24 * 3600 * 1000)
    assert nonce_in_window(24 * 3600 * 1000, 0)
    assert not nonce_in_window(0, 2 * 24 * 3600 * 1000 + 1)
    assert not nonce_in_window(24 * 3600 * 1000 + 1, 0)


def test_bad_constructor_and_now_args() -> None:
    def _mk(v: Any) -> NonceManager:
        return NonceManager(AGENT, last_nonce=v)

    with pytest.raises(ValueError):
        _mk(-1)
    with pytest.raises(ValueError):
        _mk(1.5)
    m = NonceManager(AGENT)

    def _next(v: Any) -> int:
        return m.next_nonce(v)

    for bad in (-1, 1.5, "x", True):
        with pytest.raises(ValueError):
            _next(bad)


def test_rejects_bool_nonce_inputs() -> None:
    m = NonceManager(AGENT)

    def _next(v: Any) -> int:
        return m.next_nonce(v)

    with pytest.raises(ValueError):
        _next(True)


def test_restored_manager_continues_monotonic() -> None:
    m = NonceManager(AGENT, last_nonce=T0 + 41)
    assert m.next_nonce(T0 + 41) == T0 + 42
    assert m.next_nonce(T0) == T0 + 43


def test_a_seed_only_ever_raises_the_last_nonce() -> None:
    m = NonceManager(AGENT, last_nonce=T0 + 10)
    m.seed(T0 + 500)
    assert m.next_nonce(T0) == T0 + 501
    m.seed(T0 + 5)
    assert m.next_nonce(T0) == T0 + 502
    fresh = NonceManager(OTHER_AGENT)
    fresh.seed(T0 + 7)
    assert fresh.next_nonce(T0) == T0 + 8
    assert fresh.next_nonce(T0 + 100) == T0 + 100


def test_a_thousand_nonces_in_one_millisecond_are_unique_and_increasing() -> None:
    m = NonceManager(AGENT)
    nonces = [m.next_nonce(T0) for _ in range(1000)]
    assert nonces == list(range(T0, T0 + 1000))


@pytest.fixture()
def tiny_switch_interval() -> Iterator[None]:
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    yield
    sys.setswitchinterval(previous)


@pytest.mark.usefixtures("tiny_switch_interval")
def test_threads_sharing_one_manager_never_get_the_same_nonce() -> None:
    m = NonceManager(AGENT)
    per_thread = 2000
    results: list[list[int]] = [[] for _ in range(8)]
    start = threading.Barrier(len(results))

    def work(out: list[int]) -> None:
        start.wait()
        for _ in range(per_thread):
            out.append(m.next_nonce(T0))

    threads = [threading.Thread(target=work, args=(out,)) for out in results]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    nonces = [n for out in results for n in out]
    assert len(nonces) == len(set(nonces)) == len(results) * per_thread
    assert all(out == sorted(out) for out in results)


def test_for_agent_returns_the_one_manager_for_that_key() -> None:
    first = NonceManager.for_agent(AGENT)
    for spelling in (AGENT, "0x" + "AB" * 20, "0x" + "aB" * 20, "0x" + "Ab" * 20):
        assert NonceManager.for_agent(spelling) is first
    assert NonceManager.for_agent(OTHER_AGENT) is not first


def test_another_case_of_one_agent_key_is_refused_a_second_manager() -> None:
    NonceManager(AGENT)
    with pytest.raises(ValueError, match="already"):
        NonceManager("0x" + "AB" * 20)


@pytest.mark.parametrize("registered", [False, True])
def test_the_0X_prefix_is_refused_by_for_agent_and_the_constructor(registered: bool) -> None:
    if registered:
        NonceManager(AGENT)
    upper_prefix = "0X" + "ab" * 20
    with pytest.raises(ValueError, match="agent address"):
        NonceManager.for_agent(upper_prefix)
    with pytest.raises(ValueError, match="agent address"):
        NonceManager(upper_prefix)


def test_a_second_manager_for_one_agent_key_is_refused() -> None:
    first = NonceManager(AGENT)
    first.next_nonce(T0)
    with pytest.raises(ValueError, match="already"):
        NonceManager(AGENT, last_nonce=T0 - 10)
    assert NonceManager.for_agent(AGENT).next_nonce(T0) == T0 + 1


@pytest.mark.usefixtures("tiny_switch_interval")
def test_threads_asking_for_one_agent_share_one_manager() -> None:
    seen: list[NonceManager] = []
    start = threading.Barrier(8)

    def work() -> None:
        start.wait()
        seen.append(NonceManager.for_agent(AGENT))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(m) for m in seen}) == 1


@pytest.mark.parametrize("bad", ["", "0x1234", "11" * 20, "0x" + "zz" * 20, None, 7])
def test_the_agent_address_must_be_an_address(bad: Any) -> None:
    with pytest.raises(ValueError, match="agent address"):
        NonceManager(bad)
