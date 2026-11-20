from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from hl_guard.policy.ceilings import SCHEDULE_CANCEL_MIN_S
from hl_guard.policy.model import (
    Account,
    AddMargin,
    Allowed,
    AssetState,
    Close,
    Decision,
    Entry,
    Latch,
    Policy,
    Proposal,
    Rejected,
    Rule,
    ScheduleCancel,
    SetLeverage,
)

_DAY_MS = 24 * 60 * 60 * 1000
_HUNDRED = Decimal(100)


def check(policy: Policy, account: Account, proposal: Proposal) -> Decision:
    match proposal:
        case Entry():
            return _check_entry(policy, account, proposal)
        case Close():
            return _check_close(policy, account, proposal)
        case SetLeverage():
            return _check_set_leverage(policy, account, proposal)
        case ScheduleCancel():
            return _check_schedule_cancel(policy, account, proposal)
        case AddMargin():
            return _check_add_margin(policy, account, proposal)


def trip(policy: Policy, account: Account) -> Latch | None:
    """The latch a caller must record once the daily loss or the loss streak hits its limit."""
    loss_limit = account.day_start_equity_usd * policy.daily_loss_pct_max / _HUNDRED
    if account.realized_pnl_today_usd <= -loss_limit:
        return Latch(
            Rule.DAILY_LOSS_PCT_MAX,
            f"realized PnL today {account.realized_pnl_today_usd} USD reached the "
            f"{policy.daily_loss_pct_max}% daily loss limit ({loss_limit} USD)",
        )
    if account.loss_streak >= policy.loss_streak_max:
        return Latch(
            Rule.LOSS_STREAK_MAX,
            f"{account.loss_streak} losses in a row reached the cap {policy.loss_streak_max}",
        )
    return None


def drawdown(policy: Policy, day_start_usd: Decimal, value_usd: Decimal) -> str | None:
    """The exchange's account value catches losses the ledger never saw, such
    as a liquidation or a trade made outside hl-guard. A withdrawal reads as a
    loss too. Clearing this latch does not reopen entries that UTC day while
    the value stays at or past the limit: the day start stays, so the next
    entry latches again. Closes and halt never wait for a latch."""
    limit = policy.daily_loss_pct_max
    if value_usd > day_start_usd * (1 - limit / _HUNDRED):
        return None
    drop = (day_start_usd - value_usd) / day_start_usd * _HUNDRED
    return (
        f"the account value {value_usd} USD is {drop:.4f}% below the day start "
        f"{day_start_usd} USD, at or past the {limit}% daily loss limit"
    )


def gross_exposure_usd(account: Account) -> Decimal:
    positions = sum(
        (abs(size) * account.assets[coin].mark_px for coin, size in account.positions.items()),
        Decimal(0),
    )
    resting = sum((o.notional_usd for o in account.open_orders if not o.reduce_only), Decimal(0))
    return positions + resting


def _liquidation_pct(asset: AssetState, is_buy: bool) -> Decimal:
    """Hyperliquid liquidates when equity falls to the maintenance margin, half
    the initial margin at the coin's maximum leverage."""
    maintenance = 1 / Decimal(2 * asset.max_leverage)
    side = -1 if is_buy else 1
    return (1 / Decimal(asset.leverage) - maintenance) / (1 + side * maintenance) * _HUNDRED


def _pct(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b) / b * _HUNDRED


def _fresh(policy: Policy, account: Account, at_ms: int) -> bool:
    return 0 <= account.now_ms - at_ms <= policy.snapshot_max_age_s * 1000


def _today(times_ms: Iterable[int], now_ms: int) -> int:
    today = now_ms // _DAY_MS
    return sum(1 for at_ms in times_ms if at_ms // _DAY_MS == today)


def _later_than_now(times_ms: Iterable[int], now_ms: int) -> int | None:
    return next((at_ms for at_ms in times_ms if at_ms > now_ms), None)


def _exposed_coins(account: Account) -> list[str]:
    held = {coin for coin, size in account.positions.items() if size != 0}
    resting = {o.coin for o in account.open_orders if not o.reduce_only}
    return sorted(held | resting)


def _blocks_new_risk(
    policy: Policy, account: Account, marked_coins: Iterable[str]
) -> Rejected | None:
    if account.latched:
        return Rejected(
            Rule.LATCHED,
            "the account is latched after a daily-loss or loss-streak trip; "
            "only closes pass until an operator runs hl-guard latch clear",
        )
    latch = trip(policy, account)
    if latch is not None:
        return Rejected(latch.rule, f"{latch.detail}; only closes pass")
    max_age = policy.snapshot_max_age_s
    if not _fresh(policy, account, account.snapshot_at_ms):
        return Rejected(
            Rule.SNAPSHOT_MAX_AGE_S,
            f"the account snapshot from {account.snapshot_at_ms} is not within "
            f"{max_age} s before now {account.now_ms}",
        )
    for coin in marked_coins:
        if not _fresh(policy, account, account.assets[coin].mark_at_ms):
            return Rejected(
                Rule.SNAPSHOT_MAX_AGE_S,
                f"the {coin} mark from {account.assets[coin].mark_at_ms} is not within "
                f"{max_age} s before now {account.now_ms}",
            )
    return None


def open_orders_cap(policy: Policy, opening: int) -> Rejected | None:
    if opening + 1 > policy.open_orders_max:
        return Rejected(
            Rule.OPEN_ORDERS_MAX,
            f"{opening} opening orders rest; the cap is {policy.open_orders_max}",
        )
    return None


def _outside_band(policy: Policy, limit_px: Decimal, mark_px: Decimal) -> Rejected | None:
    deviation = _pct(limit_px, mark_px)
    if deviation > policy.price_band_pct_max:
        return Rejected(
            Rule.PRICE_BAND_PCT_MAX,
            f"limit {limit_px} is {deviation:.4f}% from mark {mark_px}, "
            f"outside the {policy.price_band_pct_max}% band",
        )
    return None


def _unlisted(policy: Policy, account: Account, coin: str) -> Rejected | None:
    if coin not in policy.coins:
        return Rejected(Rule.COINS, f"{coin} is not in the policy coins {sorted(policy.coins)}")
    if coin not in account.assets:
        return Rejected(Rule.COINS, f"{coin} is not in the account snapshot assets")
    return None


def _check_entry(policy: Policy, account: Account, e: Entry) -> Decision:
    refused = _unlisted(policy, account, e.coin) or _blocks_new_risk(
        policy, account, [e.coin, *_exposed_coins(account)]
    )
    if refused is not None:
        return refused
    held = account.positions.get(e.coin, Decimal(0))
    if (held < 0) if e.is_buy else (held > 0):
        return Rejected(
            Rule.OPPOSITE_POSITION,
            f"the account holds {held} {e.coin}; a {'buy' if e.is_buy else 'sell'} entry against "
            "it would leave its reduce-only stops protecting nothing",
        )
    asset = account.assets[e.coin]
    if asset.margin_mode != "isolated" or asset.leverage > policy.leverage_max:
        return Rejected(
            Rule.LEVERAGE_MODE,
            f"{e.coin} is {asset.margin_mode} {asset.leverage}x on the exchange; "
            f"entries need isolated at most {policy.leverage_max}x",
        )
    outside = _outside_band(policy, e.limit_px, asset.mark_px)
    if outside is not None:
        return outside

    if e.stop_loss_px is None:
        return Rejected(Rule.STOP_LOSS_REQUIRED, "every entry needs a stop-loss price")
    worst_px = min(e.limit_px, asset.mark_px) if e.is_buy else max(e.limit_px, asset.mark_px)
    protects = e.stop_loss_px < worst_px if e.is_buy else e.stop_loss_px > worst_px
    if not protects:
        return Rejected(
            Rule.STOP_LOSS_REQUIRED,
            f"stop-loss {e.stop_loss_px} is not on the loss side of limit {e.limit_px} "
            f"and mark {asset.mark_px}",
        )
    sl_pct = _pct(e.stop_loss_px, e.limit_px)
    if sl_pct > policy.stop_loss_max_pct:
        return Rejected(
            Rule.STOP_LOSS_MAX_PCT,
            f"stop-loss is {sl_pct:.4f}% from the limit, above {policy.stop_loss_max_pct}%",
        )
    liquidation_pct = _liquidation_pct(asset, e.is_buy)
    if sl_pct >= liquidation_pct:
        return Rejected(
            Rule.STOP_LOSS_LIQUIDATION,
            f"stop-loss is {sl_pct:.4f}% from the limit; at isolated {asset.leverage}x on a coin "
            f"with a {asset.max_leverage}x maximum the position is liquidated "
            f"{liquidation_pct:.4f}% away",
        )
    if e.take_profit_px is not None:
        on_profit_side = (
            e.take_profit_px > max(e.limit_px, asset.mark_px)
            if e.is_buy
            else e.take_profit_px < min(e.limit_px, asset.mark_px)
        )
        tp_pct = _pct(e.take_profit_px, e.limit_px)
        if not on_profit_side or tp_pct > policy.take_profit_max_pct:
            return Rejected(
                Rule.TAKE_PROFIT_MAX_PCT,
                f"take-profit {e.take_profit_px} must be on the profit side of limit "
                f"{e.limit_px} and mark {asset.mark_px}, at most "
                f"{policy.take_profit_max_pct}% from the limit",
            )

    notional = e.size_coins * max(e.limit_px, asset.mark_px)
    if notional > policy.per_order_usd_max:
        return Rejected(
            Rule.PER_ORDER_USD_MAX,
            f"order notional {notional} USD is above {policy.per_order_usd_max} USD",
        )
    total = gross_exposure_usd(account) + notional
    if total > policy.total_exposure_usd_max:
        return Rejected(
            Rule.TOTAL_EXPOSURE_USD_MAX,
            f"exposure would be {total} USD, above {policy.total_exposure_usd_max} USD",
        )
    refused = open_orders_cap(policy, sum(1 for o in account.open_orders if not o.reduce_only))
    if refused is not None:
        return refused

    future = _later_than_now((past.at_ms for past in account.entries), account.now_ms)
    if future is not None:
        return Rejected(
            Rule.COOLDOWN_S,
            f"an entry at {future} is later than now {account.now_ms}: clock moved back",
        )
    cooldown_ms = policy.cooldown_s * 1000
    for past in account.entries:
        if past.coin == e.coin and account.now_ms - past.at_ms < cooldown_ms:
            return Rejected(
                Rule.COOLDOWN_S,
                f"{e.coin} entered at {past.at_ms}; cooldown of {policy.cooldown_s} s is active",
            )
    entries_today = _today((past.at_ms for past in account.entries), account.now_ms)
    if entries_today >= policy.entries_per_day_max:
        return Rejected(
            Rule.ENTRIES_PER_DAY_MAX,
            f"{entries_today} entries today (UTC) reached the cap {policy.entries_per_day_max}",
        )
    return Allowed()


def _check_close(policy: Policy, account: Account, c: Close) -> Decision:
    position = account.positions.get(c.coin, Decimal(0))
    if c.size_coins > abs(position):
        return Rejected(
            Rule.REDUCE_ONLY,
            f"close size {c.size_coins} is above the open {c.coin} position {abs(position)}",
        )
    mark = account.assets[c.coin].mark_px
    floor, cap = policy.close_sell_floor, policy.close_buy_cap
    if position > 0 and c.limit_px < mark * floor:
        return Rejected(
            Rule.EXIT_PRICE_CAP,
            f"sell close limit {c.limit_px} is below {floor}x the last mark {mark}",
        )
    if position < 0 and c.limit_px > mark * cap:
        return Rejected(
            Rule.EXIT_PRICE_CAP,
            f"buy close limit {c.limit_px} is above {cap}x the last mark {mark}",
        )
    return Allowed()


def _check_set_leverage(policy: Policy, account: Account, s: SetLeverage) -> Decision:
    refused = _unlisted(policy, account, s.coin) or _blocks_new_risk(policy, account, [])
    if refused is not None:
        return refused
    if s.leverage > policy.leverage_max:
        return Rejected(
            Rule.LEVERAGE_MAX, f"leverage {s.leverage}x is above {policy.leverage_max}x"
        )
    return Allowed()


def _check_schedule_cancel(policy: Policy, account: Account, s: ScheduleCancel) -> Decision:
    earliest = account.now_ms + SCHEDULE_CANCEL_MIN_S * 1000
    latest = account.now_ms + policy.schedule_cancel_max_s * 1000
    if not earliest <= s.time_ms <= latest:
        return Rejected(
            Rule.SCHEDULE_CANCEL,
            f"scheduleCancel time {s.time_ms} must be between "
            f"now + {SCHEDULE_CANCEL_MIN_S} s ({earliest}) "
            f"and now + {policy.schedule_cancel_max_s} s ({latest})",
        )
    return Allowed()


def _check_add_margin(policy: Policy, account: Account, m: AddMargin) -> Decision:
    refused = _unlisted(policy, account, m.coin) or _blocks_new_risk(policy, account, [])
    if refused is not None:
        return refused
    if m.usd > policy.add_margin_usd_max:
        return Rejected(
            Rule.ADD_MARGIN,
            f"margin addition {m.usd} USD is above {policy.add_margin_usd_max} USD",
        )
    if account.positions.get(m.coin, Decimal(0)) == 0 or (
        account.assets[m.coin].margin_mode != "isolated"
    ):
        return Rejected(Rule.ADD_MARGIN, f"margin goes only to an open isolated {m.coin} position")
    future = _later_than_now(account.margin_added_at_ms, account.now_ms)
    if future is not None:
        return Rejected(
            Rule.MARGIN_ADDITIONS_PER_DAY_MAX,
            f"a margin addition at {future} is later than now {account.now_ms}: clock moved back",
        )
    added_today = _today(account.margin_added_at_ms, account.now_ms)
    if added_today >= policy.margin_additions_per_day_max:
        return Rejected(
            Rule.MARGIN_ADDITIONS_PER_DAY_MAX,
            f"{added_today} margin additions today (UTC) reached the cap "
            f"{policy.margin_additions_per_day_max}",
        )
    return Allowed()
