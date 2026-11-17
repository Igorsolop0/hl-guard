"""The guarded write tools: propose_order, close_position, cancel_order, halt.

Every call writes a ``proposal`` record and at least one ``decision`` record.
An entry that needs approval goes through the risk gate twice: when it is
proposed, and again on a fresh read when the approval arrives. No tool
approves. The server learns of an approval only from an ``approval`` record
that ``hl-guard approve`` appends to the ledger, and then places the order.
"""

from __future__ import annotations

import dataclasses
import fcntl
import os
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeVar

from hl_guard.approval.model import (
    PROTECTION,
    TIFS,
    Order,
    Proposal,
    Status,
    fold,
    offline_problem,
    order_from_json,
    proposal_hash,
    wire,
)
from hl_guard.exchange.actions import Action, build_action
from hl_guard.exchange.nonces import NonceManager, NonceWindowError
from hl_guard.exchange.signer import Signer, SignerError
from hl_guard.exchange.transport import ExchangeTransport, TransportError
from hl_guard.ledger.ledger import Kind, Record
from hl_guard.policy import gate
from hl_guard.policy.ceilings import MAX_LEVERAGE
from hl_guard.policy.model import (
    Account,
    ApproveAboveUsd,
    ApproveNever,
    Close,
    Entry,
    Policy,
    Rule,
    SetLeverage,
)
from hl_guard.policy.model import Rejected as GateRejected
from hl_guard.server.config import SERVE_LOCK, lock_dir
from hl_guard.server.fills import FILLS_PAGE_ROWS, FillBook, FillsUnread
from hl_guard.server.info import COSTS, InfoRateLimited
from hl_guard.server.parse import (
    AccountState,
    Filled,
    Market,
    OrderAck,
    OrderRow,
    Refused,
    Resting,
    ShapeError,
    exchange_text,
    parse_active_asset,
    parse_cancel_acks,
    parse_clearinghouse_state,
    parse_default_ack,
    parse_open_orders,
    parse_order_acks,
)
from hl_guard.server.snapshot import (
    GATE_CONTEXT,
    Exchange,
    LedgerView,
    Mark,
    SnapshotError,
    build_account,
    coins_needing_leverage,
    perp_orders,
    position_marks,
)
from hl_guard.server.tools_read import (
    LISTING_TTL_MS,
    ChoiceArg,
    CoinArg,
    DecimalArg,
    Failure,
    IntArg,
    PatternArg,
    Runtime,
    Tool,
    ToolFailure,
    as_failure,
)

T = TypeVar("T")

EXCHANGE_WEIGHT = 1
PRICE_SIG_FIGS = 5
PERP_MAX_DECIMALS = 6
BATCH_MAX = 20
RETRY_MS = 5_000
RISK_HEADROOM = (
    COSTS["metaAndAssetCtxs"].base
    + COSTS["frontendOpenOrders"].base
    + COSTS["clearinghouseState"].base
    + 2 * EXCHANGE_WEIGHT
)
ENTRY_FIXED_WEIGHT = (
    COSTS["metaAndAssetCtxs"].base
    + COSTS["clearinghouseState"].base
    + COSTS["frontendOpenOrders"].base
    + COSTS["orderStatus"].base
)
FILLS_PAGE_WEIGHT = COSTS["userFillsByTime"].of(FILLS_PAGE_ROWS)
FILLS_SHORT_PAGE_WEIGHT = COSTS["userFillsByTime"].of(COSTS["userFillsByTime"].rows_per_weight)
EXPIRES_AFTER_MS = 30_000
PENDING_MAX = 5
NO_POSITION_REUSE_MS = 10_000
LOCK_TRIES = 10
LOCK_RETRY_S = 0.02
PROPOSAL_ID = re.compile(r"[0-9a-f]{12}")


class Check(StrEnum):
    HALTED = "halted"
    NO_EQUITY = "no_equity"
    DELISTED = "delisted"
    SIZE_DECIMALS = "size_decimals"
    PRICE_TICK = "price_tick"
    NO_POSITION = "no_position"
    NO_ORDER = "no_order"
    PROTECTS_POSITION = "protects_position"
    EXCHANGE = "exchange"
    USER_REJECTED = "user_rejected"
    APPROVAL = "approval"
    EXPIRED = "expired"
    PENDING_MAX = "pending_max"
    FILLS_UNREAD = "fills_unread"
    WIDE_BOOK = "wide_book"
    INTERNAL = "internal"


@dataclass(frozen=True)
class Rejected:
    id: str
    rule: str
    detail: str
    result: Literal["rejected"] = "rejected"


@dataclass(frozen=True)
class PendingApproval:
    id: str
    ttl_s: int
    expires_ms: int
    order: Mapping[str, Any]
    approve_with: str
    result: Literal["pending_approval"] = "pending_approval"


@dataclass(frozen=True)
class Placed:
    id: str
    oid: int
    status: Literal["resting", "filled"]
    filled_size: Decimal | None
    avg_px: Decimal | None
    protection: str | None = None
    result: Literal["placed"] = "placed"


@dataclass(frozen=True)
class Cancelled:
    id: str
    oid: int
    result: Literal["cancelled"] = "cancelled"


Outcome = Rejected | PendingApproval | Placed


_ELAPSED_CLOCK = getattr(time, "CLOCK_BOOTTIME", time.CLOCK_MONOTONIC)


def _elapsed_ms() -> int:
    """Counts system sleep: CLOCK_MONOTONIC does on macOS, where
    time.monotonic stops in it, and CLOCK_BOOTTIME does on Linux."""
    return time.clock_gettime_ns(_ELAPSED_CLOCK) // 1_000_000


@dataclass(frozen=True)
class _Deadline:
    wall_ms: int
    elapsed_ms: int

    def left_ms(self, now_ms: int) -> int:
        return min(self.wall_ms - now_ms, self.elapsed_ms - _elapsed_ms())


@dataclass(frozen=True)
class _Pending:
    id: str
    order: Order
    hash: str
    expires: _Deadline
    ttl_s: int


@dataclass(frozen=True)
class _Approved:
    pending: _Pending
    retry_at_elapsed_ms: int
    deadline: _Deadline


@dataclass(frozen=True)
class _Plan:
    market: Market
    set_leverage: bool
    exposure_after: Decimal


class _TtlRanOut(Exception):
    pass


class _ExchangeRefused(Exception):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class ServeLockRefused(Exception):
    pass


def _lock(path: Path) -> int:
    """``hl-guard approve`` probes the data-dir lock with a shared lock for an
    instant, so a start that meets it tries again before it refuses."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise ServeLockRefused(f"cannot open the lock file {path}: {exc.strerror}") from None
    for _ in range(LOCK_TRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            time.sleep(LOCK_RETRY_S)
        else:
            return fd
    os.close(fd)
    raise ServeLockRefused(f"another hl-guard serve with write tools holds {path}; stop it first")


def account_lock_path(rt: Runtime) -> Path:
    """One lock per network and account in the user's lock dir, so two data
    dirs cannot trade one account."""
    return lock_dir() / f"serve-{rt.network}-{rt.config.account}.lock"


def _serve_locks(rt: Runtime) -> tuple[int, ...]:
    if not lock_dir().is_absolute():
        raise ServeLockRefused(
            f"HOME must be an absolute path: the account lock dir would be {lock_dir()}, "
            "relative to the working dir, so two serves could trade one account"
        )
    try:
        lock_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ServeLockRefused(f"cannot create the lock dir {lock_dir()}: {exc.strerror}") from None
    data = _lock(rt.config.data_dir / SERVE_LOCK)
    try:
        return data, _lock(account_lock_path(rt))
    except ServeLockRefused:
        os.close(data)
        raise


def _cloid() -> str:
    return "0x" + secrets.token_hex(16)


def _decimals(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def on_price_grid(px: Decimal, sz_decimals: int) -> bool:
    """An integer price always fits; any other has at most 5 significant figures
    and at most 6 - szDecimals decimals."""
    if px == px.to_integral_value():
        return True
    digits = len(px.normalize().as_tuple().digits)
    return digits <= PRICE_SIG_FIGS and _decimals(px) <= PERP_MAX_DECIMALS - sz_decimals


def to_price_grid(px: Decimal, sz_decimals: int, *, up: bool) -> Decimal:
    max_decimals = max(0, PERP_MAX_DECIMALS - sz_decimals)
    exponent = min(0, max(px.adjusted() - (PRICE_SIG_FIGS - 1), -max_decimals))
    return px.quantize(Decimal(1).scaleb(exponent), rounding=ROUND_CEILING if up else ROUND_FLOOR)


def leverage_reads_max(policy: Policy) -> int:
    """An entry passes the open_orders_max count only with at most
    open_orders_max - 1 opening orders, so the coins of those and its own
    need at most this many leverage reads. A coin with only reduce-only
    orders needs one too; wide_book refuses a book past this."""
    return policy.open_orders_max


def write_weight_floor(policy: Policy) -> int:
    """Every entry reads fills before the per-coin reads, and each page it reads
    stays read. So one page has to fit an empty minute, and an entry has to fit
    a minute with one short page of new fills and the widest book the policy
    lets an entry through with."""
    leverage = COSTS["activeAssetData"].base * leverage_reads_max(policy)
    return ENTRY_FIXED_WEIGHT + max(FILLS_PAGE_WEIGHT, FILLS_SHORT_PAGE_WEIGHT + leverage)


@dataclass
class Writer:
    """``pending`` holds proposals waiting for the user, ``approved`` those the
    user approved that wait for a read the budget allows. ``serve_locks`` holds
    the lock files of the data dir and of the account, so one writer owns its
    ledger and its account."""

    rt: Runtime
    transport: ExchangeTransport
    nonces: NonceManager
    serve_locks: tuple[int, ...]
    fills: FillBook
    pending: dict[str, _Pending] = field(default_factory=dict)
    approved: dict[str, _Approved] = field(default_factory=dict)
    backlog: list[Record] = field(default_factory=list)
    last_state: tuple[AccountState, int, int] | None = None
    seen_seq: int = 0
    seen_size: int = -1

    @classmethod
    def start(cls, rt: Runtime) -> Writer:
        if rt.agent is None:
            raise ValueError("the write tools need an agent key")
        signer = Signer(rt.agent, rt.config.account, network=rt.network)
        transport = ExchangeTransport(
            signer, rt.network, rt.halt, timeout_s=rt.config.info_timeout_s
        )
        locks = _serve_locks(rt)
        nonces = NonceManager.for_agent(signer.agent_address)
        sent = [r.payload["nonce"] for r in rt.records() if r.kind is Kind.ORDER]
        nonces.seed(max((n for n in sent if type(n) is int), default=0))
        writer = cls(rt, transport, nonces, locks, FillBook(rt))
        answered = [
            p
            for p in fold(rt.records()).values()
            if p.status in (Status.PENDING, Status.APPROVED)
            and not writer._expire(p.id, "the server restarted")
        ]
        writer._queue_answered(answered)
        lost = list(writer.fills.fold().unresolved.values())
        if lost:
            writer._set_halt(lost[0], f"{len(lost)} orders from before the restart have no outcome")
        writer.seen_seq = len(rt.records())
        rt.risk()  # fold the ledger now rather than in the first tool call
        return writer

    def _queue_answered(self, answered: list[Proposal]) -> None:
        first: dict[Any, Record] = {}
        for r in self.rt.records():
            if r.kind is Kind.APPROVAL:
                first.setdefault(r.payload.get("id"), r)
        for p in answered:
            order = order_from_json(p.order)
            answer = first[p.id]
            if order is None or p.expires_ms is None or p.ttl_s is None:
                self._refuse(p.id, "the proposal from before the restart does not parse")
            elif not 0 <= self._now() - answer.ts_ms <= p.ttl_s * 1000:
                detail = "the server started outside the approval's ttl"
                self._decide(p.id, Status.EXPIRED, Check.EXPIRED, detail)
            else:
                expires = self._deadline(p.expires_ms, p.ttl_s * 1000)
                self.pending[p.id] = _Pending(p.id, order, p.hash, expires, p.ttl_s)
                self.backlog.append(answer)

    def release(self) -> None:
        for fd in self.serve_locks:
            os.close(fd)

    def _now(self) -> int:
        return self.rt.now_ms()

    def _deadline(self, wall_ms: int, ttl_ms: int) -> _Deadline:
        return _Deadline(wall_ms, _elapsed_ms() + min(ttl_ms, wall_ms - self._now()))

    def _record(self, kind: Kind, payload: Mapping[str, Any]) -> Record:
        return self.rt.ledger.append(kind, payload)

    def _decide(
        self, pid: str, outcome: Status, rule: str = "", detail: str = "", **extra: Any
    ) -> None:
        payload = {"id": pid, "outcome": outcome.value, "rule": rule, "detail": detail}
        self._record(Kind.DECISION, payload | extra)

    def _reject(self, pid: str, rule: str, detail: str) -> Rejected:
        self._decide(pid, Status.REJECTED, rule, detail)
        return Rejected(pid, rule, detail)

    def _failed(self, pid: str, text: str) -> Rejected:
        self._decide(pid, Status.FAILED, Check.EXCHANGE, text)
        return Rejected(pid, Check.EXCHANGE, text)

    def _propose(
        self, tool: str, order: Mapping[str, Any], digest: str = "", pid: str | None = None
    ) -> str:
        pid = pid or secrets.token_hex(6)
        self._record(Kind.PROPOSAL, {"id": pid, "tool": tool, "order": order, "hash": digest})
        return pid

    def _guarded(self, pid: str, run: Callable[[], T]) -> T:
        try:
            return run()
        except Exception as exc:
            failure = as_failure(exc)
            if failure is not None:
                self._decide(pid, Status.FAILED, failure.kind.value, failure.detail)
            raise

    def _listing(self, coin: str) -> Market:
        return self.rt.market(coin, LISTING_TTL_MS)

    def _set_halt(self, pid: str, reason: str) -> None:
        try:
            fd = os.open(
                self.rt.halt.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as f:
                f.write(f"{reason}\n")
        self._record(Kind.HALT, {"id": pid, "reason": reason})

    def _unknown(self, pid: str, why: str) -> NoReturn:
        self._set_halt(pid, f"{why} (proposal {pid})")
        raise ToolFailure(
            Failure.NETWORK,
            f"{why}; hl-guard set the HALT file. Read open_orders and positions; the user "
            "removes the HALT file to trade again",
        )

    def _read_book(self) -> Exchange:
        rt = self.rt
        markets = rt.markets_within(LISTING_TTL_MS)
        state = parse_clearinghouse_state(rt.user_post("clearinghouseState"))
        received = rt.now_ms()
        rows = parse_open_orders(rt.user_post("frontendOpenOrders"))
        return Exchange(state, received, markets, position_marks(state, received), rows, {})

    def _leverage_coins(self, book: Exchange, coin: str) -> list[str]:
        held = {p.coin for p in book.state.positions if p.size != 0}
        return sorted(set(coins_needing_leverage(book)) | ({coin} - held))

    def _read_leverage(self, book: Exchange, coins: list[str]) -> Exchange:
        leverages, marks = {}, dict(book.marks)
        for c in coins:
            active = parse_active_asset(self.rt.user_post("activeAssetData", coin=c), c)
            leverages[c] = active.leverage
            marks[c] = Mark(active.mark_px, self.rt.now_ms())
        return dataclasses.replace(book, leverages=leverages, marks=marks)

    def _book_reject(self, pid: str, book: Exchange, coins: list[str]) -> Rejected | None:
        """Refuse before the per-coin reads what the gate would refuse anyway,
        and a book wider than the weight floor is priced for."""
        opening = sum(1 for o in perp_orders(book) if not o.reduce_only)
        capped = gate.open_orders_cap(self.rt.policy, opening)
        if capped is not None:
            return self._reject(pid, capped.rule, capped.detail)
        if len(coins) > leverage_reads_max(self.rt.policy):
            detail = (
                f"the entry needs the leverage of {len(coins)} coins with orders and no "
                f"position; info_weight_per_min is priced for {leverage_reads_max(self.rt.policy)}"
            )
            return self._reject(pid, Check.WIDE_BOOK, detail)
        return None

    def _account(self, exchange: Exchange, coin: str | None) -> tuple[Account, bool]:
        """The account and whether its day-start equity is known. A close never
        reads the daily allowance, so an unfunded day reads as equity 1 there."""
        now = self._now()
        view = self.rt.risk().view(now, self.rt.policy.cooldown_s)
        equity = self.rt.first_equity_today(now, exchange.state.account_value)
        extra = [coin] if coin else []
        account = build_account(now, equity or Decimal(1), exchange, view, extra)
        if equity is not None and not account.latched:
            with localcontext(GATE_CONTEXT):
                tripped = gate.trip(self.rt.policy, account)
                drawdown = gate.drawdown(self.rt.policy, equity, exchange.state.account_value)
            if tripped is not None:
                self._record(Kind.LATCH, {"rule": tripped.rule.value, "detail": tripped.detail})
            elif drawdown is not None:
                self._record(
                    Kind.LATCH, {"rule": Rule.DAILY_LOSS_PCT_MAX.value, "detail": drawdown}
                )
                account = dataclasses.replace(account, latched=True)
        return account, equity is not None

    def _reconcile_or_record(self, pid: str) -> None:
        """After an order went out, a fills read that fails is recorded; the next
        entry reads the fills again and fails closed until it can."""
        try:
            self.fills.reconcile(None)
        except Exception as exc:
            failure = as_failure(exc)
            if failure is None and not isinstance(exc, FillsUnread):
                raise
            detail = str(exc) if failure is None else failure.detail
            self._record(Kind.ERROR, {"id": pid, "detail": f"fills not read: {detail}"})

    def _fills_unread_reason(self, exchange: Exchange) -> str | None:
        try:
            self.fills.reconcile({o.oid: o for o in exchange.orders})
        except InfoRateLimited:
            raise
        except Exception as exc:
            if not isinstance(exc, FillsUnread) and as_failure(exc) is None:
                raise
            return f"the fills of this server's orders cannot be read: {exc}"
        return None

    def _reject_without_read(self, pid: str, order: Order) -> Rejected | None:
        if self.rt.halt.present():
            return self._reject(pid, Check.HALTED, "the HALT file is in the data dir")
        problem = offline_problem(self.rt.policy, order)
        return None if problem is None else self._reject(pid, *problem)

    def _format_reject(self, pid: str, order: Order, market: Market) -> Rejected | None:
        if market.delisted:
            return self._reject(pid, Check.DELISTED, f"{order.coin} is delisted")
        if _decimals(order.size) > market.sz_decimals:
            detail = f"{order.coin} sizes have at most {market.sz_decimals} decimals"
            return self._reject(pid, Check.SIZE_DECIMALS, detail)
        prices = (
            ("limit_px", order.limit_px),
            ("stop_loss_px", order.stop_loss_px),
            ("take_profit_px", order.take_profit_px),
        )
        for name, px in prices:
            if px is not None and not on_price_grid(px, market.sz_decimals):
                detail = (
                    f"{name} {wire(px)} is off the {order.coin} price grid: at most 5 significant "
                    f"figures and {PERP_MAX_DECIMALS - market.sz_decimals} decimals"
                )
                return self._reject(pid, Check.PRICE_TICK, detail)
        return None

    def _evaluate(self, pid: str, order: Order) -> Rejected | _Plan:
        market = self._listing(order.coin)
        refused = self._format_reject(pid, order, market)
        if refused is not None:
            return refused
        book = self._read_book()
        unread = self._fills_unread_reason(book)
        if unread is not None:
            return self._reject(pid, Check.FILLS_UNREAD, unread)
        coins = self._leverage_coins(book, order.coin)
        wide = self._book_reject(pid, book, coins)
        if wide is not None:
            return wide
        exchange = self._read_leverage(book, coins)
        account, funded = self._account(exchange, order.coin)
        if not funded:
            return self._reject(pid, Check.NO_EQUITY, "the account value is 0")
        asset = account.assets[order.coin]
        held = account.positions.get(order.coin, Decimal(0)) != 0
        if held and asset.leverage != order.leverage:
            detail = f"the open {order.coin} position is at {asset.leverage}x; propose at that"
            return self._reject(pid, Rule.LEVERAGE_MODE, detail)
        set_leverage = not held and (
            asset.margin_mode != "isolated" or asset.leverage != order.leverage
        )
        entry = Entry(
            order.coin,
            order.is_buy,
            order.size,
            order.limit_px,
            order.stop_loss_px,
            order.take_profit_px,
        )
        with localcontext(GATE_CONTEXT):
            if set_leverage:
                change = SetLeverage(order.coin, order.leverage)
                decision = gate.check(self.rt.policy, account, change)
                if isinstance(decision, GateRejected):
                    return self._reject(pid, decision.rule, decision.detail)
                after = dataclasses.replace(asset, margin_mode="isolated", leverage=order.leverage)
                account = dataclasses.replace(account, assets={**account.assets, order.coin: after})
            decision = gate.check(self.rt.policy, account, entry)
            notional = order.size * max(order.limit_px, account.assets[order.coin].mark_px)
            exposure = gate.gross_exposure_usd(account) + notional
        if isinstance(decision, GateRejected):
            return self._reject(pid, decision.rule, decision.detail)
        return _Plan(market, set_leverage, exposure)

    def _needs_approval(self, exposure_after: Decimal) -> bool:
        match self.rt.policy.approval:
            case ApproveNever():
                return False
            case ApproveAboveUsd(usd):
                return exposure_after > usd
            case _:
                return True

    def propose(self, args: Mapping[str, Any]) -> Outcome:
        order = Order(
            coin=args["coin"],
            is_buy=args["side"] == "buy",
            size=args["size"],
            limit_px=args["limit_px"],
            tif=args["tif"],
            stop_loss_px=args.get("stop_loss_px"),
            take_profit_px=args.get("take_profit_px"),
            leverage=args["leverage"],
        )
        pid = secrets.token_hex(6)
        body = order.as_json()
        digest = proposal_hash(pid, self.rt.network, self.rt.config.account, body)
        self._propose("propose_order", body, digest, pid)
        return self._guarded(pid, lambda: self._propose_entry(pid, order, digest))

    def _propose_entry(self, pid: str, order: Order, digest: str) -> Outcome:
        refused = self._reject_without_read(pid, order)
        if refused is not None:
            return refused
        if len(self.pending) >= PENDING_MAX:
            detail = (
                f"{len(self.pending)} proposals already wait for the user; the cap is {PENDING_MAX}"
            )
            return self._reject(pid, Check.PENDING_MAX, detail)
        plan = self._evaluate(pid, order)
        if isinstance(plan, Rejected):
            return plan
        if not self._needs_approval(plan.exposure_after):
            self._decide(pid, Status.ALLOWED, detail="the policy needs no approval for it")
            return self._place(pid, order, plan)
        ttl = self.rt.config.approval_ttl_s
        expires = self._deadline(self._now() + ttl * 1000, ttl * 1000)
        self.pending[pid] = _Pending(pid, order, digest, expires, ttl)
        detail = "waits for hl-guard approve"
        self._decide(pid, Status.PENDING, detail=detail, expires_ms=expires.wall_ms, ttl_s=ttl)
        how = f"the user approves it in a terminal with: hl-guard approve {pid}"
        return PendingApproval(pid, ttl, expires.wall_ms, order.as_json(), how)

    def _send(
        self, pid: str, action: Action, headroom: int, deadline: _Deadline | None = None
    ) -> Any:
        """Every action is signed with expiresAfter, after which Hyperliquid
        refuses it, so a POST delayed past it can never land."""
        now = self._now()
        lifetime = EXPIRES_AFTER_MS if deadline is None else deadline.left_ms(now)
        if lifetime < 0:
            raise _TtlRanOut
        expires = now + min(lifetime, EXPIRES_AFTER_MS)
        budget = self.rt.info.budget
        token = budget.reserve(EXCHANGE_WEIGHT, headroom)
        try:
            nonce = self.nonces.next_nonce(time.time_ns() // 1_000_000)
        except NonceWindowError as exc:
            self._set_halt(pid, "the clock left the exchange nonce window")
            raise ToolFailure(Failure.REFUSED, str(exc)) from None
        self._record(
            Kind.ORDER, {"id": pid, "nonce": nonce, "expires_after": expires, "action": action}
        )
        try:
            body = self.transport.send(action, nonce, expires)
        except SignerError as exc:
            self._ack(pid, nonce, {"error": exchange_text(exc)})
            cloids = [o["c"] for o in action.get("orders", [])]
            self._decide(pid, Status.FAILED, Failure.REFUSED.value, "never sent", cloids=cloids)
            raise ToolFailure(Failure.REFUSED, f"the signer refused: {exc}") from None
        except TransportError as exc:
            text = exchange_text(exc)
            self._ack(pid, nonce, {"error": text, "outcome_unknown": exc.outcome_unknown})
            if (self.transport.last_response or {}).get("http_status") == 429:
                self._bookkeep(pid, budget.back_off)
            if exc.outcome_unknown:
                self._unknown(pid, "the exchange gave no usable answer")
            raise _ExchangeRefused(text) from None
        finally:
            self._bookkeep(pid, lambda: budget.settle(token, EXCHANGE_WEIGHT))
        self._ack(pid, nonce, {"response": body})
        return body

    def _bookkeep(self, pid: str, step: Callable[[], None]) -> None:
        try:
            step()
        except InfoRateLimited as exc:
            self._record(Kind.ERROR, {"id": pid, "detail": f"weight budget: {exc}"})

    def _ack(self, pid: str, nonce: int, detail: Mapping[str, Any]) -> None:
        try:
            self._record(Kind.ACK, {"id": pid, "nonce": nonce, **detail})
        except ValueError:
            self._record(Kind.ACK, {"id": pid, "nonce": nonce, "raw": exchange_text(detail)})

    def _order_acks(self, pid: str, body: Any, cloids: tuple[str, ...]) -> tuple[OrderAck, ...]:
        try:
            return parse_order_acks(body, cloids)
        except ShapeError:
            self._unknown(pid, "the exchange answer has an unknown shape")

    def _order_action(self, wires: list[dict[str, Any]]) -> Action:
        return build_action("order", {"orders": wires, "grouping": "na"})

    def _place(
        self, pid: str, order: Order, plan: _Plan, deadline: _Deadline | None = None
    ) -> Outcome:
        asset = plan.market.asset_id
        if plan.set_leverage:
            change = {"asset": asset, "isCross": False, "leverage": order.leverage}
            leverage = build_action("updateLeverage", change)
            try:
                parse_default_ack(self._send(pid, leverage, 0, deadline))
            except _ExchangeRefused as exc:
                return self._failed(pid, exc.text)
            except ShapeError:
                return self._failed(pid, "the exchange did not confirm the leverage change")
        entry = {
            "a": asset,
            "b": order.is_buy,
            "p": wire(order.limit_px),
            "s": wire(order.size),
            "r": False,
            "t": {"limit": {"tif": order.tif}},
            "c": _cloid(),
        }
        (outcome,) = self._send_orders(pid, [(order.coin, entry)], 0, deadline)
        if isinstance(outcome, Placed):
            return dataclasses.replace(outcome, protection=PROTECTION)
        return outcome

    def _send_orders(
        self,
        pid: str,
        orders: list[tuple[str, dict[str, Any]]],
        headroom: int,
        deadline: _Deadline | None = None,
    ) -> list[Outcome]:
        cloids = [w["c"] for _, w in orders]
        try:
            body = self._send(pid, self._order_action([w for _, w in orders]), headroom, deadline)
        except _ExchangeRefused as exc:
            self._decide(pid, Status.FAILED, Check.EXCHANGE, exc.text, cloids=cloids)
            return [Rejected(pid, Check.EXCHANGE, exc.text) for _ in orders]
        acks = self._order_acks(pid, body, tuple(cloids))
        outcomes = [
            self._settle_ack(pid, ack, coin, w) for (coin, w), ack in zip(orders, acks, strict=True)
        ]
        if any(isinstance(a, Filled) for a in acks):
            self._reconcile_or_record(pid)
        return outcomes

    def _settle_ack(self, pid: str, ack: OrderAck, coin: str, w: Mapping[str, Any]) -> Outcome:
        cloid = w["c"]
        match ack:
            case Refused(message):
                self._decide(pid, Status.FAILED, Check.EXCHANGE, message, cloids=[cloid])
                return Rejected(pid, Check.EXCHANGE, message)
            case Resting(oid):
                self.fills.placed(pid, oid, "resting", w["s"], coin, cloid)
                return Placed(pid, oid, "resting", None, None)
            case Filled(oid, total, avg):
                self.fills.placed(pid, oid, "filled", wire(total), coin, cloid)
                return Placed(pid, oid, "filled", total, avg)

    def _exit_px(self, mark: Decimal, long: bool, market: Market) -> Decimal:
        policy = self.rt.policy
        with localcontext(GATE_CONTEXT):
            if long:
                return to_price_grid(mark * policy.close_sell_floor, market.sz_decimals, up=True)
            return to_price_grid(mark * policy.close_buy_cap, market.sz_decimals, up=False)

    def _close_wire(
        self, pid: str, account: Account, coin: str, size: Decimal, market: Market
    ) -> dict[str, Any] | Rejected:
        long = account.positions.get(coin, Decimal(0)) > 0
        px = self._exit_px(account.assets[coin].mark_px, long, market)
        with localcontext(GATE_CONTEXT):
            decision = gate.check(self.rt.policy, account, Close(coin, size, px))
        if isinstance(decision, GateRejected):
            return self._reject(pid, decision.rule, decision.detail)
        return {
            "a": market.asset_id,
            "b": not long,
            "p": wire(px),
            "s": wire(size),
            "r": True,
            "t": {"limit": {"tif": "Ioc"}},
            "c": _cloid(),
        }

    def _state(self) -> tuple[AccountState, int]:
        raw = self.rt.user_post("clearinghouseState", headroom=RISK_HEADROOM)
        state, received = parse_clearinghouse_state(raw), self.rt.now_ms()
        self.last_state = state, received, len(self.fills.fold().sent)
        return state, received

    def _no_position_lately(self, coin: str) -> int | None:
        """Closes answered from a recent account read spend no /info weight, so
        they cannot use up the burst a real close needs. A sent order with no
        outcome may land at any moment, so it voids the read."""
        if self.last_state is None:
            return None
        state, at, sent = self.last_state
        age = self._now() - at
        book = self.fills.fold()
        if sent != len(book.sent) or book.unresolved or not 0 <= age <= NO_POSITION_REUSE_MS:
            return None
        if any(p.coin == coin and p.size != 0 for p in state.positions):
            return None
        if any(o.coin == coin for o in book.outstanding.values()):
            return None
        return age

    def _one_coin(self, state: AccountState, received: int, coin: str, market: Market) -> Account:
        """An account of one position, enough for the close rules, so one odd coin
        never stops the close of another."""
        held = tuple(p for p in state.positions if p.coin == coin)
        one = dataclasses.replace(state, positions=held)
        exchange = Exchange(one, received, {coin: market}, position_marks(one, received), (), {})
        nothing = LedgerView(Decimal(0), 0, None, ())
        return build_account(self._now(), Decimal(1), exchange, nothing)

    def close(self, args: Mapping[str, Any]) -> Outcome:
        coin, size = args["coin"], args.get("size")
        pid = self._propose("close_position", {"coin": coin, "size": size and wire(size)})
        return self._guarded(pid, lambda: self._close(pid, coin, size))

    def _close(self, pid: str, coin: str, size: Decimal | None) -> Outcome:
        age = self._no_position_lately(coin)
        if age is not None:
            detail = f"no open {coin} position in the account read {age} ms ago"
            return self._reject(pid, Check.NO_POSITION, detail)
        state, received = self._state()
        if not any(p.coin == coin and p.size != 0 for p in state.positions):
            return self._reject(pid, Check.NO_POSITION, f"no open {coin} position")
        market = self.rt.market(coin, LISTING_TTL_MS, RISK_HEADROOM)
        account = self._one_coin(state, received, coin, market)
        position = account.positions.get(coin, Decimal(0))
        if position == 0:
            return self._reject(pid, Check.NO_POSITION, f"no open {coin} position")
        size = abs(position) if size is None else size
        if _decimals(size) > market.sz_decimals:
            detail = f"{coin} sizes have at most {market.sz_decimals} decimals"
            return self._reject(pid, Check.SIZE_DECIMALS, detail)
        built = self._close_wire(pid, account, coin, size, market)
        if isinstance(built, Rejected):
            return built
        self._decide(pid, Status.ALLOWED, detail="a reduce-only close")
        (outcome,) = self._send_orders(pid, [(coin, built)], RISK_HEADROOM)
        return outcome

    def cancel(self, args: Mapping[str, Any]) -> Rejected | Cancelled:
        coin, oid = args["coin"], args["oid"]
        pid = self._propose("cancel_order", {"coin": coin, "oid": oid})
        return self._guarded(pid, lambda: self._cancel(pid, coin, oid))

    def _cancel(self, pid: str, coin: str, oid: int) -> Rejected | Cancelled:
        rows = parse_open_orders(self.rt.user_post("frontendOpenOrders"))
        row = next((o for o in rows if o.oid == oid and o.coin == coin), None)
        if row is None:
            return self._reject(pid, Check.NO_ORDER, f"no open {coin} order with oid {oid}")
        if row.reduce_only:
            detail = "a reduce-only order protects a position; only the user cancels it"
            return self._reject(pid, Check.PROTECTS_POSITION, detail)
        refused = self._cancel_rows(pid, [row], {coin: self._listing(coin)}, 0)
        if refused:
            return self._failed(pid, refused[0][1])
        return Cancelled(pid, oid)

    def _cancel_rows(
        self, pid: str, rows: list[OrderRow], markets: Mapping[str, Market], headroom: int
    ) -> list[tuple[int, str]]:
        refused: list[tuple[int, str]] = []
        for start in range(0, len(rows), BATCH_MAX):
            batch = rows[start : start + BATCH_MAX]
            cancels = [{"a": markets[o.coin].asset_id, "o": o.oid} for o in batch]
            try:
                body = self._send(pid, build_action("cancel", {"cancels": cancels}), headroom)
            except _ExchangeRefused as exc:
                refused += [(o.oid, exc.text) for o in batch]
                continue
            try:
                acks = parse_cancel_acks(body, len(batch))
            except ShapeError:
                self._unknown(pid, "the exchange answer to a cancel has an unknown shape")
            for o, ack in zip(batch, acks, strict=True):
                if ack is None:
                    self._decide(pid, Status.CANCELLED, oid=o.oid, coin=o.coin)
                else:
                    refused.append((o.oid, ack.message))
        return refused

    def halt(self, args: Mapping[str, Any]) -> dict[str, Any]:
        pid = self._propose("halt", {})
        return self._guarded(pid, lambda: self._halt(pid))

    def _halt(self, pid: str) -> dict[str, Any]:
        """Each phase runs even when the one before it failed; what did not finish
        is named in the answer and in an error record."""
        self._set_halt(pid, f"the halt tool (proposal {pid})")
        self._decide(pid, Status.ALLOWED, detail="cancel opening orders and close every position")
        report: dict[str, Any] = {
            "result": "halted",
            "id": pid,
            "cancelled": [],
            "cancel_refused": [],
            "closes": [],
            "unfinished": [],
        }
        for name, phase in (("cancel", self._halt_cancels), ("close", self._halt_closes)):
            try:
                phase(pid, report)
            except Exception as exc:
                failure = as_failure(exc)
                if failure is None:
                    raise
                report["unfinished"].append(f"{name}: {failure.kind.value}: {failure.detail}")
        if report["unfinished"]:
            self._record(Kind.ERROR, {"id": pid, "detail": "; ".join(report["unfinished"])})
        return report

    def _halt_cancels(self, pid: str, report: dict[str, Any]) -> None:
        markets = self.rt.markets_within(LISTING_TTL_MS, RISK_HEADROOM)
        rows = parse_open_orders(self.rt.user_post("frontendOpenOrders", headroom=RISK_HEADROOM))
        opening = [o for o in rows if not o.reduce_only]
        listed = [o for o in opening if o.coin in markets]
        refused = self._cancel_rows(pid, listed, markets, RISK_HEADROOM)
        refused += [(o.oid, f"{o.coin} is not a listed perp") for o in opening if o not in listed]
        failed = {oid for oid, _ in refused}
        report["cancelled"] = [o.oid for o in listed if o.oid not in failed]
        report["cancel_refused"] = [{"oid": oid, "refused": text} for oid, text in refused]

    def _halt_closes(self, pid: str, report: dict[str, Any]) -> None:
        markets = self.rt.markets_within(LISTING_TTL_MS, RISK_HEADROOM)
        state, received = self._state()
        held = sorted((p.coin, p.size) for p in state.positions if p.size != 0)
        if any(coin not in markets for coin, _ in held):
            markets = self.rt.markets_within(0, RISK_HEADROOM)
        closes: list[dict[str, Any]] = report["closes"]
        wires: list[tuple[str, dict[str, Any]]] = []
        for coin, size in held:
            built = self._halt_close(pid, state, received, coin, abs(size), markets.get(coin))
            if isinstance(built, Rejected):
                closes.append({"coin": coin, "refused": f"{built.rule}: {built.detail}"})
            else:
                wires.append((coin, built))
        for start in range(0, len(wires), BATCH_MAX):
            batch = wires[start : start + BATCH_MAX]
            sent = self._send_orders(pid, batch, RISK_HEADROOM)
            for (coin, _), outcome in zip(batch, sent, strict=True):
                match outcome:
                    case Placed(oid=oid, status=status):
                        closes.append({"coin": coin, "oid": oid, "status": status})
                    case Rejected(detail=detail):
                        closes.append({"coin": coin, "refused": detail})

    def _halt_close(
        self,
        pid: str,
        state: AccountState,
        received: int,
        coin: str,
        size: Decimal,
        market: Market | None,
    ) -> dict[str, Any] | Rejected:
        if market is None:
            return Rejected(pid, Check.EXCHANGE, f"{coin} is not a listed perp")
        try:
            account = self._one_coin(state, received, coin, market)
        except SnapshotError as exc:
            return Rejected(pid, Check.EXCHANGE, str(exc))
        return self._close_wire(pid, account, coin, size, market)

    def status(self, args: Mapping[str, Any]) -> dict[str, Any]:
        found = fold(self.rt.records()).get(args["id"])
        if found is None:
            raise ToolFailure(Failure.BAD_ARGUMENTS, f"no proposal {args['id']}")
        return {
            "id": found.id,
            "tool": found.tool,
            "status": found.status.value,
            "detail": found.detail,
            "oid": found.oid,
            "expires_ms": found.expires_ms,
            "order": found.order,
        }

    def poll(self) -> None:
        """Act on new approval records, then expire stale proposals, then retry
        placements. Approvals go first so one that came in time is never judged
        by how late the poll ran."""
        self._approvals()
        now = self._now()
        for pid, p in list(self.pending.items()):
            if p.expires.left_ms(now) < 0 and not self._expire(pid, "no approval within the ttl"):
                self._approvals()
        for waiting in list(self.approved.values()):
            if _elapsed_ms() >= waiting.retry_at_elapsed_ms:
                self._try_place(waiting.pending, waiting.deadline)

    def _approvals(self) -> None:
        backlog, self.backlog = self.backlog, []
        for record in backlog:
            self._on_approval(record)
        try:
            size = os.stat(self.rt.ledger.path).st_size
        except FileNotFoundError:
            size = 0
        if size != self.seen_size:
            records = self.rt.records()
            start = self.seen_seq if self.seen_seq <= len(records) else 0
            fresh, self.seen_seq, self.seen_size = records[start:], len(records), size
            for record in fresh:
                if record.kind is Kind.APPROVAL:
                    self._on_approval(record)

    def _expire(self, pid: str, detail: str) -> bool:
        """Expire under the ledger lock only while no answer is recorded after
        the records the approval poll has read, so an approval the CLI wrote
        meanwhile is acted on instead."""

        def unanswered(records: list[Record], ts_ms: int) -> None:
            if any(r.kind is Kind.APPROVAL and r.payload.get("id") == pid for r in records):
                raise ValueError(f"proposal {pid} has an answer")

        payload = {
            "id": pid,
            "outcome": Status.EXPIRED.value,
            "rule": Check.EXPIRED.value,
            "detail": detail,
        }
        try:
            self.rt.tail().append(Kind.DECISION, payload, after_seq=self.seen_seq, check=unanswered)
        except ValueError:
            return False
        self.pending.pop(pid, None)
        return True

    def _refuse(self, pid: str, detail: str) -> None:
        self._decide(pid, Status.REFUSED, Check.APPROVAL, detail)

    def _on_approval(self, record: Record) -> None:
        pid = record.payload.get("id")
        if not isinstance(pid, str):
            return
        p = self.pending.pop(pid, None)
        if p is None:
            self._refuse(pid, "this proposal was decided already")
        elif record.payload.get("hash") != p.hash:
            self._refuse(pid, "the approval names another proposal hash")
        elif record.ts_ms > p.expires.wall_ms:
            self._decide(pid, Status.EXPIRED, Check.EXPIRED, "the approval came after the ttl")
        elif record.payload.get("approved") is not True:
            self._decide(pid, Status.REJECTED, Check.USER_REJECTED, "the user rejected it")
        else:
            ttl_ms = p.ttl_s * 1000
            self._try_place(p, self._deadline(record.ts_ms + ttl_ms, ttl_ms))

    def _sent(self, pid: str) -> bool:
        return any(r.kind is Kind.ORDER and r.payload.get("id") == pid for r in self.rt.records())

    def _try_place(self, p: _Pending, deadline: _Deadline) -> None:
        """Place an approved proposal after a fresh gate pass. A read the budget
        refuses is retried until ``deadline``, unless an action of the proposal
        already went out."""
        self.approved.pop(p.id, None)
        if deadline.left_ms(self._now()) < 0:
            detail = "the approval's ttl ran out before the server could place it"
            self._decide(p.id, Status.EXPIRED, Check.EXPIRED, detail)
            return
        if self.rt.halt.present():
            self._reject(p.id, Check.HALTED, "the HALT file is in the data dir")
            return
        try:
            plan = self._evaluate(p.id, p.order)
            if isinstance(plan, _Plan):
                self._decide(p.id, Status.ALLOWED, detail="the user approved it")
                self._place(p.id, p.order, plan, deadline)
        except _TtlRanOut:
            detail = "the approval's ttl ran out before the server could send it"
            self._decide(p.id, Status.EXPIRED, Check.EXPIRED, detail)
        except InfoRateLimited as exc:
            if self._sent(p.id) or deadline.left_ms(self._now()) < RETRY_MS:
                self._decide(p.id, Status.FAILED, Failure.RATE_LIMITED.value, str(exc))
            else:
                self.approved[p.id] = _Approved(p, _elapsed_ms() + RETRY_MS, deadline)
        except Exception as exc:
            failure = as_failure(exc)
            if failure is None:
                detail = f"{type(exc).__name__}: {exc}"
                self._decide(p.id, Status.FAILED, Check.INTERNAL, detail[:500])
                raise
            self._decide(p.id, Status.FAILED, failure.kind.value, failure.detail)


_COIN = CoinArg("perp coin name as Hyperliquid lists it, for example BTC")

_WRITE: tuple[Tool[Writer], ...] = (
    Tool(
        "propose_order",
        "Propose an isolated-margin limit entry. The server checks it against the user's "
        "policy and answers rejected with the rule, pending_approval with an id that the "
        "user approves outside this chat, or placed with the exchange order id. Every "
        "entry needs a stop-loss price; the gate checks it and the ledger records it, but "
        "hl-guard places no stop-loss or take-profit orders, so the user or the client must "
        "place them on the exchange. An entry counts toward the daily entry cap and its coin's "
        "cooldown once the exchange accepts it, even when it is later cancelled unfilled; "
        "an order the exchange refuses does not count.",
        {
            "coin": _COIN,
            "side": ChoiceArg("buy opens or adds to a long, sell a short", ("buy", "sell")),
            "size": DecimalArg("size in coins"),
            "limit_px": DecimalArg("limit price in USD"),
            "stop_loss_px": DecimalArg("stop-loss price in USD", required=False),
            "take_profit_px": DecimalArg("take-profit price in USD", required=False),
            "tif": ChoiceArg("time in force", TIFS, default="Gtc"),
            "leverage": IntArg("isolated leverage", 1, MAX_LEVERAGE, 1),
        },
        lambda w, a: w.propose(a),
        read_only=False,
    ),
    Tool(
        "close_position",
        "Close all or part of an open position with a reduce-only order at the policy's "
        "exit price cap. A close needs no approval.",
        {"coin": _COIN, "size": DecimalArg("size in coins; all of it when left out", False)},
        lambda w, a: w.close(a),
        read_only=False,
    ),
    Tool(
        "cancel_order",
        "Cancel one resting opening order by its exchange order id. Reduce-only orders stay.",
        {"coin": _COIN, "oid": IntArg("exchange order id", 0, 2**63 - 1, None)},
        lambda w, a: w.cancel(a),
        read_only=False,
    ),
    Tool(
        "halt",
        "Stop trading: set the HALT file, cancel every opening order and close every "
        "position reduce-only. Only the user can lift a halt.",
        {},
        lambda w, a: w.halt(a),
        read_only=False,
    ),
    Tool(
        "proposal_status",
        "Read where a proposal stands, with its exchange order id: proposed, pending, "
        "approved, allowed, placed, rejected, refused, expired, cancelled or failed.",
        {"id": PatternArg("the proposal id", PROPOSAL_ID)},
        lambda w, a: w.status(a),
    ),
)

WRITE_TOOLS: Mapping[str, Tool[Writer]] = {tool.name: tool for tool in _WRITE}
