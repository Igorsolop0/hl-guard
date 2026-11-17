import ast
import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import hyperliquid
import pytest
from hyperliquid.utils.signing import float_to_wire

from hl_guard.exchange.actions import (
    ALLOWED_ACTION_TYPES,
    DOCUMENTED_ACTION_TYPES,
    FORBIDDEN_ACTION_TYPES,
    ActionConstructionError,
    ForbiddenActionError,
    build_action,
)

ALLOWED = {
    "order",
    "cancel",
    "cancelByCloid",
    "scheduleCancel",
    "updateLeverage",
    "updateIsolatedMargin",
}


def test_allowed_action_types_are_the_six_trade_verbs() -> None:
    assert ALLOWED_ACTION_TYPES == ALLOWED


def test_documented_types_partition_into_allowed_and_forbidden() -> None:
    assert ALLOWED_ACTION_TYPES | FORBIDDEN_ACTION_TYPES == DOCUMENTED_ACTION_TYPES
    assert not (ALLOWED_ACTION_TYPES & FORBIDDEN_ACTION_TYPES)


def test_every_money_and_permission_type_is_forbidden() -> None:
    money_and_permission = {
        "sendAsset",
        "agentSendAsset",
        "sendToEvmWithData",
        "usdSend",
        "spotSend",
        "withdraw3",
        "usdClassTransfer",
        "cDeposit",
        "cWithdraw",
        "tokenDelegate",
        "vaultTransfer",
        "hip3LiquidatorTransfer",
        "approveAgent",
        "approveBuilderFee",
        "twapOrder",
        "twapCancel",
        "reserveRequestWeight",
        "userDexAbstraction",
        "agentDexAbstraction",
        "agentSetAbstraction",
        "userSetAbstraction",
        "noop",
    }
    assert money_and_permission <= FORBIDDEN_ACTION_TYPES


DOCS_SNAPSHOT_URL = (
    "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint.md"
)
DOCS_SNAPSHOT_FETCHED = date(2026, 10, 20)
DOCS_SNAPSHOT_TYPES = {
    "agentEnableDexAbstraction",
    "agentSendAsset",
    "agentSetAbstraction",
    "approveAgent",
    "approveBuilderFee",
    "authorizeAqav2Role",
    "batchModify",
    "cDeposit",
    "cWithdraw",
    "cancel",
    "cancelByCloid",
    "claimRewards",
    "hip3LiquidatorTransfer",
    "modify",
    "noop",
    "order",
    "reserveRequestWeight",
    "scheduleCancel",
    "sendAsset",
    "sendToEvmWithData",
    "spotSend",
    "tokenDelegate",
    "topUpIsolatedOnlyMargin",
    "trailingStop",
    "twapCancel",
    "twapOrder",
    "updateIsolatedMargin",
    "updateLeverage",
    "usdClassTransfer",
    "usdSend",
    "userDexAbstraction",
    "userOutcome",
    "userSetAbstraction",
    "validatorL1Stream",
    "vaultTransfer",
    "withdraw3",
}


def test_every_type_on_the_docs_page_is_allowed_or_forbidden() -> None:
    source = f"{DOCS_SNAPSHOT_URL} fetched {DOCS_SNAPSHOT_FETCHED}"
    assert DOCS_SNAPSHOT_TYPES <= DOCUMENTED_ACTION_TYPES, source
    assert DOCS_SNAPSHOT_TYPES - ALLOWED_ACTION_TYPES <= FORBIDDEN_ACTION_TYPES, source
    assert "batchModify" in FORBIDDEN_ACTION_TYPES


def test_modify_refused_until_sdk_conflict_resolved() -> None:
    with pytest.raises(ForbiddenActionError, match="batchModify"):
        build_action("modify", payload={"order": {}})


@pytest.mark.parametrize("action_type", sorted(FORBIDDEN_ACTION_TYPES - {"modify"}))
def test_every_forbidden_type_is_rejected_before_signing(action_type: str) -> None:
    with pytest.raises(ForbiddenActionError) as excinfo:
        build_action(action_type, payload={"anything": 1})
    assert action_type in str(excinfo.value)


@pytest.mark.parametrize(
    "bad_name",
    [
        "Order",
        "ORDER",
        " order",
        "order ",
        "usdSend\x00",
        "agentSendAsset ",
        "approveagent",
        "withdraw",
        "sendAsset2",
        "twapOrder/twapCancel",
        "usdClassTransfer\x0b",
        "reserverequestweight",
        "batchmodify",
        "",
        "🎲",
        "order'; --",
        "__import__",
        "order\n",
    ],
)
def test_fuzzed_or_unknown_names_are_rejected(bad_name: str) -> None:
    with pytest.raises(ActionConstructionError):
        build_action(bad_name, payload={})


def _wire() -> dict[str, Any]:
    return {
        "a": 0,
        "b": True,
        "p": "10",
        "s": "1",
        "r": False,
        "t": {"limit": {"tif": "Gtc"}},
        "c": "0x" + "a" * 32,
    }


def test_build_order_action_matches_sdk_wire() -> None:
    action = build_action("order", payload={"orders": [_wire()], "grouping": "na"})
    assert list(action) == ["type", "orders", "grouping"]
    assert action["type"] == "order"


@pytest.mark.parametrize("value", ["20.0", "20.00", "4.30", "0.00020"])
def test_build_order_canonicalizes_decimal_strings_to_sdk_wire(value: str) -> None:
    action = build_action("order", payload={"orders": [{**_wire(), "s": value}], "grouping": "na"})
    assert action["orders"][0]["s"] == float_to_wire(float(value))


def test_build_order_rejects_decimal_string_that_changes_value_when_rounded() -> None:
    with pytest.raises(ActionConstructionError, match="cannot be represented"):
        build_action(
            "order", payload={"orders": [{**_wire(), "s": "0.123456789"}], "grouping": "na"}
        )


def test_build_order_rejects_string_value_loss_below_float_tolerance() -> None:
    with pytest.raises(ActionConstructionError, match="normalises to '1', changing its value"):
        build_action(
            "order", payload={"orders": [{**_wire(), "s": "1.0000000000001"}], "grouping": "na"}
        )


def test_order_msgpack_equals_sdk_action() -> None:
    import msgpack
    from hyperliquid.utils.signing import order_wires_to_order_action

    ours = build_action("order", payload={"orders": [_wire()], "grouping": "na"})
    sdk = order_wires_to_order_action([_wire()])
    assert msgpack.packb(ours, use_bin_type=True) == msgpack.packb(sdk, use_bin_type=True)


def test_cancel_wire_shape_and_key_rules() -> None:
    action = build_action("cancel", payload={"cancels": [{"a": 0, "o": 123}]})
    assert list(action) == ["type", "cancels"]
    assert action["cancels"] == [{"a": 0, "o": 123}]
    with pytest.raises(ActionConstructionError, match="unknown keys"):
        build_action("cancel", payload={"cancels": [{"a": 0, "o": 1, "c": "0x"}]})
    with pytest.raises(ActionConstructionError, match="non-empty"):
        build_action("cancel", payload={"cancels": []})
    with pytest.raises(ActionConstructionError, match="required"):
        build_action("cancel", payload={"cancels": [{"a": 0}]})
    with pytest.raises(ActionConstructionError, match="oid"):
        build_action("cancel", payload={"cancels": [{"a": 0, "o": "x"}]})
    with pytest.raises(ActionConstructionError, match="asset id"):
        build_action("cancel", payload={"cancels": [{"a": True, "o": 1}]})


def test_cancel_by_cloid_wire_shape() -> None:
    action = build_action(
        "cancelByCloid", payload={"cancels": [{"asset": 0, "cloid": "0x" + "a" * 32}]}
    )
    assert action["type"] == "cancelByCloid"
    assert list(action) == ["type", "cancels"]
    assert action["cancels"] == [{"asset": 0, "cloid": "0x" + "a" * 32}]
    with pytest.raises(ActionConstructionError, match="unknown keys"):
        build_action("cancelByCloid", payload={"cancels": [{"asset": 0, "cloid": "x", "o": 1}]})
    with pytest.raises(ActionConstructionError, match="required"):
        build_action("cancelByCloid", payload={"cancels": [{"asset": 0}]})
    with pytest.raises(ActionConstructionError, match="cloid"):
        build_action("cancelByCloid", payload={"cancels": [{"asset": 0, "cloid": 5}]})


def test_cancel_msgpack_equals_sdk_shape() -> None:
    import msgpack

    ours = build_action("cancel", payload={"cancels": [{"a": 0, "o": 123}]})
    sdk = {"type": "cancel", "cancels": [{"a": 0, "o": 123}]}
    assert msgpack.packb(ours, use_bin_type=True) == msgpack.packb(sdk, use_bin_type=True)

    ours = build_action(
        "cancelByCloid", payload={"cancels": [{"asset": 0, "cloid": "0x" + "a" * 32}]}
    )
    sdk = {"type": "cancelByCloid", "cancels": [{"asset": 0, "cloid": "0x" + "a" * 32}]}
    assert msgpack.packb(ours, use_bin_type=True) == msgpack.packb(sdk, use_bin_type=True)


def test_nested_order_wire_canonicalised() -> None:
    w = _wire()
    reordered = dict(reversed(list(w.items())))
    action = build_action("order", payload={"orders": [reordered], "grouping": "na"})
    assert list(action["orders"][0]) == ["a", "b", "p", "s", "r", "t", "c"]

    bad_t = dict(w)
    bad_t["t"] = {"limit": {"tif": "Gtc", "x": 1}}
    with pytest.raises(ActionConstructionError, match="unknown keys"):
        build_action("order", payload={"orders": [bad_t], "grouping": "na"})

    trig = {
        "c": "0x" + "5" * 32,
        "r": True,
        "t": {"trigger": {"tpsl": "sl", "triggerPx": "9", "isMarket": True}},
        "s": "1",
        "p": "9",
        "b": False,
        "a": 0,
    }
    action = build_action("order", payload={"orders": [trig], "grouping": "na"})
    wire = action["orders"][0]
    assert list(wire) == ["a", "b", "p", "s", "r", "t", "c"]
    assert list(wire["t"]["trigger"]) == ["isMarket", "triggerPx", "tpsl"]

    src = _wire()
    action = build_action("order", payload={"orders": [src], "grouping": "na"})
    src["p"] = "999"
    assert action["orders"][0]["p"] == "10"

    with pytest.raises(ActionConstructionError, match="asset id"):
        build_action("order", payload={"orders": [{**_wire(), "a": True}], "grouping": "na"})
    norm = build_action("order", payload={"orders": [{**_wire(), "p": 10.5}], "grouping": "na"})
    assert norm["orders"][0]["p"] == "10.5"
    with pytest.raises(ActionConstructionError, match="price"):
        build_action("order", payload={"orders": [{**_wire(), "p": True}], "grouping": "na"})
    with pytest.raises(ActionConstructionError, match="price"):
        build_action("order", payload={"orders": [{**_wire(), "p": None}], "grouping": "na"})
    with pytest.raises(ActionConstructionError, match="cloid"):
        build_action("order", payload={"orders": [{**_wire(), "c": 123}], "grouping": "na"})


def test_schedule_cancel_shape() -> None:
    action = build_action("scheduleCancel", payload={"time": 1694000000000})
    assert list(action) == ["type", "time"]
    with pytest.raises(ActionConstructionError, match="time"):
        build_action("scheduleCancel", payload={"when": 1})
    with pytest.raises(ActionConstructionError, match="time"):
        build_action("scheduleCancel", payload={"time": True})
    with pytest.raises(ActionConstructionError, match="positive"):
        build_action("scheduleCancel", payload={"time": 0})


def test_update_leverage_wire_shape() -> None:
    action = build_action("updateLeverage", payload={"asset": 0, "isCross": False, "leverage": 1})
    assert list(action) == ["type", "asset", "isCross", "leverage"]
    with pytest.raises(ActionConstructionError, match="unknown keys"):
        build_action(
            "updateLeverage", payload={"asset": 0, "isCross": False, "leverage": 1, "extra": 1}
        )


def test_update_isolated_margin_wire_shape() -> None:
    action = build_action(
        "updateIsolatedMargin", payload={"asset": 0, "ntli": 5_000_000, "isBuy": True}
    )
    assert list(action) == ["type", "asset", "isBuy", "ntli"]
    assert action["isBuy"] is True


def test_no_passthrough_keys_or_builder() -> None:
    with pytest.raises(ActionConstructionError, match="unknown keys"):
        build_action("order", payload={"orders": [_wire()], "builder": {"b": 1}})
    with pytest.raises(ActionConstructionError, match="unknown keys"):
        build_action("order", payload={"coin": "BTC", "orders": [_wire()]})


def test_vault_address_key_rejected_entirely() -> None:
    with pytest.raises(ActionConstructionError, match="vaultAddress"):
        build_action("cancel", payload={"cancels": [{"a": 0, "o": 1}], "vaultAddress": "0xdead"})
    with pytest.raises(ActionConstructionError, match="vaultAddress"):
        build_action("order", payload={"orders": [_wire()], "vaultAddress": ""})


def _order_payload(**over: Any) -> dict[str, Any]:
    base = {"orders": [_wire()], "grouping": "na"}
    base.update(over)
    return base


def test_order_with_tpsl_grouping_is_rejected() -> None:
    with pytest.raises(ActionConstructionError, match="grouping"):
        build_action("order", payload=_order_payload(grouping="normalTpsl"))
    with pytest.raises(ActionConstructionError, match="grouping"):
        build_action("order", payload=_order_payload(grouping="positionTpsl"))


def test_order_trigger_must_be_reduce_only() -> None:
    payload = _order_payload()
    payload["orders"] = [
        {
            "a": 0,
            "b": False,
            "p": "9",
            "s": "1",
            "r": False,
            "t": {"trigger": {"isMarket": True, "triggerPx": "9", "tpsl": "sl"}},
            "c": "0x" + "5" * 32,
        }
    ]
    with pytest.raises(ActionConstructionError, match="reduceOnly"):
        build_action("order", payload=payload)


def test_order_bad_tif_rejected() -> None:
    payload = _order_payload()
    payload["orders"] = [
        {
            "a": 0,
            "b": True,
            "p": "10",
            "s": "1",
            "r": False,
            "t": {"limit": {"tif": "Fok"}},
            "c": "0x" + "f" * 32,
        }
    ]
    with pytest.raises(ActionConstructionError, match="tif"):
        build_action("order", payload=payload)


def test_order_without_cloid_rejected() -> None:
    payload = _order_payload()
    payload["orders"] = [
        {"a": 0, "b": True, "p": "10", "s": "1", "r": False, "t": {"limit": {"tif": "Gtc"}}}
    ]
    with pytest.raises(ActionConstructionError, match=r"c\b.*required|cloid"):
        build_action("order", payload=payload)


@pytest.mark.parametrize("leverage", [1, 2, 3, 4, 5])
def test_update_leverage_builds_isolated_up_to_5x(leverage: int) -> None:
    action = build_action(
        "updateLeverage", payload={"asset": 0, "isCross": False, "leverage": leverage}
    )
    assert action == {"type": "updateLeverage", "asset": 0, "isCross": False, "leverage": leverage}


@pytest.mark.parametrize("leverage", [0, 6, 50])
def test_update_leverage_refuses_outside_1_to_5x(leverage: int) -> None:
    with pytest.raises(ActionConstructionError, match=f"leverage {leverage} outside 1..5"):
        build_action("updateLeverage", payload={"asset": 0, "isCross": False, "leverage": leverage})


@pytest.mark.parametrize("leverage", [1, 5])
def test_update_leverage_refuses_cross(leverage: int) -> None:
    with pytest.raises(ActionConstructionError, match="isolated only"):
        build_action("updateLeverage", payload={"asset": 0, "isCross": True, "leverage": leverage})


def test_margin_addition_cap_is_the_order_ceiling() -> None:
    build_action(
        "updateIsolatedMargin", payload={"asset": 0, "ntli": 5000 * 1_000_000, "isBuy": True}
    )
    with pytest.raises(ActionConstructionError, match=r"above the \$5000 addition cap"):
        build_action(
            "updateIsolatedMargin",
            payload={"asset": 0, "ntli": 5000 * 1_000_000 + 1, "isBuy": True},
        )


def test_update_isolated_margin_addition_only() -> None:
    build_action("updateIsolatedMargin", payload={"asset": 0, "ntli": 5_000_000, "isBuy": True})
    with pytest.raises(ActionConstructionError, match="ntli"):
        build_action(
            "updateIsolatedMargin", payload={"asset": 0, "ntli": -5_000_000, "isBuy": True}
        )
    with pytest.raises(ActionConstructionError, match="ntli"):
        build_action("updateIsolatedMargin", payload={"asset": 0, "sz": 5})


def test_build_action_requires_payload_mapping() -> None:
    with pytest.raises(ActionConstructionError):
        build_action("cancel", payload=None)


def test_allowed_action_types_carry_no_user_signed_actions() -> None:
    user_signed = {
        "sendAsset",
        "usdSend",
        "spotSend",
        "withdraw3",
        "usdClassTransfer",
        "cDeposit",
        "cWithdraw",
        "approveAgent",
        "approveBuilderFee",
    }
    assert not (user_signed & ALLOWED_ACTION_TYPES)


def test_update_isolated_margin_isbuy_required_and_typed() -> None:
    action = build_action(
        "updateIsolatedMargin", payload={"asset": 0, "ntli": 5_000_000, "isBuy": False}
    )
    assert action["isBuy"] is False
    with pytest.raises(ActionConstructionError, match="isBuy"):
        build_action("updateIsolatedMargin", payload={"asset": 0, "ntli": 5_000_000})
    with pytest.raises(ActionConstructionError, match="isBuy"):
        build_action(
            "updateIsolatedMargin", payload={"asset": 0, "ntli": 5_000_000, "isBuy": "yes"}
        )


def test_price_size_triggerpx_must_be_decimal_strings() -> None:
    for bad in ("1e5", " 10", "10 ", "abc", "", "nan", "inf", "-"):
        with pytest.raises(ActionConstructionError, match="price"):
            build_action("order", payload={"orders": [{**_wire(), "p": bad}], "grouping": "na"})
        with pytest.raises(ActionConstructionError, match="size"):
            build_action("order", payload={"orders": [{**_wire(), "s": bad}], "grouping": "na"})
    with pytest.raises(ActionConstructionError, match="triggerPx"):
        build_action(
            "order",
            payload={
                "orders": [
                    {
                        "a": 0,
                        "b": False,
                        "p": "9",
                        "s": "1",
                        "r": True,
                        "t": {"trigger": {"isMarket": True, "triggerPx": "9e1", "tpsl": "sl"}},
                        "c": "0x" + "5" * 32,
                    }
                ],
                "grouping": "na",
            },
        )
    build_action("order", payload={"orders": [{**_wire(), "p": "100000.5"}], "grouping": "na"})


SDK_ONLY_TYPES = {
    "convertToMultiSigUser",
    "multiSig",
    "subAccountTransfer",
    "subAccountSpotTransfer",
    "createSubAccount",
    "setReferrer",
    "evmUserModify",
    "perpDeploy",
    "spotDeploy",
    "CSignerAction",
    "CValidatorAction",
    "gossipPriorityBid",
}


@pytest.mark.parametrize("action_type", sorted(SDK_ONLY_TYPES))
def test_sdk_known_types_are_forbidden_not_unknown(action_type: str) -> None:
    with pytest.raises(ForbiddenActionError):
        build_action(action_type, payload={})


def _trigger_wire(trigger_px: Any) -> dict[str, Any]:
    return {
        **_wire(),
        "r": True,
        "t": {"trigger": {"isMarket": True, "triggerPx": trigger_px, "tpsl": "sl"}},
    }


NEGATIVES = [-5, -1.5, -0.0, Decimal("-2"), Decimal("-0.5")]


@pytest.mark.parametrize("value", NEGATIVES, ids=repr)
@pytest.mark.parametrize("field", ["p", "s"])
def test_a_negative_price_or_size_is_refused(field: str, value: Any) -> None:
    with pytest.raises(ActionConstructionError, match="decimal string"):
        build_action("order", payload={"orders": [{**_wire(), field: value}]})


@pytest.mark.parametrize("value", [*NEGATIVES, -3], ids=repr)
def test_a_negative_trigger_price_is_refused(value: Any) -> None:
    with pytest.raises(ActionConstructionError, match="triggerPx"):
        build_action("order", payload={"orders": [_trigger_wire(value)]})


def test_a_zero_size_order_stays_buildable() -> None:
    for zero in (0, "0", 0.0, Decimal("0")):
        action = build_action("order", payload={"orders": [{**_wire(), "s": zero}]})
        assert action["orders"][0]["s"] == "0"


@pytest.mark.parametrize("tif", [[], {}, ["Gtc"]], ids=repr)
def test_an_unhashable_tif_raises_the_construction_error(tif: Any) -> None:
    wire = {**_wire(), "t": {"limit": {"tif": tif}}}
    with pytest.raises(ActionConstructionError, match="tif"):
        build_action("order", payload={"orders": [wire]})


def test_an_unhashable_tpsl_raises_the_construction_error() -> None:
    wire = _trigger_wire("9")
    wire["t"]["trigger"]["tpsl"] = []
    with pytest.raises(ActionConstructionError, match="tpsl"):
        build_action("order", payload={"orders": [wire]})


EIP712_FIELD_TYPE = re.compile(r"(?:string|address|bool|bytes\d*|u?int\d*)(?:\[\])?")


def _sdk_action_types() -> set[str]:
    root = Path(hyperliquid.__file__).parent
    found: set[str] = set()
    for path in (root / "exchange.py", root / "utils" / "signing.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            entries = {
                k.value: v
                for k, v in zip(node.keys, node.values, strict=True)
                if isinstance(k, ast.Constant)
            }
            value = entries.get("type")
            if not isinstance(value, ast.Constant):
                continue
            if set(entries) == {"name", "type"} and EIP712_FIELD_TYPE.fullmatch(str(value.value)):
                continue
            found.add(str(value.value))
    return found


def test_every_sdk_action_type_is_allowed_or_forbidden() -> None:
    sdk_types = _sdk_action_types()
    assert {"order", "withdraw3", "usdSend", "multiSig", "createSubAccount"} <= sdk_types
    assert sdk_types - DOCUMENTED_ACTION_TYPES == set()


Payload = Callable[[Any], dict[str, Any]]

ASSET_FIELDS: dict[str, tuple[str, Payload]] = {
    "order a": ("order", lambda v: {"orders": [{**_wire(), "a": v}]}),
    "cancel a": ("cancel", lambda v: {"cancels": [{"a": v, "o": 1}]}),
    "cancelByCloid asset": (
        "cancelByCloid",
        lambda v: {"cancels": [{"asset": v, "cloid": "0x" + "a" * 32}]},
    ),
    "updateLeverage asset": (
        "updateLeverage",
        lambda v: {"asset": v, "isCross": False, "leverage": 1},
    ),
    "updateIsolatedMargin asset": (
        "updateIsolatedMargin",
        lambda v: {"asset": v, "ntli": 1, "isBuy": True},
    ),
}
U64_FIELDS: dict[str, tuple[str, Payload]] = {
    "cancel o": ("cancel", lambda v: {"cancels": [{"a": 0, "o": v}]}),
    "scheduleCancel time": ("scheduleCancel", lambda v: {"time": v}),
}
NOT_INTS: list[Any] = [True, False, 1.0, "1", None, Decimal(1)]


@pytest.mark.parametrize("field", ASSET_FIELDS)
@pytest.mark.parametrize("bad", [-1, 10_000, 10_001, 100_000, 2**64, 2**70, *NOT_INTS])
def test_an_asset_id_outside_the_perp_universe_is_refused_at_build(field: str, bad: Any) -> None:
    action_type, payload = ASSET_FIELDS[field]
    with pytest.raises(ActionConstructionError, match="asset"):
        build_action(action_type, payload(bad))


@pytest.mark.parametrize("field", ASSET_FIELDS)
@pytest.mark.parametrize("good", [0, 1, 9_999])
def test_a_perp_asset_id_builds(field: str, good: int) -> None:
    action_type, payload = ASSET_FIELDS[field]
    action = build_action(action_type, payload(good))
    wire = (action.get("orders") or action.get("cancels") or [action])[0]
    assert wire.get("a", wire.get("asset")) == good


@pytest.mark.parametrize("field", U64_FIELDS)
@pytest.mark.parametrize("bad", [-1, 2**64, 2**70, *NOT_INTS])
def test_a_u64_field_outside_64_bits_is_refused_at_build(field: str, bad: Any) -> None:
    action_type, payload = U64_FIELDS[field]
    with pytest.raises(ActionConstructionError):
        build_action(action_type, payload(bad))


@pytest.mark.parametrize("field", U64_FIELDS)
@pytest.mark.parametrize("good", [1, 2**64 - 1])
def test_a_u64_field_builds_up_to_the_64_bit_limit(field: str, good: int) -> None:
    action_type, payload = U64_FIELDS[field]
    action = build_action(action_type, payload(good))
    wire = (action.get("cancels") or [action])[0]
    assert wire.get("o", wire.get("time")) == good


CLOID_FIELDS: dict[str, tuple[str, Payload]] = {
    "order c": ("order", lambda v: {"orders": [{**_wire(), "c": v}]}),
    "cancelByCloid cloid": ("cancelByCloid", lambda v: {"cancels": [{"asset": 0, "cloid": v}]}),
}


@pytest.mark.parametrize("field", CLOID_FIELDS)
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "0x",
        "0xabc",
        "hello",
        "a" * 32,
        "0x" + "a" * 31,
        "0x" + "a" * 33,
        "0X" + "a" * 32,
        "0x" + "g" * 32,
        "0x" + "a" * 32 + "\n",
        " 0x" + "a" * 32,
        "0x" + chr(0x661) * 32,
        5,
        None,
        b"0x" + b"a" * 32,
    ],
)
def test_a_cloid_outside_the_sdk_format_is_refused_at_build(field: str, bad: Any) -> None:
    action_type, payload = CLOID_FIELDS[field]
    with pytest.raises(ActionConstructionError, match="cloid"):
        build_action(action_type, payload(bad))


@pytest.mark.parametrize("field", CLOID_FIELDS)
@pytest.mark.parametrize("good", ["0x" + "0" * 32, "0x" + "aB" * 16, "0x" + "F" * 32])
def test_a_cloid_in_the_sdk_format_builds_lowercase(field: str, good: str) -> None:
    action_type, payload = CLOID_FIELDS[field]
    action = build_action(action_type, payload(good))
    wire = (action.get("orders") or action["cancels"])[0]
    assert wire.get("c", wire.get("cloid")) == good.lower()


def test_cloid_is_built_lowercase_and_hashes_like_the_sdk_lowercase_cloid() -> None:
    from hyperliquid.utils.signing import action_hash as sdk_hash
    from hyperliquid.utils.types import Cloid

    from hl_guard.exchange.signer import action_hash

    upper = "0x" + "AB" * 16
    lower = Cloid.from_str(upper.lower()).to_raw()
    order = build_action("order", payload={"orders": [{**_wire(), "c": upper}]})
    by_cloid = build_action("cancelByCloid", {"cancels": [{"asset": 0, "cloid": upper}]})
    assert order["orders"][0]["c"] == lower
    assert by_cloid["cancels"][0]["cloid"] == lower
    sdk_order = {**order, "orders": [{**order["orders"][0], "c": lower}]}
    assert action_hash(order, 1, None) == sdk_hash(sdk_order, None, 1, None)
    assert Cloid.from_str(upper).to_raw() == upper
    sdk_upper = {**order, "orders": [{**order["orders"][0], "c": upper}]}
    assert action_hash(order, 1, None) != sdk_hash(sdk_upper, None, 1, None)
