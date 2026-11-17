from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from types import MappingProxyType


class NetworkError(ValueError):
    """The named network is unknown or not enabled for this process."""


@dataclass(frozen=True)
class Network:
    name: str
    exchange_url: str
    info_url: str
    phantom_source: str


NETWORKS: Mapping[str, Network] = MappingProxyType(
    {
        "testnet": Network(
            name="testnet",
            exchange_url="https://api.hyperliquid-testnet.xyz/exchange",
            info_url="https://api.hyperliquid-testnet.xyz/info",
            phantom_source="b",
        ),
        "mainnet": Network(
            name="mainnet",
            exchange_url="https://api.hyperliquid.xyz/exchange",
            info_url="https://api.hyperliquid.xyz/info",
            phantom_source="a",
        ),
    }
)

_enabled: Network | None = None


def enabled() -> Network:
    """The row this process is bound to. Unbound resolves testnet."""
    return NETWORKS["testnet"] if _enabled is None else _enabled


def _resolve(name: object, *, allow_mainnet: bool) -> Network:
    if not isinstance(name, str) or name not in NETWORKS:
        raise NetworkError(f"unknown network {name!r}")
    if name == "mainnet" and allow_mainnet is not True:
        raise NetworkError("mainnet is refused without the explicit mainnet flag")
    return NETWORKS[name]


def enable_network(name: object, *, allow_mainnet: bool = False) -> Network:
    """Bind this process to ``name``. Does not read a file or a key."""
    global _enabled
    row = _resolve(name, allow_mainnet=allow_mainnet)
    _enabled = row
    return row


@contextmanager
def bound_network(name: object, *, allow_mainnet: bool = False) -> Iterator[Network]:
    """``enable_network`` for one block, restored when the block ends."""
    global _enabled
    row = _resolve(name, allow_mainnet=allow_mainnet)
    previous = _enabled
    _enabled = row
    try:
        yield row
    finally:
        _enabled = previous


def network_for(name: object) -> Network:
    """The process-enabled network called ``name``. Fails closed otherwise."""
    if not isinstance(name, str) or name not in NETWORKS:
        raise NetworkError(f"unknown network {name!r}")
    current = enabled()
    if name != current.name:
        raise NetworkError(
            f"network {name!r} is not enabled for this process (enabled: {current.name!r})"
        )
    return NETWORKS[name]
