from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hl_guard.exchange import network
from hl_guard.keys.agent_key import KeySlot, Record, key_address, save
from hl_guard.ledger.ledger import Kind, Ledger
from hl_guard.server.app import StartupRefused, boot
from server.fake_info import write_config
from server.venue import configure


@pytest.fixture()
def config(tmp_path: Path, durable: None) -> Path:
    return write_config(tmp_path)


def mode(config: Path, text: str, perms: int = 0o600) -> None:
    path = config.parent / "data" / "mode"
    path.write_text(text)
    path.chmod(perms)


def refused(config: Path, expect: str | None = None) -> str:
    with pytest.raises(StartupRefused) as info:
        boot(config, expect)
    return str(info.value)


def test_no_mode_file_boots_testnet(config: Path) -> None:
    runtime = boot(config, None)

    assert runtime.network == "testnet"
    assert network.enabled().name == "testnet"


def test_expect_testnet_matches_the_default(config: Path) -> None:
    assert boot(config, "testnet").network == "testnet"


def test_mainnet_needs_the_mode_file_and_the_flag(tmp_path: Path, durable: None) -> None:
    config = write_config(tmp_path)
    policy = tmp_path / "policy.yaml"
    policy.write_text(policy.read_text().replace("network: testnet", "network: mainnet"))

    assert "expect-network mainnet does not match" in refused(config, "mainnet")
    mode(config, "mainnet\n")
    assert "start with --expect-network mainnet" in refused(config)
    assert boot(config, "mainnet").network == "mainnet"
    assert network.enabled().name == "mainnet"


def test_policy_network_must_match_the_mode(config: Path) -> None:
    mode(config, "mainnet")

    assert "policy says testnet" in refused(config, "mainnet")


@pytest.mark.parametrize(
    ("text", "perms", "problem"),
    [("mainnet", 0o620, "group or others"), ("devnet", 0o600, "allowed")],
)
def test_unsafe_or_unknown_mode_file_is_refused(
    config: Path, text: str, perms: int, problem: str
) -> None:
    mode(config, text, perms)

    assert problem in refused(config)


def test_data_dir_under_a_temp_dir_is_refused(tmp_path: Path) -> None:
    assert "temp dir" in refused(write_config(tmp_path))


def test_missing_data_dir_is_refused(tmp_path: Path, durable: None) -> None:
    assert "mkdir -m 700" in refused(write_config(tmp_path, data_dir="nowhere"))


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (lambda t: t + "network: mainnet\n", "unknown keys ['network']"),
        (lambda t: t.replace("account: '0x", "account: '0y"), "40 hex digits"),
        (lambda t: t.replace("data_dir: data\n", ""), "missing keys ['data_dir']"),
        (lambda t: "base: &a data\n" + t, "anchors"),
        (lambda t: t + "info_weight_per_min: 19\n", "info_weight_per_min must be an integer"),
        (lambda t: t + "info_weight_per_min: 601\n", "from 20 to 600"),
        (lambda t: t + "info_weight_per_min: true\n", "info_weight_per_min must be an integer"),
        (lambda t: t + "info_timeout_s: 0\n", "info_timeout_s must be a number of seconds"),
        (lambda t: t + "info_timeout_s: .inf\n", "info_timeout_s must be a number of seconds"),
    ],
)
def test_bad_config_is_refused(config: Path, edit: object, problem: str) -> None:
    assert callable(edit)
    config.write_text(edit(config.read_text()))

    assert problem in refused(config)


def test_info_limits_default_to_300_weight_and_10_seconds(config: Path) -> None:
    serve = boot(config, None).config

    assert (serve.info_weight_per_min, serve.info_timeout_s) == (300, 10.0)


def test_info_limits_come_from_the_config(config: Path) -> None:
    config.write_text(config.read_text() + "info_weight_per_min: 60\ninfo_timeout_s: 2.5\n")

    serve = boot(config, None).config

    assert (serve.info_weight_per_min, serve.info_timeout_s) == (60, 2.5)


def test_group_writable_config_is_refused(config: Path) -> None:
    config.chmod(0o620)

    assert "group or others" in refused(config)


def test_policy_breaking_a_ceiling_is_refused(tmp_path: Path, durable: None) -> None:
    config = write_config(tmp_path)
    policy = tmp_path / "policy.yaml"
    policy.write_text(policy.read_text().replace("leverage_max: 1", "leverage_max: 9"))

    assert "ceiling" in refused(config)


def _ledger(config: Path) -> Path:
    path = config.parent / "data" / "ledger.jsonl"
    led = Ledger(path)
    for n in range(1, 5):
        led.append(Kind.DECISION, {"n": n})
    return path


def test_verified_ledger_boots(config: Path) -> None:
    _ledger(config)

    assert boot(config, None).network == "testnet"


def test_ledger_tampered_before_the_tail_is_refused(config: Path) -> None:
    path = _ledger(config)
    assert Ledger(path).torn_tail() is None
    path.write_text(path.read_text().replace('"n":1', '"n":7'))

    assert "failed verification" in refused(config)


def test_torn_tail_is_refused_until_repair(config: Path) -> None:
    path = _ledger(config)
    with path.open("a") as f:
        f.write('{"seq":3')

    assert "hl-guard ledger repair" in refused(config)
    Ledger(path).repair()
    assert boot(config, None).network == "testnet"


def test_ledger_others_can_read_is_refused(config: Path) -> None:
    _ledger(config).chmod(0o644)

    assert "chmod 600" in refused(config)


def test_serve_cli_exits_2_with_one_line_on_refusal(tmp_path: Path) -> None:
    config = write_config(tmp_path)

    result = subprocess.run(
        [sys.executable, "-m", "hl_guard.cli", "serve", "--config", str(config)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 2
    assert result.stderr.startswith("hl-guard serve refused: ")
    assert "temp dir" in result.stderr
    assert len(result.stderr.splitlines()) == 1


@pytest.mark.parametrize("perms", [0o720, 0o702])
def test_a_data_dir_others_can_write_is_refused(config: Path, perms: int) -> None:
    (config.parent / "data").chmod(perms)

    assert refused(config).startswith(f"data dir {config.parent / 'data'} has mode")


def default_config(tmp_path: Path) -> Path:
    tmp_path.mkdir(exist_ok=True)
    config = write_config(tmp_path)
    config.write_text(config.read_text().replace("info_budget_file: data/info-weight.json\n", ""))
    return config


def test_the_default_budget_file_is_one_per_user_on_macos(
    tmp_path: Path, durable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    runtime = boot(default_config(tmp_path / "a"), None)

    expected = tmp_path / "home" / "Library" / "Application Support" / "hl-guard"
    assert runtime.config.budget_path == expected / "info-weight.json"
    assert expected.stat().st_mode & 0o777 == 0o700
    other = boot(default_config(tmp_path / "b"), None)
    assert other.config.budget_path == runtime.config.budget_path


@pytest.mark.parametrize(
    ("xdg", "where"), [("state", "state"), ("", ".local/state"), ("relative", ".local/state")]
)
def test_the_default_budget_file_on_linux_follows_xdg_state_home(
    tmp_path: Path, durable: None, monkeypatch: pytest.MonkeyPatch, xdg: str, where: str
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "home" / xdg) if xdg == "state" else xdg)

    runtime = boot(default_config(tmp_path), None)

    assert runtime.config.budget_path == tmp_path / "home" / where / "hl-guard" / "info-weight.json"


def test_info_budget_file_overrides_the_default(tmp_path: Path, durable: None) -> None:
    config = default_config(tmp_path)
    config.write_text(config.read_text() + "info_budget_file: shared/w.json\n")

    assert boot(config, None).config.budget_path == tmp_path / "shared" / "w.json"
    assert (tmp_path / "shared").stat().st_mode & 0o777 == 0o700


def test_a_budget_dir_others_can_write_is_refused(tmp_path: Path, durable: None) -> None:
    config = default_config(tmp_path)
    config.write_text(config.read_text() + "info_budget_file: shared/w.json\n")
    (tmp_path / "shared").mkdir(mode=0o700)
    (tmp_path / "shared").chmod(0o770)

    assert "budget dir" in refused(config)


def test_an_agent_that_init_refused_does_not_serve(tmp_path: Path, durable: None) -> None:
    config = configure(tmp_path)
    slot = KeySlot(tmp_path / "agent.key")
    address = key_address(slot.key_path)
    assert address is not None
    save(slot, Record.first("testnet", address).refusing(address))

    assert f"init refused the agent {address}" in refused(config)


def test_an_agent_that_init_approved_serves(tmp_path: Path, durable: None) -> None:
    config = configure(tmp_path)
    slot = KeySlot(tmp_path / "agent.key")
    address = key_address(slot.key_path)
    assert address is not None
    save(slot, Record.first("testnet", address).confirmed(address, 2**40))

    assert boot(config, None).agent is not None
