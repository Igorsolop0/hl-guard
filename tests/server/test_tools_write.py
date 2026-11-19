from __future__ import annotations

import dataclasses
import fcntl
import json
import os
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp_types import TextContent

from hl_guard.approval import cli as approval_cli
from hl_guard.approval import model as approval_model
from hl_guard.approval.cli import decide
from hl_guard.exchange.nonces import NonceManager
from hl_guard.exchange.transport import ExchangeTransport
from hl_guard.ledger.cli import latch_clear
from hl_guard.ledger.ledger import Kind, Ledger, Record
from hl_guard.server import tools_write
from hl_guard.server.app import StartupRefused, boot, build_server, serve, startup_lines
from hl_guard.server.info import InfoRateLimited
from hl_guard.server.tools_read import TOOLS
from hl_guard.server.tools_write import (
    RISK_HEADROOM,
    ServeLockRefused,
    Writer,
    on_price_grid,
    to_price_grid,
)
from server.conftest import Clock, work_under_exclusive_lock
from server.fake_info import Answer, FakeInfo, write_config
from server.venue import Position, Venue, configure

BTC = {"coin": "BTC", "side": "buy", "size": "0.0002", "limit_px": "81171", "stop_loss_px": "80000"}
Step = tuple[str, dict[str, Any]] | Callable[[Writer, list[Any]], Any]


def session(config: Path, *steps: Step, autonomous: bool = False) -> list[Any]:
    runtime = boot(config, None, autonomous=autonomous)
    writer = Writer.start(runtime)

    async def run() -> list[Any]:
        out: list[Any] = []
        async with Client(build_server(runtime, writer)) as client:
            for step in steps:
                if callable(step):
                    out.append(await anyio.to_thread.run_sync(step, writer, out))
                    continue
                name, args = step
                result = await client.call_tool(name, args)
                content = result.content[0]
                assert isinstance(content, TextContent)
                out.append((result.is_error, json.loads(content.text)))
        return out

    try:
        return anyio.run(run)
    finally:
        writer.release()


def records(config: Path) -> list[Record]:
    return list(Ledger(config.parent / "data" / "ledger.jsonl").records())


def trail(config: Path, pid: str) -> list[str]:
    out = []
    for r in records(config):
        if r.payload.get("id") != pid:
            continue
        outcome = r.payload.get("outcome")
        out.append(f"{r.kind.value}:{outcome}" if outcome else r.kind.value)
    return out


def last_id(out: list[Any]) -> str:
    found: str = next(
        r[1]["id"]
        for r in reversed(out)
        if isinstance(r, tuple) and isinstance(r[1], dict) and "id" in r[1]
    )
    return found


def approve(config: Path, approved: bool = True) -> Callable[[Writer, list[Any]], Any]:
    def run(writer: Writer, out: list[Any]) -> Any:
        code, lines = decide(config, last_id(out), approved)
        writer.poll()
        return code, lines

    return run


def rest(args: dict[str, Any]) -> Callable[[Writer, list[Any]], Any]:
    """A resting Gtc entry with no stops on the exchange, as hl-guard 0.1.0 placed
    it: the gate passes it, then the plain order goes out. The ledger of a server
    upgraded to 0.2.0 can still hold one, and its fills must still be booked."""

    def run(writer: Writer, out: list[Any]) -> Any:
        body = {"tif": "Gtc", "leverage": 1, "take_profit_px": None, **args}
        order = approval_model.order_from_json(body)
        assert order is not None
        pid = writer._propose("propose_order", order.as_json())
        plan = writer._evaluate(pid, order)
        if isinstance(plan, tools_write.Rejected):
            return False, dataclasses.asdict(plan)
        writer._decide(pid, approval_model.Status.ALLOWED, detail="a 0.1.0 entry")
        asset = plan.market.asset_id
        if plan.set_leverage:
            change = {"asset": asset, "isCross": False, "leverage": 1}
            writer._send(pid, tools_write.build_action("updateLeverage", change), 0)
        entry = {
            "a": asset,
            "b": order.is_buy,
            "p": args["limit_px"],
            "s": args["size"],
            "r": False,
            "t": {"limit": {"tif": "Gtc"}},
            "c": tools_write._cloid(),
        }
        try:
            (outcome,) = writer._send_orders(pid, [(order.coin, entry)], 0)
        except tools_write.ToolFailure as exc:
            return True, {"error": exc.kind.value, "detail": exc.detail}
        return False, json.loads(json.dumps(dataclasses.asdict(outcome), default=str))

    return run


def _status_of() -> Callable[[Writer, list[Any]], Any]:
    def run(writer: Writer, out: list[Any]) -> Any:
        return False, writer.status({"id": last_id(out)})

    return run


def actions_sent(config: Path) -> list[dict[str, Any]]:
    return [r.payload["action"] for r in records(config) if r.kind is Kind.ORDER]


def info_kinds(fake: FakeInfo) -> list[str]:
    return [r["type"] for r in fake.requests]


def test_an_order_above_the_cap_is_rejected_before_any_read(
    config: Path, fake: FakeInfo, venue: Venue
) -> None:
    ((error, result),) = session(config, ("propose_order", {**BTC, "size": "0.002"}))

    assert error is False
    assert result["result"] == "rejected"
    assert result["rule"] == "per_order_usd_max"
    assert venue.actions == []
    assert info_kinds(fake) == []
    assert trail(config, result["id"]) == ["proposal", "decision:rejected"]


@pytest.mark.parametrize(
    ("args", "rule"),
    [
        ({k: v for k, v in BTC.items() if k != "stop_loss_px"}, "stop_loss_required"),
        ({**BTC, "coin": "DOGE"}, "coins"),
        ({**BTC, "size": "0.0001"}, "min_notional"),
        ({**BTC, "leverage": 2}, "leverage_max"),
    ],
    ids=["no stop-loss", "coin outside the policy", "below 10 USD", "leverage above the policy"],
)
def test_static_rules_reject_without_a_read(
    config: Path, fake: FakeInfo, venue: Venue, args: dict[str, Any], rule: str
) -> None:
    ((_, result),) = session(config, ("propose_order", args))

    assert (result["result"], result["rule"]) == ("rejected", rule)
    assert venue.actions == []
    assert "clearinghouseState" not in info_kinds(fake)


@pytest.mark.parametrize(
    ("args", "rule"),
    [
        ({**BTC, "size": "0.000201"}, "size_decimals"),
        ({**BTC, "limit_px": "81171.5"}, "price_tick"),
        ({**BTC, "stop_loss_px": "80000.5"}, "price_tick"),
        ({**BTC, "limit_px": "81900", "stop_loss_px": "81000"}, "price_band_pct_max"),
        ({**BTC, "stop_loss_px": "79000"}, "stop_loss_max_pct"),
    ],
    ids=["size decimals", "limit tick", "stop tick", "price band", "stop too far"],
)
def test_market_and_gate_rules_reject_with_their_name(
    config: Path, venue: Venue, args: dict[str, Any], rule: str
) -> None:
    ((_, result),) = session(config, ("propose_order", args))

    assert (result["result"], result["rule"]) == ("rejected", rule)
    assert venue.actions == []


def test_a_short_stop_past_liquidation_at_5x_is_rejected(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(
        tmp_path, policy={"coins": "[BTC, APE]", "leverage_max": "5", "stop_loss_max_pct": "10"}
    )
    venue.move("APE", "0.14324")
    short = {"coin": "APE", "side": "sell", "size": "100", "limit_px": "0.14324", "leverage": 5}

    ((_, beyond), (_, inside)) = session(
        config,
        ("propose_order", {**short, "stop_loss_px": "0.15634"}),
        ("propose_order", {**short, "stop_loss_px": "0.1562"}),
    )

    assert (beyond["result"], beyond["rule"]) == ("rejected", "stop_loss_liquidation")
    assert inside["result"] == "pending_approval"


def test_a_halt_file_rejects_entries_by_halted(config: Path, venue: Venue) -> None:
    (config.parent / "data" / "HALT").write_text("x")

    ((_, result),) = session(config, ("propose_order", BTC))

    assert (result["result"], result["rule"]) == ("rejected", "halted")


def test_approval_always_waits_then_approve_places_the_order(config: Path, venue: Venue) -> None:
    out = session(config, ("propose_order", BTC), approve(config), ("positions", {}))

    (_, pending), (code, lines), (_, positions) = out
    assert pending["result"] == "pending_approval"
    assert pending["ttl_s"] == 120
    assert code == 0
    assert lines[0].startswith(f"approved {pending['id']}: buy 0.0002 BTC limit 81171")
    assert [p["coin"] for p in positions] == ["BTC"]
    assert [a["type"] for a in venue.actions] == ["updateLeverage", "order"]
    assert venue.actions[0] | {} == {
        "type": "updateLeverage",
        "asset": 3,
        "isCross": False,
        "leverage": 1,
    }
    assert trail(config, pending["id"]) == [
        "proposal",
        "decision:pending",
        "approval",
        "decision:allowed",
        "order",
        "ack",
        "child",
        "order",
        "ack",
        "decision:placed",
        "decision:placed",
        "fill",
    ]


def test_the_approval_binds_the_proposal_hash(config: Path, venue: Venue) -> None:
    (_, pending), approval = session(config, ("propose_order", BTC), approve(config))[:2]
    proposal = next(r for r in records(config) if r.kind is Kind.PROPOSAL)
    record = next(r for r in records(config) if r.kind is Kind.APPROVAL)

    assert record.payload == {
        "id": pending["id"],
        "hash": proposal.payload["hash"],
        "approved": True,
    }
    assert approval[1][2] == f"hash {proposal.payload['hash']}"


def test_an_approval_for_another_hash_is_refused_and_sends_nothing(
    config: Path, venue: Venue
) -> None:
    def forged(writer: Writer, out: list[Any]) -> None:
        writer.rt.ledger.append(
            Kind.APPROVAL, {"id": out[-1][1]["id"], "hash": "0" * 64, "approved": True}
        )
        writer.poll()

    (_, pending), _, (_, status) = session(
        config,
        ("propose_order", BTC),
        forged,
        _status_of(),
    )

    assert venue.actions == []
    assert status["status"] == "refused"
    assert "another proposal hash" in status["detail"]
    assert decide(config, pending["id"], True)[0] == 1


def test_a_replayed_approval_is_refused_and_places_nothing_twice(
    config: Path, venue: Venue
) -> None:
    def replay(writer: Writer, out: list[Any]) -> Any:
        first = next(r for r in records(config) if r.kind is Kind.APPROVAL)
        writer.rt.ledger.append(Kind.APPROVAL, dict(first.payload))
        writer.poll()
        return decide(config, first.payload["id"], True)

    _, _, (code, lines), (_, status) = session(
        config, ("propose_order", BTC), approve(config), replay, _status_of()
    )

    assert code == 1
    assert "is placed, not pending" in lines[0]
    assert [a["type"] for a in venue.actions] == ["updateLeverage", "order"]
    assert status["status"] == "placed"
    refused = [r for r in records(config) if r.payload.get("outcome") == "refused"]
    assert len(refused) == 1


@pytest.mark.parametrize("approved", ["yes", "true", 1, None])
def test_an_approval_that_is_not_true_places_nothing(
    config: Path, venue: Venue, approved: Any
) -> None:
    def answer(writer: Writer, out: list[Any]) -> None:
        pid = out[-1][1]["id"]
        digest = next(r.payload["hash"] for r in records(config) if r.kind is Kind.PROPOSAL)
        payload = {"id": pid, "hash": digest} | ({} if approved is None else {"approved": approved})
        writer.rt.ledger.append(Kind.APPROVAL, payload)
        writer.poll()

    _, _, (_, status) = session(config, ("propose_order", BTC), answer, _status_of())

    assert venue.actions == []
    assert status["status"] == "rejected"


def test_an_expired_proposal_cannot_be_approved(
    tmp_path: Path, durable: None, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configure(tmp_path, extra="approval_ttl_s: 10\n")

    def late(writer: Writer, out: list[Any]) -> Any:
        now = approval_cli._now_ms()
        monkeypatch.setattr(approval_cli, "_now_ms", lambda: now + 11_000)
        writer.rt.now_ms = lambda: now + 11_000
        code = decide(config, out[-1][1]["id"], True)
        writer.poll()
        return code

    _, (code, lines), (_, status) = session(config, ("propose_order", BTC), late, _status_of())

    assert code == 1
    assert "expired" in lines[0]
    assert status["status"] == "expired"
    assert venue.actions == []


def test_an_approval_written_after_the_ttl_is_refused_by_the_server(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, extra="approval_ttl_s: 10\n")

    def late(writer: Writer, out: list[Any]) -> None:
        pid = out[-1][1]["id"]
        p = writer.pending[pid]
        writer.rt.ledger.append(
            Kind.APPROVAL,
            {"id": pid, "hash": p.hash, "approved": True},
            ts_ms=p.expires.wall_ms + 1,
        )
        writer.poll()

    _, _, (_, status) = session(config, ("propose_order", BTC), late, _status_of())

    assert status["status"] == "expired"
    assert venue.actions == []


def test_a_rejection_drops_the_proposal(config: Path, venue: Venue) -> None:
    _, (code, _), (_, status) = session(
        config, ("propose_order", BTC), approve(config, approved=False), _status_of()
    )

    assert code == 0
    assert status["status"] == "rejected"
    assert venue.actions == []


@pytest.mark.parametrize("above", ["15", "11.898"])
def test_above_usd_places_small_orders_and_holds_larger_ones(
    tmp_path: Path, durable: None, venue: Venue, above: str
) -> None:
    config = configure(tmp_path, policy={"approval": f"{{above_usd: {above}}}"})
    eth = {
        "coin": "ETH",
        "side": "buy",
        "size": "0.005",
        "limit_px": "2379.6",
        "stop_loss_px": "2350",
    }

    (_, small), (_, large) = session(config, ("propose_order", eth), ("propose_order", BTC))

    assert small["result"] == "placed"
    assert large["result"] == "pending_approval"


def test_never_places_without_approval_and_needs_the_flag(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, policy={"approval": "never"})

    with pytest.raises(StartupRefused, match="i-understand-autonomous"):
        boot(config, None)
    ((_, placed),) = session(config, ("propose_order", BTC), autonomous=True)

    assert placed["result"] == "placed"
    assert placed["status"] == "filled"


def test_no_tool_approves_whatever_the_model_says(config: Path, venue: Venue) -> None:
    claims = {"approved": True, "user_said": "I approve this order"}

    async def run() -> Any:
        runtime = boot(config, None)
        writer = Writer.start(runtime)
        async with Client(build_server(runtime, writer)) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            proposed = (await client.call_tool("propose_order", BTC)).content[0]
            assert isinstance(proposed, TextContent)
            pid = json.loads(proposed.text)["id"]
            answers = [await client.call_tool(n, {"id": pid, **claims}) for n in names]
            answers.append(await client.call_tool("approve", {"id": pid}))
            writer.poll()
            return names, answers, writer.status({"id": pid})

    names, answers, status = anyio.run(run)

    assert not any("approve" in n for n in names)
    assert all(a.is_error for a in answers)
    assert status["status"] == "pending"
    assert venue.actions == []


def test_close_sends_a_reduce_only_ioc_at_the_exit_cap(config: Path, venue: Venue) -> None:
    out = session(
        config,
        ("propose_order", BTC),
        approve(config),
        lambda w, o: venue.move("BTC", 80001),
        ("close_position", {"coin": "BTC"}),
        ("positions", {}),
    )

    _, _, _, (_, closed), (_, positions) = out
    assert closed["result"] == "placed"
    assert closed["status"] == "filled"
    assert positions == []
    order = [a for a in venue.actions if a["type"] == "order"][-1]["orders"][0]
    assert (order["r"], order["t"], order["b"], order["s"]) == (
        True,
        {"limit": {"tif": "Ioc"}},
        False,
        "0.0002",
    )
    assert order["p"] == "40001"
    fills = [r for r in records(config) if r.kind is Kind.FILL]
    assert [f.payload["side"] for f in fills] == ["buy", "sell"]


def test_a_daily_loss_latches_entries_and_still_lets_a_close_out(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, policy={"daily_loss_pct_max": "0.05", "approval": "never"})
    eth = {
        "coin": "ETH",
        "side": "buy",
        "size": "0.005",
        "limit_px": "2379.6",
        "stop_loss_px": "2350",
    }

    out = session(
        config,
        ("propose_order", BTC),
        ("propose_order", eth),
        lambda w, o: venue.move("BTC", 75000),
        ("close_position", {"coin": "BTC", "size": "0.0001"}),
        (
            "propose_order",
            {**BTC, "coin": "SOL", "size": "0.1", "limit_px": "103.62", "stop_loss_px": "102"},
        ),
        ("close_position", {"coin": "BTC"}),
        ("close_position", {"coin": "ETH"}),
        autonomous=True,
    )

    results = [r[1]["result"] if isinstance(r, tuple) else None for r in out]
    assert results == ["placed", "placed", None, "placed", "rejected", "placed", "placed"]
    assert out[4][1]["rule"] == "daily_loss_pct_max"
    latches = [r for r in records(config) if r.kind is Kind.LATCH]
    assert len(latches) == 1
    assert latches[0].payload["rule"] == "daily_loss_pct_max"
    assert venue.positions == {}


def test_halt_cancels_opening_orders_closes_positions_and_blocks_entries(
    config: Path, venue: Venue
) -> None:
    resting = {**BTC, "limit_px": "81000", "stop_loss_px": "80000", "coin": "BTC"}
    eth = {
        "coin": "ETH",
        "side": "buy",
        "size": "0.005",
        "limit_px": "2379.6",
        "stop_loss_px": "2350",
    }
    out = session(
        config,
        rest(resting),
        ("propose_order", eth),
        approve(config),
        lambda w, o: venue.orders.append({**venue.orders[0], "oid": 99, "r": True}),
        ("halt", {}),
        ("propose_order", eth),
        ("open_orders", {}),
    )

    halted = out[4][1]
    assert halted["result"] == "halted"
    assert len(halted["cancelled"]) == 1
    assert [c["coin"] for c in halted["closes"]] == ["ETH"]
    assert [s["state"] for s in halted["stops"]["ETH"].values()] == ["cancelled"]
    assert venue.positions == {}
    assert out[5][1]["rule"] == "halted"
    assert [o["oid"] for o in out[6][1]] == [99]
    assert (config.parent / "data" / "HALT").exists()


def test_cancel_takes_an_opening_order_and_keeps_reduce_only_ones(
    config: Path, venue: Venue
) -> None:
    resting = {**BTC, "limit_px": "81000"}
    out = session(config, rest(resting))
    oid = next(r.payload["oid"] for r in records(config) if r.payload.get("outcome") == "placed")
    venue.orders.append({**venue.orders[0], "oid": 77, "r": True})

    (_, gone), (_, kept), (_, missing) = session(
        config,
        ("cancel_order", {"coin": "BTC", "oid": oid}),
        ("cancel_order", {"coin": "BTC", "oid": 77}),
        ("cancel_order", {"coin": "BTC", "oid": 5}),
    )

    assert (out[0][1]["result"], out[0][1]["status"]) == ("placed", "resting")
    assert gone == {"id": gone["id"], "oid": oid, "result": "cancelled"}
    assert (kept["result"], kept["rule"]) == ("rejected", "protects_position")
    assert (missing["result"], missing["rule"]) == ("rejected", "no_order")
    assert [o["oid"] for o in venue.orders] == [77]


def test_a_resting_order_filled_later_is_recorded_on_the_next_call(
    config: Path, venue: Venue
) -> None:
    session(config, rest({**BTC, "limit_px": "81000"}))
    venue.fill_resting(venue.orders[0]["oid"])

    session(
        config,
        (
            "propose_order",
            {**BTC, "coin": "ETH", "size": "0.005", "limit_px": "2379.6", "stop_loss_px": "2350"},
        ),
    )

    fills = [r for r in records(config) if r.kind is Kind.FILL]
    assert [(f.payload["coin"], f.payload["size"]) for f in fills] == [("BTC", "0.0002")]


def test_a_fill_the_exchange_reports_late_is_recorded_once(config: Path, venue: Venue) -> None:
    venue.fills_lag = 1
    session(config, ("propose_order", BTC), approve(config))
    assert [r for r in records(config) if r.kind is Kind.FILL] == []
    venue.fills_lag = 0

    session(config, ("risk_status", {}), ("close_position", {"coin": "BTC"}))

    fills = [r.payload["tid"] for r in records(config) if r.kind is Kind.FILL]
    assert len(fills) == len(set(fills)) == 2


def test_an_exchange_refusal_is_rejected_by_exchange(config: Path, venue: Venue) -> None:
    def refuse(body: dict[str, Any]) -> Answer | None:
        if body["action"]["type"] != "order":
            return None
        return 200, json.dumps({"status": "err", "response": "Insufficient margin"}).encode(), {}

    venue.answer_after = refuse
    _, _, (_, status) = session(config, ("propose_order", BTC), approve(config), _status_of())

    assert status["status"] == "failed"
    assert status["detail"] == "exchange: exchange rejected: Insufficient margin"
    assert status["protection"]["sl"]["state"] == "refused: exchange rejected: Insufficient margin"
    assert not (config.parent / "data" / "HALT").exists()


def test_an_ioc_entry_that_does_not_fill_sets_the_halt_file(config: Path, venue: Venue) -> None:
    """No testnet run showed the ack of a grouped Ioc entry that does not fill, so
    its child slots are an unknown shape."""
    venue.move("BTC", 90000)
    ioc = {**BTC, "limit_px": "89600", "stop_loss_px": "88500"}

    _, _, (_, status) = session(config, ("propose_order", ioc), approve(config), _status_of())

    assert status["status"] == "failed"
    assert status["detail"].startswith("network: the exchange answer has an unknown shape")
    assert (config.parent / "data" / "HALT").exists()


def test_an_unknown_outcome_sets_the_halt_file(config: Path, venue: Venue) -> None:
    venue.exchange_override = lambda body: (500, b"upstream", {})

    _, _, (_, status) = session(config, ("propose_order", BTC), approve(config), _status_of())

    assert (config.parent / "data" / "HALT").exists()
    assert status["status"] == "failed"
    assert status["detail"].startswith("network: the exchange gave no usable answer")
    assert any(r.kind is Kind.HALT for r in records(config))


def test_a_pending_proposal_expires_when_the_server_restarts(config: Path, venue: Venue) -> None:
    ((_, pending),) = session(config, ("propose_order", BTC))

    session(config)

    assert decide(config, pending["id"], True)[0] == 1
    assert trail(config, pending["id"]) == ["proposal", "decision:pending", "decision:expired"]


def test_an_approval_committed_while_the_server_restarts_is_honoured(
    config: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    ((_, pending),) = session(config, ("propose_order", BTC))
    pid = pending["id"]
    real_fold = approval_model.fold
    answers: list[tuple[int, list[str]]] = []

    def fold_then_the_user_approves(recs: Any) -> Any:
        folded = real_fold(recs)
        if not answers:
            answers.append(decide(config, pid, True))
        return folded

    monkeypatch.setattr(tools_write, "fold", fold_then_the_user_approves)

    session(config, lambda writer, out: writer.poll())

    assert answers[0][0] == 0
    steps = trail(config, pid)
    assert steps[:4] == ["proposal", "decision:pending", "approval", "decision:allowed"]
    assert "decision:placed" in steps and "decision:expired" not in steps
    assert "BTC" in venue.positions


def test_an_approval_older_than_its_ttl_at_restart_expires(
    config: Path, venue: Venue, clock: Clock
) -> None:
    (_, pending), (code, _) = session(
        config, ("propose_order", BTC), lambda writer, out: decide(config, last_id(out), True)
    )
    assert code == 0
    clock.advance(121)

    session(config, lambda writer, out: writer.poll())

    assert trail(config, pending["id"])[-1] == "decision:expired"
    assert venue.actions == []


def test_an_approval_from_the_future_at_restart_expires(
    config: Path, venue: Venue, clock: Clock
) -> None:
    clock.advance(3600)
    (_, pending), (code, _) = session(
        config, ("propose_order", BTC), lambda writer, out: decide(config, last_id(out), True)
    )
    assert code == 0
    clock.step(-3600 + 600)

    session(config, lambda writer, out: writer.poll())

    assert trail(config, pending["id"])[-1] == "decision:expired"
    assert venue.actions == []


def test_approve_after_a_clock_step_back_refuses_what_the_server_would_expire(
    config: Path, venue: Venue, clock: Clock
) -> None:
    clock.advance(3600)
    session(config, ("propose_order", {**BTC, "size": "0.002"}))
    clock.step(-3600)

    def approve_now(writer: Writer, out: list[Any]) -> tuple[int, list[str]]:
        result = decide(config, last_id(out), True)
        writer.poll()
        return result

    (_, pending), (code, lines) = session(config, ("propose_order", BTC), approve_now)

    assert code == 1 and lines[0].startswith("approve refused: "), lines
    assert "check the system clock" in lines[0]
    assert "approval" not in trail(config, pending["id"])


def test_a_restart_measures_an_approval_by_the_ttl_it_was_made_under(
    config: Path, venue: Venue, clock: Clock
) -> None:
    (_, pending), (code, _) = session(
        config, ("propose_order", BTC), lambda writer, out: decide(config, last_id(out), True)
    )
    assert code == 0
    clock.advance(300)
    config.write_text(config.read_text() + "approval_ttl_s: 900\n")

    session(config, lambda writer, out: writer.poll())

    assert trail(config, pending["id"])[-1] == "decision:expired"
    assert venue.actions == []


def test_a_starting_server_waits_out_the_approve_probe(config: Path, venue: Venue) -> None:
    lock = config.parent / "data" / "serve.lock"
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    threading.Timer(0.05, os.close, (fd,)).start()

    Writer.start(boot(config, None)).release()


def test_day_start_equity_survives_a_restart(config: Path, venue: Venue) -> None:
    session(config, ("risk_status", {}))
    venue.account_value = Decimal(400)

    ((_, risk),) = session(config, ("risk_status", {}))

    assert risk["daily_loss"]["day_start_equity_usd"] == "1000"
    starts = [r for r in records(config) if r.kind is Kind.DAY_START]
    assert len(starts) == 1


def _just_after_midnight(clock: Clock, seconds: float) -> None:
    now = time.time_ns() // 1_000_000
    clock.advance((86_400_000 - now % 86_400_000) / 1000 + seconds)


def test_latch_clear_parses_and_folds_only_a_few_lines_under_its_exclusive_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.jsonl"
    led = Ledger(path)
    for n in range(300):
        led.append(Kind.HALT, {"n": n})
    led.append(Kind.LATCH, {"rule": "daily_loss_pct_max", "detail": "x"})
    locked = work_under_exclusive_lock(monkeypatch, path)

    code, out = latch_clear(path)

    assert code == 0 and out[0].startswith("unlatched: wrote record 302, which clears record 301")
    assert len(locked) <= 6, locked


def test_the_day_start_parses_and_folds_only_a_few_lines_under_the_exclusive_lock(
    config: Path, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    session(config, ("propose_order", BTC))
    path = config.parent / "data" / "ledger.jsonl"
    for n in range(300):
        Ledger(path).append(Kind.ERROR, {"id": "seed", "detail": str(n)})
    clock.advance(86_400)
    runtime = boot(config, None)
    writer = Writer.start(runtime)
    try:
        runtime.records()
        locked = work_under_exclusive_lock(monkeypatch, path)
        runtime.first_equity_today(time.time_ns() // 1_000_000, Decimal(1000))
    finally:
        writer.release()

    assert [r.kind for r in records(config)].count(Kind.DAY_START) == 2
    assert len(locked) <= 6, locked


def test_a_small_clock_step_back_still_records_the_day_start(
    config: Path, venue: Venue, clock: Clock
) -> None:
    _just_after_midnight(clock, 5)
    session(config, ("propose_order", {**BTC, "size": "5"}))
    clock.step(-3)

    ((_, out),) = session(config, ("propose_order", BTC))

    assert out["result"] == "pending_approval"
    (day_start,) = [r for r in records(config) if r.kind is Kind.DAY_START]
    assert day_start.ts_ms == records(config)[day_start.seq - 2].ts_ms


def test_a_clock_back_across_midnight_refuses_the_entry_by_the_clock(
    config: Path, venue: Venue, clock: Clock
) -> None:
    _just_after_midnight(clock, 2)
    session(config, ("propose_order", {**BTC, "size": "5"}))
    clock.step(-5)

    ((error, out),) = session(config, ("propose_order", BTC))

    assert (error, out["error"]) == (True, "refused")
    assert "behind the ledger's newest record" in out["detail"]
    assert [r for r in records(config) if r.kind is Kind.DAY_START] == []


def test_risk_status_on_a_read_only_serve_writes_no_day_start(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = write_config(tmp_path)
    runtime = boot(config, None)
    assert runtime.agent is None

    risk = TOOLS["risk_status"].run(runtime, {})

    assert risk["daily_loss"]["day_start_equity_usd"] == Decimal(1000)
    assert risk["daily_loss"]["day_start_recorded"] is False
    assert [r for r in records(config) if r.kind is Kind.DAY_START] == []


def test_risk_status_on_a_write_serve_records_the_day_start(config: Path, venue: Venue) -> None:
    ((_, risk),) = session(config, ("risk_status", {}))

    assert risk["daily_loss"]["day_start_recorded"] is True
    (start,) = [r for r in records(config) if r.kind is Kind.DAY_START]
    assert start.payload["day"] == start.ts_ms // 86_400_000


def test_clearing_a_drawdown_latch_says_the_next_entry_latches_again(
    config: Path, venue: Venue
) -> None:
    session(config, ("risk_status", {}))
    venue.account_value = Decimal(900)
    ((_, first),) = session(config, ("propose_order", BTC))

    code, lines = latch_clear(config.parent / "data" / "ledger.jsonl")
    ((_, second),) = session(config, ("propose_order", BTC))

    assert first["rule"] == "latched"
    assert code == 0
    assert lines[-1] == (
        "note: the day start stays, so the next entry latches again while today's loss is at or "
        "past the daily loss limit, until the UTC day ends"
    )
    assert second["rule"] == "latched"


def test_one_nonce_manager_serves_every_writer_of_a_key(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    first_config = configure(tmp_path / "a")
    second_config = configure(tmp_path / "b")
    key = (tmp_path / "a" / "agent.key").read_text()
    (tmp_path / "b" / "agent.key").write_text(key)
    other = second_config.read_text().replace("ab" * 20, "cd" * 20)
    second_config.write_text(other)
    first = Writer.start(boot(first_config, None))
    second = Writer.start(boot(second_config, None))

    assert first.nonces is second.nonces
    assert first.nonces is NonceManager.for_agent(first.transport._signer.agent_address)
    first.release()
    second.release()


def test_one_writer_owns_a_data_dir(config: Path, venue: Venue) -> None:
    first = Writer.start(boot(config, None))

    with pytest.raises(ServeLockRefused, match=r"serve\.lock"):
        Writer.start(boot(config, None))
    first.release()
    Writer.start(boot(config, None)).release()


def test_two_data_dirs_on_one_account_are_refused(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    shared = tmp_path / "state"
    shared.mkdir(mode=0o700)
    configs = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        path = configure(tmp_path / name)
        text = path.read_text().replace("data/info-weight.json", f"{shared}/w.json")
        path.write_text(text)
        configs.append(path)
    first = Writer.start(boot(configs[0], None))

    with pytest.raises(ServeLockRefused, match=r"serve-testnet-0x[0-9a-f]{40}\.lock"):
        Writer.start(boot(configs[1], None))
    first.release()
    Writer.start(boot(configs[1], None)).release()


def test_two_data_dirs_with_their_own_budget_files_still_share_the_account_lock(
    tmp_path: Path, durable: None, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    configs = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        configs.append(configure(tmp_path / name))
    first = Writer.start(boot(configs[0], None))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "b"))

    with pytest.raises(ServeLockRefused, match=r"serve-testnet-0x[0-9a-f]{40}\.lock"):
        Writer.start(boot(configs[1], None))
    first.release()


@pytest.mark.parametrize("home", ["relhome", "./relhome"])
def test_a_home_that_is_not_absolute_refuses_the_account_lock(
    config: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch, home: str
) -> None:
    monkeypatch.setenv("HOME", home)
    monkeypatch.chdir(config.parent)

    with pytest.raises(ServeLockRefused, match="HOME must be an absolute path"):
        Writer.start(boot(config, None))
    assert not (config.parent / "relhome").exists()
    assert not (config.parent / "Library").exists()


def test_nonces_continue_above_the_ledger_after_a_restart_with_the_clock_back(
    tmp_path: Path, durable: None, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = configure(tmp_path, policy={"approval": "never"})
    session(config, ("propose_order", BTC), autonomous=True)
    (first,) = [r.payload["nonce"] for r in records(config) if r.kind is Kind.ORDER][-1:]
    monkeypatch.setattr(NonceManager, "_by_agent", {})
    clock.step(-5)

    ((_, closed),) = session(config, ("close_position", {"coin": "BTC"}), autonomous=True)

    assert closed["result"] == "placed"

    later = [r.payload["nonce"] for r in records(config) if r.kind is Kind.ORDER]
    assert later[-1] > first
    assert len(set(later)) == len(later)


@pytest.mark.parametrize("key", ["00" * 32, "ff" * 32], ids=["zero", "past the curve order"])
def test_an_out_of_range_agent_key_refuses_serve_in_one_line(
    config: Path, key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    (config.parent / "agent.key").write_text(key + "\n")

    assert serve(config, None) == 2

    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith("hl-guard serve refused: the agent key ")
    assert "is not a valid secp256k1 private key" in err
    assert key not in err


def test_a_symlinked_serve_lock_refuses_serve_in_one_line(
    config: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "elsewhere").write_text("")
    (config.parent / "data" / "serve.lock").symlink_to(tmp_path / "elsewhere")

    assert serve(config, None) == 2

    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert err.startswith("hl-guard serve refused: cannot open the lock file ")


def test_exchange_actions_spend_the_shared_weight_budget(config: Path, venue: Venue) -> None:
    session(config, ("propose_order", BTC), approve(config))

    budget = json.loads((config.parent / "data" / "info-weight.json").read_text())
    spent = sum(w for _, w in budget["spent"])
    meta, gate_pass, fills, children, actions = 20, 2 + 20 + 20, 21, 20, 2
    assert spent == meta + 2 * gate_pass + fills + children + actions


def test_without_an_agent_key_only_read_tools_are_listed(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    runtime = boot(write_config(tmp_path), None)

    async def run() -> list[str]:
        async with Client(build_server(runtime)) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert "propose_order" not in anyio.run(run)


def test_write_tools_are_annotated_as_writes(config: Path, venue: Venue) -> None:
    async def run() -> Any:
        runtime = boot(config, None)
        async with Client(build_server(runtime, Writer.start(runtime))) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(run)}
    for name in ("propose_order", "close_position", "cancel_order", "halt"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.read_only_hint is False
    status = tools["proposal_status"].annotations
    assert status is not None
    assert status.read_only_hint is True


@pytest.mark.parametrize(("mode", "word"), [(0o640, "mode 0640"), (0o604, "mode 0604")])
def test_an_agent_key_others_can_read_refuses_start(config: Path, mode: int, word: str) -> None:
    key = config.parent / "agent.key"
    secret = key.read_text().strip()
    key.chmod(mode)

    with pytest.raises(StartupRefused) as info:
        boot(config, None)

    assert word in str(info.value)
    assert secret not in str(info.value)


def test_a_malformed_agent_key_is_refused_without_quoting_it(config: Path) -> None:
    key = config.parent / "agent.key"
    key.write_text("0x" + "zz" * 32)

    with pytest.raises(StartupRefused, match="64 hex digits") as info:
        boot(config, None)

    assert "zz" not in str(info.value)


@pytest.mark.parametrize(
    ("px", "sz_decimals", "fits"),
    [
        ("81171", 5, True),
        ("123456", 5, True),
        ("81171.5", 5, False),
        ("2379.6", 4, True),
        ("2379.65", 4, False),
        ("0.14324", 1, True),
        ("0.143245", 1, False),
        ("1.2345", 3, False),
    ],
)
def test_the_price_grid(px: str, sz_decimals: int, fits: bool) -> None:
    assert on_price_grid(Decimal(px), sz_decimals) is fits


@pytest.mark.parametrize(
    ("px", "sz_decimals", "up", "expected"),
    [
        ("40585.5", 5, True, "40586"),
        ("162342", 5, False, "162342"),
        ("1189.81", 4, True, "1189.9"),
        ("0.071621", 1, False, "0.07162"),
    ],
)
def test_exit_prices_round_onto_the_grid_toward_the_cap(
    px: str, sz_decimals: int, up: bool, expected: str
) -> None:
    rounded = to_price_grid(Decimal(px), sz_decimals, up=up)
    assert rounded == Decimal(expected)
    assert on_price_grid(rounded, sz_decimals)


def fill_budget(config: Path, to: int) -> Callable[[Writer, list[Any]], None]:
    budget_path = config.parent / "data" / "info-weight.json"

    def full(writer: Writer, out: list[Any]) -> None:
        empty = '{"until": 0, "spent": [], "held": []}'
        budget = json.loads(budget_path.read_text() if budget_path.exists() else empty)
        used = sum(w for _, w in budget["spent"])
        budget["spent"].append([int(time.time()), to - used])
        budget_path.write_text(json.dumps(budget))

    return full


def budget_used(config: Path) -> int:
    budget = json.loads((config.parent / "data" / "info-weight.json").read_text())
    return sum(w for _, w in budget["spent"])


@pytest.mark.parametrize(
    ("spent", "closed"),
    [(600, True), (600 + RISK_HEADROOM - 2, False), (600 + RISK_HEADROOM, False)],
    ids=["at the cap", "room for the read, not the order", "burst spent"],
)
def test_a_close_uses_one_close_burst_beyond_the_cap_and_no_more(
    config: Path, venue: Venue, spent: int, closed: bool
) -> None:
    out = session(
        config,
        ("propose_order", BTC),
        approve(config),
        fill_budget(config, spent),
        ("close_position", {"coin": "BTC"}),
        ("propose_order", BTC),
    )

    assert (out[3][1].get("result") == "placed") is closed
    assert (venue.positions == {}) is closed
    assert out[4][1]["error"] == "rate_limited"


def test_a_close_without_a_position_reads_only_the_account_beyond_the_cap(
    config: Path, fake: FakeInfo, venue: Venue
) -> None:
    out = session(
        config,
        fill_budget(config, 600),
        *[("close_position", {"coin": "ETH"})] * 30,
    )

    assert {r[1]["rule"] for r in out[1:3]} == {"no_position"}
    assert "metaAndAssetCtxs" not in info_kinds(fake)
    assert budget_used(config) <= 600 + RISK_HEADROOM


def test_closes_of_a_coin_with_no_position_leave_the_burst_to_a_real_close(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, weight=300)
    session(config, ("propose_order", BTC), approve(config))
    spam = [("close_position", {"coin": "SOL"})] * (RISK_HEADROOM // 2)

    out = session(config, fill_budget(config, 300), *spam, ("close_position", {"coin": "BTC"}))

    assert {o[1].get("rule") for o in out[1:-1]} == {"no_position"}
    assert out[-1][1]["result"] == "placed"
    assert "BTC" not in venue.positions
    assert budget_used(config) <= 300 + RISK_HEADROOM


def test_a_close_reads_the_account_again_once_an_order_of_that_coin_rests(
    config: Path, venue: Venue
) -> None:
    resting = {**BTC, "limit_px": "81000"}

    def fills(writer: Writer, out: list[Any]) -> None:
        venue.fill_resting(venue.orders[0]["oid"])

    out = session(
        config,
        rest(resting),
        ("close_position", {"coin": "BTC"}),
        fills,
        ("close_position", {"coin": "BTC"}),
    )

    assert out[1][1]["rule"] == "no_position"
    assert out[3][1]["result"] == "placed"


def test_a_close_reads_the_account_again_after_an_order_went_out(
    config: Path, venue: Venue
) -> None:
    out = session(
        config,
        ("close_position", {"coin": "BTC"}),
        ("propose_order", BTC),
        approve(config),
        ("close_position", {"coin": "BTC"}),
    )

    assert out[0][1]["rule"] == "no_position"
    assert out[3][1]["result"] == "placed"


def test_a_close_after_the_clock_went_back_reads_the_account_again(
    config: Path, venue: Venue, clock: Clock
) -> None:
    def opened_elsewhere(writer: Writer, out: list[Any]) -> None:
        clock.step(-20)
        venue.positions["BTC"] = Position(Decimal("0.0002"), Decimal("81171"), 1)
        venue.leverage["BTC"] = ("isolated", 1)

    out = session(
        config,
        ("close_position", {"coin": "BTC"}),
        opened_elsewhere,
        ("close_position", {"coin": "BTC"}),
    )

    assert out[0][1]["rule"] == "no_position"
    assert out[2][1]["result"] == "placed"


def test_a_close_reads_the_account_again_while_a_sent_order_has_no_outcome(
    config: Path, venue: Venue
) -> None:
    venue.leverage["BTC"] = ("isolated", 1)
    venue.hold = True
    session(config, ("propose_order", BTC), approve(config))

    def lands(writer: Writer, out: list[Any]) -> None:
        venue.land()

    out = session(
        config,
        ("close_position", {"coin": "BTC"}),
        lands,
        ("close_position", {"coin": "BTC"}),
    )

    assert out[0][1]["rule"] == "no_position"
    assert out[2][1]["result"] == "placed", out[2]
    assert "BTC" not in venue.positions


def test_halt_cancels_and_closes_within_one_burst_beyond_the_cap(
    config: Path, venue: Venue
) -> None:
    venue.positions["ETH"] = Position(Decimal("0.005"), Decimal("2379.6"), 1)
    venue.leverage["ETH"] = ("isolated", 1)
    resting = {**BTC, "limit_px": "81000"}

    out = session(config, rest(resting), fill_budget(config, 600), ("halt", {}))

    halted = out[-1][1]
    assert halted["unfinished"] == []
    assert len(halted["cancelled"]) == 1
    assert [c["coin"] for c in halted["closes"]] == ["ETH"]
    assert venue.orders == [] and venue.positions == {}
    assert budget_used(config) <= 600 + RISK_HEADROOM


def test_an_entry_gets_no_weight_beyond_the_cap(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, policy={"approval": "never"})
    venue.leverage["BTC"] = ("isolated", 1)
    gate_reads = 20 + 2 + 20 + 20

    out = session(
        config, fill_budget(config, 600 - gate_reads), ("propose_order", BTC), autonomous=True
    )

    assert out[-1][1]["error"] == "rate_limited"
    assert venue.actions == []


def test_cancel_order_gets_no_weight_beyond_the_cap(config: Path, venue: Venue) -> None:
    resting = {**BTC, "limit_px": "81000"}

    def cancel(writer: Writer, out: list[Any]) -> str:
        try:
            writer.cancel({"coin": "BTC", "oid": venue.orders[0]["oid"]})
        except InfoRateLimited:
            return "rate_limited"
        return "sent"

    out = session(config, rest(resting), fill_budget(config, 600 - 20), cancel)

    assert out[-1] == "rate_limited"
    assert len(venue.orders) == 1
    assert "cancel" not in [a["type"] for a in venue.actions]


def test_an_entry_on_a_held_coin_must_use_its_leverage(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, policy={"leverage_max": "3"})
    venue.leverage["BTC"] = ("isolated", 3)
    venue.positions["BTC"] = Position(Decimal("0.0001"), Decimal(81171), 3)

    (_, other), (_, same) = session(
        config, ("propose_order", BTC), ("propose_order", {**BTC, "leverage": 3})
    )

    assert (other["rule"], other["detail"]) == (
        "leverage_mode",
        "the open BTC position is at 3x; propose at that",
    )
    assert same["result"] == "pending_approval"


ETH = {"coin": "ETH", "side": "buy", "size": "0.005", "limit_px": "2379.6", "stop_loss_px": "2350"}
SOL = {"coin": "SOL", "side": "buy", "size": "0.1", "limit_px": "103.62", "stop_loss_px": "102"}


def test_a_part_filled_order_records_each_fill_once(config: Path, venue: Venue) -> None:
    session(config, rest({**BTC, "limit_px": "81000"}))
    oid = venue.orders[0]["oid"]
    venue.fill_resting(oid, "0.0001")

    session(config, ("propose_order", ETH), ("propose_order", SOL))

    fills = [r.payload for r in records(config) if r.kind is Kind.FILL]
    assert [(f["oid"], f["size"]) for f in fills] == [(oid, "0.0001")]


def test_a_filled_order_whose_fill_lags_past_an_order_read_still_records_it(
    config: Path, venue: Venue
) -> None:
    venue.fills_lag = 1

    session(
        config,
        ("propose_order", BTC),
        approve(config),
        ("propose_order", ETH),
        lambda w, o: setattr(venue, "fills_lag", 0),
        ("propose_order", SOL),
    )

    fills = [r.payload["coin"] for r in records(config) if r.kind is Kind.FILL]
    assert fills == ["BTC"]


def test_an_unfunded_account_rejects_entries(config: Path, venue: Venue) -> None:
    venue.account_value = Decimal(0)

    ((_, result),) = session(config, ("propose_order", BTC))

    assert (result["rule"], result["detail"]) == ("no_equity", "the account value is 0")


def test_a_close_larger_than_the_position_is_rejected(config: Path, venue: Venue) -> None:
    out = session(
        config,
        ("propose_order", BTC),
        approve(config),
        ("close_position", {"coin": "BTC", "size": "0.0003"}),
    )

    assert (out[2][1]["result"], out[2][1]["rule"]) == ("rejected", "reduce_only")
    assert [a["type"] for a in venue.actions] == ["updateLeverage", "order"]


def test_a_halt_between_proposal_and_approval_rejects_the_order(config: Path, venue: Venue) -> None:
    halt = config.parent / "data" / "HALT"
    out = session(
        config,
        ("propose_order", BTC),
        lambda w, o: halt.write_text("user halt"),
        approve(config),
        _status_of(),
    )

    assert out[3][1]["status"] == "rejected"
    assert out[3][1]["detail"] == "halted: the HALT file is in the data dir"
    assert venue.actions == []


def test_an_exchange_429_backs_the_shared_budget_off(config: Path, venue: Venue) -> None:
    venue.exchange_override = lambda body: (429, b"", {})

    out = session(config, ("propose_order", BTC), approve(config), _status_of())

    budget = json.loads((config.parent / "data" / "info-weight.json").read_text())
    assert budget["until"] > time.time() + 50
    assert out[2][1]["status"] == "failed"


def test_an_order_whose_answer_was_lost_is_found_by_cloid_and_its_fill_recorded(
    config: Path, venue: Venue
) -> None:
    def lost(body: dict[str, Any]) -> Answer | None:
        return (500, b"upstream", {}) if body["action"]["type"] == "order" else None

    venue.answer_after = lost
    halt = config.parent / "data" / "HALT"

    def recover(writer: Writer, out: list[Any]) -> None:
        venue.answer_after = None
        halt.unlink()

    out = session(
        config,
        ("propose_order", BTC),
        approve(config),
        recover,
        ("propose_order", ETH),
        ("close_position", {"coin": "BTC"}),
    )

    assert out[4][1]["result"] == "placed", out
    fills = [r.payload for r in records(config) if r.kind is Kind.FILL]
    assert [(f["coin"], f["side"]) for f in fills] == [("BTC", "buy"), ("BTC", "sell")]
    placed = [r.payload for r in records(config) if r.payload.get("outcome") == "placed"]
    assert placed[0]["cloid"] == venue.actions[1]["orders"][0]["c"]


def test_a_restart_with_an_order_that_has_no_answer_sets_the_halt_file(
    config: Path, venue: Venue
) -> None:
    led = Ledger(config.parent / "data" / "ledger.jsonl")
    wire = {"a": 3, "b": True, "p": "81171", "s": "0.0002", "r": False}
    order = {**wire, "t": {"limit": {"tif": "Gtc"}}, "c": "0x" + "1" * 32}
    action = {"type": "order", "orders": [order], "grouping": "na"}
    led.append(Kind.ORDER, {"id": "a1b2c3d4e5f6", "nonce": 1, "action": action})

    session(config)

    assert (config.parent / "data" / "HALT").exists()
    halts = [r.payload for r in records(config) if r.kind is Kind.HALT]
    assert halts == [
        {"id": "a1b2c3d4e5f6", "reason": "1 orders from before the restart have no outcome"}
    ]


def test_above_usd_counts_the_exposure_so_split_orders_still_wait(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, policy={"approval": "{above_usd: 15}"})

    (_, first), (_, second) = session(config, ("propose_order", ETH), ("propose_order", SOL))

    assert first["result"] == "placed"
    assert second["result"] == "pending_approval"


def test_halt_closes_the_listed_positions_when_one_coin_is_unlisted(
    config: Path, venue: Venue
) -> None:
    def unlisted(writer: Writer, out: list[Any]) -> None:
        venue.marks["ZZZ"] = Decimal(5)
        venue.positions["ZZZ"] = Position(Decimal(3), Decimal(5), 1)

    out = session(config, ("propose_order", ETH), approve(config), unlisted, ("halt", {}))

    halted = out[3][1]
    assert halted["unfinished"] == [], halted
    assert {c["coin"]: ("oid" in c) for c in halted["closes"]} == {"ETH": True, "ZZZ": False}
    assert (
        "ZZZ is not a listed perp"
        in next(c for c in halted["closes"] if c["coin"] == "ZZZ")["refused"]
    )
    assert set(venue.positions) == {"ZZZ"}


def test_halt_still_closes_when_the_order_read_fails(
    config: Path, fake: FakeInfo, venue: Venue
) -> None:
    def broken(writer: Writer, out: list[Any]) -> None:
        fake.answers["frontendOpenOrders"] = lambda body: (500, b"", {})

    out = session(config, ("propose_order", ETH), approve(config), broken, ("halt", {}))

    halted = out[3][1]
    cancel, stops = halted["unfinished"]
    assert cancel == "cancel: network: /info frontendOpenOrders: HTTP 500"
    assert stops.startswith("stops ETH: stops not confirmed cancelled: ")
    assert [c["coin"] for c in halted["closes"]] == ["ETH"]
    assert venue.positions == {}
    assert any(r.kind is Kind.ERROR for r in records(config))


def test_an_approval_in_time_places_even_when_the_poll_runs_late(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, extra="approval_ttl_s: 10\n")

    def late_poll(writer: Writer, out: list[Any]) -> Any:
        pid = last_id(out)
        code = decide(config, pid, True)
        approved_at = next(r.ts_ms for r in records(config) if r.kind is Kind.APPROVAL)
        p = writer.pending[pid]
        writer.pending[pid] = dataclasses.replace(
            p, expires=dataclasses.replace(p.expires, wall_ms=approved_at)
        )
        time.sleep(0.01)
        writer.poll()
        return code

    out = session(config, ("propose_order", BTC), late_poll, _status_of())

    assert out[1][0] == 0
    assert out[2][1]["status"] == "placed", out[2]


def test_a_resting_order_that_filled_while_its_fill_lags_is_asked_about_and_recorded(
    config: Path, venue: Venue
) -> None:
    session(config, rest({**BTC, "limit_px": "81000"}))
    oid = venue.orders[0]["oid"]
    venue.fill_resting(oid)
    venue.fills_lag = 1

    session(
        config,
        ("propose_order", ETH),
        lambda w, o: setattr(venue, "fills_lag", 0),
        ("propose_order", SOL),
    )

    fills = [(r.payload["oid"], r.payload["size"]) for r in records(config) if r.kind is Kind.FILL]
    assert fills == [(oid, "0.0002")]


def test_a_cancelled_order_with_a_part_fill_keeps_the_part(config: Path, venue: Venue) -> None:
    session(config, rest({**BTC, "limit_px": "81000"}))
    oid = venue.orders[0]["oid"]
    venue.fill_resting(oid, "0.0001")

    session(config, ("cancel_order", {"coin": "BTC", "oid": oid}), ("propose_order", ETH))

    fills = [(r.payload["oid"], r.payload["size"]) for r in records(config) if r.kind is Kind.FILL]
    assert fills == [(oid, "0.0001")]


def test_an_equity_drop_the_ledger_never_saw_latches_entries(config: Path, venue: Venue) -> None:
    session(config, ("risk_status", {}))
    venue.account_value = Decimal(969)

    (_, first), (_, second) = session(config, ("propose_order", BTC), ("propose_order", ETH))

    assert (first["rule"], second["rule"]) == ("latched", "latched")
    (latch,) = [r.payload for r in records(config) if r.kind is Kind.LATCH]
    assert latch["rule"] == "daily_loss_pct_max"
    assert latch["detail"].startswith("the account value 969 USD is 3.1000% below the day start")
    assert venue.actions == []


def test_an_equity_dip_inside_the_limit_does_not_latch(config: Path, venue: Venue) -> None:
    session(config, ("risk_status", {}))
    venue.account_value = Decimal(971)

    ((_, result),) = session(config, ("propose_order", BTC))

    assert result["result"] == "pending_approval"
    assert [r for r in records(config) if r.kind is Kind.LATCH] == []


def test_an_approved_order_waits_for_budget_room_then_goes_out(config: Path, venue: Venue) -> None:
    budget_path = config.parent / "data" / "info-weight.json"
    saved: list[str] = []

    def full_then_approve(writer: Writer, out: list[Any]) -> Any:
        saved.append(budget_path.read_text())
        budget = json.loads(saved[0])
        budget["spent"].append([int(time.time()), 600])
        budget_path.write_text(json.dumps(budget))
        code, _ = decide(config, last_id(out), True)
        writer.poll()
        return code, sorted(writer.approved)

    def room(writer: Writer, out: list[Any]) -> None:
        budget_path.write_text(saved[0])
        for pid, waiting in list(writer.approved.items()):
            writer.approved[pid] = dataclasses.replace(waiting, retry_at_elapsed_ms=0)
        writer.poll()

    out = session(config, ("propose_order", BTC), full_then_approve, room, _status_of())

    pid = out[0][1]["id"]
    assert out[1] == (0, [pid])
    assert out[3][1]["status"] == "placed"


def test_an_adopted_approval_stops_waiting_for_budget_at_the_ttl_it_was_made_under(
    config: Path, venue: Venue, clock: Clock
) -> None:
    (_, pending), (code, _) = session(
        config, ("propose_order", BTC), lambda writer, out: decide(config, last_id(out), True)
    )
    assert code == 0
    clock.advance(60)
    config.write_text(config.read_text() + "approval_ttl_s: 900\n")

    def later(writer: Writer, out: list[Any]) -> None:
        clock.advance(70)
        writer.approved = {
            pid: dataclasses.replace(w, retry_at_elapsed_ms=0) for pid, w in writer.approved.items()
        }
        writer.poll()

    session(config, fill_budget(config, 10_000), lambda w, out: w.poll(), later)

    assert trail(config, pending["id"])[-1] == "decision:expired"
    assert venue.actions == []


def _retry_now(writer: Writer) -> None:
    writer.approved = {
        pid: dataclasses.replace(w, retry_at_elapsed_ms=0) for pid, w in writer.approved.items()
    }
    writer.poll()


@pytest.mark.parametrize(
    "case,outcome",
    [
        ("steady clock", "decision:failed"),
        ("clock stepped back while it waits", "decision:failed"),
        ("clock stepped back before the poll", "decision:failed"),
        ("suspended", "decision:expired"),
        ("system sleep", "decision:expired"),
    ],
)
def test_an_approval_waiting_for_budget_never_goes_out_after_its_ttl(
    config: Path, venue: Venue, clock: Clock, case: str, outcome: str
) -> None:
    budget_path = config.parent / "data" / "info-weight.json"

    def first_poll(writer: Writer, out: list[Any]) -> None:
        if case == "clock stepped back before the poll":
            clock.step(-3600)
        writer.poll()

    def wait(writer: Writer, out: list[Any]) -> None:
        assert writer.approved, "the approval waits for budget"
        if case == "clock stepped back while it waits":
            clock.step(-3600)
        for _ in range(50):
            (clock.sleep if case == "system sleep" else clock.advance)(39)
            if case not in ("suspended", "system sleep"):
                fill_budget(config, 10_000)(writer, out)
                _retry_now(writer)
        budget_path.write_text('{"until": 0, "spent": [], "held": []}')
        _retry_now(writer)

    (_, pending), *_ = session(
        config,
        ("propose_order", BTC),
        fill_budget(config, 10_000),
        lambda writer, out: decide(config, last_id(out), True),
        first_poll,
        wait,
    )

    assert trail(config, pending["id"])[-1] == outcome
    assert [a for a in venue.actions if a["type"] == "order"] == []


def test_a_pending_proposal_expires_by_the_elapsed_clock_after_a_wall_step_back(
    config: Path, venue: Venue, clock: Clock
) -> None:
    def stepped_back_then_two_ttls(writer: Writer, out: list[Any]) -> Any:
        clock.step(-3600)
        clock.advance(240)
        writer.poll()
        result = decide(config, last_id(out), True)
        writer.poll()
        return result

    (_, pending), (code, lines) = session(
        config, ("propose_order", BTC), stepped_back_then_two_ttls
    )

    assert (code, lines) == (
        1,
        [f"approve refused: proposal {pending['id']} is expired, not pending"],
    )
    assert trail(config, pending["id"])[-1] == "decision:expired"
    _no_exchange_action(venue)


def test_an_approval_the_poll_saw_late_keeps_the_deadline_of_its_record(
    config: Path, venue: Venue, clock: Clock
) -> None:
    budget_path = config.parent / "data" / "info-weight.json"

    def late_poll(writer: Writer, out: list[Any]) -> None:
        clock.advance(100)
        writer.poll()
        assert writer.approved, "the approval waits for budget"
        clock.step(-3600)
        clock.advance(30)
        budget_path.write_text('{"until": 0, "spent": [], "held": []}')
        _retry_now(writer)

    (_, pending), *_ = session(
        config,
        ("propose_order", BTC),
        fill_budget(config, 10_000),
        lambda writer, out: decide(config, last_id(out), True),
        late_poll,
    )

    assert trail(config, pending["id"])[-1] == "decision:expired"
    assert [a for a in venue.actions if a["type"] == "order"] == []


def _no_exchange_action(venue: Venue) -> None:
    assert venue.actions == [], venue.actions


def test_an_approval_waiting_for_budget_counts_system_sleep_past_a_wall_step_back(
    config: Path, venue: Venue, clock: Clock
) -> None:
    budget_path = config.parent / "data" / "info-weight.json"

    def slept_then_stepped_back(writer: Writer, out: list[Any]) -> None:
        writer.poll()
        assert writer.approved, "the approval waits for budget"
        clock.sleep(102)
        clock.step(-102)
        for _ in range(6):
            clock.advance(5)
            fill_budget(config, 10_000)(writer, out)
            _retry_now(writer)
        budget_path.write_text('{"until": 0, "spent": [], "held": []}')
        _retry_now(writer)

    (_, pending), *_ = session(
        config,
        ("propose_order", BTC),
        fill_budget(config, 10_000),
        lambda writer, out: decide(config, last_id(out), True),
        slept_then_stepped_back,
    )

    assert trail(config, pending["id"])[-1] == "decision:failed"
    _no_exchange_action(venue)


def test_an_approval_waiting_for_budget_retries_on_the_elapsed_clock(
    config: Path, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    evaluated: list[str] = []
    real = Writer._evaluate

    def counted(self: Writer, pid: str, order: Any) -> Any:
        evaluated.append(pid)
        return real(self, pid, order)

    def waiting(writer: Writer, out: list[Any]) -> list[int]:
        writer.poll()
        assert writer.approved, "the approval waits for budget"
        monkeypatch.setattr(Writer, "_evaluate", counted)
        counts = []
        for advance in (0, 0, tools_write.RETRY_MS / 1000):
            clock.advance(advance)
            writer.poll()
            counts.append(len(evaluated))
        return counts

    out = session(
        config,
        ("propose_order", BTC),
        fill_budget(config, 10_000),
        lambda writer, out: decide(config, last_id(out), True),
        waiting,
    )

    assert out[-1] == [0, 0, 1]


def test_an_approval_whose_ttl_ran_out_after_the_gate_passed_is_not_sent(
    config: Path, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = Writer._place

    def suspended_after_the_gate(self: Writer, *args: Any, **kwargs: Any) -> Any:
        clock.advance(3600)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Writer, "_place", suspended_after_the_gate)

    (_, pending), _ = session(config, ("propose_order", BTC), approve(config))

    assert trail(config, pending["id"])[-2:] == ["decision:allowed", "decision:expired"]
    _no_exchange_action(venue)


def test_an_approval_whose_gate_reads_outlast_its_ttl_is_not_sent(
    config: Path, venue: Venue, fake: FakeInfo, clock: Clock
) -> None:
    inner, slow = fake.answers["clearinghouseState"], [False]

    def thirty_seconds_once_slow(body: dict[str, Any]) -> Any:
        if slow[0]:
            clock.advance(30)
        return inner(body)

    fake.answers["clearinghouseState"] = thirty_seconds_once_slow

    def polled_with_20_s_left(writer: Writer, out: list[Any]) -> None:
        assert decide(config, last_id(out), True)[0] == 0
        clock.advance(100)
        slow[0] = True
        writer.poll()

    (_, pending), _ = session(config, ("propose_order", BTC), polled_with_20_s_left)

    assert trail(config, pending["id"])[-1] == "decision:expired"
    _no_exchange_action(venue)


def test_an_approved_order_is_signed_to_expire_with_the_approval(
    config: Path, venue: Venue, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = ExchangeTransport.send
    signed: list[int] = []

    def twenty_seconds_in_flight(
        self: Any, action: Any, nonce: int, expires_after: int | None = None
    ) -> Any:
        assert expires_after is not None
        signed.append(expires_after)
        if action["type"] == "order":
            clock.advance(20)
        return real(self, action, nonce, expires_after)

    def polled_with_10_s_left(writer: Writer, out: list[Any]) -> None:
        assert decide(config, last_id(out), True)[0] == 0
        clock.advance(110)
        monkeypatch.setattr(ExchangeTransport, "send", twenty_seconds_in_flight)
        writer.poll()

    (_, pending), _ = session(config, ("propose_order", BTC), polled_with_10_s_left)

    rows = [r for r in records(config) if r.payload.get("id") == pending["id"]]
    approved_at = next(r.ts_ms for r in rows if r.kind is Kind.APPROVAL)
    recorded = [r.payload["expires_after"] for r in rows if r.kind is Kind.ORDER]
    assert recorded == signed != []
    assert max(signed) <= approved_at + 120_000
    assert [a["type"] for a in venue.actions] == ["updateLeverage", "order"]
    assert venue.orders == []
    assert trail(config, pending["id"])[-1] == "decision:failed"


def test_at_most_five_proposals_wait_for_the_user(config: Path, venue: Venue) -> None:
    out = session(config, *[("propose_order", BTC)] * 6)

    assert [r[1]["result"] for r in out] == ["pending_approval"] * 5 + ["rejected"]
    assert out[5][1]["rule"] == "pending_max"


def test_a_budget_file_failure_after_the_send_keeps_the_answer(
    config: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hl_guard.server import info

    real = info.WeightBudget.settle

    def broken(self: info.WeightBudget, token: str, weight: int) -> None:
        if weight == 1:
            raise info.InfoRateLimited("the budget file is gone")
        real(self, token, weight)

    monkeypatch.setattr(info.WeightBudget, "settle", broken)

    out = session(config, ("propose_order", BTC), approve(config), _status_of())

    assert out[2][1]["status"] == "placed"
    errors = [r.payload["detail"] for r in records(config) if r.kind is Kind.ERROR]
    assert errors == ["weight budget: the budget file is gone"] * 2
    assert [r for r in records(config) if r.kind is Kind.FILL] != []


def test_an_approval_is_not_retried_once_an_action_went_out(
    config: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hl_guard.server import info

    real = Writer._send
    sent: list[str] = []

    def second_refused(self: Writer, pid: str, action: Any, *rest: Any) -> Any:
        sent.append(action["type"])
        if action["type"] == "order":
            raise info.InfoRateLimited("no room for the order")
        return real(self, pid, action, *rest)

    monkeypatch.setattr(Writer, "_send", second_refused)

    def approve_and_check(writer: Writer, out: list[Any]) -> Any:
        decide(config, last_id(out), True)
        writer.poll()
        return sorted(writer.approved)

    out = session(config, ("propose_order", BTC), approve_and_check, _status_of())

    assert out[1] == []
    assert out[2][1]["status"] == "failed"
    assert sent == ["updateLeverage", "order"]


def test_a_failing_poll_is_reported_once_and_tried_again(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from hl_guard.ledger.ledger import LedgerError
    from hl_guard.server import app

    calls: list[int] = []

    class Broken:
        def poll(self) -> None:
            calls.append(1)
            if len(calls) <= 3:
                raise LedgerError("broken")

    monkeypatch.setattr(app, "POLL_S", 0.0)
    monkeypatch.setattr(app, "POLL_FAILED_S", 0.0)

    async def run() -> None:
        with anyio.move_on_after(0.3):
            await app._poll(Broken(), anyio.CapacityLimiter(1))

    anyio.run(run)

    assert len(calls) > 4
    err = capsys.readouterr().err
    assert err.count("the approval poll failed: LedgerError: broken") == 1


def test_a_fill_seen_twice_across_lagging_reads_is_recorded_once(
    config: Path, venue: Venue
) -> None:
    session(config, rest({**BTC, "limit_px": "81000"}))
    oid = venue.orders[0]["oid"]
    venue.fill_resting(oid, "0.0001")
    session(config, ("propose_order", ETH))
    venue.fill_resting(oid, "0.0001")
    venue.fills_lag = 1

    session(
        config,
        ("propose_order", ETH),
        lambda w, o: setattr(venue, "fills_lag", 0),
        ("propose_order", SOL),
    )

    tids = [r.payload["tid"] for r in records(config) if r.kind is Kind.FILL]
    assert len(tids) == len(set(tids)) == 2


def test_an_order_the_signer_refused_needs_no_answer_after_a_restart(
    config: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hl_guard.exchange.signer import Signer, SignerError

    def refuse(self: Signer, action: Any, nonce: int, expires_after: Any = None) -> Any:
        raise SignerError("refused for the test")

    venue.leverage["BTC"] = ("isolated", 1)
    with monkeypatch.context() as patched:
        patched.setattr(Signer, "sign_action", refuse)
        session(config, ("propose_order", BTC), approve(config))
    assert [a["type"] for a in actions_sent(config)] == ["order"]

    session(config)

    assert not (config.parent / "data" / "HALT").exists()


PROTECTION = (
    "stop-loss and optional take-profit placed on the exchange with the Ioc entry as "
    "reduce-only market triggers, sized to the filled lot"
)


def test_a_placed_entry_shows_its_live_stop_and_risk_status_says_how_stops_go_out(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    config = configure(tmp_path, policy={"approval": "never"})

    (_, placed), (_, risk) = session(
        config, ("propose_order", BTC), ("risk_status", {}), autonomous=True
    )

    (row,) = venue.orders
    assert placed["result"] == "placed"
    assert placed["protection"] == {
        "sl": {"cloid": row["c"], "trigger_px": "80000", "size": "0.0002", "state": "live"}
    }
    assert placed["warning"] is None
    assert risk["protection"] == PROTECTION


@pytest.mark.parametrize(
    ("approval", "word"), [("never", "never (autonomous)"), ("{above_usd: 15}", "above 15 USD")]
)
def test_serve_warns_that_entries_go_out_without_exchange_stops(
    tmp_path: Path, durable: None, venue: Venue, approval: str, word: str
) -> None:
    config = configure(tmp_path, policy={"approval": approval})
    runtime = boot(config, None, autonomous=True)

    lines = startup_lines(runtime, True)

    assert lines[1] == (
        f"hl-guard serve: warning: with approval {word}, entries go out without "
        "exchange-side stops; the stop-loss is checked by the gate, not placed"
    )


def test_serve_with_approval_always_prints_no_warning(config: Path) -> None:
    assert len(startup_lines(boot(config, None), True)) == 1


def test_an_approval_written_in_time_during_the_expiry_pass_is_honoured(
    tmp_path: Path, durable: None, venue: Venue
) -> None:
    cfg = configure(tmp_path, extra="approval_ttl_s: 10\n")

    def race(writer: Writer, out: list[Any]) -> Any:
        pid = out[-1][1]["id"]
        p = writer.pending[pid]
        writer.poll()
        real_now = writer.rt.now_ms
        calls = [0]

        def now() -> int:
            calls[0] += 1
            if calls[0] == 1:
                writer.rt.ledger.append(
                    Kind.APPROVAL,
                    {"id": pid, "hash": p.hash, "approved": True},
                    ts_ms=p.expires.wall_ms,
                )
                return p.expires.wall_ms + 1
            return real_now()

        writer.rt.now_ms = now
        try:
            writer.poll()
        finally:
            writer.rt.now_ms = real_now
        return writer.status({"id": pid})

    (_, pending), status = session(cfg, ("propose_order", BTC), race)

    assert status["status"] == "placed"
    assert "decision:expired" not in trail(cfg, pending["id"])
    assert [a["type"] for a in venue.actions] == ["updateLeverage", "order"]


def test_an_unknown_error_after_an_approval_records_a_failed_decision(
    config: Path, venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: Writer, pid: str, order: Any) -> Any:
        raise KeyError("surprise")

    def run(writer: Writer, out: list[Any]) -> Any:
        pid = out[-1][1]["id"]
        decide(config, pid, True)
        monkeypatch.setattr(Writer, "_evaluate", boom)
        with pytest.raises(KeyError):
            writer.poll()
        return writer.status({"id": pid})

    _, status = session(config, ("propose_order", BTC), run)

    assert (status["status"], status["detail"]) == ("failed", "internal: KeyError: 'surprise'")
    assert venue.actions == []
