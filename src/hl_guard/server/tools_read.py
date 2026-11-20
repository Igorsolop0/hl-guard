from __future__ import annotations

import contextlib
import dataclasses
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any, Generic, TypeVar

from eth_account.signers.local import LocalAccount

from hl_guard.approval.model import PROTECTION
from hl_guard.exchange.halt import HaltFlag
from hl_guard.ledger.ledger import Kind, Ledger, LedgerError, Record, Tail
from hl_guard.policy.gate import drawdown, gross_exposure_usd
from hl_guard.policy.model import ApproveAboveUsd, ApproveAlways, Policy, Rule, in_size
from hl_guard.server.children import fold_children, stop_warnings
from hl_guard.server.config import ServeConfig
from hl_guard.server.info import InfoClient, InfoRateLimited, InfoRefused, InfoUnavailable
from hl_guard.server.parse import (
    PERP_NAME,
    Market,
    ShapeError,
    parse_active_asset,
    parse_book,
    parse_candles,
    parse_clearinghouse_state,
    parse_fills,
    parse_markets,
    parse_open_orders,
)
from hl_guard.server.snapshot import (
    DAY_MS,
    GATE_CONTEXT,
    Exchange,
    Folded,
    LedgerView,
    Mark,
    RiskFold,
    SnapshotError,
    build_account,
    coins_needing_leverage,
    held_blocks,
    new_entry_blocks,
)

MARKETS_TTL_MS = 60_000
LISTING_TTL_MS = 3_600_000
C = TypeVar("C")
MINUTE_MS = 60_000
CANDLE_MS: Mapping[str, int] = {
    "1m": MINUTE_MS,
    "3m": 3 * MINUTE_MS,
    "5m": 5 * MINUTE_MS,
    "15m": 15 * MINUTE_MS,
    "30m": 30 * MINUTE_MS,
    "1h": 60 * MINUTE_MS,
    "2h": 120 * MINUTE_MS,
    "4h": 240 * MINUTE_MS,
    "8h": 480 * MINUTE_MS,
    "12h": 720 * MINUTE_MS,
    "1d": 1440 * MINUTE_MS,
    "3d": 3 * 1440 * MINUTE_MS,
    "1w": 7 * 1440 * MINUTE_MS,
    "1M": 30 * 1440 * MINUTE_MS,
}


class Failure(StrEnum):
    BAD_ARGUMENTS = "bad_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_COIN = "unknown_coin"
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    REFUSED = "refused"
    BAD_SHAPE = "bad_shape"
    LEDGER = "ledger"


class ToolFailure(Exception):
    def __init__(self, kind: Failure, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


_FAILURES: tuple[tuple[type[Exception], Failure], ...] = (
    (InfoUnavailable, Failure.NETWORK),
    (InfoRateLimited, Failure.RATE_LIMITED),
    (InfoRefused, Failure.REFUSED),
    (ShapeError, Failure.BAD_SHAPE),
    (SnapshotError, Failure.BAD_SHAPE),
    (LedgerError, Failure.LEDGER),
)


def as_failure(exc: Exception) -> ToolFailure | None:
    if isinstance(exc, ToolFailure):
        return exc
    for error, kind in _FAILURES:
        if isinstance(exc, error):
            return ToolFailure(kind, str(exc))
    return None


def _bad(detail: str) -> ToolFailure:
    return ToolFailure(Failure.BAD_ARGUMENTS, detail)


@dataclass(frozen=True)
class CoinArg:
    description: str
    required = True

    def schema(self) -> dict[str, Any]:
        return {
            "type": "string",
            "pattern": f"^{PERP_NAME.pattern}$",
            "description": self.description,
        }

    def parse(self, name: str, v: Any) -> str:
        if not isinstance(v, str) or not PERP_NAME.fullmatch(v):
            raise _bad(f"{name} must be a perp coin name of 1 to 16 ASCII letters or digits")
        return v


@dataclass(frozen=True)
class CoinsArg:
    description: str
    max_items: int
    required = False

    def schema(self) -> dict[str, Any]:
        return {
            "type": "array",
            "items": CoinArg("").schema() | {"description": "a perp coin name"},
            "minItems": 1,
            "maxItems": self.max_items,
            "uniqueItems": True,
            "description": self.description,
        }

    def parse(self, name: str, v: Any) -> tuple[str, ...]:
        if not isinstance(v, list) or not 1 <= len(v) <= self.max_items:
            raise _bad(f"{name} must be a list of 1 to {self.max_items} coin names")
        coins = tuple(CoinArg("").parse(f"{name}[{i}]", c) for i, c in enumerate(v))
        if len(set(coins)) != len(coins):
            raise _bad(f"{name} repeats a coin")
        return coins


@dataclass(frozen=True)
class IntArg:
    description: str
    low: int
    high: int
    default: int | None

    @property
    def required(self) -> bool:
        return self.default is None

    def schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "integer",
            "minimum": self.low,
            "maximum": self.high,
            "description": self.description,
        }
        return schema if self.default is None else schema | {"default": self.default}

    def parse(self, name: str, v: Any) -> int:
        if type(v) is not int or not self.low <= v <= self.high:
            raise _bad(f"{name} must be an integer from {self.low} to {self.high}")
        return v


@dataclass(frozen=True)
class ChoiceArg:
    description: str
    choices: tuple[str, ...]
    default: str | None = None

    @property
    def required(self) -> bool:
        return self.default is None

    def schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "string",
            "enum": list(self.choices),
            "description": self.description,
        }
        return schema if self.default is None else schema | {"default": self.default}

    def parse(self, name: str, v: Any) -> str:
        if not isinstance(v, str) or v not in self.choices:
            raise _bad(f"{name} must be one of {list(self.choices)}")
        return v


_DECIMAL_TEXT = re.compile(r"[0-9]{1,30}(\.[0-9]{1,30})?")


@dataclass(frozen=True)
class DecimalArg:
    """A JSON number arrives as a float; its repr is the shortest spelling, so
    0.1 reads as Decimal("0.1")."""

    description: str
    required: bool = True

    def schema(self) -> dict[str, Any]:
        return {
            "type": ["string", "number"],
            "pattern": f"^{_DECIMAL_TEXT.pattern}$",
            "exclusiveMinimum": 0,
            "description": self.description,
        }

    def parse(self, name: str, v: Any) -> Decimal:
        text = repr(v) if type(v) in (int, float) else v
        if not isinstance(text, str) or not _DECIMAL_TEXT.fullmatch(text):
            raise _bad(f'{name} must be a positive decimal such as 0.001 or "0.001"')
        value = Decimal(text)
        if not in_size(value) or value == 0:
            raise _bad(f"{name} must be above 0 and at most 1e30")
        return value


@dataclass(frozen=True)
class PatternArg:
    description: str
    pattern: re.Pattern[str]
    required = True

    def schema(self) -> dict[str, Any]:
        return {
            "type": "string",
            "pattern": f"^{self.pattern.pattern}$",
            "description": self.description,
        }

    def parse(self, name: str, v: Any) -> str:
        if not isinstance(v, str) or not self.pattern.fullmatch(v):
            raise _bad(f"{name} must match {self.pattern.pattern}")
        return v


Arg = CoinArg | CoinsArg | IntArg | ChoiceArg | DecimalArg | PatternArg


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class Runtime:
    config: ServeConfig
    policy: Policy
    network: str
    info: InfoClient
    ledger: Ledger
    halt: HaltFlag
    now_ms: Callable[[], int] = _now_ms
    agent: LocalAccount | None = field(default=None, repr=False)
    _markets: tuple[int, dict[str, Market]] | None = field(default=None, repr=False)
    _tail: Tail | None = field(default=None, repr=False)
    _risk: Folded[RiskFold] = field(default_factory=lambda: Folded(RiskFold), repr=False)

    def tail(self) -> Tail:
        if self._tail is None or self._tail.ledger is not self.ledger:
            self._tail = Tail(self.ledger)
        return self._tail

    def records(self) -> list[Record]:
        return self.tail().records()

    def risk(self) -> RiskFold:
        return self._risk.over(self.records())

    def fresh_markets(self, headroom: int = 0) -> tuple[int, dict[str, Market]]:
        markets = parse_markets(self.info.post({"type": "metaAndAssetCtxs"}, headroom=headroom))
        self._markets = (self.now_ms(), markets)
        return self._markets

    def markets_within(self, max_age_ms: int, headroom: int = 0) -> dict[str, Market]:
        """The cached market read, if younger than ``max_age_ms``. Its prices are
        for display: the gate takes marks from fresh reads."""
        cached = self._markets
        if cached is None or self.now_ms() - cached[0] > max_age_ms:
            cached = self.fresh_markets(headroom)
        return cached[1]

    def market(self, coin: str, max_age_ms: int = MARKETS_TTL_MS, headroom: int = 0) -> Market:
        found = self.markets_within(max_age_ms, headroom).get(coin)
        if found is None:
            raise ToolFailure(
                Failure.UNKNOWN_COIN, f"{coin} is not a perp market on {self.network}"
            )
        return found

    def user_post(self, kind: str, headroom: int = 0, **extra: str | int) -> Any:
        body = {"type": kind, "user": self.config.account, **extra}
        return self.info.post(body, headroom=headroom)

    def first_equity_today(self, now_ms: int, account_value: Decimal) -> Decimal | None:
        """The equity of the first funded read of the UTC day, kept in the ledger
        so a restart keeps the day's loss allowance. Only a server with write
        tools records it."""
        day = now_ms // DAY_MS
        recorded = self.risk().state.day_starts.get(day)
        if recorded is not None or account_value <= 0:
            return recorded
        if self.agent is None:
            return account_value
        newest = self.risk().last_ts_ms
        if max(now_ms, newest) // DAY_MS != day:
            raise ToolFailure(
                Failure.REFUSED,
                f"the clock is {newest - now_ms} ms behind the ledger's newest record, across "
                f"UTC midnight, so the day start of day {day} cannot be recorded; wait for the "
                "clock to pass it",
            )
        with contextlib.suppress(ValueError):
            payload = {"day": day, "equity_usd": str(account_value)}
            self.tail().append(Kind.DAY_START, payload, ts_ms=max(now_ms, newest))
        return self.risk().state.day_starts.get(day)

    def day_start_recorded(self, now_ms: int) -> bool:
        return now_ms // DAY_MS in self.risk().state.day_starts


@dataclass(frozen=True)
class Tool(Generic[C]):
    name: str
    description: str
    args: Mapping[str, Arg]
    run: Callable[[C, Mapping[str, Any]], Any]
    read_only: bool = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {name: arg.schema() for name, arg in self.args.items()},
            "required": [name for name, arg in self.args.items() if arg.required],
            "additionalProperties": False,
        }

    def parse_args(self, raw: Mapping[str, Any] | None) -> dict[str, Any]:
        given = dict(raw or {})
        unknown = sorted(set(given) - set(self.args))
        if unknown:
            raise _bad(
                f"{self.name} takes no argument {unknown[0]!r:.60}; it takes {sorted(self.args)}"
            )
        missing = [n for n, a in self.args.items() if a.required and n not in given]
        if missing:
            raise _bad(f"{self.name} needs the argument {missing[0]!r}")
        parsed: dict[str, Any] = {}
        for name, arg in self.args.items():
            if name in given:
                parsed[name] = arg.parse(name, given[name])
            elif isinstance(arg, IntArg | ChoiceArg) and arg.default is not None:
                parsed[name] = arg.default
        return parsed


def _markets(rt: Runtime, args: Mapping[str, Any]) -> Any:
    _, markets = rt.fresh_markets()
    coins = args.get("coins") or tuple(sorted(rt.policy.coins))
    unknown = [c for c in coins if c not in markets]
    if unknown:
        raise ToolFailure(Failure.UNKNOWN_COIN, f"{unknown} are not perp markets on {rt.network}")
    return {"network": rt.network, "markets": [markets[c] for c in coins]}


def _order_book(rt: Runtime, args: Mapping[str, Any]) -> Any:
    coin, depth = args["coin"], args["depth"]
    rt.market(coin)
    book = parse_book(rt.info.post({"type": "l2Book", "coin": coin}), coin)
    return dataclasses.replace(book, bids=book.bids[:depth], asks=book.asks[:depth])


def _candles(rt: Runtime, args: Mapping[str, Any]) -> Any:
    coin, interval, count = args["coin"], args["interval"], args["count"]
    rt.market(coin)
    end = rt.now_ms()
    request = {"coin": coin, "interval": interval, "startTime": end - count * CANDLE_MS[interval]}
    raw = rt.info.post(
        {"type": "candleSnapshot", "req": request | {"endTime": end}}, rows_max=count + 1
    )
    return parse_candles(raw, coin, interval)[-count:]


def _state(rt: Runtime) -> Any:
    return parse_clearinghouse_state(rt.user_post("clearinghouseState"))


def _account(rt: Runtime, args: Mapping[str, Any]) -> Any:
    state = _state(rt)
    return {
        "account": rt.config.account,
        "network": rt.network,
        "account_value_usd": state.account_value,
        "position_notional_usd": state.total_ntl_pos,
        "margin_used_usd": state.total_margin_used,
        "withdrawable_usd": state.withdrawable,
        "time_ms": state.time_ms,
    }


def _positions(rt: Runtime, args: Mapping[str, Any]) -> Any:
    positions = _state(rt).positions
    records = rt.records()
    kids = fold_children(records)
    return [
        dataclasses.asdict(p) | {"stop_warnings": stop_warnings(kids, records, p.coin, p.size)}
        for p in positions
    ]


def _open_orders(rt: Runtime, args: Mapping[str, Any]) -> Any:
    return parse_open_orders(rt.user_post("frontendOpenOrders"))


def _fills(rt: Runtime, args: Mapping[str, Any]) -> Any:
    return parse_fills(rt.user_post("userFills"), args["limit"])


def _approval(policy: Policy) -> Any:
    match policy.approval:
        case ApproveAlways():
            return "always"
        case ApproveAboveUsd(usd):
            return {"above_usd": usd}
        case _:
            return "never"


def _limits(policy: Policy) -> dict[str, Any]:
    return dataclasses.asdict(policy) | {
        "coins": sorted(policy.coins),
        "stop_loss_required": True,
        "approval": _approval(policy),
    }


def _latch(view: LedgerView) -> Any:
    if view.latch is None:
        return None
    payload = view.latch.payload
    return {"seq": view.latch.seq, "ts_ms": view.latch.ts_ms} | {
        k: payload.get(k) for k in ("rule", "detail")
    }


def _read_exchange(rt: Runtime) -> Exchange:
    markets_received, markets = rt.fresh_markets()
    state = _state(rt)
    state_received = rt.now_ms()
    orders = parse_open_orders(rt.user_post("frontendOpenOrders"))
    marks = {coin: Mark(m.mark_px, markets_received) for coin, m in markets.items()}
    partial = Exchange(state, state_received, markets, marks, orders, {})
    leverages = {
        coin: parse_active_asset(rt.user_post("activeAssetData", coin=coin), coin).leverage
        for coin in coins_needing_leverage(partial)
    }
    return dataclasses.replace(partial, leverages=leverages)


def _risk_status(rt: Runtime, args: Mapping[str, Any]) -> Any:
    policy = rt.policy
    exchange = _read_exchange(rt)
    now = rt.now_ms()
    view = rt.risk().view(now, policy.cooldown_s)
    halted = rt.halt.present()
    report: dict[str, Any] = {
        "network": rt.network,
        "account": rt.config.account,
        "halted": halted,
        "protection": PROTECTION,
        "latch": _latch(view),
        "limits": _limits(policy),
        "loss_streak": {"current": view.loss_streak, "max": policy.loss_streak_max},
        "entries_today": {
            "count": sum(1 for e in view.entries if e.at_ms // DAY_MS == now // DAY_MS),
            "max": policy.entries_per_day_max,
        },
    }
    equity = rt.first_equity_today(now, exchange.state.account_value)
    if equity is None:
        report["daily_loss"] = {"day_start_equity_usd": None, "remaining_usd": Decimal(0)}
        blocked = held_blocks(halted, view.latch is not None)
        return report | {"new_entries_blocked_by": [*blocked, "no equity: the account value is 0"]}
    account = build_account(now, equity, exchange, view)
    with localcontext(GATE_CONTEXT):
        limit = equity * policy.daily_loss_pct_max / 100
        dropped = drawdown(policy, equity, exchange.state.account_value)
        remaining = max(Decimal(0), limit + account.realized_pnl_today_usd)
        if dropped is not None:
            remaining = Decimal(0)
        gross = gross_exposure_usd(account)
        opening = sum(1 for o in account.open_orders if not o.reduce_only)
        report |= {
            "daily_loss": {
                "day_start_equity_usd": equity,
                "day_start_recorded": rt.day_start_recorded(now),
                "limit_usd": limit,
                "realized_pnl_today_usd": account.realized_pnl_today_usd,
                "remaining_usd": remaining,
            },
            "opening_orders": {"count": opening, "max": policy.open_orders_max},
            "exposure_usd": {
                "gross": gross,
                "max": policy.total_exposure_usd_max,
                "remaining": max(Decimal(0), policy.total_exposure_usd_max - gross),
            },
        }
    blocks = new_entry_blocks(policy, account, halted=halted)
    rule = Rule.DAILY_LOSS_PCT_MAX.value
    if dropped is not None and not any(b.startswith(f"{rule}:") for b in blocks):
        blocks.append(f"{rule}: {dropped}")
    return report | {"new_entries_blocked_by": blocks}


_COIN = CoinArg("perp coin name as Hyperliquid lists it, for example BTC")

TOOLS: Mapping[str, Tool[Runtime]] = {
    tool.name: tool
    for tool in (
        Tool(
            "markets",
            "Read perp market data: mark, mid and oracle prices, funding, open interest and "
            "24 h volume. Without coins it reads the coins in the user's policy.",
            {"coins": CoinsArg("perp coin names to read", 50)},
            _markets,
        ),
        Tool(
            "order_book",
            "Read the L2 order book of one perp coin: price levels with size and order count.",
            {"coin": _COIN, "depth": IntArg("levels per side", 1, 20, 10)},
            _order_book,
        ),
        Tool(
            "candles",
            "Read recent OHLCV candles of one perp coin, oldest first.",
            {
                "coin": _COIN,
                "interval": ChoiceArg("candle interval", tuple(CANDLE_MS)),
                "count": IntArg("number of candles", 1, 500, 50),
            },
            _candles,
        ),
        Tool(
            "account",
            "Read the configured account's perp equity, position notional, margin in use "
            "and free collateral in USD.",
            {},
            _account,
        ),
        Tool(
            "positions",
            "Read the configured account's open perp positions with size, entry price, "
            "leverage and unrealized PnL. An account with no positions reads as an empty list. "
            "stop_warnings lists each open hl-guard stop on the coin, as the ledger records it, "
            "that is larger than hl-guard's own lot: its own net fills, capped by the position. "
            "A trade made in the app on the same coin can hide this, since hl-guard cannot tell "
            "the user's coins from its own. This tool never changes a stop.",
            {},
            _positions,
        ),
        Tool(
            "open_orders",
            "Read the configured account's resting orders, including reduce-only and trigger "
            "orders.",
            {},
            _open_orders,
        ),
        Tool(
            "fills",
            "Read the configured account's recent fills, newest first.",
            {"limit": IntArg("number of fills", 1, 200, 20)},
            _fills,
        ),
        Tool(
            "risk_status",
            "Read the user's risk limits and where the account stands against them: the "
            "remaining daily loss allowance, entries today, exposure, the loss-streak latch "
            "and whether new entries are blocked. Only the user's policy file sets the limits.",
            {},
            _risk_status,
        ),
    )
}


def _default(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, frozenset):
        return sorted(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def encode(value: Any) -> str:
    return json.dumps(value, default=_default, separators=(",", ":"))
