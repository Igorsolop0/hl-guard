from __future__ import annotations

import math
import os
import re
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from eth_account import Account
from eth_account.signers.local import LocalAccount

from hl_guard.exchange.halt import HaltFlag, HaltPathError
from hl_guard.exchange.network import NETWORKS
from hl_guard.policy.load import PolicyError, _parse_yaml
from hl_guard.server.info import INFO_TIMEOUT_S_MAX

CONFIG_BYTES_MAX = 4096
MODE_FILENAME = "mode"
LEDGER_FILENAME = "ledger.jsonl"
SERVE_LOCK = "serve.lock"
BUDGET_FILENAME = "info-weight.json"
INFO_WEIGHT_PER_MIN = 300
INFO_WEIGHT_PER_MIN_RANGE = (20, 600)
INFO_TIMEOUT_S = 10.0
INFO_TIMEOUT_S_RANGE = (1.0, INFO_TIMEOUT_S_MAX)
_KEYS = frozenset({"policy", "data_dir", "account"})
APPROVAL_TTL_S = 120
APPROVAL_TTL_S_RANGE = (10, 900)
AGENT_KEY_BYTES_MAX = 128
_OPTIONAL_KEYS = frozenset(
    {
        "info_weight_per_min",
        "info_timeout_s",
        "info_budget_file",
        "agent_key_file",
        "approval_ttl_s",
    }
)
_AGENT_KEY = re.compile(r"(0x)?[0-9a-fA-F]{64}")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")


class ConfigError(ValueError):
    pass


def lock_dir() -> Path:
    """The per-user dir of the account locks. It ignores XDG_STATE_HOME and
    info_budget_file, so every config of one user locks in the same place."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "hl-guard"
    return Path.home() / ".local" / "state" / "hl-guard"


def default_budget_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        state = os.environ.get("XDG_STATE_HOME", "")
        base = Path(state) if os.path.isabs(state) else Path.home() / ".local" / "state"
    return base / "hl-guard" / BUDGET_FILENAME


@dataclass(frozen=True)
class ServeConfig:
    policy_path: Path
    data_dir: Path
    account: str
    budget_path: Path
    info_weight_per_min: int = INFO_WEIGHT_PER_MIN
    info_timeout_s: float = INFO_TIMEOUT_S
    agent_key_path: Path | None = None
    approval_ttl_s: int = APPROVAL_TTL_S

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / LEDGER_FILENAME


def _owner_only_file(path: Path, label: str, size_max: int) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise ConfigError(f"cannot read {label} {path}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f"{label} {path} is not a regular file")
        if st.st_uid != os.geteuid():
            raise ConfigError(f"{label} {path} belongs to uid {st.st_uid}, not you")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ConfigError(
                f"{label} {path} has mode {stat.S_IMODE(st.st_mode):04o}: group or others can "
                f"write it; run chmod 600 {path}"
            )
        data = f.read(size_max + 1)
    if len(data) > size_max:
        raise ConfigError(f"{label} {path} is larger than {size_max} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"{label} {path} is not UTF-8") from None


def _path(v: Any, key: str, base: Path) -> Path:
    if not isinstance(v, str) or not v:
        raise ConfigError(f"config {key} must be a path string, got {v!r}")
    path = Path(v).expanduser()
    return path if path.is_absolute() else base / path


def _weight(v: Any) -> int:
    low, high = INFO_WEIGHT_PER_MIN_RANGE
    if type(v) is not int or not low <= v <= high:
        raise ConfigError(f"config info_weight_per_min must be an integer from {low} to {high}")
    return v


def _seconds(v: Any) -> float:
    low, high = INFO_TIMEOUT_S_RANGE
    if type(v) not in (int, float) or not math.isfinite(v) or not low <= v <= high:
        raise ConfigError(f"config info_timeout_s must be a number of seconds from {low} to {high}")
    return float(v)


def _ttl(v: Any) -> int:
    low, high = APPROVAL_TTL_S_RANGE
    if type(v) is not int or not low <= v <= high:
        raise ConfigError(f"config approval_ttl_s must be an integer from {low} to {high}")
    return v


def parse_config(raw: Any, base: Path) -> ServeConfig:
    if not isinstance(raw, Mapping):
        raise ConfigError("config must be a mapping with keys account, data_dir and policy")
    missing = sorted(_KEYS - set(raw))
    unknown = sorted(set(raw) - _KEYS - _OPTIONAL_KEYS)
    if missing or unknown:
        raise ConfigError(f"config: missing keys {missing}, unknown keys {unknown}")
    account = raw["account"]
    if not isinstance(account, str) or not _ADDRESS.fullmatch(account):
        raise ConfigError(f"config account must be 0x and 40 hex digits, got {account!r}")
    return ServeConfig(
        policy_path=_path(raw["policy"], "policy", base),
        data_dir=_path(raw["data_dir"], "data_dir", base),
        account=account.lower(),
        budget_path=_path(raw["info_budget_file"], "info_budget_file", base)
        if "info_budget_file" in raw
        else default_budget_path(),
        info_weight_per_min=_weight(raw.get("info_weight_per_min", INFO_WEIGHT_PER_MIN)),
        info_timeout_s=_seconds(raw.get("info_timeout_s", INFO_TIMEOUT_S)),
        agent_key_path=_path(raw["agent_key_file"], "agent_key_file", base)
        if "agent_key_file" in raw
        else None,
        approval_ttl_s=_ttl(raw.get("approval_ttl_s", APPROVAL_TTL_S)),
    )


def load_config(path: Path) -> ServeConfig:
    text = _owner_only_file(path, "config", CONFIG_BYTES_MAX)
    try:
        raw = _parse_yaml(text)
    except PolicyError as exc:
        raise ConfigError(f"config {path}: {exc}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"config {path} is not valid YAML: {exc}") from None
    config = parse_config(raw, path.absolute().parent)
    if not config.data_dir.is_dir():
        raise ConfigError(
            f"data dir {config.data_dir} is not a directory; create it with "
            f"mkdir -m 700 {config.data_dir}"
        )
    _private_dir(config.data_dir, "data dir")
    try:
        HaltFlag(config.data_dir)
    except HaltPathError as exc:
        raise ConfigError(str(exc)) from None
    budget_dir = config.budget_path.parent
    try:
        budget_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"cannot create the budget dir {budget_dir}: {exc.strerror}") from None
    _private_dir(budget_dir, "budget dir")
    return config


def _private_dir(path: Path, label: str) -> None:
    st = path.stat()
    if st.st_uid != os.geteuid():
        raise ConfigError(f"{label} {path} belongs to uid {st.st_uid}, not you")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            f"{label} {path} has mode {stat.S_IMODE(st.st_mode):04o}: group or others can "
            f"write it; run chmod 700 {path}"
        )


def load_agent_key(path: Path) -> LocalAccount:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ConfigError(f"cannot read the agent key {path}: {exc.strerror}") from None
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid():
            raise ConfigError(f"the agent key {path} must be a regular file you own")
        if st.st_mode & 0o077:
            raise ConfigError(
                f"the agent key {path} has mode {stat.S_IMODE(st.st_mode):04o}; "
                f"run chmod 600 {path}"
            )
        data = f.read(AGENT_KEY_BYTES_MAX + 1)
    text = data.decode("ascii", errors="replace").strip()
    if len(data) > AGENT_KEY_BYTES_MAX or not _AGENT_KEY.fullmatch(text):
        raise ConfigError(f"the agent key {path} must hold 64 hex digits, optionally after 0x")
    try:
        account: LocalAccount = Account.from_key(text)
    except ValueError:
        raise ConfigError(f"the agent key {path} is not a valid secp256k1 private key") from None
    return account


def read_mode(data_dir: Path) -> str | None:
    path = data_dir / MODE_FILENAME
    if not os.path.lexists(path):
        return None
    text = _owner_only_file(path, "mode file", 64).strip()
    if text not in NETWORKS:
        raise ConfigError(f"mode file {path} says {text!r:.40}; allowed: {sorted(NETWORKS)}")
    return text


def resolve_network(policy_network: str, mode: str | None, expect: str | None) -> str:
    network = mode or "testnet"
    if expect is not None and expect != network:
        raise ConfigError(
            f"--expect-network {expect} does not match the data dir mode {network}"
            + ("" if mode else " (no mode file: testnet)")
        )
    if network == "mainnet" and expect != "mainnet":
        raise ConfigError("the mode file says mainnet: start with --expect-network mainnet")
    if policy_network != network:
        raise ConfigError(f"the policy says {policy_network} but the data dir mode is {network}")
    return network
