import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from eth_account.signers.local import LocalAccount
from hyperliquid.utils.signing import sign_l1_action as sdk_sign

from hl_guard.exchange import network
from hl_guard.exchange.actions import Action
from hl_guard.exchange.network import Network, NetworkError, network_for
from hl_guard.exchange.signer import Signer, SignerError

PKG = Path(network.__file__).resolve().parents[1]
OTHER = Network(
    name="othernet",
    exchange_url="https://exchange.othernet.invalid/exchange",
    info_url="https://info.othernet.invalid/info",
    phantom_source="a",
)


@pytest.fixture()
def two_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        network, "NETWORKS", MappingProxyType({**network.NETWORKS, "othernet": OTHER})
    )


def test_the_build_knows_testnet_and_mainnet_and_enables_testnet_until_bound() -> None:
    assert set(network.NETWORKS) == {"testnet", "mainnet"}
    t = network_for("testnet")
    assert t.exchange_url == "https://api.hyperliquid-testnet.xyz/exchange"
    assert t.info_url == "https://api.hyperliquid-testnet.xyz/info"
    assert t.phantom_source == "b"
    main = network.NETWORKS["mainnet"]
    assert main.exchange_url == "https://api.hyperliquid.xyz/exchange"
    assert main.info_url == "https://api.hyperliquid.xyz/info"
    assert main.phantom_source == "a"
    with pytest.raises(NetworkError, match="not enabled"):
        network_for("mainnet")


def test_mainnet_binds_only_with_the_explicit_flag() -> None:
    with pytest.raises(NetworkError, match="mainnet flag"):
        network.enable_network("mainnet")
    assert network.enabled().name == "testnet"
    with network.bound_network("mainnet", allow_mainnet=True) as row:
        assert row.name == "mainnet"
        assert network_for("mainnet") == row
    assert network.enabled().name == "testnet"


def test_a_worker_thread_sees_the_process_network() -> None:
    seen: list[str] = []

    def _run() -> None:
        try:
            seen.append(network_for("mainnet").name)
        except NetworkError as exc:
            seen.append(str(exc))

    with network.bound_network("mainnet", allow_mainnet=True):
        thread = threading.Thread(target=_run)
        thread.start()
        thread.join()
    assert seen == ["mainnet"]


@pytest.mark.parametrize("name", ["mainnet", "live", "Testnet", "", None, 1, "testnet "])
def test_any_other_name_is_refused(name: Any) -> None:
    with pytest.raises(NetworkError):
        network_for(name)


@pytest.mark.parametrize("name", ["live", "Testnet", "", None, 1, "othernet"])
def test_enable_network_refuses_unknown_names(name: Any) -> None:
    with pytest.raises(NetworkError, match="unknown"):
        network.enable_network(name, allow_mainnet=True)


def test_a_known_network_that_is_not_enabled_is_refused(two_networks: None) -> None:
    with pytest.raises(NetworkError, match="not enabled"):
        network_for("othernet")


def test_the_table_cannot_be_edited_in_place() -> None:
    table: Any = network.NETWORKS
    with pytest.raises(TypeError):
        table["mainnet"] = OTHER
    row: Any = network_for("testnet")
    with pytest.raises(AttributeError):
        row.exchange_url = "https://x.invalid"


def test_url_text_lives_in_network_py_only() -> None:
    holders = sorted(
        str(p.relative_to(PKG))
        for p in PKG.rglob("*.py")
        if "api.hyperliquid" in p.read_text(encoding="utf-8")
    )
    assert holders == ["exchange/network.py"]


def test_signer_refuses_a_disabled_network(agent: LocalAccount, master: str) -> None:
    assert Signer(agent, master, network="testnet").network == "testnet"
    for name in ("mainnet", "othernet", ""):
        with pytest.raises(SignerError):
            Signer(agent, master, network=name)


def test_a_disabled_network_is_refused_before_the_key_is_checked(master: str) -> None:
    not_an_account: Any = object()
    with pytest.raises(SignerError, match="mainnet"):
        Signer(not_an_account, master, network="mainnet")


def test_the_network_goes_into_the_signature(
    agent: LocalAccount,
    master: str,
    two_networks: None,
    order_action: Callable[[], Action],
) -> None:
    action, nonce, exp = order_action(), 77, 1757300000000
    _, test_sig = Signer(agent, master, network="testnet").sign_action(action, nonce, exp)
    with network.bound_network("othernet"):
        _, other_sig = Signer(agent, master, network="othernet").sign_action(action, nonce, exp)
    assert test_sig == sdk_sign(agent, action, None, nonce, exp, False)
    assert other_sig == sdk_sign(agent, action, None, nonce, exp, True)
    assert test_sig != other_sig


def test_a_row_with_the_same_name_and_another_source_does_not_sign(
    agent: LocalAccount, master: str, order_action: Callable[[], Action]
) -> None:
    signer = Signer(agent, master, network="testnet")
    signer._network = replace(network.NETWORKS["testnet"], phantom_source="a")
    assert signer.network == "testnet"
    with pytest.raises(SignerError, match="differs from the network table"):
        signer.sign_action(order_action(), 1, None)


def test_a_signer_whose_network_was_disabled_later_does_not_sign(
    agent: LocalAccount, master: str, order_action: Callable[[], Action]
) -> None:
    signer = Signer(agent, master, network="testnet")
    with (
        network.bound_network("mainnet", allow_mainnet=True),
        pytest.raises(SignerError, match="not enabled"),
    ):
        signer.sign_action(order_action(), 1, None)


@pytest.mark.parametrize("flag", ["no", "true", "True", 1, 1.0, "yes", [True], object()])
def test_mainnet_needs_the_flag_to_be_true_itself(flag: Any) -> None:
    with pytest.raises(NetworkError, match="mainnet flag"):
        network.enable_network("mainnet", allow_mainnet=flag)
    with (
        pytest.raises(NetworkError, match="mainnet flag"),
        network.bound_network("mainnet", allow_mainnet=flag),
    ):
        pytest.fail("bound_network entered mainnet with a non-True flag")
    assert network._enabled is None
