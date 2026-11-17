from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from server.fake_info import Answer, FakeInfo, point_testnet_at, serving

from hl_guard.keys.agent_key import AgentKeyError
from hl_guard.keys.agents import Listed, listed_agents
from hl_guard.server.info import COSTS, RESPONSE_BYTES_MAX, InfoClient, WeightBudget

MASTER = "0x" + "cd" * 20
AGENT = "0x" + "aB" * 20
UNTIL = 1_757_300_000_000


@pytest.fixture()
def fake() -> Iterator[FakeInfo]:
    with serving(FakeInfo()) as running:
        point_testnet_at(running.url)
        yield running


def client(tmp_path: Path) -> InfoClient:
    return InfoClient("testnet", WeightBudget(tmp_path / "info-weight.json", 300), timeout_s=5)


def answering(fake: FakeInfo, payload: object, status: int = 200) -> None:
    def answer(body: dict[str, Any]) -> Answer:
        return status, json.dumps(payload).encode(), {}

    fake.answers["extraAgents"] = answer


def test_extra_agents_costs_20_weight() -> None:
    assert COSTS["extraAgents"].of(0) == 20


def test_listed_agents_reads_the_masters_agents_once(fake: FakeInfo, tmp_path: Path) -> None:
    answering(
        fake,
        [
            {"name": "hl-guard", "address": AGENT, "validUntil": UNTIL},
            {"name": "other", "address": "0x" + "11" * 20, "validUntil": UNTIL + 1},
        ],
    )
    assert listed_agents(client(tmp_path), MASTER, [AGENT, "0x" + "11" * 20]) == {
        AGENT.lower(): Listed(("hl-guard",), UNTIL),
        "0x" + "11" * 20: Listed(("other",), UNTIL + 1),
    }
    assert fake.requests == [{"type": "extraAgents", "user": MASTER}]
    spent = json.loads((tmp_path / "info-weight.json").read_text())["spent"]
    assert sum(row[1] for row in spent) == 20


def test_an_empty_list_lists_nothing(fake: FakeInfo, tmp_path: Path) -> None:
    answering(fake, [])
    assert listed_agents(client(tmp_path), MASTER, [AGENT]) == {}


OTHER = "0x" + "99" * 20


def test_every_agent_is_returned_and_foreign_rows_may_be_malformed(
    fake: FakeInfo, tmp_path: Path
) -> None:
    answering(
        fake,
        [
            {"name": "manual", "address": OTHER, "validUntil": None},
            {"name": "manual", "address": "0x" + "88" * 20, "validUntil": UNTIL},
            {"address": "0x1234", "validUntil": UNTIL},
            {"validUntil": UNTIL},
            ["hl-guard", AGENT, UNTIL],
            None,
            {"name": 7, "address": AGENT.upper().replace("0X", "0x"), "validUntil": UNTIL},
        ],
    )
    assert listed_agents(client(tmp_path), MASTER, [AGENT]) == {
        "0x" + "88" * 20: Listed(("manual",), UNTIL),
        AGENT.lower(): Listed((None,), UNTIL),
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"address": AGENT, "validUntil": UNTIL},
        None,
        [{"address": AGENT, "validUntil": None}],
        [{"address": AGENT, "validUntil": str(UNTIL)}],
        [{"address": AGENT, "validUntil": True}],
        [{"address": AGENT, "validUntil": 10**20}],
        [{"address": AGENT, "validUntil": 0}],
        [{"address": AGENT}],
    ],
)
def test_a_bad_shape_is_refused(fake: FakeInfo, tmp_path: Path, payload: object) -> None:
    answering(fake, payload)
    with pytest.raises(AgentKeyError, match="agent list is malformed"):
        listed_agents(client(tmp_path), MASTER, [AGENT])


def test_a_bad_shape_echoes_at_most_200_chars(fake: FakeInfo, tmp_path: Path) -> None:
    answering(fake, [{"name": "x" * 5000, "address": AGENT, "validUntil": None}])
    with pytest.raises(AgentKeyError, match="malformed") as caught:
        listed_agents(client(tmp_path), MASTER, [AGENT])
    assert len(str(caught.value)) < 300


@pytest.mark.parametrize("status", [500, 429, 404])
def test_an_http_error_is_refused(fake: FakeInfo, tmp_path: Path, status: int) -> None:
    answering(fake, [], status)
    with pytest.raises(AgentKeyError, match="cannot read the exchange's agent list"):
        listed_agents(client(tmp_path), MASTER, [AGENT])


def test_an_oversize_answer_is_refused(fake: FakeInfo, tmp_path: Path) -> None:
    row = {"name": "x", "address": AGENT, "validUntil": UNTIL}
    answering(fake, [row] * (RESPONSE_BYTES_MAX // len(json.dumps(row)) + 1))
    with pytest.raises(AgentKeyError, match="larger than"):
        listed_agents(client(tmp_path), MASTER, [AGENT])


def test_an_unreachable_exchange_is_refused(tmp_path: Path) -> None:
    point_testnet_at("http://127.0.0.1:9/info")
    with pytest.raises(AgentKeyError, match=r"unreachable|within"):
        listed_agents(client(tmp_path), MASTER, [AGENT])


def test_rows_for_one_address_merge_whatever_its_case(fake: FakeInfo, tmp_path: Path) -> None:
    answering(
        fake,
        [
            {"name": "a", "address": OTHER, "validUntil": UNTIL + 2},
            {"name": "b", "address": OTHER.upper().replace("0X", "0x"), "validUntil": UNTIL + 1},
        ],
    )
    assert listed_agents(client(tmp_path), MASTER, []) == {OTHER: Listed(("a", "b"), UNTIL + 2)}
