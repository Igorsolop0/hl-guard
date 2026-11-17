from __future__ import annotations

import os
import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from eth_account import Account
from eth_account.signers.local import LocalAccount

from hl_guard.cli.init import init, revoke, status
from hl_guard.keys.agent_key import AGENT_NAME
from hl_guard.keys.approve_page import DAY_MS, expiry_text
from hl_guard.server.info import InfoClient

from .conftest import FakeExchange, MakeConfig
from .test_slot_invariant import (
    HOUR_MS,
    Browser,
    Crash,
    current_address,
    leaked,
    now_ms,
    phrased,
    run,
)

SEEDS = int(os.environ.get("HL_GUARD_SLOT_SEEDS", "24"))
STEPS = int(os.environ.get("HL_GUARD_SLOT_STEPS", "14"))
CLOCKS = (0, 0, 0, 5 * HOUR_MS, -5 * HOUR_MS, 3 * DAY_MS, -3 * DAY_MS)
MODES = ("approve", "approve", "approve", "refuse", "ignore", "sign-only", "tab", "tab")
SHIFTS = (0, 0, 0, 1, 3650 * DAY_MS, -31 * DAY_MS)
MUTATIONS = ("replace", "link", "unlink", "rename", "fsync")


@contextmanager
def crash_at(step: int) -> Iterator[None]:
    calls = [0]
    real_os = {name: getattr(os, name) for name in MUTATIONS}
    real_post = InfoClient.post

    def counted(real: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            calls[0] += 1
            if calls[0] == step:
                raise Crash
            return real(*args, **kwargs)

        return call

    for name, real in real_os.items():
        setattr(os, name, counted(real))
    InfoClient.post = counted(real_post)  # type: ignore[method-assign]
    try:
        yield
    finally:
        for name, real in real_os.items():
            setattr(os, name, real)
        InfoClient.post = real_post  # type: ignore[method-assign]


def exchange_side(rng: random.Random, exchange: FakeExchange, master: str) -> str:
    user = master.lower()
    ours = [key for key in exchange.agents if key[0] == user]
    kind = rng.choice(("rename", "drop", "foreign", "shift", "fail"))
    if kind == "rename" and ours:
        key = rng.choice(ours)
        exchange.agents[(user, rng.choice(("legacy", "", "other")))] = exchange.agents.pop(key)
    elif kind == "drop" and ours:
        del exchange.agents[rng.choice(ours)]
    elif kind == "foreign":
        until = rng.choice((1, 30)) * DAY_MS + now_ms()
        exchange.list_agent(master, AGENT_NAME, Account.create().address, until)
    elif kind == "shift":
        exchange.until_shift_ms = rng.choice(SHIFTS)
    elif kind == "fail":
        exchange.failed_reads = rng.choice((1, 2, 5))
    return kind


def stated_expiry(outcome: tuple[int, list[str]]) -> str | None:
    first = outcome[1][0] if outcome[1] else ""
    if first.startswith("agent approved, expires "):
        return first.removeprefix("agent approved, expires ")
    if first.startswith("agent replaced: "):
        return first.split(", expires ", 1)[1].split(";", 1)[0]
    return None


def listed_expiry(exchange: FakeExchange, config: Any) -> str:
    current = current_address(config)
    return expiry_text(
        max(until for address, until in exchange.agents.values() if address.lower() == current)
    )


@pytest.mark.parametrize("names", ["base", "wire"])
@pytest.mark.parametrize("seed", range(SEEDS))
def test_no_clean_exit_while_the_exchange_lists_an_agent_the_slot_signed_other_than_the_current(
    make_config: MakeConfig, exchange: FakeExchange, seed: int, names: str
) -> None:
    rng = random.Random(seed)
    exchange.names = names
    wallet: LocalAccount = Account.create()
    config = make_config(wallet.address)
    with config.open("a") as fh:
        fh.write("info_weight_per_min: 600\n")
    browser = Browser(wallet, exchange)
    trail: list[str] = []
    clean = late = 0
    world = 0
    for _ in range(STEPS):
        pick = rng.random()
        if pick < 0.15:
            trail.append("exchange " + exchange_side(rng, exchange, wallet.address))
            continue
        if pick < 0.3 and browser.tabs:
            answer = browser.land_late(rng.randrange(len(browser.tabs)), now_ms() + world)
            late += answer == {"status": "ok", "response": {"type": "default"}}
            trail.append(f"late tab at world={world} -> {answer}")
            continue
        verb = rng.choice((init, init, revoke, revoke, status))
        browser.mode = rng.choice(MODES)
        clock, days = rng.choice(CLOCKS), rng.choice((1, 30, 180))
        world = max(world, clock)
        cut = rng.randint(1, 12) if pick > 0.85 else 0
        step = f"{verb.__name__} {browser.mode} clock={clock} days={days} cut={cut}"
        try:
            with crash_at(cut):
                outcome = run(verb, config, browser, clock=clock, days=days)
        except Crash:
            trail.append(step + " -> crash")
            continue
        trail.append(f"{step} -> {outcome}")
        assert phrased(outcome), (seed, trail)
        assert outcome[0] != 0 or not leaked(config, exchange, browser), (
            seed,
            sorted(leaked(config, exchange, browser)),
            trail,
        )
        stated = stated_expiry(outcome) if outcome[0] == 0 else None
        assert stated is None or stated == listed_expiry(exchange, config), (seed, trail)
        clean += outcome[0] == 0
    print(
        f"seed {seed} {names}: {len(trail)} steps, {clean} clean exits, "
        f"{len(browser.signed)} signed, {late} late signatures landed"
    )
