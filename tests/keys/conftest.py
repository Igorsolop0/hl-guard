from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hyperliquid.utils.signing import recover_user_from_user_signed_action
from server.fake_info import Answer, FakeInfo, point_testnet_at, serving

from hl_guard.exchange import halt, network
from hl_guard.keys.approve_page import APPROVE_AGENT_FIELDS, PRIMARY_TYPE
from hl_guard.server import info

EXAMPLE_POLICY = Path(__file__).resolve().parents[2] / "examples" / "policy.yaml"


@pytest.fixture(autouse=True)
def _home_per_test(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))


@pytest.fixture(autouse=True)
def _restore_network() -> Iterator[None]:
    rows, enabled = network.NETWORKS, network._enabled
    yield
    network.NETWORKS, network._enabled = rows, enabled


@pytest.fixture(autouse=True)
def _info_worker_ends() -> Iterator[None]:
    yield
    if info._worker is not None:
        info._worker.join(5)
        assert not info._worker.is_alive()


@dataclass
class FakeExchange:
    url: str = ""
    agents: dict[tuple[str, str], tuple[str, int]] = field(default_factory=dict)
    reads: int = 0
    failed_reads: int = 0
    until_shift_ms: int = 0
    extra_rows: list[object] = field(default_factory=list)
    names: str = "base"

    def approve_agent(self, body: dict[str, Any]) -> Answer:
        action = body["action"]
        user = recover_user_from_user_signed_action(
            dict(action), body["signature"], APPROVE_AGENT_FIELDS, PRIMARY_TYPE, False
        )
        base, _, until = action["agentName"].rpartition(" valid_until ")
        name = base if self.names == "base" else action["agentName"]
        self.list_agent(user, name, action["agentAddress"], int(until) + self.until_shift_ms)
        return 200, json.dumps({"status": "ok", "response": {"type": "default"}}).encode(), {}

    def list_agent(self, user: str, name: str, address: str, until: int) -> None:
        self.agents[(user.lower(), name)] = (address, until)

    def extra_agents(self, body: dict[str, Any]) -> Answer:
        self.reads += 1
        if self.failed_reads:
            self.failed_reads -= 1
            return 500, b"", {}
        rows: list[object] = [
            {"name": name, "address": address, "validUntil": until}
            for (user, name), (address, until) in self.agents.items()
            if user == body["user"].lower()
        ]
        rows.extend(self.extra_rows)
        return 200, json.dumps(rows).encode(), {}


@pytest.fixture()
def exchange() -> Iterator[FakeExchange]:
    fake = FakeExchange()
    with serving(FakeInfo(exchange=fake.approve_agent)) as running:
        running.answers["extraAgents"] = fake.extra_agents
        fake.url = running.url.removesuffix("/info") + "/exchange"
        point_testnet_at(running.url, fake.url)
        yield fake


@pytest.fixture()
def durable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(halt, "temp_dirs", lambda: [])


MakeConfig = Callable[..., Path]


@pytest.fixture()
def make_config(tmp_path: Path, durable: None, exchange: FakeExchange) -> MakeConfig:
    def make(account: str, *, key_line: bool = True) -> Path:
        return _write_config(tmp_path, account, key_line=key_line)

    return make


def _write_config(root: Path, account: str, *, key_line: bool) -> Path:
    data = root / "data"
    data.mkdir(mode=0o700)
    policy = root / "policy.yaml"
    shutil.copy(EXAMPLE_POLICY, policy)
    policy.chmod(0o600)
    lines = [
        f"policy: {policy}",
        f"data_dir: {data}",
        f"account: '{account}'",
        f"info_budget_file: {data / 'info-weight.json'}",
    ]
    if key_line:
        lines.append(f"agent_key_file: {data / 'agent.key'}")
    config = root / "config.yaml"
    config.write_text("\n".join(lines) + "\n")
    config.chmod(0o600)
    return config
