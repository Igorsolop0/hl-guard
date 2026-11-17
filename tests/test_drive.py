import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SERVER = [sys.executable, str(ROOT / "tests" / "echo_server.py")]


def drive(
    tmp_path: Path, scenario: object, *options: str, server: list[str] = SERVER
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "scenario.json"
    path.write_text(scenario if isinstance(scenario, str) else json.dumps(scenario))
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / "drive.py"), *options, str(path), "--", *server],
        capture_output=True,
        text=True,
        timeout=60,
    )


def sent(stdout: str, method: str) -> dict[str, object]:
    line = next(line for line in stdout.splitlines() if line.startswith(f">> {method} "))
    payload: dict[str, object] = json.loads(line.removeprefix(f">> {method} "))
    return payload


def assert_one_line_harness_error(result: subprocess.CompletedProcess[str], text: str) -> None:
    assert result.returncode == 2
    assert result.stderr.startswith("drive: harness error: ")
    assert len(result.stderr.splitlines()) == 1, result.stderr
    assert text in result.stderr
    assert "Traceback" not in result.stderr


def test_drive_prints_initialized_tools_and_echo(tmp_path: Path) -> None:
    result = drive(tmp_path, [{"tool": "echo", "arguments": {"text": "ping-42"}}])

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "initialized" in lines
    assert "tools: echo, fail, sleep, pid, ask_client" in lines
    response = next(line for line in lines if line.startswith("<< tools/call "))
    payload = json.loads(response.removeprefix("<< tools/call "))
    assert payload["content"] == [{"type": "text", "text": "ping-42"}]
    assert payload["isError"] is False


def test_drive_prints_the_real_requests(tmp_path: Path) -> None:
    result = drive(tmp_path, [{"tool": "echo", "arguments": {"text": "x"}}])

    assert result.returncode == 0, result.stderr
    initialize = sent(result.stdout, "initialize")
    assert set(initialize) >= {"protocolVersion", "capabilities", "clientInfo"}
    assert ">> notifications/initialized {}" in result.stdout.splitlines()
    assert sent(result.stdout, "tools/list") == {}
    assert sent(result.stdout, "tools/call") == {"name": "echo", "arguments": {"text": "x"}}


def test_drive_prints_server_requests_and_client_responses(tmp_path: Path) -> None:
    result = drive(tmp_path, [{"tool": "ask_client", "arguments": {}}])

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert any(line.startswith("<< ping ") for line in lines), result.stdout
    assert any(line.startswith("<< elicitation/create ") for line in lines), result.stdout
    responses = [
        json.loads(line.removeprefix(">> response "))
        for line in lines
        if line.startswith(">> response ")
    ]
    assert [set(r) for r in responses] == [{"id", "result"}, {"id", "error"}], responses
    assert responses[0]["result"] == {}
    assert "Elicitation not supported" in json.dumps(responses[1]["error"])


def test_tool_error_exits_1_after_running_every_call(tmp_path: Path) -> None:
    result = drive(
        tmp_path,
        [
            {"tool": "fail", "arguments": {"reason": "nope"}},
            {"tool": "echo", "arguments": {"text": "after"}},
        ],
    )

    assert result.returncode == 1, result.stderr
    assert '"text": "after"' in result.stdout.splitlines()[-1]


def test_hung_tool_times_out_with_exit_2(tmp_path: Path) -> None:
    start = time.monotonic()
    result = drive(tmp_path, [{"tool": "sleep", "arguments": {"seconds": 30}}], "--timeout", "1")

    assert time.monotonic() - start < 15
    assert_one_line_harness_error(result, "timed out after 1 s")


@pytest.mark.parametrize(
    ("scenario", "message"),
    [
        ("{not json", "JSONDecodeError"),
        ({"tool": "echo"}, "scenario must be a JSON list"),
        ([{"tool": "echo", "args": {}}], "scenario call 0: expected keys"),
        ([{"run": []}], "scenario call 0: 'run' must be a non-empty list"),
        ([{"run": ["echo", 1]}], "scenario call 0: 'run' must be a non-empty list"),
        ([{"wait": -1}], "scenario call 0: 'wait' must be 0 to 3600 seconds"),
        ([{"wait": True}], "scenario call 0: 'wait' must be 0 to 3600 seconds"),
        ([{"run": ["true", "{id}"]}], "no tool result had an id"),
        ([{"tool": 7}], "scenario call 0: 'tool' must be a string"),
    ],
)
def test_bad_scenario_exits_2(tmp_path: Path, scenario: object, message: str) -> None:
    assert_one_line_harness_error(drive(tmp_path, scenario), message)


@pytest.mark.parametrize("value", ["nan", "inf", "0", "-1"])
def test_bad_timeout_exits_2(tmp_path: Path, value: str) -> None:
    result = drive(tmp_path, [], "--timeout", value)

    assert result.returncode == 2
    assert "argument --timeout" in result.stderr


@pytest.mark.parametrize("gaps", [[], [0.3], [0.02] * 250], ids=["one", "two", "storm"])
def test_sigterm_shuts_the_server_down(tmp_path: Path, gaps: list[float]) -> None:
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            [{"tool": "pid", "arguments": {}}, {"tool": "sleep", "arguments": {"seconds": 30}}]
        )
    )
    command = [sys.executable, str(ROOT / "tools" / "drive.py"), "--timeout", "40", str(scenario)]
    proc = subprocess.Popen(
        [*command, "--", *SERVER], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    assert proc.stdout is not None
    server_pid = 0
    try:
        for line in proc.stdout:
            if line.startswith("<< tools/call ") and not server_pid:
                server_pid = int(
                    json.loads(line.removeprefix("<< tools/call "))["content"][0]["text"]
                )
            if server_pid and line.startswith(">> tools/call ") and '"sleep"' in line:
                break
        proc.send_signal(signal.SIGTERM)
        for gap in gaps:
            time.sleep(gap)
            if proc.poll() is not None:
                break
            with suppress(ProcessLookupError):
                proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=20)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and alive(server_pid):
            time.sleep(0.1)
        assert not alive(server_pid), "server outlived drive.py"
        assert proc.returncode == 2
        assert proc.stderr is not None
        assert proc.stderr.read().strip() == "drive: harness error: terminated by SIGTERM"
    finally:
        proc.kill()
        if server_pid:
            with suppress(ProcessLookupError):
                os.kill(server_pid, signal.SIGKILL)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_missing_server_exits_2(tmp_path: Path) -> None:
    result = drive(tmp_path, [], server=[str(tmp_path / "no-such-server")])

    assert_one_line_harness_error(result, "FileNotFoundError")


def test_a_run_step_gets_the_last_result_id_and_a_wait_step_pauses(tmp_path: Path) -> None:
    scenario = [
        {"tool": "echo", "arguments": {"text": json.dumps({"id": "abc123"})}},
        {"wait": 0.2},
        {
            "run": [
                sys.executable,
                "-c",
                "import sys; print('got', sys.argv[1]); sys.exit(3)",
                "{id}",
            ]
        },
        {"tool": "echo", "arguments": {"text": "after {id}"}},
    ]

    result = drive(tmp_path, scenario)

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "wait 0.2 s" in lines
    assert any(line.startswith("$ ") and line.endswith(" abc123") for line in lines)
    assert "| got abc123" in lines
    assert "run exit 3" in lines
    after = next(i for i, line in enumerate(lines) if '"after abc123"' in line)
    assert lines.index("run exit 3") < after
