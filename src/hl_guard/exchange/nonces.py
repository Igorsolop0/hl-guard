from __future__ import annotations

import re
import threading
from typing import ClassVar

WINDOW_BACK_MS = 2 * 24 * 3600 * 1000
WINDOW_FWD_MS = 1 * 24 * 3600 * 1000
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


class NonceWindowError(RuntimeError):
    """The next nonce falls outside the exchange window. Halt, do not retry."""


def nonce_in_window(nonce: int, now_ms: int) -> bool:
    return now_ms - WINDOW_BACK_MS <= nonce <= now_ms + WINDOW_FWD_MS


def _agent_key(agent_address: object) -> str:
    if not isinstance(agent_address, str) or not _ADDRESS_RE.fullmatch(agent_address):
        raise ValueError(f"agent address must be 0x and 40 hex digits: {agent_address!r}")
    return agent_address.lower()


class NonceManager:
    """Strictly increasing nonces for one agent key.

    The exchange tracks nonces per signer, so two managers for one agent key
    would hand out the same nonce. A process holds at most one manager per
    agent address: ``for_agent`` returns it, and constructing a second raises.
    """

    _by_agent: ClassVar[dict[str, NonceManager]] = {}
    _registry_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self, agent_address: str, last_nonce: int | None = None) -> None:
        key = _agent_key(agent_address)
        if last_nonce is not None and (type(last_nonce) is not int or last_nonce < 0):
            raise ValueError("last_nonce must be a non-negative int or None")
        self._last = last_nonce
        self._lock = threading.Lock()
        with NonceManager._registry_lock:
            if key in NonceManager._by_agent:
                raise ValueError(f"a nonce manager for agent {agent_address} already exists")
            NonceManager._by_agent[key] = self

    @classmethod
    def for_agent(cls, agent_address: str) -> NonceManager:
        with cls._registry_lock:
            existing = cls._by_agent.get(_agent_key(agent_address))
            return existing if existing is not None else cls(agent_address)

    def seed(self, last_nonce: int) -> None:
        """Never hand out ``last_nonce`` or below, such as a nonce a ledger
        recorded before a restart."""
        with self._lock:
            if self._last is None or last_nonce > self._last:
                self._last = last_nonce

    @property
    def last_nonce(self) -> int | None:
        return self._last

    def next_nonce(self, now_ms: int) -> int:
        """Return ``max(last + 1, now_ms)``. State does not advance on error."""
        if type(now_ms) is not int or now_ms < 0:
            raise ValueError("now_ms must be a non-negative int (ms epoch)")
        with self._lock:
            candidate = now_ms if self._last is None else max(self._last + 1, now_ms)
            if not nonce_in_window(candidate, now_ms):
                raise NonceWindowError(
                    f"nonce {candidate} outside the exchange window "
                    f"[{now_ms - WINDOW_BACK_MS}, {now_ms + WINDOW_FWD_MS}] "
                    f"at now_ms={now_ms} (clock rollback or jump: halt, do not retry)"
                )
            self._last = candidate
            return candidate
