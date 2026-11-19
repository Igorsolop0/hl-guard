"""The signer's normalTpsl and batchModify shapes: byte-equal to SDK 0.24.0 for
the pinned shapes, and every other shape refused before signing.

Each hostile case is a valid golden action with one field changed, and each
names the reason, so a case cannot pass for another reason than its own.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import msgpack
import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils.crypto import keccak
from hyperliquid.utils import signing as sdk
from hyperliquid.utils.signing import OrderRequest, order_request_to_order_wire
from hyperliquid.utils.types import Cloid

from hl_guard.exchange.actions import (
    ActionConstructionError,
    RecordedChild,
    build_action,
    reduces_risk,
)
from hl_guard.exchange.network import bound_network
from hl_guard.exchange.signer import Signer, SignerError, action_hash

SDK_TEST_KEY = keccak(text="hl-guard golden test key")
ENTRY_C, SL_C, TP_C = ("0x" + d * 32 for d in "abc")
ASSET = 4


def _request(is_buy: bool, sz: float, px: float, kind: dict[str, Any], r: bool, c: str) -> Any:
    request: OrderRequest = {
        "coin": "ETH",
        "is_buy": is_buy,
        "sz": sz,
        "limit_px": px,
        "order_type": kind,  # type: ignore[typeddict-item]
        "reduce_only": r,
        "cloid": Cloid.from_str(c),
    }
    return order_request_to_order_wire(request, ASSET)


def _trigger(px: float, tpsl: str) -> dict[str, Any]:
    return {"trigger": {"triggerPx": px, "isMarket": True, "tpsl": tpsl}}


def sdk_entry(is_buy: bool = True) -> Any:
    return _request(is_buy, 0.0105, 1728.3, {"limit": {"tif": "Ioc"}}, False, ENTRY_C)


def sdk_sl(is_buy: bool = True) -> Any:
    if is_buy:
        return _request(False, 0.0105, 1592.6, _trigger(1676.4, "sl"), True, SL_C)
    return _request(True, 0.0105, 1868.2, _trigger(1779.2, "sl"), True, SL_C)


def sdk_tp(is_buy: bool = True) -> Any:
    if is_buy:
        return _request(False, 0.0105, 1691.6, _trigger(1780.6, "tp"), True, TP_C)
    return _request(True, 0.0105, 1760.2, _trigger(1676.4, "tp"), True, TP_C)


def recorded(wire: dict[str, Any], size: str | None = None) -> RecordedChild:
    trigger = wire["t"]["trigger"]
    return RecordedChild(
        wire["a"], wire["b"], wire["p"], trigger["triggerPx"], trigger["tpsl"], size or wire["s"]
    )


def children() -> dict[str, RecordedChild]:
    return {SL_C: recorded(sdk_sl()), TP_C: recorded(sdk_tp())}


def sdk_modify(wire: dict[str, Any], s: str = "0.0064") -> dict[str, Any]:
    return {"oid": wire["c"], "order": {**wire, "s": s}}


def sdk_batch_modify(*wires: dict[str, Any]) -> dict[str, Any]:
    """The SDK bulk_modify_orders_new action for modifies by cloid."""
    return {"type": "batchModify", "modifies": [sdk_modify(w) for w in wires]}


def _payload(action: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in action.items() if k != "type"}


GOLDEN_ORDERS = {
    "[e, sl]": lambda: [sdk_entry(), sdk_sl()],
    "[e, sl, tp]": lambda: [sdk_entry(), sdk_sl(), sdk_tp()],
    "[e, tp, sl]": lambda: [sdk_entry(), sdk_tp(), sdk_sl()],
    "short [e, sl, tp]": lambda: [sdk_entry(False), sdk_sl(False), sdk_tp(False)],
}


@pytest.fixture()
def sdk_agent() -> LocalAccount:
    account: LocalAccount = Account.from_key(SDK_TEST_KEY)
    return account


@pytest.mark.parametrize("network", ["mainnet", "testnet"])
@pytest.mark.parametrize("shape", GOLDEN_ORDERS)
def test_a_normal_tpsl_order_is_byte_equal_to_the_sdk(
    sdk_agent: LocalAccount, network: str, shape: str
) -> None:
    wires = GOLDEN_ORDERS[shape]()
    sdk_action = sdk.order_wires_to_order_action(wires, None, "normalTpsl")
    ours = build_action("order", {"orders": copy.deepcopy(wires), "grouping": "normalTpsl"})
    assert msgpack.packb(ours) == msgpack.packb(sdk_action)
    nonce, expires = 1_795_125_314_064, 1_795_125_344_064
    assert action_hash(ours, nonce, expires) == sdk.action_hash(sdk_action, None, nonce, expires)
    with bound_network(network, allow_mainnet=True):
        signer = Signer(sdk_agent, str(Account.create().address), network=network)
        _, signature = signer.sign_action(ours, nonce, expires)
    mainnet = network == "mainnet"
    assert signature == sdk.sign_l1_action(sdk_agent, sdk_action, None, nonce, expires, mainnet)


@pytest.mark.parametrize("network", ["mainnet", "testnet"])
def test_a_two_child_batch_modify_is_byte_equal_to_the_sdk(
    sdk_agent: LocalAccount, network: str
) -> None:
    sdk_action = sdk_batch_modify(sdk_tp(), sdk_sl())
    ours = build_action("batchModify", _payload(copy.deepcopy(sdk_action)), children=children())
    assert list(ours) == ["type", "modifies"]
    assert msgpack.packb(ours) == msgpack.packb(sdk_action)
    nonce, expires = 1_795_125_320_945, 1_795_125_350_945
    assert action_hash(ours, nonce, expires) == sdk.action_hash(sdk_action, None, nonce, expires)
    with bound_network(network, allow_mainnet=True):
        master = str(Account.create().address)
        signer = Signer(sdk_agent, master, network=network, children=children)
        _, signature = signer.sign_action(ours, nonce, expires)
    mainnet = network == "mainnet"
    assert signature == sdk.sign_l1_action(sdk_agent, sdk_action, None, nonce, expires, mainnet)


def test_grouping_differs_from_na_only_in_the_grouping_string() -> None:
    wires = [sdk_entry(), sdk_sl(), sdk_tp()]
    grouped = build_action("order", {"orders": copy.deepcopy(wires), "grouping": "normalTpsl"})
    plain = build_action("order", {"orders": copy.deepcopy(wires), "grouping": "na"})
    assert {**grouped, "grouping": "na"} == plain
    assert action_hash(grouped, 1, None) != action_hash(plain, 1, None)


Edit = Callable[[list[dict[str, Any]]], Any]


def _set(index: int, path: tuple[str, ...], value: Any) -> Edit:
    def edit(orders: list[dict[str, Any]]) -> None:
        target = orders[index]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return edit


def _two_sls(orders: list[dict[str, Any]]) -> None:
    orders[2] = {**sdk_sl(), "c": TP_C}


HOSTILE_ORDERS: dict[str, tuple[Edit | None, str | None, str]] = {
    "entry tif Gtc": (_set(0, ("t", "limit", "tif"), "Gtc"), None, "Ioc limit entry"),
    "entry tif Alo": (_set(0, ("t", "limit", "tif"), "Alo"), None, "Ioc limit entry"),
    "entry is a trigger": (
        lambda o: (
            _set(0, ("r",), True)(o)
            or _set(
                0, ("t",), {"trigger": {"isMarket": True, "triggerPx": "1728.3", "tpsl": "sl"}}
            )(o)
        ),
        None,
        "Ioc limit entry",
    ),
    "entry reduce-only": (_set(0, ("r",), True), None, "must not be reduce-only"),
    "child r false": (_set(1, ("r",), False), None, "reduceOnly"),
    "child size differs": (_set(1, ("s",), "0.0104"), None, "is not the entry size"),
    "child same side": (_set(1, ("b",), True), None, "on the entry's side"),
    "child other asset": (_set(1, ("a",), 5), None, "another asset"),
    "child not market": (_set(1, ("t", "trigger", "isMarket"), False), None, "market trigger"),
    "SL on the profit side": (
        _set(1, ("t", "trigger", "triggerPx"), "1780.6"),
        None,
        "not on the loss side",
    ),
    "TP on the loss side": (
        _set(2, ("t", "trigger", "triggerPx"), "1676.4"),
        None,
        "not on the profit side",
    ),
    "child p past the trigger": (_set(1, ("p",), "1700"), None, "exit side"),
    "child p below the hard floor": (_set(1, ("p",), "160"), None, "exit side"),
    "TP without SL": (lambda o: o.pop(1), None, "exactly one sl"),
    "two SLs": (_two_sls, None, "exactly one sl"),
    "four orders": (lambda o: o.append({**sdk_tp(), "c": "0x" + "d" * 32}), None, "4 orders"),
    "duplicate cloid": (_set(2, ("c",), SL_C), None, "share a cloid"),
    "[sl, entry]": (lambda o: o.reverse(), None, "Ioc limit entry"),
    "positionTpsl": (None, "positionTpsl", "grouping"),
    "grouping {p: 1}": (None, {"p": 1}, "grouping"),
    "unknown grouping": (None, "tpslAll", "grouping"),
}


@pytest.mark.parametrize("case", HOSTILE_ORDERS)
def test_a_hostile_normal_tpsl_order_is_refused_for_its_reason(case: str) -> None:
    edit, grouping, reason = HOSTILE_ORDERS[case]
    orders = [sdk_entry(), sdk_sl(), sdk_tp()]
    build_action("order", {"orders": copy.deepcopy(orders), "grouping": "normalTpsl"})
    if edit is not None:
        edit(orders)
    with pytest.raises(ActionConstructionError, match=reason):
        build_action("order", {"orders": orders, "grouping": grouping or "normalTpsl"})


def _modify(index: int, path: tuple[str, ...], value: Any) -> Callable[[dict[str, Any]], None]:
    def edit(action: dict[str, Any]) -> None:
        target = action["modifies"][index]
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return edit


def _to_limit(action: dict[str, Any]) -> None:
    action["modifies"][0]["order"]["t"] = {"limit": {"tif": "Ioc"}}


def _unknown_cloid(action: dict[str, Any]) -> None:
    other = "0x" + "e" * 32
    action["modifies"][0]["oid"] = other
    action["modifies"][0]["order"]["c"] = other


HOSTILE_MODIFIES: dict[str, tuple[Callable[[dict[str, Any]], Any], str]] = {
    "r false": (_modify(0, ("order", "r"), False), "reduceOnly"),
    "limit order type": (_to_limit, "market trigger"),
    "not market": (_modify(0, ("order", "t", "trigger", "isMarket"), False), "market trigger"),
    "changed trigger": (_modify(0, ("order", "t", "trigger", "triggerPx"), "1711.1"), "triggerPx"),
    "changed tpsl": (_modify(0, ("order", "t", "trigger", "tpsl"), "sl"), "tpsl"),
    "changed side": (_modify(0, ("order", "b"), True), "'b'|b True"),
    "changed asset": (_modify(0, ("order", "a"), 5), "a 5"),
    "changed p": (_modify(0, ("order", "p"), "855.55"), "p '855.55'"),
    "size equal": (_modify(0, ("order", "s"), "0.0105"), "below the recorded"),
    "size larger": (_modify(0, ("order", "s"), "0.02"), "below the recorded"),
    "size zero": (_modify(0, ("order", "s"), "0"), "above 0"),
    "unknown cloid": (_unknown_cloid, "not a stop child that hl-guard recorded"),
    "oid is not c": (_modify(0, ("oid",), SL_C), "not the order's cloid"),
    "numeric oid": (_modify(0, ("oid",), 900042), "cloid string"),
    "three modifies": (
        lambda a: a["modifies"].append(copy.deepcopy(a["modifies"][0])),
        "1 to 2",
    ),
    "one cloid twice": (lambda a: a["modifies"].__setitem__(1, a["modifies"][0]), "one cloid"),
}


@pytest.mark.parametrize("case", HOSTILE_MODIFIES)
def test_a_hostile_batch_modify_is_refused_before_signing(case: str) -> None:
    edit, reason = HOSTILE_MODIFIES[case]
    action = sdk_batch_modify(sdk_tp(), sdk_sl())
    build_action("batchModify", _payload(copy.deepcopy(action)), children=children())
    edit(action)
    with pytest.raises(ActionConstructionError, match=reason):
        build_action("batchModify", _payload(action), children=children())


def test_a_batch_modify_needs_the_recorded_children() -> None:
    action = sdk_batch_modify(sdk_sl())
    with pytest.raises(ActionConstructionError, match="no recorded children"):
        build_action("batchModify", _payload(action))


def test_a_batch_modify_checks_the_latest_recorded_size() -> None:
    shrunk = {SL_C: recorded(sdk_sl(), "0.0064")}
    action = sdk_batch_modify(sdk_sl())
    with pytest.raises(ActionConstructionError, match=r"below the recorded 0\.0064"):
        build_action("batchModify", _payload(action), children=shrunk)
    action["modifies"][0]["order"]["s"] = "0.003"
    build_action("batchModify", _payload(action), children=shrunk)


def test_the_signer_refuses_a_batch_modify_for_a_cloid_it_has_not_recorded(
    agent: LocalAccount, master: str
) -> None:
    action = build_action("batchModify", _payload(sdk_batch_modify(sdk_sl())), children=children())
    with pytest.raises(SignerError, match="allowlist"):
        Signer(agent, master, network="testnet").sign_action(action, 1)
    with pytest.raises(SignerError, match="allowlist"):
        Signer(agent, master, network="testnet", children=dict).sign_action(action, 1)
    Signer(agent, master, network="testnet", children=children).sign_action(action, 1)


def test_a_shrink_passes_a_halt_and_a_grouped_entry_does_not() -> None:
    shrink = build_action(
        "batchModify", _payload(sdk_batch_modify(sdk_tp(), sdk_sl())), children=children()
    )
    entry = build_action(
        "order", {"orders": [sdk_entry(), sdk_sl(), sdk_tp()], "grouping": "normalTpsl"}
    )
    assert reduces_risk(shrink) is True
    assert reduces_risk(entry) is False
    assert reduces_risk({"type": "batchModify", "modifies": []}) is False
    opening = {"type": "batchModify", "modifies": [{"oid": SL_C, "order": {"r": False}}]}
    assert reduces_risk(opening) is False
