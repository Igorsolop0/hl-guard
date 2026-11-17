import json
import os
import secrets
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
CONFIG = ROOT / ".gitleaks.toml"
GITLEAKS = shutil.which("gitleaks")

if GITLEAKS is None and os.environ.get("HL_GUARD_REQUIRE_GITLEAKS"):
    raise RuntimeError("HL_GUARD_REQUIRE_GITLEAKS is set but gitleaks is not on PATH")

pytestmark = pytest.mark.skipif(GITLEAKS is None, reason="gitleaks binary not on PATH")


def findings(tmp_path: Path, files: dict[str, str]) -> list[str]:
    repo = tmp_path / "repo"
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "-c", "commit.gpgsign=false", "commit", "-qm", "t"], check=True)
    report = tmp_path / "report.json"
    result = subprocess.run(
        [
            "gitleaks",
            "git",
            "--config",
            str(CONFIG),
            "--no-banner",
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(report),
            str(repo),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 1), result.stderr
    return [f["RuleID"] for f in json.loads(report.read_text())]


@pytest.mark.parametrize(
    ("name", "template"),
    [
        ("planted.py", 'agent_private_key = "{key}"\n'),
        ("planted.py", 'WALLET_SECRET = "0x{key}"\n'),
        ("planted.py", 'X = "{key}"\n'),
        ("planted.txt", "0x{key}\n"),
        ("private_key.txt", "{key}\n"),
        ("a.env", 'PK = "0x{key}"\n'),
        ("uv.lock.txt", 'hash = "sha256:{key}"\n'),
        ("foo/uv.lock", 'hash = "sha256:{key}"\n'),
        ("uv.lock", 'signer = "{key}"\n'),
        ("notes.txt", "GITLEAKS_SHA256: {key}\n"),
        (".github/workflows/other.yml", "      GITLEAKS_SHA256: {key}\n"),
    ],
)
def test_hex_key_is_found(tmp_path: Path, name: str, template: str) -> None:
    key = secrets.token_hex(32)

    assert "hex-private-key" in findings(tmp_path, {name: template.format(key=key)})


@pytest.mark.parametrize(
    ("name", "template"),
    [
        ("uv.lock", 'wheels = [{{ url = "x.whl", hash = "sha256:{key}" }}]\n'),
        (".github/workflows/ci.yml", "    env:\n      GITLEAKS_SHA256: {key}\n"),
    ],
)
def test_allowlisted_hash_is_not_found(tmp_path: Path, name: str, template: str) -> None:
    key = secrets.token_hex(32)

    assert findings(tmp_path, {name: template.format(key=key)}) == []


def test_real_lockfile_and_ci_pin_are_not_found(tmp_path: Path) -> None:
    files = {name: (ROOT / name).read_text() for name in ("uv.lock", ".github/workflows/ci.yml")}

    assert findings(tmp_path, files) == []
