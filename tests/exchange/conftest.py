from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount

from hl_guard.exchange import halt, network
from hl_guard.exchange.actions import Action, build_action
from hl_guard.exchange.nonces import NonceManager


@pytest.fixture(autouse=True)
def _unbound_network() -> Iterator[None]:
    previous = network._enabled
    network._enabled = None
    yield
    network._enabled = previous


@pytest.fixture(autouse=True)
def _no_nonce_managers() -> Iterator[None]:
    NonceManager._by_agent.clear()
    yield
    NonceManager._by_agent.clear()


@pytest.fixture()
def durable_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(halt, "temp_dirs", lambda: [])
    return tmp_path


@pytest.fixture()
def agent() -> LocalAccount:
    account: LocalAccount = Account.create()
    return account


@pytest.fixture()
def master() -> str:
    return str(Account.create().address)


def _order_action() -> Action:
    return build_action(
        "order",
        {
            "orders": [
                {
                    "a": 1,
                    "b": True,
                    "p": "100000",
                    "s": "0.0001",
                    "r": False,
                    "t": {"limit": {"tif": "Gtc"}},
                    "c": "0x" + "0" * 32,
                }
            ],
            "grouping": "na",
        },
    )


@pytest.fixture()
def order_action() -> Callable[[], Action]:
    return _order_action


class KeyWatch(LocalAccount):
    def __init__(self, account: LocalAccount) -> None:
        super().__init__(account._key_obj, account._publicapi)
        self.touches = 0

    @property
    def key(self) -> Any:
        self.touches += 1
        return super().key

    def sign_message(self, signable_message: Any) -> Any:
        self.touches += 1
        return super().sign_message(signable_message)


@pytest.fixture()
def key_watch() -> Callable[[LocalAccount], KeyWatch]:
    return KeyWatch
