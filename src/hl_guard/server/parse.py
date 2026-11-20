from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, TypeVar, get_args

T = TypeVar("T")

Side = Literal["buy", "sell"]
MarginMode = Literal["cross", "isolated"]
OrderType = Literal[
    "Limit", "Market", "Stop Market", "Stop Limit", "Take Profit Market", "Take Profit Limit"
]
Tif = Literal["Alo", "Ioc", "Gtc", "FrontendMarket", "LiquidationMarket"]

ROWS_MAX = 1000
MARKETS_MAX = 4096
FILLS_MAX = 2000
CANDLES_MAX = 5000
LEVELS_MAX = 100
_INT_MAX = 2**63 - 1

PERP_NAME = re.compile(r"[A-Za-z0-9]{1,16}")
_MARKET_NAME = re.compile(
    r"[A-Za-z0-9]{1,16}|@[0-9]{1,6}|[A-Za-z0-9]{1,16}/[A-Za-z0-9]{1,16}|[a-z0-9]{1,8}:[A-Za-z0-9]{1,16}"
)
_DECIMAL = re.compile(r"-?[0-9]{1,30}(\.[0-9]{1,30})?")
_CLOID = re.compile(r"0x[0-9a-f]{32}")
_SIDES: Mapping[str, Side] = {"B": "buy", "A": "sell"}
_MARGIN_MODES: tuple[MarginMode, ...] = get_args(MarginMode)
_ORDER_TYPES: tuple[OrderType, ...] = get_args(OrderType)
_TIFS: tuple[Tif, ...] = get_args(Tif)


def _shown(v: Any) -> str:
    if isinstance(v, str):
        return f"a {len(v)}-character string"
    if v is None or isinstance(v, bool | int):
        return repr(v)[:40]
    return f"a {type(v).__name__}"


class ShapeError(ValueError):
    def __init__(self, endpoint: str, path: str, problem: str) -> None:
        super().__init__(f"{endpoint} response at {path or 'top level'}: {problem}")


@dataclass(frozen=True)
class _At:
    endpoint: str
    path: str = ""

    def __getitem__(self, key: str | int) -> _At:
        if isinstance(key, int):
            return _At(self.endpoint, f"{self.path}[{key}]")
        return _At(self.endpoint, f"{self.path}.{key}" if self.path else key)

    def fail(self, problem: str) -> ShapeError:
        return ShapeError(self.endpoint, self.path, problem)


def _obj(v: Any, at: _At, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(v, dict):
        raise at.fail(f"expected an object, got {type(v).__name__}")
    missing = sorted(keys - set(v))
    if missing:
        raise at.fail(f"missing keys {missing}")
    return v


def _list(v: Any, at: _At, cap: int) -> list[Any]:
    if not isinstance(v, list):
        raise at.fail(f"expected a list, got {type(v).__name__}")
    if len(v) > cap:
        raise at.fail(f"holds {len(v)} items; the cap is {cap}")
    return v


def _rows(v: Any, at: _At, cap: int, row: Callable[[Any, _At], T]) -> tuple[T, ...]:
    return tuple(row(item, at[i]) for i, item in enumerate(_list(v, at, cap)))


def _decimal(v: Any, at: _At) -> Decimal:
    if not isinstance(v, str) or not _DECIMAL.fullmatch(v):
        raise at.fail(f"expected a plain decimal string, got {_shown(v)}")
    return Decimal(v)


def _non_negative(v: Any, at: _At) -> Decimal:
    d = _decimal(v, at)
    if d < 0:
        raise at.fail("must not be negative")
    return d


def _positive(v: Any, at: _At) -> Decimal:
    d = _decimal(v, at)
    if d <= 0:
        raise at.fail("must be above 0")
    return d


def _optional(parse: Callable[[Any, _At], T]) -> Callable[[Any, _At], T | None]:
    def parse_or_none(v: Any, at: _At) -> T | None:
        return None if v is None else parse(v, at)

    return parse_or_none


def _int(v: Any, at: _At, minimum: int = 0) -> int:
    if type(v) is not int or not minimum <= v <= _INT_MAX:
        raise at.fail(f"expected an integer from {minimum} to 2**63-1, got {_shown(v)}")
    return v


def _bool(v: Any, at: _At) -> bool:
    if not isinstance(v, bool):
        raise at.fail(f"expected true or false, got {_shown(v)}")
    return v


def _name(v: Any, at: _At, pattern: re.Pattern[str] = _MARKET_NAME) -> str:
    if not isinstance(v, str) or not pattern.fullmatch(v):
        raise at.fail(f"not a market name: {_shown(v)}")
    return v


def _one_of(v: Any, at: _At, allowed: tuple[T, ...]) -> T:
    for option in allowed:
        if v == option:
            return option
    raise at.fail(f"expected one of {list(allowed)}, got {_shown(v)}")


def _side(v: Any, at: _At) -> Side:
    if not isinstance(v, str) or v not in _SIDES:
        raise at.fail(f"expected side B or A, got {_shown(v)}")
    return _SIDES[v]


def _same(v: Any, at: _At, expected: str) -> None:
    if v != expected:
        raise at.fail(f"expected {expected!r}, got {_shown(v)}")


@dataclass(frozen=True)
class Market:
    name: str
    asset_id: int
    sz_decimals: int
    max_leverage: int
    only_isolated: bool
    delisted: bool
    mark_px: Decimal
    mid_px: Decimal | None
    oracle_px: Decimal
    prev_day_px: Decimal
    funding: Decimal
    open_interest: Decimal
    day_ntl_vlm: Decimal


def parse_markets(raw: Any) -> dict[str, Market]:
    at = _At("metaAndAssetCtxs")
    pair = _list(raw, at, 2)
    if len(pair) != 2:
        raise at.fail("expected [meta, asset contexts]")
    meta = _obj(pair[0], at[0], frozenset({"universe"}))
    universe = _list(meta["universe"], at[0]["universe"], MARKETS_MAX)
    contexts = _list(pair[1], at[1], MARKETS_MAX)
    if len(universe) != len(contexts):
        raise at.fail(f"{len(universe)} assets but {len(contexts)} asset contexts")
    markets: dict[str, Market] = {}
    for asset_id, (u, c) in enumerate(zip(universe, contexts, strict=True)):
        ua, ca = at[0]["universe"][asset_id], at[1][asset_id]
        info = _obj(u, ua, frozenset({"name", "szDecimals", "maxLeverage"}))
        ctx = _obj(
            c,
            ca,
            frozenset(
                {"markPx", "midPx", "oraclePx", "prevDayPx", "funding", "openInterest", "dayNtlVlm"}
            ),
        )
        name = _name(info["name"], ua["name"], PERP_NAME)
        if name in markets:
            raise ua["name"].fail(f"repeats the market name {name}")
        markets[name] = Market(
            name=name,
            asset_id=asset_id,
            sz_decimals=_int(info["szDecimals"], ua["szDecimals"]),
            max_leverage=_int(info["maxLeverage"], ua["maxLeverage"], minimum=1),
            only_isolated=_bool(info.get("onlyIsolated", False), ua["onlyIsolated"]),
            delisted=_bool(info.get("isDelisted", False), ua["isDelisted"]),
            mark_px=_positive(ctx["markPx"], ca["markPx"]),
            mid_px=_optional(_positive)(ctx["midPx"], ca["midPx"]),
            oracle_px=_positive(ctx["oraclePx"], ca["oraclePx"]),
            prev_day_px=_positive(ctx["prevDayPx"], ca["prevDayPx"]),
            funding=_decimal(ctx["funding"], ca["funding"]),
            open_interest=_non_negative(ctx["openInterest"], ca["openInterest"]),
            day_ntl_vlm=_non_negative(ctx["dayNtlVlm"], ca["dayNtlVlm"]),
        )
    return markets


@dataclass(frozen=True)
class Level:
    px: Decimal
    sz: Decimal
    orders: int


@dataclass(frozen=True)
class Book:
    coin: str
    time_ms: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]


def _level(v: Any, at: _At) -> Level:
    o = _obj(v, at, frozenset({"px", "sz", "n"}))
    return Level(
        px=_positive(o["px"], at["px"]),
        sz=_positive(o["sz"], at["sz"]),
        orders=_int(o["n"], at["n"], minimum=1),
    )


def parse_book(raw: Any, coin: str) -> Book:
    at = _At("l2Book")
    o = _obj(raw, at, frozenset({"coin", "time", "levels"}))
    _same(o["coin"], at["coin"], coin)
    levels = _list(o["levels"], at["levels"], 2)
    if len(levels) != 2:
        raise at["levels"].fail("expected [bids, asks]")
    return Book(
        coin=coin,
        time_ms=_int(o["time"], at["time"]),
        bids=_rows(levels[0], at["levels"][0], LEVELS_MAX, _level),
        asks=_rows(levels[1], at["levels"][1], LEVELS_MAX, _level),
    )


@dataclass(frozen=True)
class Candle:
    open_ms: int
    close_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades: int


def parse_candles(raw: Any, coin: str, interval: str) -> tuple[Candle, ...]:
    def candle(v: Any, at: _At) -> Candle:
        o = _obj(v, at, frozenset({"t", "T", "s", "i", "o", "c", "h", "l", "v", "n"}))
        _same(o["s"], at["s"], coin)
        _same(o["i"], at["i"], interval)
        return Candle(
            open_ms=_int(o["t"], at["t"]),
            close_ms=_int(o["T"], at["T"]),
            open=_positive(o["o"], at["o"]),
            high=_positive(o["h"], at["h"]),
            low=_positive(o["l"], at["l"]),
            close=_positive(o["c"], at["c"]),
            volume=_non_negative(o["v"], at["v"]),
            trades=_int(o["n"], at["n"]),
        )

    return _rows(raw, _At("candleSnapshot"), CANDLES_MAX, candle)


@dataclass(frozen=True)
class Leverage:
    mode: MarginMode
    value: int


def _leverage(v: Any, at: _At) -> Leverage:
    o = _obj(v, at, frozenset({"type", "value"}))
    return Leverage(
        mode=_one_of(o["type"], at["type"], _MARGIN_MODES),
        value=_int(o["value"], at["value"], minimum=1),
    )


@dataclass(frozen=True)
class Position:
    coin: str
    size: Decimal
    entry_px: Decimal
    leverage: Leverage
    position_value: Decimal
    unrealized_pnl: Decimal
    liquidation_px: Decimal | None
    margin_used: Decimal


@dataclass(frozen=True)
class AccountState:
    account_value: Decimal
    total_ntl_pos: Decimal
    total_margin_used: Decimal
    withdrawable: Decimal
    positions: tuple[Position, ...]
    time_ms: int


def _position(v: Any, at: _At) -> Position:
    row = _obj(v, at, frozenset({"type", "position"}))
    _same(row["type"], at["type"], "oneWay")
    pa = at["position"]
    p = _obj(
        row["position"],
        pa,
        frozenset(
            {
                "coin",
                "szi",
                "entryPx",
                "leverage",
                "positionValue",
                "unrealizedPnl",
                "liquidationPx",
                "marginUsed",
            }
        ),
    )
    return Position(
        coin=_name(p["coin"], pa["coin"], PERP_NAME),
        size=_decimal(p["szi"], pa["szi"]),
        entry_px=_positive(p["entryPx"], pa["entryPx"]),
        leverage=_leverage(p["leverage"], pa["leverage"]),
        position_value=_non_negative(p["positionValue"], pa["positionValue"]),
        unrealized_pnl=_decimal(p["unrealizedPnl"], pa["unrealizedPnl"]),
        liquidation_px=_optional(_non_negative)(p["liquidationPx"], pa["liquidationPx"]),
        margin_used=_non_negative(p["marginUsed"], pa["marginUsed"]),
    )


def parse_clearinghouse_state(raw: Any) -> AccountState:
    at = _At("clearinghouseState")
    o = _obj(raw, at, frozenset({"marginSummary", "withdrawable", "assetPositions", "time"}))
    ma = at["marginSummary"]
    m = _obj(o["marginSummary"], ma, frozenset({"accountValue", "totalNtlPos", "totalMarginUsed"}))
    positions = _rows(o["assetPositions"], at["assetPositions"], ROWS_MAX, _position)
    coins = [p.coin for p in positions]
    if len(set(coins)) != len(coins):
        raise at["assetPositions"].fail("repeats a coin")
    return AccountState(
        account_value=_decimal(m["accountValue"], ma["accountValue"]),
        total_ntl_pos=_non_negative(m["totalNtlPos"], ma["totalNtlPos"]),
        total_margin_used=_non_negative(m["totalMarginUsed"], ma["totalMarginUsed"]),
        withdrawable=_non_negative(o["withdrawable"], at["withdrawable"]),
        positions=positions,
        time_ms=_int(o["time"], at["time"]),
    )


@dataclass(frozen=True)
class OrderRow:
    coin: str
    side: Side
    limit_px: Decimal
    size: Decimal
    orig_size: Decimal
    oid: int
    timestamp_ms: int
    reduce_only: bool
    order_type: OrderType
    tif: Tif | None
    trigger_px: Decimal | None
    cloid: str | None


def _cloid(v: Any, at: _At) -> str:
    if not isinstance(v, str) or not _CLOID.fullmatch(v):
        raise at.fail(f"expected a cloid of 0x and 32 lowercase hex digits, got {_shown(v)}")
    return v


def _order(v: Any, at: _At) -> OrderRow:
    o = _obj(
        v,
        at,
        frozenset(
            {
                "coin",
                "side",
                "limitPx",
                "sz",
                "origSz",
                "oid",
                "timestamp",
                "reduceOnly",
                "orderType",
                "tif",
                "isTrigger",
                "triggerPx",
                "cloid",
            }
        ),
    )
    is_trigger = _bool(o["isTrigger"], at["isTrigger"])
    trigger_px = _non_negative(o["triggerPx"], at["triggerPx"])
    return OrderRow(
        coin=_name(o["coin"], at["coin"]),
        side=_side(o["side"], at["side"]),
        limit_px=_non_negative(o["limitPx"], at["limitPx"]),
        size=_non_negative(o["sz"], at["sz"]),
        orig_size=_non_negative(o["origSz"], at["origSz"]),
        oid=_int(o["oid"], at["oid"]),
        timestamp_ms=_int(o["timestamp"], at["timestamp"]),
        reduce_only=_bool(o["reduceOnly"], at["reduceOnly"]),
        order_type=_one_of(o["orderType"], at["orderType"], _ORDER_TYPES),
        tif=None if o["tif"] is None else _one_of(o["tif"], at["tif"], _TIFS),
        trigger_px=trigger_px if is_trigger else None,
        cloid=_optional(_cloid)(o["cloid"], at["cloid"]),
    )


def parse_open_orders(raw: Any) -> tuple[OrderRow, ...]:
    return _rows(raw, _At("frontendOpenOrders"), ROWS_MAX, _order)


@dataclass(frozen=True)
class FillRow:
    coin: str
    side: Side
    px: Decimal
    size: Decimal
    time_ms: int
    start_position: Decimal
    closed_pnl: Decimal
    fee: Decimal
    fee_token: str
    oid: int
    tid: int
    crossed: bool


def _fill(v: Any, at: _At) -> FillRow:
    o = _obj(
        v,
        at,
        frozenset(
            {
                "coin",
                "side",
                "px",
                "sz",
                "time",
                "startPosition",
                "closedPnl",
                "fee",
                "feeToken",
                "oid",
                "tid",
                "crossed",
            }
        ),
    )
    return FillRow(
        coin=_name(o["coin"], at["coin"]),
        side=_side(o["side"], at["side"]),
        px=_positive(o["px"], at["px"]),
        size=_positive(o["sz"], at["sz"]),
        time_ms=_int(o["time"], at["time"]),
        start_position=_decimal(o["startPosition"], at["startPosition"]),
        closed_pnl=_decimal(o["closedPnl"], at["closedPnl"]),
        fee=_decimal(o["fee"], at["fee"]),
        fee_token=_name(o["feeToken"], at["feeToken"], PERP_NAME),
        oid=_int(o["oid"], at["oid"]),
        tid=_int(o["tid"], at["tid"]),
        crossed=_bool(o["crossed"], at["crossed"]),
    )


def parse_fills(raw: Any, limit: int = FILLS_MAX) -> tuple[FillRow, ...]:
    """``userFills`` answers newest first, at most 2000 rows."""
    at = _At("userFills")
    return _rows(_list(raw, at, FILLS_MAX)[:limit], at, FILLS_MAX, _fill)


@dataclass(frozen=True)
class ActiveAsset:
    leverage: Leverage
    mark_px: Decimal


def parse_active_asset(raw: Any, coin: str) -> ActiveAsset:
    at = _At("activeAssetData")
    o = _obj(raw, at, frozenset({"coin", "leverage", "markPx"}))
    _same(o["coin"], at["coin"], coin)
    return ActiveAsset(
        _leverage(o["leverage"], at["leverage"]), _positive(o["markPx"], at["markPx"])
    )


@dataclass(frozen=True)
class Resting:
    oid: int


@dataclass(frozen=True)
class Filled:
    oid: int
    total_sz: Decimal
    avg_px: Decimal


@dataclass(frozen=True)
class Refused:
    message: str


OrderAck = Resting | Filled | Refused
EXCHANGE_TEXT_MAX = 200


def exchange_text(v: Any) -> str:
    text = "".join(c for c in str(v) if c.isascii() and c.isprintable())
    return text[:EXCHANGE_TEXT_MAX]


def _statuses(raw: Any, at: _At, kind: str, n: int, *, exact: bool = True) -> list[Any]:
    o = _obj(raw, at, frozenset({"status", "response"}))
    _same(o["status"], at["status"], "ok")
    ra = at["response"]
    response = _obj(o["response"], ra, frozenset({"type", "data"}))
    _same(response["type"], ra["type"], kind)
    data = _obj(response["data"], ra["data"], frozenset({"statuses"}))
    statuses = _list(data["statuses"], ra["data"]["statuses"], n)
    if exact and len(statuses) != n:
        raise ra["data"]["statuses"].fail(f"holds {len(statuses)} statuses for {n} items")
    return statuses


def _order_ack(v: Any, at: _At, cloid: str) -> OrderAck:
    if not isinstance(v, dict) or len(v) != 1:
        raise at.fail("expected one of resting, filled or error")
    ((key, body),) = v.items()
    if key == "error":
        return Refused(exchange_text(body))
    if key == "resting":
        o = _obj(body, at["resting"], frozenset({"oid"}))
        _echoed_cloid(o, at["resting"], cloid)
        return Resting(_int(o["oid"], at["resting"]["oid"]))
    if key == "filled":
        fa = at["filled"]
        o = _obj(body, fa, frozenset({"oid", "totalSz", "avgPx"}))
        _echoed_cloid(o, fa, cloid)
        return Filled(
            _int(o["oid"], fa["oid"]),
            _positive(o["totalSz"], fa["totalSz"]),
            _positive(o["avgPx"], fa["avgPx"]),
        )
    raise at.fail(f"unknown order status {_shown(key)}")


def _echoed_cloid(o: Mapping[str, Any], at: _At, cloid: str) -> None:
    if "cloid" in o and o["cloid"] != cloid:
        raise at["cloid"].fail(f"names another cloid than the one sent: {_shown(o['cloid'])}")


def parse_order_acks(raw: Any, cloids: tuple[str, ...]) -> tuple[OrderAck, ...]:
    at = _At("exchange order")
    statuses = _statuses(raw, at, "order", len(cloids))
    return tuple(_order_ack(v, at[i], cloids[i]) for i, v in enumerate(statuses))


ChildSlot = Literal["waitingForFill", "waitingForTrigger"]
_CHILD_SLOT: Mapping[type, ChildSlot] = {Resting: "waitingForFill", Filled: "waitingForTrigger"}


def parse_entry_acks(raw: Any, cloids: tuple[str, ...]) -> tuple[OrderAck, tuple[ChildSlot, ...]]:
    """The ack of a normalTpsl entry: the entry's slot, then one bare string per
    child, ``waitingForFill`` for a resting entry and ``waitingForTrigger`` for a
    filled one. A child slot has no oid. A refused entry answers one error slot
    for the whole group, and its children are never placed (testnet T9). Any
    other child slot or count is an unknown shape."""
    at = _At("exchange order")
    statuses = _statuses(raw, at, "order", len(cloids), exact=False)
    if len(statuses) == 1 and len(cloids) > 1:
        refused = _order_ack(statuses[0], at[0], cloids[0])
        if not isinstance(refused, Refused):
            raise at.fail(f"holds 1 status for {len(cloids)} orders that is not an error")
        return refused, ()
    if len(statuses) != len(cloids):
        raise at.fail(f"holds {len(statuses)} statuses for {len(cloids)} orders")
    entry = _order_ack(statuses[0], at[0], cloids[0])
    slot = _CHILD_SLOT.get(type(entry))
    for i, v in enumerate(statuses[1:], start=1):
        if slot is None or v != slot:
            raise at[i].fail(f"expected the child slot {slot!r} after {type(entry).__name__}")
    return entry, tuple(slot for _ in statuses[1:] if slot is not None)


def parse_modify_acks(raw: Any, cloids: tuple[str, ...]) -> tuple[Resting | Refused, ...]:
    """A batchModify answers like an order: each modified child rests again
    under a new oid, with the same cloid."""
    at = _At("exchange batchModify")
    acks: list[Resting | Refused] = []
    for i, v in enumerate(_statuses(raw, at, "order", len(cloids))):
        ack = _order_ack(v, at[i], cloids[i])
        if isinstance(ack, Filled):
            raise at[i].fail("a modified stop child cannot fill at once")
        acks.append(ack)
    return tuple(acks)


def parse_cancel_acks(raw: Any, n: int) -> tuple[Refused | None, ...]:
    """``None`` for each cancel the exchange made."""
    at = _At("exchange cancel")
    acks: list[Refused | None] = []
    for i, v in enumerate(_statuses(raw, at, "cancel", n)):
        if v == "success":
            acks.append(None)
        elif isinstance(v, dict) and set(v) == {"error"}:
            acks.append(Refused(exchange_text(v["error"])))
        else:
            raise at[i].fail(f"expected success or an error, got {_shown(v)}")
    return tuple(acks)


def parse_default_ack(raw: Any) -> None:
    at = _At("exchange")
    o = _obj(raw, at, frozenset({"status", "response"}))
    _same(o["status"], at["status"], "ok")
    response = _obj(o["response"], at["response"], frozenset({"type"}))
    _same(response["type"], at["response"]["type"], "default")


@dataclass(frozen=True)
class OrderState:
    """One order as ``orderStatus`` reports it. ``status`` is the exchange's
    lowercase word, such as open, filled or canceled."""

    oid: int
    coin: str
    status: str
    orig_size: Decimal
    remaining: Decimal

    @property
    def resting(self) -> bool:
        return self.status in ("open", "triggered")


_STATUS_WORD = re.compile(r"[a-zA-Z]{1,40}")


def parse_order_status(raw: Any) -> OrderState | None:
    at = _At("orderStatus")
    o = _obj(raw, at, frozenset({"status"}))
    if o["status"] == "unknownOid":
        return None
    _same(o["status"], at["status"], "order")
    oa = at["order"]
    outer = _obj(o.get("order"), oa, frozenset({"order", "status"}))
    inner = _obj(outer["order"], oa["order"], frozenset({"oid", "coin", "origSz", "sz"}))
    word = outer["status"]
    if not isinstance(word, str) or not _STATUS_WORD.fullmatch(word):
        raise oa["status"].fail(f"expected a status word, got {_shown(word)}")
    return OrderState(
        oid=_int(inner["oid"], oa["order"]["oid"]),
        coin=_name(inner["coin"], oa["order"]["coin"]),
        status=word.lower(),
        orig_size=_positive(inner["origSz"], oa["order"]["origSz"]),
        remaining=_non_negative(inner["sz"], oa["order"]["sz"]),
    )
