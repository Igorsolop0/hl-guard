import json
from collections.abc import Callable
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_keys.main import KeyAPI
from hyperliquid.utils.signing import action_hash as sdk_hash
from hyperliquid.utils.signing import l1_payload as sdk_payload
from hyperliquid.utils.signing import sign_l1_action as sdk_sign

from hl_guard.exchange.actions import Action, ActionConstructionError, build_action
from hl_guard.exchange.signer import Signer, SignerError, action_hash, l1_payload


@pytest.fixture()
def signer(agent: LocalAccount, master: str) -> Signer:
    return Signer(agent, account_address=master, network="testnet")


def test_action_hash_matches_sdk(order_action: Callable[[], Action]) -> None:
    action = order_action()
    for nonce, exp in [(0, None), (7, 10**9), (2**64 - 1, 2**64 - 1)]:
        assert action_hash(action, nonce, exp) == sdk_hash(action, None, nonce, exp)


def test_l1_payload_matches_sdk() -> None:
    agent = {"source": "b", "connectionId": b"\x01" * 32}
    assert l1_payload(agent) == sdk_payload(agent)


def test_l1_payload_testnet_source() -> None:
    payload = l1_payload({"source": "b", "connectionId": b"\x02" * 32})
    assert payload["message"]["source"] == "b"
    assert payload["primaryType"] == "Agent"
    assert payload["domain"]["name"] == "Exchange"


def test_signer_rejects_non_build_action_dicts(signer: Signer) -> None:
    bad: list[Any] = [{}, {"type": "order"}, {"coin": "BTC", "orders": []}, [1, 2], "order"]
    for value in bad:
        with pytest.raises(SignerError):
            signer.sign_action(value, nonce=0)


def test_signer_rejects_forbidden_action_types(signer: Signer) -> None:
    for ftype in (
        "agentSendAsset",
        "usdClassTransfer",
        "withdraw3",
        "approveAgent",
        "approveBuilderFee",
        "sendAsset",
    ):
        with pytest.raises(ActionConstructionError):
            build_action(ftype, {"whatever": 1})
        with pytest.raises(SignerError):
            signer.sign_action({"type": ftype, "whatever": 1}, nonce=0)


def test_signer_rejects_fuzzed_type_names(signer: Signer) -> None:
    for name in (
        "Order",
        " order",
        "order\n",
        "ord\x00er",
        "order;",
        "usdClassTransfer ",
        "ORDER",
        "sign_l1_action",
    ):
        with pytest.raises(ActionConstructionError):
            build_action(name, {})
        with pytest.raises(SignerError):
            signer.sign_action({"type": name}, nonce=0)


def test_signer_rejects_mutated_action_dict(
    signer: Signer, order_action: Callable[[], Action]
) -> None:
    action = order_action()
    action["vaultAddress"] = "0x" + "cd" * 20
    with pytest.raises(SignerError):
        signer.sign_action(action, nonce=0)

    action = order_action()
    action["builder"] = {"b": 1}
    with pytest.raises(SignerError):
        signer.sign_action(action, nonce=0)

    action = order_action()
    reordered = {"orders": action["orders"], "grouping": action["grouping"], "type": "order"}
    with pytest.raises(SignerError, match="canonical"):
        signer.sign_action(reordered, nonce=0)

    action = order_action()
    action["orders"][0]["s"] = "0.0002"
    canonical, _ = signer.sign_action(action, nonce=0)
    assert canonical["orders"][0]["s"] == "0.0002"


def test_signer_rejects_bad_nonce_expires_master(
    agent: LocalAccount, signer: Signer, order_action: Callable[[], Action]
) -> None:
    bad_nonces: list[Any] = [-1, "0", True, 1.0, None, 2**64]
    for nonce in bad_nonces:
        with pytest.raises(SignerError):
            signer.sign_action(order_action(), nonce=nonce)
    with pytest.raises(SignerError):
        signer.sign_action(order_action(), nonce=0, expires_after=-5)
    with pytest.raises(SignerError):
        Signer(agent, account_address="", network="testnet")
    not_an_account: Any = agent.key.hex()
    with pytest.raises(SignerError, match="LocalAccount"):
        Signer(not_an_account, account_address=agent.address, network="testnet")


def test_signature_is_recoverable_to_agent_address(
    signer: Signer, order_action: Callable[[], Action]
) -> None:
    action = order_action()
    canonical, sig = signer.sign_action(action, nonce=42, expires_after=None)
    assert canonical == action

    digest = action_hash(action, 42, None)
    signable = encode_typed_data(full_message=l1_payload({"source": "b", "connectionId": digest}))
    raw = (
        int(sig["r"], 16).to_bytes(32, "big")
        + int(sig["s"], 16).to_bytes(32, "big")
        + bytes([int(sig["v"])])
    )
    assert Account.recover_message(signable, signature=raw) == signer.agent_address
    assert set(sig) == {"r", "s", "v"}
    assert int(sig["v"]) in (27, 28)


def test_signature_varies_with_nonce_and_action(
    signer: Signer, order_action: Callable[[], Action]
) -> None:
    action = order_action()
    _, s1 = signer.sign_action(action, nonce=1)
    _, s2 = signer.sign_action(action, nonce=2)
    assert s1["r"] != s2["r"]
    action2 = build_action(
        "order", {"orders": [{**action["orders"][0], "s": "0.0002"}], "grouping": "na"}
    )
    _, s3 = signer.sign_action(action2, nonce=1)
    assert s1["r"] != s3["r"]


def test_signed_hash_is_identical_for_equivalent_decimal_string_and_float() -> None:
    wire: dict[str, Any] = {"a": 25, "b": True, "r": False, "t": {"limit": {"tif": "Ioc"}}}
    string_action = build_action(
        "order", {"orders": [{**wire, "p": "0.72703", "s": "20.00", "c": "0x" + "1" * 32}]}
    )
    float_action = build_action(
        "order", {"orders": [{**wire, "p": 0.72703, "s": 20.0, "c": "0x" + "1" * 32}]}
    )
    assert action_hash(string_action, 42, 102) == action_hash(float_action, 42, 102)


def test_signer_exposes_no_private_key(agent: LocalAccount, signer: Signer) -> None:
    blob = json.dumps([signer.agent_address, signer.account_address, signer.network])
    assert agent.key.hex().removeprefix("0x") not in blob
    assert not hasattr(signer, "private_key")
    assert not hasattr(signer, "key")


def test_signature_bytes_match_sdk_sign_l1_action(
    agent: LocalAccount, signer: Signer, order_action: Callable[[], Action]
) -> None:
    action = order_action()
    nonce, expires = 77, 1757300000000
    _, ours = signer.sign_action(action, nonce=nonce, expires_after=expires)
    assert ours == sdk_sign(agent, action, None, nonce, expires, False)
    assert len(ours["r"]) <= 66 and len(ours["s"]) <= 66


def test_signing_runs_on_libsecp256k1() -> None:
    assert type(KeyAPI().backend).__name__ == "CoinCurveECCBackend"


def test_the_key_watch_sees_a_normal_signature(
    key_watch: Callable[[LocalAccount], Any],
    agent: LocalAccount,
    master: str,
    order_action: Callable[[], Action],
) -> None:
    watched = key_watch(agent)
    Signer(watched, master, network="testnet").sign_action(order_action(), nonce=1)
    assert watched.touches > 0


@pytest.mark.parametrize(
    "backend",
    ["eth_keys.backends.NativeECCBackend", "eth_keys.backends.base.BaseECCBackend"],
)
def test_signing_is_refused_off_libsecp256k1(
    key_watch: Callable[[LocalAccount], Any],
    agent: LocalAccount,
    master: str,
    order_action: Callable[[], Action],
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    watched = key_watch(agent)
    signer = Signer(watched, master, network="testnet")
    monkeypatch.setenv("ECC_BACKEND_CLASS", backend)
    with pytest.raises(SignerError, match="libsecp256k1"):
        signer.sign_action(order_action(), nonce=1)
    assert watched.touches == 0


def test_signing_is_refused_when_coincurve_is_missing(
    agent: LocalAccount,
    master: str,
    order_action: Callable[[], Action],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import eth_keys.backends

    monkeypatch.delenv("ECC_BACKEND_CLASS", raising=False)
    monkeypatch.setattr(eth_keys.backends, "is_coincurve_available", lambda: False)
    with pytest.raises(SignerError, match="libsecp256k1"):
        Signer(agent, master, network="testnet").sign_action(order_action(), nonce=1)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "0x",
        "0x1234",
        "0x" + "ab" * 19,
        "0x" + "ab" * 21,
        "0x" + "zz" * 20,
        "ab" * 20,
        " 0x" + "ab" * 20,
        "0x" + "ab" * 20 + "\n",
        None,
        12,
        b"0x" + b"ab" * 20,
    ],
    ids=repr,
)
def test_the_account_address_must_be_an_address(agent: LocalAccount, bad: Any) -> None:
    with pytest.raises(SignerError, match="account_address"):
        Signer(agent, bad, network="testnet")


@pytest.mark.parametrize(
    ("action_type", "payload"),
    [
        ("cancel", {"cancels": [{"a": 9_999, "o": 2**64 - 1}]}),
        ("scheduleCancel", {"time": 2**64 - 1}),
    ],
)
def test_the_largest_buildable_ints_sign(
    signer: Signer, action_type: str, payload: dict[str, Any]
) -> None:
    canonical, sig = signer.sign_action(build_action(action_type, payload), nonce=2**64 - 1)
    assert set(sig) == {"r", "s", "v"}
    assert canonical == build_action(action_type, payload)


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("msgpack.packb", OverflowError("Integer value out of range")),
        ("msgpack.packb", ValueError("bad value")),
        ("encode_typed_data", ValueError("bad typed data")),
        ("encode_typed_data", OverflowError("too big")),
    ],
)
def test_an_encoding_error_inside_the_signer_is_a_signer_error(
    monkeypatch: pytest.MonkeyPatch,
    key_watch: Callable[[LocalAccount], Any],
    agent: LocalAccount,
    master: str,
    order_action: Callable[[], Action],
    target: str,
    error: Exception,
) -> None:
    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(f"hl_guard.exchange.signer.{target}", _raise)
    watched = key_watch(agent)
    with pytest.raises(SignerError, match="encod"):
        Signer(watched, master, network="testnet").sign_action(order_action(), nonce=1)
    assert watched.touches == 0
