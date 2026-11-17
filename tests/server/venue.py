"""A small Hyperliquid stand-in for the write path: one account, marks you set,
orders that fill at the mark when they cross it, and the /info reads that
follow from that state."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from server.fake_info import Answer, FakeInfo, recorded, write_config

TAKER_FEE = Decimal("0.00045")


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _s(d: Decimal) -> str:
    return f"{d.normalize():f}" if d else "0.0"


def _expired(body: dict[str, Any]) -> bool:
    after = body.get("expiresAfter")
    return after is not None and _now_ms() > after


def _ok(kind: str, statuses: list[Any]) -> Answer:
    body = {"status": "ok", "response": {"type": kind, "data": {"statuses": statuses}}}
    return 200, json.dumps(body).encode(), {}


@dataclass
class Position:
    size: Decimal
    entry_px: Decimal
    leverage: int


@dataclass
class Venue:
    account_value: Decimal = Decimal(1000)
    marks: dict[str, Decimal] = field(default_factory=dict)
    names: list[str] = field(default_factory=list)
    leverage: dict[str, tuple[str, int]] = field(default_factory=dict)
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[dict[str, Any]] = field(default_factory=list)
    fills: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    next_oid: int = 1000
    next_tid: int = 5000
    exchange_override: Callable[[dict[str, Any]], Answer] | None = None
    fills_lag: int = 0
    by_cloid: dict[str, dict[str, Any]] = field(default_factory=dict)
    answer_after: Callable[[dict[str, Any]], Answer | None] | None = None
    page_rows: int = 2000
    fills_asked: list[dict[str, Any]] = field(default_factory=list)
    hold: bool = False
    hidden: set[int] = field(default_factory=set)
    held: list[dict[str, Any]] = field(default_factory=list)
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        meta, ctxs = json.loads(recorded("metaAndAssetCtxs"))
        self.names = [u["name"] for u in meta["universe"]]
        self.marks = {
            u["name"]: Decimal(c["markPx"]) for u, c in zip(meta["universe"], ctxs, strict=True)
        }

    def install(self, fake: FakeInfo) -> None:
        for kind in (
            "clearinghouseState",
            "frontendOpenOrders",
            "activeAssetData",
            "userFillsByTime",
            "orderStatus",
        ):
            fake.answers[kind] = getattr(self, f"_{kind}")
        fake.exchange = self.exchange

    def lev(self, coin: str) -> tuple[str, int]:
        return self.leverage.get(coin, ("cross", 10))

    def _clearinghouseState(self, body: dict[str, Any]) -> Answer:
        rows = []
        for coin, p in self.positions.items():
            mode, value = self.lev(coin)
            mark = self.marks[coin]
            rows.append(
                {
                    "type": "oneWay",
                    "position": {
                        "coin": coin,
                        "szi": _s(p.size),
                        "leverage": {"type": mode, "value": value},
                        "entryPx": _s(p.entry_px),
                        "positionValue": _s(abs(p.size) * mark),
                        "unrealizedPnl": _s(p.size * (mark - p.entry_px)),
                        "liquidationPx": None,
                        "marginUsed": _s(abs(p.size) * mark / value),
                    },
                }
            )
        ntl = sum((abs(p.size) * self.marks[c] for c, p in self.positions.items()), Decimal(0))
        state = {
            "marginSummary": {
                "accountValue": _s(self.account_value),
                "totalNtlPos": _s(ntl),
                "totalRawUsd": _s(self.account_value),
                "totalMarginUsed": "0.0",
            },
            "withdrawable": _s(self.account_value),
            "assetPositions": rows,
            "time": _now_ms(),
        }
        return 200, json.dumps(state).encode(), {}

    def _frontendOpenOrders(self, body: dict[str, Any]) -> Answer:
        rows = [
            {
                "coin": o["coin"],
                "side": "B" if o["b"] else "A",
                "limitPx": o["p"],
                "sz": o["s"],
                "origSz": o["s"],
                "oid": o["oid"],
                "timestamp": o["at"],
                "reduceOnly": o["r"],
                "orderType": "Limit",
                "tif": o["tif"],
                "isTrigger": False,
                "triggerPx": "0.0",
                "cloid": o["c"],
                "children": [],
                "isPositionTpsl": False,
                "triggerCondition": "N/A",
            }
            for o in self.orders
            if o["oid"] not in self.hidden
        ]
        return 200, json.dumps(rows).encode(), {}

    def _activeAssetData(self, body: dict[str, Any]) -> Answer:
        coin = body["coin"]
        mode, value = self.lev(coin)
        data = {
            "coin": coin,
            "leverage": {"type": mode, "value": value},
            "maxTradeSzs": ["1", "1"],
            "availableToTrade": ["1", "1"],
            "markPx": _s(self.marks[coin]),
        }
        return 200, json.dumps(data).encode(), {}

    def _userFillsByTime(self, body: dict[str, Any]) -> Answer:
        self.fills_asked.append(body)
        start = body["startTime"]
        seen = self.fills[: len(self.fills) - self.fills_lag] if self.fills_lag else self.fills
        rows = sorted((f for f in seen if f["time"] >= start), key=lambda f: f["time"])
        rows = rows[: self.page_rows]
        return 200, json.dumps(rows).encode(), {}

    def _orderStatus(self, body: dict[str, Any]) -> Answer:
        asked = body["oid"]
        known = next(
            (k for c, k in self.by_cloid.items() if asked in (c, k["oid"])),
            None,
        )
        if known is None:
            return 200, json.dumps({"status": "unknownOid"}).encode(), {}
        order = {k: known[k] for k in ("coin", "oid", "origSz", "sz")}
        answer = {"status": "order", "order": {"order": order, "status": known["status"]}}
        return 200, json.dumps(answer).encode(), {}

    def _fill(self, coin: str, is_buy: bool, size: Decimal, oid: int) -> Decimal:
        px = self.marks[coin]
        signed = size if is_buy else -size
        held = self.positions.get(coin)
        before = held.size if held else Decimal(0)
        after = before + signed
        closed = Decimal(0)
        if held is not None and (before > 0) != (signed > 0):
            closed = min(abs(before), size) * (px - held.entry_px) * (1 if before > 0 else -1)
        if after == 0:
            self.positions.pop(coin, None)
        elif held is None or (before > 0) == (signed > 0):
            entry = (before * (held.entry_px if held else px) + signed * px) / after
            self.positions[coin] = Position(after, entry, self.lev(coin)[1])
        elif (after > 0) != (before > 0):
            self.positions[coin] = Position(after, px, self.lev(coin)[1])
        else:
            self.positions[coin] = Position(after, held.entry_px, self.lev(coin)[1])
        fee = size * px * TAKER_FEE
        self.account_value += closed - fee
        self.fills.append(
            {
                "coin": coin,
                "side": "B" if is_buy else "A",
                "px": _s(px),
                "sz": _s(size),
                "time": _now_ms(),
                "startPosition": _s(before),
                "closedPnl": _s(closed),
                "fee": _s(fee),
                "feeToken": "USDC",
                "oid": oid,
                "tid": self.next_tid,
                "crossed": True,
                "hash": "0x" + "0" * 64,
                "dir": "Open Long",
            }
        )
        self.next_tid += 1
        return px

    def _order(self, wire: dict[str, Any]) -> Any:
        coin = self.names[wire["a"]]
        px, size, is_buy = Decimal(wire["p"]), Decimal(wire["s"]), wire["b"]
        tif = wire["t"]["limit"]["tif"]
        held = self.positions.get(coin)
        if wire["r"] and (held is None or (held.size > 0) == is_buy or size > abs(held.size)):
            return {"error": "Reduce only order would increase position."}
        if not wire["r"] and size * px < 10:
            return {"error": "Order must have minimum value of $10."}
        mark = self.marks[coin]
        crosses = px >= mark if is_buy else px <= mark
        self.next_oid += 1
        oid = self.next_oid
        known = {"coin": coin, "oid": oid, "origSz": _s(size), "sz": _s(size), "status": "open"}
        self.by_cloid[wire["c"]] = known
        if crosses:
            avg = self._fill(coin, is_buy, size, oid)
            known |= {"sz": "0.0", "status": "filled"}
            return {"filled": {"totalSz": _s(size), "avgPx": _s(avg), "oid": oid}}
        if tif == "Ioc":
            known["status"] = "canceled"
            text = "Order could not immediately match against any resting orders."
            return {"error": f"{text} asset={wire['a']}"}
        self.orders.append({**wire, "coin": coin, "oid": oid, "tif": tif, "at": _now_ms()})
        return {"resting": {"oid": oid}}

    def exchange(self, body: dict[str, Any]) -> Answer:
        self.bodies.append(body)
        if self.hold:
            self.held.append(body)
            return 500, b"upstream timeout", {}
        self.actions.append(body["action"])
        if self.exchange_override is not None:
            return self.exchange_override(body)
        if _expired(body):
            return 200, json.dumps({"status": "err", "response": "expired"}).encode(), {}
        done = self._act(body["action"])
        late = None if self.answer_after is None else self.answer_after(body)
        return done if late is None else late

    def _act(self, action: dict[str, Any]) -> Answer:
        match action["type"]:
            case "order":
                return _ok("order", [self._order(w) for w in action["orders"]])
            case "cancel":
                statuses: list[Any] = []
                for c in action["cancels"]:
                    found = [o for o in self.orders if o["oid"] == c["o"]]
                    self.orders = [o for o in self.orders if o["oid"] != c["o"]]
                    for o in found:
                        self.by_cloid[o["c"]]["status"] = "canceled"
                    statuses.append(
                        "success"
                        if found
                        else {"error": "Order was never placed, already canceled, or filled."}
                    )
                return _ok("cancel", statuses)
            case "updateLeverage":
                coin = self.names[action["asset"]]
                self.leverage[coin] = ("isolated", action["leverage"])
                return (
                    200,
                    json.dumps({"status": "ok", "response": {"type": "default"}}).encode(),
                    {},
                )
        return 200, json.dumps({"status": "err", "response": "unknown action"}).encode(), {}

    def land(self) -> list[Answer]:
        bodies, self.held, self.hold = self.held, [], False
        return [self.exchange(b) for b in bodies]

    def move(self, coin: str, mark: Decimal | int | str) -> None:
        self.marks[coin] = Decimal(mark)

    def fill_resting(self, oid: int, size: str | None = None) -> None:
        (order,) = [o for o in self.orders if o["oid"] == oid]
        part = Decimal(size or order["s"])
        left = Decimal(order["s"]) - part
        self.orders.remove(order)
        if left:
            self.orders.append({**order, "s": _s(left)})
        self.by_cloid[order["c"]] |= {"sz": _s(left), "status": "open" if left else "filled"}
        self._fill(order["coin"], order["b"], part, oid)


def write_agent_key(root: Path) -> Path:
    from eth_account import Account

    path = root / "agent.key"
    path.write_text(Account.create().key.hex() + "\n")
    path.chmod(0o600)
    return path


def configure(
    root: Path, *, policy: dict[str, str] | None = None, extra: str = "", weight: int = 600
) -> Path:
    config = write_config(root)
    write_agent_key(root)
    config.write_text(
        config.read_text() + f"agent_key_file: agent.key\ninfo_weight_per_min: {weight}\n" + extra
    )
    path = root / "policy.yaml"
    text = path.read_text()
    for key, value in (policy or {}).items():
        lines = [line for line in text.splitlines() if not line.startswith(f"{key}:")]
        text = "\n".join([*lines, f"{key}: {value}"]) + "\n"
    path.write_text(text)
    return config
