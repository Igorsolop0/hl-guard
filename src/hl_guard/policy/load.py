from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import yaml

from hl_guard.exchange.actions import SPOT_ASSET_OFFSET
from hl_guard.policy.ceilings import AT_MOST, CEILINGS, DEFAULTS
from hl_guard.policy.model import (
    COUNT_FIELDS,
    INT_LIMIT,
    SIZE_RULE,
    Account,
    AddMargin,
    Approval,
    ApproveAboveUsd,
    ApproveAlways,
    ApproveNever,
    AssetState,
    Close,
    Entry,
    EntryRecord,
    OpenOrder,
    Policy,
    Proposal,
    ScheduleCancel,
    SetLeverage,
    in_size,
    is_coin_name,
)

T = TypeVar("T")

MAX_POLICY_BYTES = 16 * 1024
_MAX_DEPTH = 16
_MAX_KEYS_SHOWN = 5
MAX_ACCOUNT_RECORDS = 1000
_MODE_HINT = "copy examples/policy.yaml to ~/.config/hl-guard/policy.yaml and run chmod 600 on it"


class PolicyError(ValueError):
    pass


def _number(v: Any, field: str) -> Decimal:
    if isinstance(v, bool) or not isinstance(v, int | float | Decimal):
        raise PolicyError(f"{field} must be a number, got {v!r}")
    try:
        d = Decimal(str(v))
    except ValueError:
        raise PolicyError(f"{field} has too many digits") from None
    if not d.is_finite():
        raise PolicyError(f"{field} must be finite, got {v!r}")
    if not in_size(d):
        raise PolicyError(f"{field} {SIZE_RULE}, got {v!r}")
    return d


def _positive(v: Any, field: str) -> Decimal:
    d = _number(v, field)
    if d <= 0:
        raise PolicyError(f"{field} must be above 0, got {v!r}")
    return d


def _non_negative(v: Any, field: str) -> Decimal:
    d = _number(v, field)
    if d < 0:
        raise PolicyError(f"{field} must not be negative, got {v!r}")
    return d


def _int(v: Any, field: str, minimum: int = 0) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not minimum <= v < INT_LIMIT:
        raise PolicyError(f"{field} must be an integer from {minimum} to 2**63-1, got {v!r}")
    return v


def _bool(v: Any, field: str) -> bool:
    if not isinstance(v, bool):
        raise PolicyError(f"{field} must be true or false, got {v!r}")
    return v


def _coin(v: Any, field: str) -> str:
    if not is_coin_name(v):
        raise PolicyError(f"{field} must be a coin name of ASCII letters and digits, got {v!r}")
    return v


def _mapping(v: Any, field: str, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(v, Mapping):
        raise PolicyError(f"{field} must be a mapping, got {type(v).__name__}")
    missing = sorted(keys - set(v))
    unknown = sorted(set(v) - keys)
    if missing or unknown:
        more = len(unknown) - _MAX_KEYS_SHOWN
        rest = f" and {more} more" if more > 0 else ""
        raise PolicyError(
            f"{field}: missing keys {missing}, unknown keys {unknown[:_MAX_KEYS_SHOWN]}{rest}"
        )
    return v


def _list(
    v: Any, field: str, item: Callable[[Any, str], T], max_len: int | None = None
) -> tuple[T, ...]:
    if not isinstance(v, list):
        raise PolicyError(f"{field} must be a list, got {type(v).__name__}")
    if max_len is not None and len(v) > max_len:
        raise PolicyError(f"{field} holds {len(v)} items; the cap is {max_len}")
    return tuple(item(x, f"{field}[{i}]") for i, x in enumerate(v))


def _count(v: Any, field: str) -> int:
    return _int(v, field, minimum=1)


def _approval(v: Any) -> Approval:
    if v == "always":
        return ApproveAlways()
    if v == "never":
        return ApproveNever()
    if isinstance(v, Mapping) and set(v) == {"above_usd"}:
        return ApproveAboveUsd(_positive(v["above_usd"], "approval.above_usd"))
    raise PolicyError(f"approval must be always, never or {{above_usd: N}}, got {v!r}")


_LIMIT_PARSERS: Mapping[str, Callable[[Any, str], Decimal | int]] = {
    bound.field: _count if bound.field in COUNT_FIELDS else _positive for bound in CEILINGS
}

_POLICY_KEYS = frozenset({"network", "coins", "stop_loss_required", "approval", *_LIMIT_PARSERS})


def parse_policy(raw: Any) -> Policy:
    p = _mapping({**DEFAULTS, **raw} if isinstance(raw, Mapping) else raw, "policy", _POLICY_KEYS)
    if p["network"] not in ("testnet", "mainnet"):
        raise PolicyError(f"network must be testnet or mainnet, got {p['network']!r}")
    coins = _list(p["coins"], "coins", _coin)
    if not coins:
        raise PolicyError("coins must name at least one coin")
    if p["stop_loss_required"] is not True:
        raise PolicyError(
            "stop_loss_required must be true: the product requires a stop-loss on every entry"
        )
    fields = {
        "network": p["network"],
        "coins": frozenset(coins),
        "approval": _approval(p["approval"]),
    }
    for name, parse in _LIMIT_PARSERS.items():
        fields[name] = parse(p[name], name)
    for field, cap in AT_MOST:
        if field not in raw:
            fields[field] = min(fields[field], fields[cap])
    try:
        return Policy(**fields)
    except ValueError as exc:
        raise PolicyError(str(exc)) from None


_PLAIN_NUMBERS = {
    "tag:yaml.org,2002:int": re.compile(r"[-+]?(0|[1-9][0-9]*)"),
    "tag:yaml.org,2002:float": re.compile(
        r"[-+]?([0-9]+\.[0-9]*|\.[0-9]+)([eE][-+][0-9]+)?|[-+]?\.(inf|Inf|INF)|\.(nan|NaN|NAN)"
    ),
}


def _refuse_anchors_and_deep_nesting(text: str) -> None:
    open_is_flow: list[bool] = []
    depth = {True: 0, False: 0}
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            line = event.start_mark.line + 1
            raise PolicyError(f"YAML anchors and aliases are not allowed (line {line})")
        if isinstance(event, yaml.CollectionStartEvent):
            flow = event.flow_style is True
            open_is_flow.append(flow)
            depth[flow] += 1
            if depth[flow] > _MAX_DEPTH:
                style = "flow" if flow else "block"
                raise PolicyError(f"{style} collections nest deeper than {_MAX_DEPTH} levels")
        elif isinstance(event, yaml.CollectionEndEvent):
            depth[open_is_flow.pop()] -= 1


def _refuse_odd_nodes(node: yaml.Node) -> None:
    plain = _PLAIN_NUMBERS.get(node.tag)
    if plain is not None and not plain.fullmatch(node.value):
        raise PolicyError(
            f"line {node.start_mark.line + 1}: {node.value} is not a plain decimal number; "
            "YAML reads a leading 0 as octal, 0x as hex and a colon as base 60, and drops "
            "underscores, so write digits only, like 3600 or 0.5"
        )
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            line = key.start_mark.line + 1
            if not isinstance(key, yaml.ScalarNode) or key.tag == "tag:yaml.org,2002:merge":
                raise PolicyError(
                    f"only plain keys are allowed, no merge or complex keys (line {line})"
                )
            if key.value in seen:
                raise PolicyError(f"duplicate key {key.value!r} at line {line}")
            seen.add(key.value)
            _refuse_odd_nodes(value)
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            _refuse_odd_nodes(item)


def _parse_yaml(text: str) -> Any:
    _refuse_anchors_and_deep_nesting(text)
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            return None
        _refuse_odd_nodes(node)
        return loader.construct_document(node)
    finally:
        loader.dispose()


def _refuse_writable_dir(directory: Path) -> None:
    mode = os.stat(directory).st_mode
    if mode & stat.S_IWOTH and not mode & stat.S_ISVTX:
        raise PolicyError(
            f"policy folder {directory} is world-writable without the sticky bit; {_MODE_HINT}"
        )


def _refuse_unsafe_file(path: Path, st: os.stat_result) -> None:
    if stat.S_ISDIR(st.st_mode):
        raise PolicyError(f"policy {path} is a directory")
    if not stat.S_ISREG(st.st_mode):
        raise PolicyError(f"policy {path} is not a regular file")
    if st.st_uid != os.geteuid():
        raise PolicyError(f"policy {path} belongs to uid {st.st_uid}, not you; {_MODE_HINT}")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PolicyError(
            f"policy {path} has mode {stat.S_IMODE(st.st_mode):04o}: group or others can "
            f"write it; {_MODE_HINT}"
        )


def _read_judging_the_open_file(path: Path) -> bytes:
    for directory in {path.absolute().parent, Path(os.path.realpath(path)).parent}:
        _refuse_writable_dir(directory)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        _refuse_unsafe_file(path, os.fstat(fd))
    except PolicyError:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_POLICY_BYTES + 1)
    if len(data) > MAX_POLICY_BYTES:
        raise PolicyError(f"policy {path} is larger than {MAX_POLICY_BYTES} bytes")
    return data


def load_policy(path: Path) -> Policy:
    try:
        text = _read_judging_the_open_file(path).decode("utf-8")
        return parse_policy(_parse_yaml(text))
    except PolicyError:
        raise
    except OSError as exc:
        raise PolicyError(f"cannot read policy {path}: {exc.strerror}") from None
    except yaml.YAMLError as exc:
        raise PolicyError(f"policy {path} is not valid YAML: {exc}") from None
    except (RecursionError, TypeError, ValueError) as exc:
        raise PolicyError(f"policy {path} is malformed: {type(exc).__name__}: {exc}") from None


_ASSET_KEYS = frozenset(
    {"asset_id", "margin_mode", "leverage", "mark_px", "mark_at_ms", "max_leverage"}
)


def _asset_state(v: Any, field: str) -> AssetState:
    """Without ``max_leverage`` the asset's own leverage stands in for it, the
    most cautious maximum for the liquidation bound."""
    if isinstance(v, Mapping) and "max_leverage" not in v:
        v = {**v, "max_leverage": v.get("leverage")}
    a = _mapping(v, field, _ASSET_KEYS)
    asset_id = _int(a["asset_id"], f"{field}.asset_id")
    if asset_id >= SPOT_ASSET_OFFSET:
        raise PolicyError(f"{field}.asset_id {asset_id} is not a perp asset id")
    if a["margin_mode"] not in ("isolated", "cross"):
        raise PolicyError(f"{field}.margin_mode must be isolated or cross")
    return AssetState(
        asset_id=asset_id,
        margin_mode=a["margin_mode"],
        leverage=_int(a["leverage"], f"{field}.leverage", minimum=1),
        mark_px=_positive(a["mark_px"], f"{field}.mark_px"),
        mark_at_ms=_int(a["mark_at_ms"], f"{field}.mark_at_ms"),
        max_leverage=_int(a["max_leverage"], f"{field}.max_leverage", minimum=1),
    )


def _open_order(v: Any, field: str) -> OpenOrder:
    o = _mapping(v, field, frozenset({"coin", "notional_usd", "reduce_only"}))
    return OpenOrder(
        coin=_coin(o["coin"], f"{field}.coin"),
        notional_usd=_non_negative(o["notional_usd"], f"{field}.notional_usd"),
        reduce_only=_bool(o["reduce_only"], f"{field}.reduce_only"),
    )


def _entry_record(v: Any, field: str) -> EntryRecord:
    r = _mapping(v, field, frozenset({"coin", "at_ms"}))
    return EntryRecord(coin=_coin(r["coin"], f"{field}.coin"), at_ms=_int(r["at_ms"], field))


_ACCOUNT_KEYS = frozenset(
    {
        "now_ms",
        "snapshot_at_ms",
        "day_start_equity_usd",
        "realized_pnl_today_usd",
        "loss_streak",
        "latched",
        "assets",
        "positions",
        "open_orders",
        "entries",
        "margin_added_at_ms",
    }
)


def parse_account(raw: Any) -> Account:
    """``entries`` and ``margin_added_at_ms`` hold today's UTC records plus the
    cooldown window, at most 1000 each: the gate reads nothing older."""
    a = _mapping(raw, "account", _ACCOUNT_KEYS)
    for name in ("assets", "positions"):
        if not isinstance(a[name], Mapping):
            raise PolicyError(f"account.{name} must be a mapping")
    try:
        return Account(
            now_ms=_int(a["now_ms"], "account.now_ms"),
            snapshot_at_ms=_int(a["snapshot_at_ms"], "account.snapshot_at_ms"),
            day_start_equity_usd=_positive(
                a["day_start_equity_usd"], "account.day_start_equity_usd"
            ),
            realized_pnl_today_usd=_number(
                a["realized_pnl_today_usd"], "account.realized_pnl_today_usd"
            ),
            loss_streak=_int(a["loss_streak"], "account.loss_streak"),
            latched=_bool(a["latched"], "account.latched"),
            assets={
                _coin(c, "account.assets"): _asset_state(s, f"account.assets.{c}")
                for c, s in a["assets"].items()
            },
            positions={
                _coin(c, "account.positions"): _number(s, f"account.positions.{c}")
                for c, s in a["positions"].items()
            },
            open_orders=_list(
                a["open_orders"], "account.open_orders", _open_order, MAX_ACCOUNT_RECORDS
            ),
            entries=_list(a["entries"], "account.entries", _entry_record, MAX_ACCOUNT_RECORDS),
            margin_added_at_ms=_list(
                a["margin_added_at_ms"], "account.margin_added_at_ms", _int, MAX_ACCOUNT_RECORDS
            ),
        )
    except PolicyError:
        raise
    except ValueError as exc:
        raise PolicyError(f"account: {exc}") from None


def _optional_px(v: Any, field: str) -> Decimal | None:
    return None if v is None else _positive(v, field)


_PROPOSAL_KEYS: Mapping[str, frozenset[str]] = {
    "entry": frozenset(
        {"kind", "coin", "is_buy", "size_coins", "limit_px", "stop_loss_px", "take_profit_px"}
    ),
    "close": frozenset({"kind", "coin", "size_coins", "limit_px"}),
    "set_leverage": frozenset({"kind", "coin", "leverage"}),
    "schedule_cancel": frozenset({"kind", "time_ms"}),
    "add_margin": frozenset({"kind", "coin", "usd"}),
}


def parse_proposal(raw: Any) -> Proposal:
    kind = raw.get("kind") if isinstance(raw, Mapping) else None
    if not isinstance(kind, str) or kind not in _PROPOSAL_KEYS:
        raise PolicyError(f"proposal.kind must be one of {sorted(_PROPOSAL_KEYS)}, got {kind!r}")
    if kind == "entry":
        raw = {"stop_loss_px": None, "take_profit_px": None, **raw}
    p = _mapping(raw, f"proposal ({kind})", _PROPOSAL_KEYS[kind])
    match kind:
        case "entry":
            return Entry(
                coin=_coin(p["coin"], "proposal.coin"),
                is_buy=_bool(p["is_buy"], "proposal.is_buy"),
                size_coins=_positive(p["size_coins"], "proposal.size_coins"),
                limit_px=_positive(p["limit_px"], "proposal.limit_px"),
                stop_loss_px=_optional_px(p["stop_loss_px"], "proposal.stop_loss_px"),
                take_profit_px=_optional_px(p["take_profit_px"], "proposal.take_profit_px"),
            )
        case "close":
            return Close(
                coin=_coin(p["coin"], "proposal.coin"),
                size_coins=_positive(p["size_coins"], "proposal.size_coins"),
                limit_px=_positive(p["limit_px"], "proposal.limit_px"),
            )
        case "set_leverage":
            return SetLeverage(
                coin=_coin(p["coin"], "proposal.coin"),
                leverage=_int(p["leverage"], "proposal.leverage", minimum=1),
            )
        case "add_margin":
            return AddMargin(
                coin=_coin(p["coin"], "proposal.coin"),
                usd=_positive(p["usd"], "proposal.usd"),
            )
        case _:
            return ScheduleCancel(time_ms=_int(p["time_ms"], "proposal.time_ms"))
