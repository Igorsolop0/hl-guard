"""The docs stay true to the code.

hl-guard has no --dry-run flag, so a command "runs dry" here by going through
the real argument parser and stopping before the command does anything. The
quick start blocks that only touch local files run for real in a temp HOME.
A block marked ``<!-- docs-test: skip <reason> -->`` needs a wallet, the
network, a client or a value only the reader has; it is parsed, not run.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from hl_guard import __version__
from hl_guard.cli.__main__ import main
from hl_guard.exchange import halt
from hl_guard.exchange.actions import FORBIDDEN_ACTION_TYPES
from hl_guard.policy.load import load_policy
from hl_guard.server.config import load_config

ROOT = Path(__file__).parent.parent
QUICKSTART = ROOT / "docs" / "quickstart.md"
DOCS = sorted(
    [
        ROOT / "README.md",
        ROOT / "SECURITY.md",
        ROOT / "CHANGELOG.md",
        ROOT / "CONTRIBUTING.md",
        *(ROOT / "docs").glob("*.md"),
        *(ROOT / "examples").glob("*.md"),
    ]
)
CLIENT_CONFIGS = sorted((ROOT / "examples").glob("*.json"))
SKIP = re.compile(r"<!-- docs-test: skip ([a-z-]+) -->")
SKIP_REASONS = {
    "network",
    "wallet",
    "client",
    "your-value",
    "needs-proposal",
    "needs-ledger",
    "needs-halt",
    "no-balance",
    "root",
}
ZERO_ADDRESS = "0x" + "0" * 40

# Every external link in the docs. A new one goes here after someone opened it.
EXTERNAL_URLS = {
    "https://astral.sh/uv/install.sh",
    "https://docs.astral.sh/uv/",
    "https://github.com/Igorsolop0/hl-guard.git",
    "https://hyperliquid.gitbook.io/hyperliquid-docs/onboarding/testnet-faucet",
}


@dataclass(frozen=True)
class Block:
    path: Path
    line: int
    lang: str
    text: str
    skip: str | None


def blocks(path: Path) -> list[Block]:
    found: list[Block] = []
    lines = path.read_text().splitlines()
    i = 0
    while i < len(lines):
        fence = re.match(r"\s*```(\w*)\s*$", lines[i])
        if fence is None:
            i += 1
            continue
        start, body = i, []
        i += 1
        while not re.match(r"\s*```\s*$", lines[i]):
            body.append(lines[i])
            i += 1
        marker = SKIP.search(lines[start - 1]) if start else None
        indent = len(lines[start]) - len(lines[start].lstrip())
        text = "\n".join(line[indent:] for line in body)
        found.append(Block(path, start + 1, fence.group(1), text, marker and marker.group(1)))
        i += 1
    return found


def commands(text: str) -> Iterator[list[str]]:
    """Shell commands of a block, continuation lines joined, heredoc bodies skipped."""
    joined = text.replace("\\\n", " ")
    heredoc: str | None = None
    for line in joined.splitlines():
        if heredoc is not None:
            heredoc = None if line.strip() == heredoc else heredoc
            continue
        here = re.search(r"<<'?(\w+)'?", line)
        if here:
            heredoc = here.group(1)
        if line.strip() and not line.lstrip().startswith("#"):
            yield shlex.split(line, comments=True)


def hl_guard_argv(argv: list[str], home: Path) -> list[str] | None:
    """The arguments after the last ``hl-guard`` or ``…/bin/hl-guard`` in ``argv``."""
    found = [i for i, arg in enumerate(argv) if arg == "hl-guard" or arg.endswith("/bin/hl-guard")]
    if not found:
        return None
    rest = argv[found[-1] + 1 :]
    return [arg.replace("PROPOSAL_ID", "0123456789ab").replace("~", str(home), 1) for arg in rest]


PROGRAMS = {
    "cat",
    "cd",
    "chmod",
    "claude",
    "cp",
    "curl",
    "dscl",
    "git",
    "grep",
    "mkdir",
    "rm",
    "sed",
    "sudo",
    "uv",
}
UV_RUN = {"hl-guard", "mypy", "pre-commit", "pytest", "python", "ruff"}
UV_COMMANDS = {"run", "sync", "--version"}


def program_problem(argv: list[str]) -> str | None:
    """Why ``argv`` names no program the docs may use, or None."""
    if re.fullmatch(r"[A-Z_]+=\S*", argv[0]):
        return None
    if argv[0] not in PROGRAMS:
        return f"unknown program {argv[0]!r}"
    if argv[0] == "uv":
        if len(argv) < 2 or argv[1] not in UV_COMMANDS:
            return f"unknown uv command {argv[1:2]!r}"
        if argv[1] == "run" and (len(argv) < 3 or argv[2] not in UV_RUN):
            return f"uv run of unknown program {argv[2:3]!r}"
    return None


class Parsed(Exception):
    def __init__(self, namespace: argparse.Namespace) -> None:
        self.namespace = namespace


def dry_run(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> argparse.Namespace:
    parse = argparse.ArgumentParser.parse_args

    def stop_after_parsing(self: argparse.ArgumentParser, *args: Any, **kwargs: Any) -> Any:
        raise Parsed(parse(self, *args, **kwargs))

    with monkeypatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, "parse_args", stop_after_parsing)
        try:
            main(argv)
        except Parsed as done:
            return done.namespace
        except SystemExit as exc:
            if exc.code == 0:
                return argparse.Namespace()
            pytest.fail(f"hl-guard {shlex.join(argv)} does not parse: exit {exc.code}")
    pytest.fail(f"hl-guard {shlex.join(argv)} did not reach the parser")


def all_blocks() -> list[Block]:
    return [block for path in DOCS for block in blocks(path)]


def shell_blocks() -> list[Block]:
    return [b for b in all_blocks() if b.lang == "sh"]


def test_every_skip_marker_names_a_known_reason() -> None:
    for path in DOCS:
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            if "docs-test" in line:
                marker = SKIP.search(line)
                assert marker and marker.group(1) in SKIP_REASONS, f"{path.name}:{n}: {line}"


def test_the_quick_start_lists_what_it_does_not_run(capsys: pytest.CaptureFixture[str]) -> None:
    skipped = [b for b in blocks(QUICKSTART) if b.lang == "sh" and b.skip]
    with capsys.disabled():
        for block in skipped:
            first = block.text.splitlines()[0]
            print(f"\nquickstart.md:{block.line} not run ({block.skip}): {first}", end="")
    assert {b.skip for b in skipped} <= SKIP_REASONS
    assert any(b.skip == "wallet" for b in skipped)


@pytest.mark.parametrize("block", shell_blocks(), ids=lambda b: f"{b.path.name}:{b.line}")
def test_every_hl_guard_command_in_the_docs_parses(
    block: Block, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for argv in commands(block.text):
        problem = program_problem(argv)
        assert problem is None, f"{block.path.name}:{block.line}: {problem}: {shlex.join(argv)}"
        cli = hl_guard_argv(argv, tmp_path)
        if cli is not None:
            dry_run(cli, monkeypatch)


@pytest.mark.parametrize(
    "line",
    ["uv run hl-gaurd serve --config c.yaml", "hl-gaurd --version", "uv runn hl-guard --version"],
)
def test_a_misspelled_program_fails(line: str) -> None:
    assert program_problem(shlex.split(line)) is not None


@pytest.mark.parametrize("path", CLIENT_CONFIGS, ids=lambda p: p.name)
def test_the_client_configs_start_a_command_that_parses(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = json.loads(path.read_text())["mcpServers"]["hl-guard"]
    cli = hl_guard_argv([server["command"], *server["args"]], tmp_path)
    assert cli is not None
    namespace = dry_run(cli, monkeypatch)
    assert namespace.command == "serve"
    assert namespace.expect_network == "testnet"


def _run_quickstart(home: Path) -> list[subprocess.CompletedProcess[str]]:
    venv_bin = Path(sys.executable).parent
    env = {
        "HOME": str(home),
        "PATH": f"{venv_bin}{os.pathsep}{os.environ['PATH']}",
        "HL_ACCOUNT": ZERO_ADDRESS,
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
    }
    uv_runs_in_this_venv = 'uv() { if [ "$1" = run ]; then shift; "$@"; else return 9; fi; }\n'
    done = []
    for block in blocks(QUICKSTART):
        if block.lang != "sh" or block.skip:
            continue
        script = "set -euo pipefail\n" + uv_runs_in_this_venv + block.text
        result = subprocess.run(
            ["bash", "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, f"quickstart.md:{block.line}\n{result.stderr}"
        done.append(result)
    return done


def test_the_quick_start_local_steps_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    outputs = [r.stdout.strip() for r in _run_quickstart(home)]

    assert f"hl-guard {__version__}" in outputs
    assert f"It prints `hl-guard {__version__}`." in QUICKSTART.read_text()
    assert any(o.startswith(f"policy {home}/.config/hl-guard/policy.yaml ok") for o in outputs)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(halt, "temp_dirs", lambda: [])
    config = load_config(home / ".config" / "hl-guard" / "serve.yaml")
    assert config.data_dir == home / ".hl-guard"
    assert config.agent_key_path == home / ".hl-guard" / "agent.key"
    load_policy(config.policy_path)

    printed = json.loads(next(o for o in outputs if o.lstrip().startswith("{")))
    server = printed["mcpServers"]["hl-guard"]
    assert server["args"][:3] == ["run", "--directory", str(ROOT)]
    cli = hl_guard_argv(server["args"], home)
    assert cli is not None
    namespace = dry_run(cli, monkeypatch)
    assert namespace.config == home / ".config" / "hl-guard" / "serve.yaml"


def _links(path: Path) -> Iterator[str]:
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    yield from re.findall(r"\]\(([^)\s]+)\)", text)


def _anchor(heading: str) -> str:
    text = re.sub(r"[`*]", "", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
    return {_anchor(h) for h in re.findall(r"^#+ (.+)$", text, flags=re.M)}


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path: Path) -> None:
    for link in _links(path):
        if re.match(r"[a-z]+:", link):
            continue
        target, _, anchor = link.partition("#")
        file = (path.parent / target).resolve() if target else path
        assert file.exists(), f"{path.name}: {link}"
        if anchor:
            assert anchor in _anchors(file), f"{path.name}: {link}"


def test_external_links_are_the_known_ones() -> None:
    found = {link for path in DOCS for link in _links(path) if re.match(r"https?:", link)}
    found |= {
        url
        for block in all_blocks()
        for url in re.findall(r"https?://[^\s\"'<>)]+", block.text)
        if "127.0.0.1" not in url
    }
    assert found == EXTERNAL_URLS


CLAIM = re.compile(
    r"\b(profit\w*|returns?|gains?|earn\w*|alpha|edge|roi|apy|yield\w*|outperform\w*|"
    r"beat the market|make money|guarantee\w*)\b",
    re.I,
)
NEGATION = re.compile(r"\b(not|no|never)\b", re.I)
TERMS = re.compile(r"take[-_ ]profit|profit side|pre-alpha", re.I)


@pytest.mark.parametrize("path", DOCS, ids=lambda p: str(p.relative_to(ROOT)))
def test_the_docs_make_no_profit_or_return_claims(path: Path) -> None:
    for n, line in enumerate(path.read_text().splitlines(), start=1):
        hit = CLAIM.search(TERMS.sub("", line))
        assert hit is None or NEGATION.search(line), f"{path.name}:{n}: {line.strip()}"


@pytest.mark.parametrize("path", [ROOT / "README.md", QUICKSTART], ids=lambda p: p.name)
def test_the_docs_say_that_hl_guard_places_no_stop(path: Path) -> None:
    lines = (line.removeprefix(">") for line in path.read_text().splitlines())
    text = " ".join(" ".join(lines).replace("*", "").split())
    assert "does not place stop-loss or take-profit orders" in text


def _e2e() -> ModuleType:
    spec = importlib.util.spec_from_file_location("e2e", ROOT / "tools" / "e2e" / "e2e.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e"] = module
    spec.loader.exec_module(module)
    return module


def _e2e_config(root: Path, *, mode: str | None, network: str = "testnet") -> Path:
    data = root / "data"
    data.mkdir(mode=0o700)
    policy = yaml.safe_load((ROOT / "examples" / "policy.yaml").read_text())
    (root / "policy.yaml").write_text(yaml.safe_dump({**policy, "network": network}))
    (root / "policy.yaml").chmod(0o600)
    if mode is not None:
        (data / "mode").write_text(f"{mode}\n")
        (data / "mode").chmod(0o600)
    config = root / "serve.yaml"
    config.write_text(
        f"policy: policy.yaml\ndata_dir: data\naccount: '{ZERO_ADDRESS}'\n"
        "agent_key_file: data/agent.key\ninfo_budget_file: data/budget.json\n"
    )
    config.chmod(0o600)
    return config


@pytest.mark.parametrize(
    ("mode", "network"), [("mainnet", "mainnet"), ("mainnet", "testnet"), (None, "mainnet")]
)
def test_the_e2e_refuses_anything_but_testnet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str | None, network: str
) -> None:
    monkeypatch.setattr(halt, "temp_dirs", lambda: [])
    e2e = _e2e()
    config = _e2e_config(tmp_path, mode=mode, network=network)
    with pytest.raises(e2e.Refused, match="e2e refused"):
        e2e.preflight(config, "BTC")


def test_the_e2e_runs_its_preflight_on_testnet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(halt, "temp_dirs", lambda: [])
    e2e = _e2e()
    lines = e2e.preflight(_e2e_config(tmp_path, mode="testnet"), "BTC")
    assert lines[0] == "network testnet"


def test_the_e2e_will_not_close_a_position_it_did_not_open(tmp_path: Path) -> None:
    e2e = _e2e()
    market = {"name": "BTC", "mark_px": "80000", "sz_decimals": 5}
    read = [
        e2e.Result("markets", False, {"network": "testnet", "markets": [market]}),
        e2e.Result("positions", False, [{"coin": "BTC", "size": "0.001"}]),
    ]
    policy = load_policy(_e2e_config(tmp_path, mode=None).parent / "policy.yaml")
    with pytest.raises(e2e.Refused, match="holds a BTC position"):
        e2e.plan(read, "BTC", policy)
    planned = e2e.plan([read[0], e2e.Result("positions", False, [])], "BTC", policy)
    assert 10 < planned.small * planned.limit_px <= policy.per_order_usd_max
    assert planned.big * planned.limit_px > policy.per_order_usd_max


def test_the_e2e_script_parses() -> None:
    subprocess.run(["bash", "-n", str(ROOT / "tools" / "e2e" / "run.sh")], check=True)


def _flat(path: Path) -> str:
    return " ".join(path.read_text().replace("*", "").split())


def _server_appended_kinds() -> set[str]:
    kinds: set[str] = set()
    for source in (ROOT / "src" / "hl_guard" / "server").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "_record")
                and node.args
                and isinstance(node.args[0], ast.Attribute)
                and isinstance(node.args[0].value, ast.Name)
                and node.args[0].value.id == "Kind"
            ):
                kinds.add(node.args[0].attr)
    return kinds


def test_the_threat_model_says_which_ledger_records_a_tool_can_cause() -> None:
    kinds = _server_appended_kinds()
    assert {"PROPOSAL", "DECISION", "ORDER", "DAY_START"} <= kinds
    assert not kinds & {"APPROVAL", "UNLATCH", "UNHALT"}
    text = _flat(ROOT / "docs" / "threat-model.md")
    assert "No tool writes the policy, the ledger" not in text
    assert "No tool writes an approval, a latch clear or an unhalt record" in text


def test_the_docs_claim_no_transfer_block_beyond_what_hl_guard_builds() -> None:
    for path in DOCS:
        assert "cannot withdraw or transfer" not in _flat(path), path.name
    assert {"vaultTransfer", "subAccountTransfer", "usdSend", "withdraw3"} <= set(
        FORBIDDEN_ACTION_TYPES
    )
    assert "never builds a withdrawal, a transfer" in _flat(ROOT / "docs" / "threat-model.md")


def test_the_loss_limits_say_they_count_only_hl_guards_own_fills() -> None:
    policy = _flat(ROOT / "docs" / "policy.md")
    assert "count only the fills of orders hl-guard placed" in policy
    assert "fill is a line" not in _flat(ROOT / "README.md")


def test_the_policy_folder_rule_names_the_sticky_bit() -> None:
    assert "a policy in a world-writable folder without the sticky bit" in _flat(
        ROOT / "docs" / "policy.md"
    )


def test_the_mode_file_rule_is_the_one_serve_checks() -> None:
    text = _flat(ROOT / "docs" / "policy.md")
    assert "`mainnet`, mode 600" not in text
    assert "`mainnet`, that only you can write" in text


def test_the_readme_ties_the_wait_to_approval_always() -> None:
    assert "By default every entry waits" not in _flat(ROOT / "README.md")


def test_the_proposal_status_text_names_every_status() -> None:
    from hl_guard.approval.model import Status
    from hl_guard.server.tools_write import WRITE_TOOLS

    text = WRITE_TOOLS["proposal_status"].description
    assert all(status.value in text for status in Status), text
