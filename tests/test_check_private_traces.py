import gzip
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "tools" / "check_private_traces.py"
PRIVATE_TRACE = "somebody-private-name"
HOME_PATH = str(Path("/Users") / "somebody" / "x")
ADDRESS = "0x" + "feedface" + "0" * 26 + "c0ffee"
MARKER = "check-private-traces: allow"


def run(
    cwd: Path,
    traces_file: Path | None,
    *paths: str,
    script: Path = SCRIPT,
    home: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "HL_GUARD_TRACES_FILE"}
    env["HOME"] = str(home or cwd)
    if traces_file is not None:
        env["HL_GUARD_TRACES_FILE"] = str(traces_file)
    return subprocess.run(
        [sys.executable, str(script), *paths],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def traces_file(tmp_path: Path) -> Path:
    path = tmp_path / "traces.txt"
    path.write_text(f"\n{PRIVATE_TRACE}\n")
    return path


@pytest.mark.parametrize(
    "line",
    [
        f"see {Path('/Users') / 'somebody' / 'x'} for details",
        f"see {Path('/home') / 'somebody' / 'x'} for details",
        f"lower {Path('/users') / 'somebody'}",
        f"ends at {Path('/Users') / 'somebody'}",
        '{"p": "\\/Users\\/somebody\\/x"}',
        "p = C:\\Users\\somebody",  # check-private-traces: allow
        "p = C:\\\\Users\\\\somebody",  # check-private-traces: allow
        "p = C:\\Users\\Игорь\\x",  # check-private-traces: allow
        "p = /home/\u0456\u0433\u043e\u0440/x",  # check-private-traces: allow
        "p = /Users/Ölaf/x",  # check-private-traces: allow
    ],
)
def test_home_path_fails(tmp_path: Path, line: str) -> None:
    (tmp_path / "leak.md").write_text(f"ok\n{line}\n")

    result = run(tmp_path, None, "leak.md")

    assert result.returncode == 1
    assert "leak.md:2: " in result.stdout
    assert "home path" in result.stdout


def test_private_trace_fails_without_printing_it(tmp_path: Path, traces_file: Path) -> None:
    (tmp_path / "config.toml").write_text(f'name = "{PRIVATE_TRACE.upper()}"\n')

    result = run(tmp_path, traces_file, "config.toml")

    assert result.returncode == 1
    assert "config.toml:1: private trace #1" in result.stdout
    assert PRIVATE_TRACE not in (result.stdout + result.stderr).lower()


def test_clean_file_passes(tmp_path: Path, traces_file: Path) -> None:
    (tmp_path / "clean.py").write_text('PATH = "~/.config/hl-guard/policy.toml"\nuid = 42\n')

    result = run(tmp_path, traces_file, "clean.py")

    assert result.returncode == 0
    assert "0 hits" in result.stdout


def test_default_traces_file_missing_runs_generic_rules_only(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(f'name = "{PRIVATE_TRACE}"\n')

    result = run(tmp_path, None, "config.toml", home=tmp_path)

    assert result.returncode == 0
    assert "0 private traces loaded" in result.stdout
    assert len(result.stderr.splitlines()) == 1
    assert "generic rules only" in result.stderr


def test_default_traces_file_dangling_symlink_says_so(tmp_path: Path) -> None:
    default = tmp_path / ".config" / "hl-guard-dev" / "traces.txt"
    default.parent.mkdir(parents=True)
    default.symlink_to(tmp_path / "gone.txt")
    (tmp_path / "leak.md").write_text(HOME_PATH + "\n")

    result = run(tmp_path, None, "leak.md", home=tmp_path)

    assert result.returncode == 1
    assert "leak.md:1: home path" in result.stdout
    assert len(result.stderr.splitlines()) == 1
    assert "broken symlink" in result.stderr


@pytest.mark.parametrize("configured", [True, False], ids=["configured", "default"])
def test_empty_traces_file_exits_2(tmp_path: Path, configured: bool) -> None:
    traces = tmp_path / ".config" / "hl-guard-dev" / "traces.txt"
    traces.parent.mkdir(parents=True)
    traces.write_text("\n  \n")
    (tmp_path / "config.toml").write_text("x = 1\n")

    result = run(tmp_path, traces if configured else None, "config.toml", home=tmp_path)

    assert result.returncode == 2
    assert len(result.stderr.splitlines()) == 1
    assert "no traces" in result.stderr


@pytest.mark.parametrize("make", [lambda p: None, Path.mkdir], ids=["missing", "directory"])
def test_configured_traces_file_unreadable_exits_2(
    tmp_path: Path, make: Callable[[Path], None]
) -> None:
    traces = tmp_path / "configured.txt"
    make(traces)
    (tmp_path / "config.toml").write_text("x = 1\n")

    result = run(tmp_path, traces, "config.toml")

    assert result.returncode == 2
    assert len(result.stderr.splitlines()) == 1
    assert "HL_GUARD_TRACES_FILE" in result.stderr


@pytest.mark.parametrize(
    "line",
    [
        f'address = "{ADDRESS}"',
        f"wallet_{ADDRESS}",
        f"{ADDRESS}_main",
    ],
)
def test_evm_address_fails(tmp_path: Path, line: str) -> None:
    (tmp_path / "config.toml").write_text(line + "\n")

    result = run(tmp_path, None, "config.toml")

    assert result.returncode == 1
    assert "config.toml:1: evm address" in result.stdout
    assert ADDRESS not in result.stdout


@pytest.mark.parametrize(
    "line",
    [
        f"tx = 0x{'ab' * 32}",
        f"sig = 0x{'cd' * 65}",
        "zero = 0x" + "0" * 40,
    ],
)
def test_hashes_and_the_zero_address_pass(tmp_path: Path, line: str) -> None:
    (tmp_path / "config.toml").write_text(line + "\n")

    result = run(tmp_path, None, "config.toml")

    assert result.returncode == 0, result.stdout


def test_allowed_address_passes(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    script = Path(shutil.copy(SCRIPT, tools))
    (tools / "allowed_addresses.txt").write_text(ADDRESS.upper() + "\n")
    (tmp_path / "config.toml").write_text(f'address = "{ADDRESS}"\n')

    result = run(tmp_path, None, "config.toml", script=script)

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(
    "line",
    [
        "chat_id = 123456789",  # check-private-traces: allow
        "chat-id: 123456789",  # check-private-traces: allow
        "telegram_user_id = 123456789",  # check-private-traces: allow
        "operator_uid = 123456789",  # check-private-traces: allow
        'TELEGRAM_CHAT_ID="-1001234567890"',  # check-private-traces: allow
        '"chat_id": -1001234567890',  # check-private-traces: allow
        "chat_id =                         123456789",  # check-private-traces: allow
        "TELEGRAM_ALLOWED_USERS=123456789",  # check-private-traces: allow
        "TELEGRAM_ALLOWED_USERS=123456789,987654321",  # check-private-traces: allow
        "TELEGRAM_HOME_CHANNEL=-1001234567890",  # check-private-traces: allow
        "telegram_user: 123456789",  # check-private-traces: allow
        "telegram_admin_id = 123456789",  # check-private-traces: allow
        '{"chat": {"id": 123456789}}',  # check-private-traces: allow
        '{"from": {"id": 123456789, "is_bot": false}}',  # check-private-traces: allow
        "{'chat': {'type': 'private', 'id': -1001234567890}}",  # check-private-traces: allow
    ],
)
def test_telegram_id_fails(tmp_path: Path, line: str) -> None:
    (tmp_path / "bot.env").write_text(line + "\n")

    result = run(tmp_path, None, "bot.env")

    assert result.returncode == 1
    assert "bot.env:1: telegram id" in result.stdout


@pytest.mark.parametrize(
    "line",
    [
        "uid = 1000000",
        "make(user_id=1234567)",
        "created_ms = 1760000000000",
        "chat_id_ts = 1760000000000",
        "chat_id = 1760000000000",
        "telegram_rate_limit_ms = 3000000",
        "Telegram supports up to 2000000 subscribers",
        "user_id=1234567",
        "telegram_message_id = 1234567",
        '{"message_id": 1234567, "update_id": 7654321}',
        "chat_completion_id: 12345678",
        '{"chat": {"id": 42}}',
        '{"order": {"id": 123456789}}',
    ],
)
def test_ordinary_numbers_pass(tmp_path: Path, line: str) -> None:
    (tmp_path / "test_x.py").write_text(line + "\n")

    result = run(tmp_path, None, "test_x.py")

    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize(("gap", "returncode"), [(40, 1), (41, 0)])
def test_telegram_key_allows_40_characters_between_the_words(
    tmp_path: Path, gap: int, returncode: int
) -> None:
    (tmp_path / "bot.env").write_text(f"chat{'x' * gap}id = {10**8}\n")

    result = run(tmp_path, None, "bot.env")

    assert result.returncode == returncode, result.stdout


@pytest.mark.parametrize("key", ["chat", "from"])
def test_telegram_id_in_pretty_printed_bot_api_json_fails(tmp_path: Path, key: str) -> None:
    user = {"id": 120000000 + 3456789, "is_bot": False}
    (tmp_path / "update.json").write_text(json.dumps({"update": 1, key: user}, indent=2) + "\n")

    result = run(tmp_path, None, "update.json")

    assert result.returncode == 1, result.stdout
    assert result.stdout.splitlines()[:-1] == ["update.json:4: telegram id"]


def test_allow_marker_silences_generic_rules_on_its_line(tmp_path: Path, traces_file: Path) -> None:
    route = "/" + "users/me"
    chat = '{"chat": {"id": ' + "123456789}}"
    (tmp_path / "routes.py").write_text(
        f'A = "{route}"  # {MARKER}\nB = "{route}"\nC = "{PRIVATE_TRACE}"  # {MARKER}\n'
        f"D = '{chat}'  # {MARKER}\n"
    )

    result = run(tmp_path, traces_file, "routes.py")

    assert result.returncode == 1
    assert "routes.py:1:" not in result.stdout
    assert "routes.py:2: home path" in result.stdout
    assert "routes.py:3: private trace #1" in result.stdout
    assert "routes.py:4:" not in result.stdout


def test_allow_marker_stops_at_a_carriage_return(tmp_path: Path) -> None:
    (tmp_path / "old_mac.txt").write_bytes(f"a  # {MARKER}\r{HOME_PATH}\r".encode())

    result = run(tmp_path, None, "old_mac.txt")

    assert result.returncode == 1
    assert "old_mac.txt:2: home path" in result.stdout


def test_non_utf8_file_is_still_scanned(tmp_path: Path) -> None:
    (tmp_path / "latin1.txt").write_bytes(b"caf\xe9 " + HOME_PATH.encode())

    result = run(tmp_path, None, "latin1.txt")

    assert result.returncode == 1
    assert "latin1.txt:1: home path" in result.stdout


@pytest.mark.parametrize(
    "encode",
    [
        lambda s: s.encode("utf-16"),
        lambda s: s.encode("utf-16-le"),
        lambda s: s.encode("utf-16-be"),
        lambda s: s.encode("utf-32"),
        lambda s: s.encode("utf-32-le"),
        lambda s: s.encode("utf-32-be"),
        lambda s: gzip.compress(s.encode()),
    ],
    ids=["utf-16-bom", "utf-16-le", "utf-16-be", "utf-32-bom", "utf-32-le", "utf-32-be", "gzip"],
)
def test_encoded_file_is_scanned(
    tmp_path: Path, traces_file: Path, encode: Callable[[str], bytes]
) -> None:
    (tmp_path / "blob.bin").write_bytes(encode(f"name: {PRIVATE_TRACE}\n"))

    result = run(tmp_path, traces_file, "blob.bin")

    assert result.returncode == 1
    assert "blob.bin:1: private trace #1" in result.stdout


@pytest.mark.parametrize(
    "disguise",
    [
        lambda s: "".join(chr(ord(c) + 0xFEE0) for c in s),
        lambda s: "\u00ad".join(s),
        lambda s: "\u200b".join(s),
    ],
    ids=["fullwidth", "soft-hyphen", "zero-width"],
)
def test_disguised_trace_fails(
    tmp_path: Path, traces_file: Path, disguise: Callable[[str], str]
) -> None:
    (tmp_path / "note.md").write_text(f"name: {disguise(PRIVATE_TRACE)}\n")

    result = run(tmp_path, traces_file, "note.md")

    assert result.returncode == 1
    assert "note.md:1: private trace #1" in result.stdout


@pytest.mark.parametrize(
    ("name", "rule"),
    [
        (f"{PRIVATE_TRACE}-notes.txt", "private trace #1"),
        (f"{PRIVATE_TRACE}_dir/a.txt", "private trace #1"),
        ("fixtures/chat_id_123456789.json", "telegram id"),  # check-private-traces: allow
        (f"wallet_{ADDRESS}.txt", "evm address"),
        (f"wallet_{ADDRESS} {MARKER}.txt", "evm address"),
    ],
)
def test_file_name_is_scanned(tmp_path: Path, traces_file: Path, name: str, rule: str) -> None:
    repo = tmp_path / "repo"
    (repo / name).parent.mkdir(parents=True)
    (repo / name).write_text("clean\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")

    for result in (run(repo, traces_file, name), run(repo, traces_file)):
        assert result.returncode == 1, result.stdout
        assert f":0: {rule}" in result.stdout
        assert PRIVATE_TRACE not in result.stdout
        assert ADDRESS not in result.stdout


@pytest.mark.parametrize(
    "name",
    [
        f"d/private-org/private-repo/wallet_{ADDRESS}.txt",
        str(Path("d") / "Users" / "bob" / f"wallet_{ADDRESS}.txt"),
    ],
    ids=["trace-across-segments", "home-path-across-segments"],
)
def test_file_name_hit_masks_the_whole_path(tmp_path: Path, name: str) -> None:
    traces = tmp_path / "traces.txt"
    traces.write_text("private-org/private-repo\n")
    repo = tmp_path / "repo"
    (repo / name).parent.mkdir(parents=True)
    (repo / name).write_text("clean\n")
    git(repo, "init", "-q")
    git(repo, "add", "-A")

    result = run(repo, traces)

    assert result.returncode == 1, result.stdout
    hits = result.stdout.splitlines()[:-1]
    assert hits, result.stdout
    assert all(line.startswith("*" * len(name) + ":0: ") for line in hits), hits


def test_committed_symlink_target_is_scanned(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "link").symlink_to(HOME_PATH)
    git(repo, "add", "link")

    result = run(repo, None)

    assert result.returncode == 1
    assert "link:1: home path" in result.stdout


def test_full_run_scans_tracked_and_untracked_but_not_ignored_files(
    tmp_path: Path, traces_file: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / ".gitignore").write_text("ignored.txt\n")
    for name in ("tracked.txt", "untracked.txt", "ignored.txt", "deleted.txt"):
        (repo / name).write_text(f"{PRIVATE_TRACE}\n")
    git(repo, "add", ".gitignore", "tracked.txt", "deleted.txt")
    (repo / "deleted.txt").unlink()

    result = run(repo, traces_file)

    assert result.returncode == 1, result.stderr
    assert sorted(line.split(":")[0] for line in result.stdout.splitlines()[:-1]) == [
        "tracked.txt",
        "untracked.txt",
    ]
    assert "3 files" in result.stdout


def bot_api_updates(count: int, indent: int | None) -> str:
    first_id = 10**8
    updates = [
        {
            "update_id": first_id + i,
            "message": {
                "message_id": i,
                "from": {"id": first_id + i, "is_bot": False, "first_name": "x"},
                "chat": {"id": first_id + i, "type": "private"},
                "date": 1_760_000_000,
                "text": "hello",
            },
        }
        for i in range(count)
    ]
    return json.dumps({"ok": True, "result": updates}, indent=indent)


def pretty_id_lines(text: str) -> set[int]:
    lines = text.splitlines()
    return {
        number
        for number, line in enumerate(lines, start=1)
        if line.strip().startswith('"id"')
        and lines[number - 2].strip() in ('"from": {', '"chat": {')
    }


@pytest.mark.parametrize("shape", ["compact", "pretty", "long key run"])
def test_large_file_scans_within_budget(tmp_path: Path, shape: str) -> None:
    if shape == "compact":
        text = bot_api_updates(25_000, None)
        expected = {1}
    elif shape == "pretty":
        text = bot_api_updates(18_000, 1)
        expected = pretty_id_lines(text)
    else:
        text = "chat" * 250_000 + "\nchat_id=" + str(10**8) + "\n"
        expected = {2}
    (tmp_path / "updates.json").write_text(text)

    try:
        result = run(tmp_path, None, "updates.json", timeout=3)
    except subprocess.TimeoutExpired:
        pytest.fail(f"{len(text) / 1e6:.1f} MB {shape} file took more than 3 s")

    assert result.returncode == 1
    hits = {line for line in result.stdout.splitlines() if line.startswith("updates.json:")}
    assert hits == {f"updates.json:{n}: telegram id" for n in expected}
