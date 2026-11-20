"""A small Hyperliquid stand-in for the write path: one account, marks you set,
orders that fill at the mark when they cross it, and the /info reads that
follow from that state.

A normalTpsl entry behaves as testnet showed on 2026-11-19: its children rest
as reduce-only triggers at the full ordered size even after a partial fill,
with their own oids and no oid in the ack; they are one-cancels-other; a
batchModify by cloid cancels and replaces them under new oids; and orderStatus
by cloid then answers with the old, cancelled order. An Ioc parent that does
not fill answers with a child slot no run has shown, so the server must treat
it as an unknown shape."""

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
    liquidity: dict[str, Decimal] = field(default_factory=dict)
    by_oid: dict[int, dict[str, Any]] = field(default_factory=dict)
    modify_override: Callable[[dict[str, Any]], Answer] | None = None

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
        rows = [self._row(o) for o in self.orders if o["oid"] not in self.hidden]
        return 200, json.dumps(rows).encode(), {}

    def _row(self, o: dict[str, Any]) -> dict[str, Any]:
        trigger = o.get("trigger")
        kind = {"sl": "Stop Market", "tp": "Take Profit Market"}
        return {
            "coin": o["coin"],
            "side": "B" if o["b"] else "A",
            "limitPx": o["p"],
            "sz": o["s"],
            "origSz": o["s"],
            "oid": o["oid"],
            "timestamp": o["at"],
            "reduceOnly": o["r"],
            "orderType": kind[trigger["tpsl"]] if trigger else "Limit",
            "tif": None if trigger else o["tif"],
            "isTrigger": trigger is not None,
            "triggerPx": trigger["triggerPx"] if trigger else "0.0",
            "cloid": o["c"],
            "children": [],
            "isPositionTpsl": False,
            "triggerCondition": "N/A",
        }

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
        known = self.by_oid.get(asked) if isinstance(asked, int) else None
        known = known or next(
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
        tif = wire["t"].get("limit", {}).get("tif")
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
            part = min(size, self.liquidity.get(coin, size))
            avg = self._fill(coin, is_buy, part, oid)
            known |= {"sz": _s(size - part), "status": "filled"}
            self._drop_orphans(coin)
            return {"filled": {"totalSz": _s(part), "avgPx": _s(avg), "oid": oid}}
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

    def _grouped(self, wires: list[dict[str, Any]]) -> list[Any]:
        parent = self._order(wires[0])
        if "filled" not in parent:
            return [parent, *("notObservedOnTestnet" for _ in wires[1:])]
        for w in wires[1:]:
            self.next_oid += 1
            coin = self.names[w["a"]]
            child = {**w, "coin": coin, "oid": self.next_oid, "tif": None, "at": _now_ms()}
            child |= {"trigger": w["t"]["trigger"], "parent": wires[0]["c"]}
            self.orders.append(child)
            self.by_cloid[w["c"]] = {
                "coin": coin,
                "oid": child["oid"],
                "origSz": w["s"],
                "sz": w["s"],
                "status": "open",
            }
        return [parent, *("waitingForTrigger" for _ in wires[1:])]

    def _modify(self, m: dict[str, Any]) -> Any:
        cloid, order = m["oid"], m["order"]
        found = [o for o in self.orders if o["c"] == cloid and o.get("trigger")]
        if not found:
            return {"error": "Cannot modify canceled or filled order"}
        (old,) = found
        self.by_cloid[cloid]["status"] = "canceled"
        old_record = self.by_oid.get(old["oid"])
        if old_record is not None:
            old_record["status"] = "canceled"
        self.next_oid += 1
        new = {**old, "s": order["s"], "oid": self.next_oid, "at": _now_ms()}
        self.orders[self.orders.index(old)] = new
        self.by_oid[new["oid"]] = {
            "coin": old["coin"],
            "oid": new["oid"],
            "origSz": order["s"],
            "sz": order["s"],
            "status": "open",
        }
        return {"resting": {"oid": new["oid"], "cloid": cloid}}

    def _record_of(self, o: dict[str, Any]) -> dict[str, Any]:
        return self.by_oid.get(o["oid"]) or self.by_cloid[o["c"]]

    def _drop_orphans(self, coin: str) -> None:
        if coin in self.positions:
            return
        for o in [o for o in self.orders if o["coin"] == coin and o.get("trigger")]:
            self.orders.remove(o)
            self._record_of(o)["status"] = "reduceOnlyCanceled"

    def trigger(self, coin: str) -> list[int]:
        """Fire every child whose trigger the mark crossed, one per entry; its
        sibling ends siblingFilledCanceled."""
        fired: list[int] = []
        mark = self.marks[coin]
        for o in [o for o in self.orders if o["coin"] == coin and o.get("trigger")]:
            if o not in self.orders:
                continue
            px, sells = Decimal(o["trigger"]["triggerPx"]), not o["b"]
            below = o["trigger"]["tpsl"] == "sl" if sells else o["trigger"]["tpsl"] == "tp"
            if not (mark <= px if below else mark >= px):
                continue
            held = self.positions.get(coin)
            self.orders.remove(o)
            record = self._record_of(o)
            if held is None or (held.size > 0) == o["b"]:
                record["status"] = "reduceOnlyCanceled"
                continue
            size = min(Decimal(o["s"]), abs(held.size))
            self._fill(coin, o["b"], size, o["oid"])
            record |= {"sz": _s(Decimal(o["s"]) - size), "status": "filled"}
            fired.append(o["oid"])
            for sibling in [s for s in self.orders if s.get("parent") == o.get("parent")]:
                self.orders.remove(sibling)
                self._record_of(sibling)["status"] = "siblingFilledCanceled"
            self._drop_orphans(coin)
        return fired

    def _act(self, action: dict[str, Any]) -> Answer:
        match action["type"]:
            case "order" if action["grouping"] == "normalTpsl":
                return _ok("order", self._grouped(action["orders"]))
            case "order":
                placed = [self._order(w) for w in action["orders"]]
                for w in action["orders"]:
                    self._drop_orphans(self.names[w["a"]])
                return _ok("order", placed)
            case "batchModify":
                if self.modify_override is not None:
                    return self.modify_override(action)
                return _ok("order", [self._modify(m) for m in action["modifies"]])
            case "cancelByCloid":
                cancelled: list[Any] = []
                for c in action["cancels"]:
                    found = [o for o in self.orders if o["c"] == c["cloid"]]
                    for o in found:
                        self.orders.remove(o)
                        self._record_of(o)["status"] = "canceled"
                    cancelled.append(
                        "success"
                        if found
                        else {"error": "Order was never placed, already canceled, or filled."}
                    )
                return _ok("cancel", cancelled)
            case "cancel":
                statuses: list[Any] = []
                for c in action["cancels"]:
                    found = [o for o in self.orders if o["oid"] == c["o"]]
                    self.orders = [o for o in self.orders if o["oid"] != c["o"]]
                    for o in found:
                        self._record_of(o)["status"] = "canceled"
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

    def resting(self) -> list[dict[str, Any]]:
        """The resting limit orders, without the stop children."""
        return [o for o in self.orders if not o.get("trigger")]

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
