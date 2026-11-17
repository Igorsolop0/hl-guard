import os
import re
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, get_type_hints

import pytest
from policy_fixtures import ACCOUNT, POLICY

from hl_guard.policy.ceilings import AT_MOST, CEILINGS, SCHEDULE_CANCEL_MIN_S
from hl_guard.policy.load import (
    MAX_POLICY_BYTES,
    PolicyError,
    _parse_yaml,
    load_policy,
    parse_account,
    parse_policy,
    parse_proposal,
)
from hl_guard.policy.model import (
    AddMargin,
    ApproveAboveUsd,
    ApproveAlways,
    ApproveNever,
    Close,
    Entry,
    Policy,
    ScheduleCancel,
    SetLeverage,
)

EXAMPLE = Path(__file__).parents[2] / "examples" / "policy.yaml"
BTC = ACCOUNT["assets"]["BTC"]
ENTRY = {
    "kind": "entry",
    "coin": "BTC",
    "is_buy": True,
    "size_coins": Decimal("0.1"),
    "limit_px": 100,
    "stop_loss_px": 99,
}


def test_the_proposed_ceilings_are_these_values() -> None:
    assert {str(b) for b in CEILINGS} == {
        "per_order_usd_max <= 5000",
        "total_exposure_usd_max <= 20000",
        "leverage_max <= 5",
        "open_orders_max <= 20",
        "entries_per_day_max <= 50",
        "daily_loss_pct_max <= 10",
        "loss_streak_max <= 10",
        "cooldown_s >= 60",
        "stop_loss_max_pct <= 10",
        "take_profit_max_pct <= 50",
        "price_band_pct_max <= 5",
        "snapshot_max_age_s <= 30",
        "margin_additions_per_day_max <= 10",
        "0.1 <= close_sell_floor <= 0.95",
        "1.05 <= close_buy_cap <= 10",
        "5 <= schedule_cancel_max_s <= 604800",
        "add_margin_usd_max",
    }
    assert AT_MOST == (("add_margin_usd_max", "per_order_usd_max"),)
    assert SCHEDULE_CANCEL_MIN_S == 5


DEFAULTS: dict[str, Decimal | int] = {
    "leverage_max": 1,
    "close_sell_floor": Decimal("0.5"),
    "close_buy_cap": Decimal(2),
    "schedule_cancel_max_s": 86400,
    "add_margin_usd_max": Decimal(10),
}


def test_every_numeric_policy_field_has_a_ceiling() -> None:
    numeric = {
        k for k, v in POLICY.items() if isinstance(v, int | float) and not isinstance(v, bool)
    }
    assert numeric | set(DEFAULTS) == {b.field for b in CEILINGS}


def test_a_policy_without_the_configurable_rules_loads_their_defaults() -> None:
    bare = {k: v for k, v in POLICY.items() if k not in DEFAULTS}
    loaded = parse_policy(bare)
    assert {name: getattr(loaded, name) for name in DEFAULTS} == DEFAULTS
    assert loaded == parse_policy({**bare, **DEFAULTS})


def test_the_margin_ceiling_follows_the_policy_order_cap() -> None:
    wide = {**POLICY, "per_order_usd_max": 50}
    assert parse_policy({**wide, "add_margin_usd_max": 50}).add_margin_usd_max == 50
    linked = r"ceiling add_margin_usd_max <= per_order_usd_max \(50\)"
    with pytest.raises(PolicyError, match=linked):
        parse_policy({**wide, "add_margin_usd_max": 51})
    at_product_cap = {**POLICY, "per_order_usd_max": 5000, "total_exposure_usd_max": 20000}
    assert parse_policy({**at_product_cap, "add_margin_usd_max": 5000}).add_margin_usd_max == 5000


def test_an_omitted_margin_cap_is_at_most_the_order_cap() -> None:
    small = {**POLICY, "per_order_usd_max": 5}
    assert parse_policy(small).add_margin_usd_max == Decimal(5)
    assert parse_policy({**POLICY, "per_order_usd_max": 50}).add_margin_usd_max == Decimal(10)
    linked = "add_margin_usd_max = 10 breaks the product hard ceiling"
    with pytest.raises(
        PolicyError, match=re.escape(f"{linked} add_margin_usd_max <= per_order_usd_max (5)")
    ):
        parse_policy({**small, "add_margin_usd_max": 10})


def test_the_example_policy_loads_with_the_tested_defaults(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_bytes(EXAMPLE.read_bytes())
    path.chmod(0o600)
    assert load_policy(path) == Policy(
        network="testnet",
        coins=frozenset({"BTC", "ETH", "SOL"}),
        per_order_usd_max=Decimal(20),
        total_exposure_usd_max=Decimal(100),
        leverage_max=1,
        open_orders_max=3,
        entries_per_day_max=2,
        daily_loss_pct_max=Decimal(3),
        loss_streak_max=5,
        cooldown_s=3600,
        stop_loss_max_pct=Decimal(2),
        take_profit_max_pct=Decimal(5),
        price_band_pct_max=Decimal("0.5"),
        snapshot_max_age_s=5,
        margin_additions_per_day_max=3,
        close_sell_floor=Decimal("0.5"),
        close_buy_cap=Decimal(2),
        schedule_cancel_max_s=86400,
        add_margin_usd_max=Decimal(10),
        approval=ApproveAlways(),
    )


def _plain(value: Decimal) -> Any:
    return int(value) if value == value.to_integral_value() else float(value)


def _edge(limit: Decimal, sign: int) -> tuple[Any, Any]:
    step = 1 if limit == limit.to_integral_value() else Decimal("0.01")
    return _plain(limit), _plain(limit + sign * step)


FIXED_EDGES: list[tuple[str, str, Any, Any]] = [
    *(
        (b.field, str(b), *_edge(limit, sign))
        for b in CEILINGS
        for limit, sign in ((b.low, -1), (b.high, 1))
        if limit is not None
    ),
    *((field, f"{field} <= {cap}", *_edge(Decimal(POLICY[cap]), 1)) for field, cap in AT_MOST),
]
EDGE_IDS = [f"{field}={edge}" for field, _, edge, _ in FIXED_EDGES]


@pytest.mark.parametrize(("field", "ceiling", "edge", "past"), FIXED_EDGES, ids=EDGE_IDS)
def test_a_value_past_its_ceiling_fails_and_names_the_ceiling(
    field: str, ceiling: str, edge: Any, past: Any
) -> None:
    broken = f"{field} = {past} breaks the product hard ceiling {ceiling}"
    with pytest.raises(PolicyError, match=re.escape(broken)):
        parse_policy({**POLICY, field: past})


@pytest.mark.parametrize(("field", "ceiling", "edge", "past"), FIXED_EDGES, ids=EDGE_IDS)
def test_a_value_at_its_ceiling_loads(field: str, ceiling: str, edge: Any, past: Any) -> None:
    kind = get_type_hints(Policy)[field]
    loaded = getattr(parse_policy({**POLICY, "total_exposure_usd_max": 20000, field: edge}), field)
    assert (type(loaded), loaded) == (kind, kind(str(edge)))


def test_the_stop_loss_cannot_be_turned_off() -> None:
    with pytest.raises(PolicyError, match="stop_loss_required must be true"):
        parse_policy({**POLICY, "stop_loss_required": False})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "20", True, None, 0, -5, [20]])
def test_a_limit_must_be_a_positive_finite_number(bad: Any) -> None:
    with pytest.raises(PolicyError, match="per_order_usd_max"):
        parse_policy({**POLICY, "per_order_usd_max": bad})


@pytest.mark.parametrize(
    "field",
    [
        "leverage_max",
        "cooldown_s",
        "loss_streak_max",
        "snapshot_max_age_s",
        "margin_additions_per_day_max",
        "schedule_cancel_max_s",
    ],
)
def test_a_count_must_be_an_integer(field: str) -> None:
    with pytest.raises(PolicyError, match=field):
        parse_policy({**POLICY, field: 1.5})


def test_unknown_and_missing_keys_fail() -> None:
    with pytest.raises(PolicyError, match="unknown keys \\['max_leverage'\\]"):
        parse_policy({**POLICY, "max_leverage": 1})
    partial = dict(POLICY)
    del partial["cooldown_s"]
    with pytest.raises(PolicyError, match="missing keys \\['cooldown_s'\\]"):
        parse_policy(partial)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("always", ApproveAlways()),
        ("never", ApproveNever()),
        ({"above_usd": 10}, ApproveAboveUsd(Decimal(10))),
    ],
)
def test_approval_modes_parse(value: Any, expected: Any) -> None:
    assert parse_policy({**POLICY, "approval": value}).approval == expected


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("approval", "sometimes"),
        ("approval", {"above_usd": -1}),
        ("approval", {"above_usd": 0}),
        ("approval", {"above_usd": 20}),
        ("approval", {"above_usd": 25}),
        ("network", "devnet"),
        ("coins", []),
        ("coins", ["BTC", "xyz:TSLA"]),
        ("coins", "BTC"),
    ],
)
def test_bad_choices_fail(field: str, bad: Any) -> None:
    with pytest.raises(PolicyError):
        parse_policy({**POLICY, field: bad})


def _write(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(text)
    path.chmod(mode)
    return path


@pytest.mark.parametrize("mode", [0o666, 0o620, 0o602, 0o660])
def test_a_group_or_world_writable_policy_fails(tmp_path: Path, mode: int) -> None:
    path = _write(tmp_path, EXAMPLE.read_text(), mode)
    with pytest.raises(PolicyError, match="group or others can write it"):
        load_policy(path)


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o400])
def test_an_owner_writable_policy_loads(tmp_path: Path, mode: int) -> None:
    load_policy(_write(tmp_path, EXAMPLE.read_text(), mode))


def test_a_symlink_is_judged_by_its_target(tmp_path: Path) -> None:
    target = _write(tmp_path, EXAMPLE.read_text(), 0o666)
    link = tmp_path / "link.yaml"
    os.symlink(target, link)
    with pytest.raises(PolicyError, match="group or others"):
        load_policy(link)


def test_a_duplicate_key_fails(tmp_path: Path) -> None:
    text = EXAMPLE.read_text() + "\nper_order_usd_max: 10\n"
    with pytest.raises(PolicyError, match="duplicate key 'per_order_usd_max'"):
        load_policy(_write(tmp_path, text))


def test_missing_file_and_bad_yaml_fail(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="cannot read"):
        load_policy(tmp_path / "absent.yaml")
    with pytest.raises(PolicyError, match="not valid YAML"):
        load_policy(_write(tmp_path, "coins: [BTC\n"))
    with pytest.raises(PolicyError, match="must be a mapping"):
        load_policy(_write(tmp_path, "- 1\n"))


def test_an_asset_id_outside_the_perp_range_fails() -> None:
    assets = {"BTC": {**ACCOUNT["assets"]["BTC"], "asset_id": 10_000}}
    with pytest.raises(PolicyError, match="not a perp asset id"):
        parse_account({**ACCOUNT, "assets": assets})


def test_a_position_needs_a_mark_price() -> None:
    with pytest.raises(PolicyError, match=r"positions \['DOGE'\] have no entry in assets"):
        parse_account({**ACCOUNT, "positions": {"DOGE": 5}})


def test_proposals_parse_into_their_variants() -> None:
    entry = parse_proposal(
        {"kind": "entry", "coin": "BTC", "is_buy": True, "size_coins": 1, "limit_px": 10}
    )
    assert entry == Entry("BTC", True, Decimal(1), Decimal(10), None, None)
    assert parse_proposal(
        {"kind": "close", "coin": "BTC", "size_coins": 1, "limit_px": 10}
    ) == Close("BTC", Decimal(1), Decimal(10))
    assert parse_proposal({"kind": "add_margin", "coin": "BTC", "usd": 5}) == AddMargin(
        "BTC", Decimal(5)
    )
    assert parse_proposal({"kind": "set_leverage", "coin": "ETH", "leverage": 2}) == SetLeverage(
        "ETH", 2
    )
    assert parse_proposal({"kind": "schedule_cancel", "time_ms": 5}) == ScheduleCancel(5)


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "withdraw", "amount": 1},
        {"kind": "close", "coin": "BTC", "size_coins": 1, "limit_px": 1, "reduce_only": False},
        {"kind": "close", "coin": "BTC", "size_coins": 1},
        {"kind": "close", "coin": "BTC", "size_coins": -1, "limit_px": 1},
        {"kind": "close", "coin": "BTC", "size_coins": float("nan"), "limit_px": 1},
        {"kind": "add_margin", "coin": "BTC", "usd": -1},
        {"kind": "add_margin", "coin": "BTC", "usd": 10**5000},
        {"kind": "entry", "coin": "BTC", "is_buy": 1, "size_coins": 1, "limit_px": 10},
        {"kind": "set_leverage", "coin": "BTC", "leverage": 0},
        ["entry"],
    ],
)
def test_bad_proposals_fail(bad: Any) -> None:
    with pytest.raises(PolicyError):
        parse_proposal(bad)


def test_per_order_cannot_exceed_total_exposure() -> None:
    with pytest.raises(
        PolicyError, match="per_order_usd_max 200 is above total_exposure_usd_max 100"
    ):
        parse_policy({**POLICY, "per_order_usd_max": 200})
    parse_policy({**POLICY, "per_order_usd_max": 100})


def test_approval_above_usd_sits_below_the_order_cap() -> None:
    assert parse_policy({**POLICY, "approval": {"above_usd": 19}}).approval == ApproveAboveUsd(
        Decimal(19)
    )
    with pytest.raises(PolicyError, match="must be below per_order_usd_max"):
        parse_policy({**POLICY, "approval": {"above_usd": 20}})


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("a: &x 1\nb: *x\n", "anchors and aliases"),
        ("a: &x [1]\n", "anchors and aliases"),
        ("base: {x: 1}\n<<: {per_order_usd_max: 20}\n", "no merge or complex keys"),
        ("? [a, b]\n: 1\n", "no merge or complex keys"),
        ("r: " + "[" * 17 + "]" * 17 + "\n", "flow collections nest deeper than 16"),
        ("r: " + "{a: " * 17 + "}" * 17 + "\n", "flow collections nest deeper than 16"),
        ("r:\n" + "- " * 2000 + "x\n", "block collections nest deeper than 16 levels"),
        ("".join("  " * i + "a:\n" for i in range(17)) + "  " * 17 + "x\n", "block collections"),
        ("per_order_usd_max: " + "9" * 5000 + "\n", "malformed"),
    ],
    ids=[
        "alias",
        "anchor",
        "merge key",
        "complex key",
        "deep flow sequence",
        "deep flow mapping",
        "deep block sequence",
        "deep block mapping",
        "huge int",
    ],
)
def test_risky_yaml_is_refused_with_a_policy_error(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(PolicyError, match=match):
        load_policy(_write(tmp_path, text))


def test_an_alias_bomb_is_refused_quickly(tmp_path: Path) -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x, x]"]
    lines += [f"a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * 10)}]" for i in range(1, 9)]
    with pytest.raises(PolicyError, match="anchors and aliases"):
        load_policy(_write(tmp_path, "\n".join(lines) + "\n"))


def test_a_policy_over_16_kib_is_refused(tmp_path: Path) -> None:
    text = EXAMPLE.read_text()
    load_policy(_write(tmp_path, text + "#" * (MAX_POLICY_BYTES - len(text))))
    with pytest.raises(PolicyError, match="larger than 16384 bytes"):
        load_policy(_write(tmp_path, text + "#" * (MAX_POLICY_BYTES - len(text) + 1)))


def test_a_policy_that_is_not_utf8_is_refused(tmp_path: Path) -> None:
    path = _write(tmp_path, "")
    path.write_bytes(b"network: \xff\n")
    with pytest.raises(PolicyError, match="malformed"):
        load_policy(path)


def test_the_mode_error_says_how_to_fix_it(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match=r"~/\.config/hl-guard/policy\.yaml and run chmod 600"):
        load_policy(_write(tmp_path, EXAMPLE.read_text(), 0o666))


def test_a_world_writable_folder_without_sticky_bit_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "open"
    folder.mkdir()
    path = _write(folder, EXAMPLE.read_text())
    folder.chmod(0o777)
    with pytest.raises(PolicyError, match="world-writable without the sticky bit"):
        load_policy(path)
    folder.chmod(0o1777)
    load_policy(path)


def test_a_symlink_from_a_safe_folder_into_an_open_one_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "open"
    folder.mkdir()
    target = _write(folder, EXAMPLE.read_text())
    folder.chmod(0o777)
    link = tmp_path / "link.yaml"
    os.symlink(target, link)
    with pytest.raises(PolicyError, match="world-writable"):
        load_policy(link)


def test_a_policy_owned_by_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, EXAMPLE.read_text())
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(PolicyError, match="belongs to uid"):
        load_policy(path)


def test_the_file_is_judged_after_it_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, EXAMPLE.read_text())
    real_open = os.open

    def swap_then_open(p: Any, flags: int, *args: Any) -> int:
        if Path(p) == path:
            path.chmod(0o666)
        return real_open(p, flags, *args)

    monkeypatch.setattr(os, "open", swap_then_open)
    with pytest.raises(PolicyError, match="group or others can write it"):
        load_policy(path)


def test_account_lists_are_capped_at_1000() -> None:
    entries = [{"coin": "BTC", "at_ms": 1}] * 1001
    with pytest.raises(PolicyError, match=r"account\.entries holds 1001 items; the cap is 1000"):
        parse_account({**ACCOUNT, "entries": entries})
    parse_account({**ACCOUNT, "entries": entries[:1000]})


def test_account_needs_the_snapshot_time_and_the_latch() -> None:
    for key in ("snapshot_at_ms", "latched", "margin_added_at_ms"):
        partial = {k: v for k, v in ACCOUNT.items() if k != key}
        with pytest.raises(PolicyError, match=f"missing keys \\['{key}'\\]"):
            parse_account(partial)
    with pytest.raises(PolicyError, match="latched must be true or false"):
        parse_account({**ACCOUNT, "latched": 0})


@pytest.mark.parametrize(
    ("closers", "match"),
    [
        ("# " + "]" * 7000, "flow collections nest deeper than 16"),
        ("y: " + "]" * 7000, "not valid YAML"),
    ],
    ids=["in a comment", "as a value"],
)
def test_closing_brackets_first_cannot_hide_deep_nesting(
    tmp_path: Path, closers: str, match: str
) -> None:
    text = closers + "\n" + EXAMPLE.read_text() + "\nx: " + "{" * 7000 + "\n"
    path = _write(tmp_path, text)
    started = time.monotonic()
    with pytest.raises(PolicyError, match=match):
        load_policy(path)
    assert time.monotonic() - started < 1


def test_brackets_in_comments_and_quoted_strings_do_not_count(tmp_path: Path) -> None:
    text = EXAMPLE.read_text().replace("coins: [BTC, ETH, SOL]", "coins: [BTC, ETH, 'SOL']")
    text += "# " + "[" * 17 + "\n"
    assert load_policy(_write(tmp_path, text)).coins == frozenset({"BTC", "ETH", "SOL"})
    quoted = EXAMPLE.read_text().replace("network: testnet", 'network: "' + "[" * 17 + '"')
    with pytest.raises(PolicyError, match="network must be testnet or mainnet"):
        load_policy(_write(tmp_path, quoted))


def test_flow_nesting_at_16_levels_loads(tmp_path: Path) -> None:
    text = EXAMPLE.read_text().replace(
        "coins: [BTC, ETH, SOL]", "coins: [BTC, ETH, SOL]\nr: " + "[" * 16 + "]" * 16
    )
    with pytest.raises(PolicyError, match="unknown keys \\['r'\\]"):
        load_policy(_write(tmp_path, text))


@pytest.mark.parametrize(
    ("field", "spelling"),
    [
        ("cooldown_s", "0600"),
        ("cooldown_s", "1:00"),
        ("per_order_usd_max", "0x14"),
        ("per_order_usd_max", "1_000"),
        ("per_order_usd_max", "1_0.5"),
        ("per_order_usd_max", "0:10.5"),
    ],
)
def test_a_number_must_be_written_in_plain_decimal(
    tmp_path: Path, field: str, spelling: str
) -> None:
    lines = [
        f"{field}: {spelling}" if line.startswith(f"{field}:") else line
        for line in EXAMPLE.read_text().splitlines()
    ]
    with pytest.raises(PolicyError, match="plain decimal"):
        load_policy(_write(tmp_path, "\n".join(lines) + "\n"))


@pytest.mark.parametrize("spelling", ["20", "+20", "20.0", "0.5", "-0", "1.5e+1", "!!int 20"])
def test_plain_decimal_numbers_load(spelling: str) -> None:
    value = _parse_yaml(f"n: {spelling}\n")["n"]
    assert Decimal(str(value)) == Decimal(spelling.removeprefix("!!int "))


def test_a_coin_name_is_at_most_16_characters() -> None:
    assert "A" * 16 in parse_policy({**POLICY, "coins": ["A" * 16]}).coins
    with pytest.raises(PolicyError, match="coin name"):
        parse_policy({**POLICY, "coins": ["A" * 17]})


def test_an_opening_order_needs_a_mark_price() -> None:
    orders = [{"coin": "XRP", "notional_usd": 10, "reduce_only": False}]
    with pytest.raises(PolicyError, match="open orders \\['XRP'\\] have no entry in assets"):
        parse_account({**ACCOUNT, "open_orders": orders})


def test_block_nesting_at_16_levels_loads(tmp_path: Path) -> None:
    nested = "".join("  " * i + "a:\n" for i in range(16)) + "  " * 16 + "x\n"
    with pytest.raises(PolicyError, match="unknown keys \\['a'\\]"):
        load_policy(_write(tmp_path, EXAMPLE.read_text() + nested))


@pytest.mark.parametrize("coin", ["BTC\u0661", "\u0411TC", "BTC\u00b2"])
def test_a_coin_name_must_be_ascii(coin: str) -> None:
    with pytest.raises(PolicyError, match="ASCII letters and digits"):
        parse_policy({**POLICY, "coins": ["BTC", coin]})


def test_approval_above_usd_takes_no_other_keys() -> None:
    with pytest.raises(PolicyError, match="approval must be"):
        parse_policy({**POLICY, "approval": {"above_usd": 5, "x": 1}})


def test_an_unknown_key_error_names_at_most_five_keys() -> None:
    extra = {f"k{i}": 1 for i in range(7)}
    with pytest.raises(PolicyError) as exc:
        parse_policy({**POLICY, **extra})
    assert str(exc.value) == (
        "policy: missing keys [], unknown keys ['k0', 'k1', 'k2', 'k3', 'k4'] and 2 more"
    )


def test_a_directory_is_named_as_one(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match=f"policy {tmp_path} is a directory"):
        load_policy(tmp_path)


def test_a_symlink_from_an_open_folder_to_a_safe_target_is_refused(tmp_path: Path) -> None:
    target = _write(tmp_path, EXAMPLE.read_text())
    folder = tmp_path / "open"
    folder.mkdir()
    link = folder / "link.yaml"
    os.symlink(target, link)
    folder.chmod(0o777)
    with pytest.raises(PolicyError, match=f"policy folder {folder} is world-writable"):
        load_policy(link)
    folder.chmod(0o1777)
    load_policy(link)


SIZE = "must be 0 or between 1e-30 and 1e30 in size"


@pytest.mark.parametrize(
    ("parse", "field"),
    [
        (lambda d: parse_proposal({**ENTRY, "size_coins": d}), "proposal.size_coins"),
        (lambda d: parse_proposal({**ENTRY, "stop_loss_px": d}), "proposal.stop_loss_px"),
        (
            lambda d: parse_account({**ACCOUNT, "assets": {"BTC": {**BTC, "mark_px": d}}}),
            "account.assets.BTC.mark_px",
        ),
        (lambda d: parse_account({**ACCOUNT, "positions": {"BTC": d}}), "account.positions.BTC"),
        (
            lambda d: parse_account({**ACCOUNT, "realized_pnl_today_usd": -d}),
            "account.realized_pnl_today_usd",
        ),
        (
            lambda d: parse_account(
                {
                    **ACCOUNT,
                    "open_orders": [{"coin": "BTC", "notional_usd": d, "reduce_only": False}],
                }
            ),
            "account.open_orders[0].notional_usd",
        ),
        (lambda d: parse_policy({**POLICY, "daily_loss_pct_max": d}), "daily_loss_pct_max"),
    ],
)
@pytest.mark.parametrize(
    "value", [Decimal("1e999999"), Decimal("1e-999999"), Decimal("1e31"), Decimal("9e-31"), 10**31]
)
def test_parsing_refuses_a_number_outside_1e_30_to_1e30(
    parse: Any, field: str, value: Decimal | int
) -> None:
    with pytest.raises(PolicyError, match=re.escape(f"{field} {SIZE}")):
        parse(value)


@pytest.mark.parametrize(
    ("parse", "field"),
    [
        (lambda v: parse_account({**ACCOUNT, "now_ms": v}), "account.now_ms"),
        (lambda v: parse_proposal({"kind": "schedule_cancel", "time_ms": v}), "proposal.time_ms"),
    ],
)
def test_parsing_takes_integers_up_to_2_63_minus_1(parse: Any, field: str) -> None:
    parse(2**63 - 1)
    with pytest.raises(
        PolicyError, match=re.escape(f"{field} must be an integer from 0 to 2**63-1")
    ):
        parse(2**63)


def test_a_fifo_as_the_policy_file_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    os.mkfifo(path, 0o600)
    with pytest.raises(PolicyError, match="is not a regular file"):
        load_policy(path)


def test_an_account_asset_without_max_leverage_reads_its_leverage_as_the_maximum() -> None:
    raw = {**ACCOUNT, "assets": {"BTC": {**ACCOUNT["assets"]["BTC"], "leverage": 3}}}
    assert parse_account(raw).assets["BTC"].max_leverage == 3
    raw["assets"]["BTC"]["max_leverage"] = 40
    assert parse_account(raw).assets["BTC"].max_leverage == 40


@pytest.mark.parametrize("value", [0, -1, 2.5, True, "40"])
def test_an_account_asset_max_leverage_must_be_a_positive_integer(value: Any) -> None:
    raw = {**ACCOUNT, "assets": {"BTC": {**ACCOUNT["assets"]["BTC"], "max_leverage": value}}}
    with pytest.raises(PolicyError, match="max_leverage"):
        parse_account(raw)
