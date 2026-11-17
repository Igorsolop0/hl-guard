"""The exchange hashes the msgpack of the action, and msgpack keeps key order, so
every action and every nested wire is rebuilt in the SDK 0.24.0 key order with
a per-type key allowlist.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn, TypeGuard

from hl_guard.policy.ceilings import MAX_LEVERAGE, PER_ORDER_USD_CEILING

ALLOWED_ACTION_TYPES: frozenset[str] = frozenset(
    {
        "order",
        "cancel",
        "cancelByCloid",
        "scheduleCancel",
        "updateLeverage",
        "updateIsolatedMargin",
    }
)

FORBIDDEN_ACTION_TYPES: frozenset[str] = frozenset(
    {
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
        "reserveRequestWeight",
        "approveAgent",
        "approveBuilderFee",
        "authorizeAqav2Role",
        "userDexAbstraction",
        "agentDexAbstraction",
        "agentEnableDexAbstraction",
        "userSetAbstraction",
        "agentSetAbstraction",
        "twapOrder",
        "twapCancel",
        "batchModify",
        "noop",
        "validatorL1Stream",
        "claimRewards",
        "userOutcome",
        "topUpIsolatedOnlyMargin",
        "modify",
        "trailingStop",
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
)

DOCUMENTED_ACTION_TYPES: frozenset[str] = ALLOWED_ACTION_TYPES | FORBIDDEN_ACTION_TYPES

_ALLOWED_TIFS = frozenset({"Gtc", "Ioc", "Alo"})
_ALLOWED_TPSL = frozenset({"tp", "sl"})
_MAX_BATCH = 20
_MAX_MARGIN_NTLI = int(PER_ORDER_USD_CEILING) * 1_000_000
_DECIMAL_RE = re.compile(r"^[0-9]+(\.[0-9]+)?$")
_CLOID_RE = re.compile(r"0x[0-9a-fA-F]{32}")
_U64_LIMIT = 2**64
SPOT_ASSET_OFFSET = 10_000

Action = dict[str, Any]


class ActionConstructionError(ValueError):
    """The action cannot be built: unknown or malformed type, or a bad payload."""


class ForbiddenActionError(ActionConstructionError):
    """The action type exists on the exchange but is not on the allowlist."""


def _fail_order_condition(detail: str) -> NoReturn:
    raise ActionConstructionError(f"order does not meet the allowlist conditions: {detail}")


def _check_keys(payload: Mapping[str, Any], allowed: frozenset[str], action_type: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise ActionConstructionError(
            f"{action_type}: unknown keys {sorted(unknown)}; "
            f"allowed {sorted(allowed)} (no pass-through)"
        )


def _is_int(v: Any) -> TypeGuard[int]:
    return isinstance(v, int) and not isinstance(v, bool)


def _u64(v: Any, field: str) -> int:
    if not _is_int(v) or not 0 <= v < _U64_LIMIT:
        raise ActionConstructionError(f"{field} must be an int in [0, 2**64): {v!r}")
    return v


def _asset(v: Any, field: str) -> int:
    if not _is_int(v) or not 0 <= v < SPOT_ASSET_OFFSET:
        raise ActionConstructionError(
            f"{field} must be a perp asset id int in [0, {SPOT_ASSET_OFFSET}), "
            f"spot and HIP-3 ids are refused: {v!r}"
        )
    return v


def _cloid(v: Any, field: str) -> str:
    """The SDK's ``Cloid.from_str(v).to_raw()`` returns ``v`` unchanged, so two
    spellings of one cloid hash to two different actions. Building only the
    lowercase spelling gives each cloid one signed form, equal to the SDK's for
    the lowercase input.
    """
    if not isinstance(v, str) or not _CLOID_RE.fullmatch(v):
        raise ActionConstructionError(f"{field} must be 0x and 32 hex digits (SDK Cloid): {v!r}")
    return v.lower()


def _is_decimal_str(v: Any) -> bool:
    return isinstance(v, str) and _DECIMAL_RE.match(v) is not None


def _bad_decimal(field: str, v: Any) -> NoReturn:
    raise ActionConstructionError(f"{field} must be a decimal string, int, float or Decimal: {v!r}")


def _normalize_decimal(v: Any, field: str) -> str:
    normalized = _wire_spelling(v, field)
    if not _is_decimal_str(normalized):
        raise ActionConstructionError(
            f"{field} must be a non-negative decimal string on the wire: {v!r} gives {normalized!r}"
        )
    return normalized


def _wire_spelling(v: Any, field: str) -> str:
    """Normalise to the SDK ``float_to_wire`` spelling, byte for byte.

    A value that wire rounding would change is refused, as the SDK refuses it.
    """
    string_value: Decimal | None = None
    if isinstance(v, str):
        if not _is_decimal_str(v):
            raise ActionConstructionError(
                f"{field} must be a plain decimal string (^[0-9]+(\\.[0-9]+)?$): {v!r}"
            )
        try:
            string_value = Decimal(v)
            x = float(string_value)
        except (InvalidOperation, ValueError, OverflowError):
            _bad_decimal(field, v)
        if x != x or abs(x) == float("inf"):
            _bad_decimal(field, v)
    elif isinstance(v, bool):
        raise ActionConstructionError(f"{field} must be decimal, got bool")
    elif isinstance(v, int):
        return str(v)
    elif isinstance(v, float):
        if v != v or abs(v) == float("inf"):
            _bad_decimal(field, v)
        x = v
    elif isinstance(v, Decimal):
        if not v.is_finite():
            raise ActionConstructionError(f"{field} is not finite: {v!r}")
        x = float(v)
    else:
        _bad_decimal(field, v)
    rounded = f"{x:.8f}"
    if abs(float(rounded) - x) >= 1e-12:
        raise ActionConstructionError(
            f"{field} {v!r} cannot be represented on the wire without rounding"
        )
    if rounded == "-0":
        rounded = "0"
    normalized = f"{Decimal(rounded).normalize():f}"
    if string_value is not None and Decimal(normalized) != string_value:
        raise ActionConstructionError(
            f"{field} {v!r} normalises to {normalized!r}, changing its value"
        )
    return normalized


def _require(payload: Mapping[str, Any], key: str, action_type: str) -> Any:
    if key not in payload:
        raise ActionConstructionError(f"{action_type}: {key} is required")
    return payload[key]


def _canonical_order_type(t: Any, reduce_only: bool) -> dict[str, Any]:
    if not isinstance(t, Mapping):
        _fail_order_condition("order type t is required")
    if "limit" in t:
        _check_keys(t, frozenset({"limit"}), "order wire t")
        limit = t["limit"]
        if not isinstance(limit, Mapping):
            _fail_order_condition("limit type must be a mapping")
        _check_keys(limit, frozenset({"tif"}), "order wire t.limit")
        tif = limit.get("tif")
        if not isinstance(tif, str) or tif not in _ALLOWED_TIFS:
            _fail_order_condition(f"tif {tif!r} not in {sorted(_ALLOWED_TIFS)}")
        return {"limit": {"tif": tif}}
    if "trigger" in t:
        _check_keys(t, frozenset({"trigger"}), "order wire t")
        trigger = t["trigger"]
        if not isinstance(trigger, Mapping):
            _fail_order_condition("trigger type must be a mapping")
        _check_keys(trigger, frozenset({"isMarket", "triggerPx", "tpsl"}), "order wire t.trigger")
        is_market = _require(trigger, "isMarket", "order wire t.trigger")
        if not isinstance(is_market, bool):
            _fail_order_condition("trigger isMarket must be a bool")
        trigger_px = _normalize_decimal(
            _require(trigger, "triggerPx", "order wire t.trigger"), "triggerPx"
        )
        tpsl = _require(trigger, "tpsl", "order wire t.trigger")
        if not isinstance(tpsl, str) or tpsl not in _ALLOWED_TPSL:
            _fail_order_condition(f"tpsl {tpsl!r} must be tp or sl")
        if reduce_only is not True:
            _fail_order_condition("trigger orders must be reduceOnly (r: true)")
        return {"trigger": {"isMarket": is_market, "triggerPx": trigger_px, "tpsl": tpsl}}
    _fail_order_condition("order type must be limit or trigger")


def _canonical_order_wire(order: Any) -> dict[str, Any]:
    if not isinstance(order, Mapping):
        _fail_order_condition("each order must be a mapping")
    _check_keys(order, frozenset({"a", "b", "p", "s", "r", "t", "c"}), "order wire")
    a = _asset(_require(order, "a", "order wire"), "order wire: a (asset id)")
    b = _require(order, "b", "order wire")
    if not isinstance(b, bool):
        _fail_order_condition("b (is_buy) must be a bool")
    p = _normalize_decimal(_require(order, "p", "order wire"), "p (price)")
    s = _normalize_decimal(_require(order, "s", "order wire"), "s (size)")
    r = _require(order, "r", "order wire")
    if not isinstance(r, bool):
        _fail_order_condition("r (reduce_only) must be a bool")
    t = _canonical_order_type(_require(order, "t", "order wire"), r)
    c = _cloid(_require(order, "c", "order wire"), "order wire: c (cloid)")
    return {"a": a, "b": b, "p": p, "s": s, "r": r, "t": t, "c": c}


def _build_order(payload: Mapping[str, Any]) -> Action:
    _check_keys(payload, frozenset({"orders", "grouping"}), "order")
    orders = payload.get("orders")
    if not isinstance(orders, list) or not orders:
        _fail_order_condition("orders must be a non-empty list")
    if len(orders) > _MAX_BATCH:
        _fail_order_condition(f"orders batch capped at {_MAX_BATCH}")
    grouping = payload.get("grouping", "na")
    if grouping != "na":
        _fail_order_condition(f"grouping {grouping!r} is not allowed (na only)")
    return {
        "type": "order",
        "orders": [_canonical_order_wire(o) for o in orders],
        "grouping": "na",
    }


def _cancel_list(payload: Mapping[str, Any], action_type: str) -> list[Any]:
    _check_keys(payload, frozenset({"cancels"}), action_type)
    cancels = payload.get("cancels")
    if not isinstance(cancels, list) or not cancels:
        raise ActionConstructionError(f"{action_type} requires a non-empty cancels list")
    if len(cancels) > _MAX_BATCH:
        raise ActionConstructionError(f"{action_type} batch capped at {_MAX_BATCH}")
    for c in cancels:
        if not isinstance(c, Mapping):
            raise ActionConstructionError(f"each {action_type} must be a mapping")
    return cancels


def _build_cancel(payload: Mapping[str, Any]) -> Action:
    wires = []
    for c in _cancel_list(payload, "cancel"):
        _check_keys(c, frozenset({"a", "o"}), "cancel wire")
        a = _asset(_require(c, "a", "cancel wire"), "cancel wire: a (asset id)")
        o = _u64(_require(c, "o", "cancel wire"), "cancel wire: o (oid)")
        wires.append({"a": a, "o": o})
    return {"type": "cancel", "cancels": wires}


def _build_cancel_by_cloid(payload: Mapping[str, Any]) -> Action:
    wires = []
    for c in _cancel_list(payload, "cancelByCloid"):
        _check_keys(c, frozenset({"asset", "cloid"}), "cancelByCloid wire")
        asset = _asset(_require(c, "asset", "cancelByCloid wire"), "cancelByCloid wire: asset")
        cloid = _cloid(_require(c, "cloid", "cancelByCloid wire"), "cancelByCloid wire: cloid")
        wires.append({"asset": asset, "cloid": cloid})
    return {"type": "cancelByCloid", "cancels": wires}


def _build_schedule_cancel(payload: Mapping[str, Any]) -> Action:
    _check_keys(payload, frozenset({"time"}), "scheduleCancel")
    time_ms = _u64(_require(payload, "time", "scheduleCancel"), "scheduleCancel: time (ms epoch)")
    if time_ms == 0:
        raise ActionConstructionError("scheduleCancel: time (ms epoch) must be positive")
    return {"type": "scheduleCancel", "time": time_ms}


def _build_update_leverage(payload: Mapping[str, Any]) -> Action:
    _check_keys(payload, frozenset({"asset", "isCross", "leverage"}), "updateLeverage")
    asset = _asset(_require(payload, "asset", "updateLeverage"), "updateLeverage: asset")
    if payload.get("isCross") is not False:
        raise ActionConstructionError("updateLeverage: isolated only (isCross: false)")
    leverage = payload.get("leverage")
    if not _is_int(leverage) or not 1 <= leverage <= MAX_LEVERAGE:
        raise ActionConstructionError(
            f"updateLeverage: leverage {leverage!r} outside 1..{MAX_LEVERAGE}"
        )
    return {"type": "updateLeverage", "asset": asset, "isCross": False, "leverage": leverage}


def _build_update_isolated_margin(payload: Mapping[str, Any]) -> Action:
    """``ntli`` has 6 decimals (1_000_000 = 1 USD)."""
    _check_keys(payload, frozenset({"asset", "ntli", "isBuy"}), "updateIsolatedMargin")
    asset = _asset(
        _require(payload, "asset", "updateIsolatedMargin"), "updateIsolatedMargin: asset"
    )
    ntli = _require(payload, "ntli", "updateIsolatedMargin")
    if not _is_int(ntli) or ntli <= 0:
        raise ActionConstructionError("updateIsolatedMargin: addition only (ntli int > 0)")
    if ntli > _MAX_MARGIN_NTLI:
        raise ActionConstructionError(
            f"updateIsolatedMargin: ntli {ntli} above the ${PER_ORDER_USD_CEILING} addition cap"
        )
    is_buy = _require(payload, "isBuy", "updateIsolatedMargin")
    if not isinstance(is_buy, bool):
        raise ActionConstructionError("updateIsolatedMargin: isBuy must be a bool")
    return {"type": "updateIsolatedMargin", "asset": asset, "isBuy": is_buy, "ntli": ntli}


_BUILDERS: Mapping[str, Callable[[Mapping[str, Any]], Action]] = {
    "order": _build_order,
    "cancel": _build_cancel,
    "cancelByCloid": _build_cancel_by_cloid,
    "scheduleCancel": _build_schedule_cancel,
    "updateLeverage": _build_update_leverage,
    "updateIsolatedMargin": _build_update_isolated_margin,
}


def build_action(action_type: str, payload: Mapping[str, Any] | None = None) -> Action:
    """Build the canonical SDK 0.24.0 wire dict for ``action_type``.

    Raises ``ForbiddenActionError`` for a documented type off the allowlist and
    ``ActionConstructionError`` for anything else invalid, always before signing.
    """
    if not isinstance(action_type, str):
        raise ActionConstructionError(
            f"action type must be a string, got {type(action_type).__name__}"
        )
    if not action_type or not action_type.isascii() or not action_type.isalnum():
        raise ActionConstructionError(f"malformed action type: {action_type!r}")
    if action_type in ALLOWED_ACTION_TYPES:
        if not isinstance(payload, Mapping):
            raise ActionConstructionError(
                f"payload for {action_type} must be a mapping, got {type(payload).__name__}"
            )
        if "vaultAddress" in payload:
            raise ActionConstructionError("vaultAddress key is rejected (no vault trading)")
        return _BUILDERS[action_type](payload)
    if action_type == "modify":
        raise ForbiddenActionError(
            "modify is refused: SDK 0.24.0 sends modify as the forbidden batchModify"
        )
    if action_type in FORBIDDEN_ACTION_TYPES:
        raise ForbiddenActionError(
            f"action type {action_type!r} is forbidden by the allowlist; rejected before signing"
        )
    raise ActionConstructionError(
        f"unknown action type {action_type!r}; deny-by-default, rejected before signing"
    )


def reduces_risk(action: Action) -> bool:
    """What a halt lets out: reduce-only orders and cancels. A cancel of a
    reduce-only order raises risk, so every caller cancels only opening orders:
    cancel_order refuses reduce-only rows and the halt keeps them."""
    if action.get("type") in ("cancel", "cancelByCloid"):
        return True
    orders = action.get("orders")
    return (
        action.get("type") == "order"
        and isinstance(orders, list)
        and all(isinstance(o, Mapping) and o.get("r") is True for o in orders)
    )
