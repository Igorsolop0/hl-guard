from __future__ import annotations

import contextlib
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from hl_guard.server import info as info_module
from hl_guard.server.app import boot, build_server
from hl_guard.server.config import INFO_TIMEOUT_S_RANGE
from hl_guard.server.info import (
    COSTS,
    RESPONSE_BYTES_MAX,
    InfoClient,
    InfoRateLimited,
    InfoUnavailable,
    WeightBudget,
)
from hl_guard.server.parse import ShapeError
from server.fake_info import FakeInfo, point_testnet_at, recorded, write_config

FILL_ROW = json.loads(recorded("userFills"))[0]
CANDLE_ROW = json.loads(recorded("candleSnapshot"))[0]


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock() -> Clock:
    return Clock()


def budget(tmp_path: Path, clock: Clock, per_minute: int = 300) -> WeightBudget:
    return WeightBudget(tmp_path / "info-weight.json", per_minute, clock=clock)


def spent(tmp_path: Path) -> int:
    state = json.loads((tmp_path / "info-weight.json").read_text())
    return sum(row[1] for row in state["spent"])


def client(tmp_path: Path, clock: Clock, per_minute: int = 300, **kw: Any) -> InfoClient:
    return InfoClient("testnet", budget(tmp_path, clock, per_minute), **kw)


def rows(row: dict[str, Any], n: int) -> bytes:
    return json.dumps([row] * n).encode()


def test_weights_follow_the_rate_limit_page() -> None:
    assert {kind: cost.base for kind, cost in COSTS.items()} == {
        "metaAndAssetCtxs": 20,
        "l2Book": 2,
        "candleSnapshot": 20,
        "clearinghouseState": 2,
        "frontendOpenOrders": 20,
        "userFills": 20,
        "userFillsByTime": 20,
        "activeAssetData": 20,
        "orderStatus": 2,
        "extraAgents": 20,
    }


@pytest.mark.parametrize(
    ("kind", "row", "n", "weight"),
    [
        ("userFills", FILL_ROW, 0, 20),
        ("userFills", FILL_ROW, 20, 21),
        ("userFills", FILL_ROW, 41, 23),
        ("userFills", FILL_ROW, 2000, 120),
        ("candleSnapshot", CANDLE_ROW, 60, 21),
        ("candleSnapshot", CANDLE_ROW, 61, 22),
    ],
)
def test_rows_are_charged_after_the_answer(
    tmp_path: Path, clock: Clock, fake: FakeInfo, kind: str, row: Any, n: int, weight: int
) -> None:
    fake.answers[kind] = lambda b: (200, rows(row, n), {})

    client(tmp_path, clock).post({"type": kind})

    assert spent(tmp_path) == weight


def test_the_most_a_request_can_cost_is_reserved_before_it_is_sent(
    tmp_path: Path, clock: Clock, fake: FakeInfo
) -> None:
    fake.answers["userFills"] = lambda b: (200, rows(FILL_ROW, 3), {})
    info = client(tmp_path, clock, per_minute=130)
    info.post({"type": "metaAndAssetCtxs"})

    with pytest.raises(InfoRateLimited, match="120"):
        info.post({"type": "userFills", "user": "0x"})

    assert fake.kinds() == ["metaAndAssetCtxs"]
    assert spent(tmp_path) == 20


def test_a_request_dearer_than_the_cap_is_refused(
    tmp_path: Path, clock: Clock, fake: FakeInfo
) -> None:
    with pytest.raises(InfoRateLimited, match=r"up to 120 .* info_weight_per_min 60"):
        client(tmp_path, clock, per_minute=60).post({"type": "userFills", "user": "0x"})

    assert fake.requests == []


def test_a_caller_can_bound_the_rows_it_reserves(
    tmp_path: Path, clock: Clock, fake: FakeInfo
) -> None:
    info = client(tmp_path, clock, per_minute=21)

    info.post({"type": "candleSnapshot"}, rows_max=60)
    clock.now += 60

    with pytest.raises(InfoRateLimited, match="up to 22"):
        info.post({"type": "candleSnapshot"}, rows_max=61)


def test_the_window_slides_after_a_minute(tmp_path: Path, clock: Clock, fake: FakeInfo) -> None:
    info = client(tmp_path, clock, per_minute=40)
    info.post({"type": "metaAndAssetCtxs"})
    info.post({"type": "metaAndAssetCtxs"})
    with pytest.raises(InfoRateLimited):
        info.post({"type": "l2Book", "coin": "BTC"})

    clock.now += 60
    with pytest.raises(InfoRateLimited):
        info.post({"type": "l2Book", "coin": "BTC"})
    clock.now += 1

    info.post({"type": "metaAndAssetCtxs"})
    assert len(fake.requests) == 3


def test_a_429_backs_off_every_process_for_a_minute(
    tmp_path: Path, clock: Clock, fake: FakeInfo
) -> None:
    fake.answers["clearinghouseState"] = lambda b: (429, b"", {})
    first, second = client(tmp_path, clock), client(tmp_path, clock)

    with pytest.raises(InfoRateLimited, match="429"):
        first.post({"type": "clearinghouseState", "user": "0x"})
    clock.now += 59
    with pytest.raises(InfoRateLimited, match="backs off"):
        second.post({"type": "l2Book", "coin": "BTC"})
    assert fake.kinds() == ["clearinghouseState"]

    clock.now += 1
    second.post({"type": "l2Book", "coin": "BTC"})
    assert fake.kinds() == ["clearinghouseState", "l2Book"]


def test_processes_share_one_budget(tmp_path: Path, clock: Clock, fake: FakeInfo) -> None:
    first, second = client(tmp_path, clock, per_minute=40), client(tmp_path, clock, per_minute=40)
    first.post({"type": "metaAndAssetCtxs"})
    second.post({"type": "metaAndAssetCtxs"})

    with pytest.raises(InfoRateLimited):
        first.post({"type": "metaAndAssetCtxs"})
    assert len(fake.requests) == 2


def test_an_in_flight_reservation_holds_its_weight_for_other_processes(
    tmp_path: Path, clock: Clock
) -> None:
    first, second = budget(tmp_path, clock, 130), budget(tmp_path, clock, 130)
    token = first.reserve(120)

    with pytest.raises(InfoRateLimited, match="spent 120"):
        second.reserve(20)
    first.settle(token, 21)
    second.reserve(20)


def test_an_in_flight_hold_outlives_the_longest_read_a_peer_can_make(
    tmp_path: Path, clock: Clock
) -> None:
    first, second = budget(tmp_path, clock, 20), budget(tmp_path, clock, 20)
    token = first.reserve(20)
    clock.now += INFO_TIMEOUT_S_RANGE[1] + info_module.CLOSE_GRACE_S

    with pytest.raises(InfoRateLimited, match="spent 20"):
        second.reserve(20)
    first.settle(token, 20)
    with pytest.raises(InfoRateLimited, match="spent 20"):
        second.reserve(20)


def test_the_hold_of_a_killed_read_frees_after_its_longest_life(
    tmp_path: Path, clock: Clock
) -> None:
    budget(tmp_path, clock, 20).reserve(20)
    clock.now += info_module.HOLD_S - 1
    with pytest.raises(InfoRateLimited, match="spent 20"):
        budget(tmp_path, clock, 20).reserve(20)
    clock.now += 1

    budget(tmp_path, clock, 20).reserve(20)


def test_a_request_that_fails_after_sending_costs_its_reservation(
    tmp_path: Path, clock: Clock, fake: FakeInfo
) -> None:
    fake.answers["userFills"] = lambda b: (500, b"null", {})

    with pytest.raises(InfoUnavailable):
        client(tmp_path, clock).post({"type": "userFills", "user": "0x"})

    assert spent(tmp_path) == 120


def test_a_request_that_never_left_costs_its_base_weight(tmp_path: Path, clock: Clock) -> None:
    closed = socket.create_server(("127.0.0.1", 0))
    point_testnet_at(f"http://127.0.0.1:{closed.getsockname()[1]}/info")
    closed.close()

    with pytest.raises(InfoUnavailable, match="unreachable"):
        client(tmp_path, clock).post({"type": "userFills", "user": "0x"})

    assert spent(tmp_path) == 20


@pytest.mark.parametrize(("n", "weight"), [(61, 22), (600, 30)])
def test_more_rows_than_reserved_is_refused_and_costs_the_rows_it_holds(
    tmp_path: Path, clock: Clock, fake: FakeInfo, n: int, weight: int
) -> None:
    fake.answers["candleSnapshot"] = lambda b: (200, rows(CANDLE_ROW, n), {})

    with pytest.raises(ShapeError, match=f"holds {n} items; the request reserved 11"):
        client(tmp_path, clock).post({"type": "candleSnapshot"}, rows_max=11)

    assert spent(tmp_path) == weight


def test_an_unreadable_budget_file_blocks_for_a_minute(
    tmp_path: Path, clock: Clock, fake: FakeInfo
) -> None:
    path = tmp_path / "info-weight.json"
    path.write_text("{not json")
    path.chmod(0o600)
    info = client(tmp_path, clock)

    with pytest.raises(InfoRateLimited):
        info.post({"type": "l2Book", "coin": "BTC"})
    clock.now += 60
    info.post({"type": "l2Book", "coin": "BTC"})

    assert fake.kinds() == ["l2Book"]


class Killed(BaseException):
    pass


def _killed_before(real: Callable[..., Any]) -> Callable[..., Any]:
    def die(*args: Any) -> Any:
        raise Killed

    return die


def _killed_halfway(real: Callable[..., Any]) -> Callable[..., Any]:
    def die(fd: int, data: bytes, offset: int) -> Any:
        real(fd, data[: len(data) // 2], offset)
        raise Killed

    return die


@pytest.mark.parametrize(
    ("call", "kill"),
    [("pwrite", _killed_before), ("pwrite", _killed_halfway), ("ftruncate", _killed_before)],
    ids=["before-write", "mid-write", "before-truncate"],
)
def test_a_kill_while_saving_never_frees_a_full_budget(
    tmp_path: Path,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    call: str,
    kill: Callable[[Callable[..., Any]], Callable[..., Any]],
) -> None:
    full = budget(tmp_path, clock)
    full.reserve(5)
    clock.now += 30
    full.settle(full.reserve(295), 295)
    clock.now += 31

    with monkeypatch.context() as crash:
        crash.setattr(os, call, kill(getattr(os, call)))
        with pytest.raises(Killed):
            full.reserve(20)

    with pytest.raises(InfoRateLimited):
        budget(tmp_path, clock).reserve(20)


def test_the_budget_file_stays_small_at_any_rate(
    tmp_path: Path, clock: Clock, fake: FakeInfo
) -> None:
    info = client(tmp_path, clock, per_minute=600)
    for _ in range(600):
        clock.now += 0.1
        with contextlib.suppress(InfoRateLimited):
            info.post({"type": "l2Book", "coin": "BTC"})

    state = json.loads((tmp_path / "info-weight.json").read_text())
    assert len(state["spent"]) <= 61 and state["held"] == []
    assert spent(tmp_path) <= 600


def test_the_budget_file_is_owner_only(tmp_path: Path, clock: Clock, fake: FakeInfo) -> None:
    client(tmp_path, clock).post({"type": "l2Book", "coin": "BTC"})

    assert (tmp_path / "info-weight.json").stat().st_mode & 0o777 == 0o600


def test_a_linked_budget_file_is_refused(tmp_path: Path, clock: Clock, fake: FakeInfo) -> None:
    (tmp_path / "elsewhere").write_text("")
    (tmp_path / "info-weight.json").symlink_to(tmp_path / "elsewhere")

    with pytest.raises(InfoRateLimited, match="budget file"):
        client(tmp_path, clock).post({"type": "l2Book", "coin": "BTC"})

    assert fake.requests == []


def _hard_link(path: Path) -> None:
    ledger = path.parent / "ledger.jsonl"
    ledger.write_text('{"seq": 1}\n')
    ledger.chmod(0o600)
    os.link(ledger, path)


def _group_writable(path: Path) -> None:
    path.write_text("")
    path.chmod(0o620)


@pytest.mark.parametrize(
    "make", [_hard_link, _group_writable, os.mkfifo], ids=["link", "mode", "fifo"]
)
def test_a_budget_file_others_can_reach_is_refused(
    tmp_path: Path, clock: Clock, fake: FakeInfo, make: Callable[[Path], None]
) -> None:
    path = tmp_path / "info-weight.json"
    make(path)

    with pytest.raises(InfoRateLimited, match="one link that only you can write"):
        client(tmp_path, clock).post({"type": "l2Book", "coin": "BTC"})

    assert fake.requests == []
    if (tmp_path / "ledger.jsonl").exists():
        assert (tmp_path / "ledger.jsonl").read_text() == '{"seq": 1}\n'


NOW = 1_800_000_000.0


@pytest.mark.parametrize(
    "text",
    [
        '{"until": 0, "spent": [[1800000000, -1000000000]], "held": []}',
        '{"until": 0, "spent": [], "held": [[1800000000, -5, "k"]]}',
        '{"until": 0, "spent": [[1800000000, 1.5]], "held": []}',
        '{"until": 0, "spent": [[1800000000, true]], "held": []}',
        '{"until": NaN, "spent": [], "held": []}',
        '{"until": Infinity, "spent": [], "held": []}',
        '{"until": 1800000121, "spent": [], "held": []}',
        '{"until": 0, "spent": [], "held": [[NaN, 5, "k"]]}',
        '{"until": 1e400, "spent": [], "held": []}',
        '{"until": -1e400, "spent": [], "held": []}',
        '{"until": 0, "spent": [], "held": [[-1e400, 5, "k"]]}',
    ],
    ids=[
        "negative",
        "negative-held",
        "fraction",
        "bool",
        "nan",
        "infinity",
        "far-until",
        "nan-held",
        "overflow",
        "underflow",
        "overflow-held",
    ],
)
def test_a_budget_file_with_bad_numbers_backs_off(
    tmp_path: Path, clock: Clock, fake: FakeInfo, text: str
) -> None:
    path = tmp_path / "info-weight.json"
    path.write_text(text)
    path.chmod(0o600)
    info = client(tmp_path, clock)

    with pytest.raises(InfoRateLimited, match="backs off /info for 60 s"):
        info.post({"type": "l2Book", "coin": "BTC"})

    assert fake.requests == []
    assert json.loads(path.read_text())["until"] == NOW + 60


def test_rows_from_the_future_count_from_now(tmp_path: Path, clock: Clock, fake: FakeInfo) -> None:
    path = tmp_path / "info-weight.json"
    ahead = int(NOW) + 3600
    path.write_text(json.dumps({"until": 0, "spent": [[ahead, 200]], "held": [[ahead, 100, "k"]]}))
    path.chmod(0o600)
    info = client(tmp_path, clock)

    with pytest.raises(InfoRateLimited, match="spent 300"):
        info.post({"type": "l2Book", "coin": "BTC"})
    clock.now += 61
    info.post({"type": "l2Book", "coin": "BTC"})

    assert fake.kinds() == ["l2Book"]


def test_a_back_off_up_to_two_minutes_ahead_is_kept(
    tmp_path: Path, clock: Clock, fake: FakeInfo
) -> None:
    path = tmp_path / "info-weight.json"
    path.write_text(json.dumps({"until": NOW + 120, "spent": [], "held": []}))
    path.chmod(0o600)

    with pytest.raises(InfoRateLimited, match="backs off /info for 120 s"):
        client(tmp_path, clock).post({"type": "l2Book", "coin": "BTC"})


def test_reservation_tokens_are_random(tmp_path: Path, clock: Clock) -> None:
    held = budget(tmp_path, clock, 600)
    tokens = [held.reserve(1) for _ in range(50)]

    assert len(set(tokens)) == 50
    assert all(len(token) == 32 and int(token, 16) >= 0 for token in tokens)
    shared = [i for i in range(32) if len({token[i] for token in tokens}) == 1]
    assert shared == [], "a fixed position holds the same digit in every token"


def _slow_server(
    trickled: str, gap_s: float, *, headers: bool = False, size: int = 400
) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            kind = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["type"]
            if kind != trickled:
                data = recorded(kind)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if headers:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
            else:
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.end_headers()
            try:
                for _ in range(size):
                    self.wfile.write(b"a" if headers else b" ")
                    self.wfile.flush()
                    time.sleep(gap_s)
            except OSError:
                pass

        def log_message(self, format: str, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/info"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture()
def slow_state() -> Iterator[str]:
    yield from _slow_server("clearinghouseState", 0.1)


@pytest.fixture(params=[False, True], ids=["body", "headers"])
def trickled(request: pytest.FixtureRequest) -> Iterator[str]:
    yield from _slow_server("clearinghouseState", 0.1, headers=request.param)


def test_a_trickled_answer_fails_at_the_total_deadline(
    tmp_path: Path, clock: Clock, trickled: str
) -> None:
    point_testnet_at(trickled)
    info = client(tmp_path, clock, timeout_s=1.0)
    start = time.monotonic()

    with pytest.raises(InfoUnavailable, match=r"within 1\.0 s"):
        info.post({"type": "clearinghouseState", "user": "0x"})

    assert 1.0 <= time.monotonic() - start < 1.5


def test_an_answer_above_two_megabytes_is_refused(
    tmp_path: Path, clock: Clock, fake: FakeInfo
) -> None:
    fake.answers["l2Book"] = lambda b: (200, b" " * (RESPONSE_BYTES_MAX + 1), {})

    with pytest.raises(InfoUnavailable, match="larger than 2097152"):
        client(tmp_path, clock).post({"type": "l2Book", "coin": "BTC"})


def test_one_stuck_read_does_not_hold_other_tools_past_the_deadline(
    tmp_path: Path, durable: None, slow_state: str
) -> None:
    point_testnet_at(slow_state)
    config = write_config(tmp_path)
    config.write_text(config.read_text() + "info_timeout_s: 1\n")
    runtime = boot(config, None)
    done: dict[str, float] = {}

    async def run() -> dict[str, Any]:
        results: dict[str, Any] = {}
        start = time.monotonic()
        async with Client(build_server(runtime)) as session:

            async def one(name: str) -> None:
                results[name] = await session.call_tool(name, {})
                done[name] = time.monotonic() - start

            async with anyio.create_task_group() as tg:
                tg.start_soon(one, "account")
                await anyio.sleep(0.1)
                tg.start_soon(one, "markets")
        return results

    results = anyio.run(run)

    assert results["account"].is_error is True
    assert json.loads(results["account"].content[0].text)["error"] == "network"
    assert results["markets"].is_error is False
    assert done["account"] < 1.5
    assert done["markets"] < 2.5


def _peer(mode: str) -> tuple[subprocess.Popen[str], int]:
    peer = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("trickle_peer.py")), mode],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert peer.stdout is not None
    return peer, int(peer.stdout.readline())


@pytest.fixture(params=["header", "proxy", "tls"])
def endless(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    peer, port = _peer(request.param)
    if request.param in ("proxy", "tls"):
        request.getfixturevalue("env_proxies")
        for name in ("https_proxy", "no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    else:
        point_testnet_at(f"http://127.0.0.1:{port}/info")
    try:
        yield
    finally:
        peer.kill()
        peer.wait()


def test_timed_out_reads_leave_no_threads_or_sockets(
    tmp_path: Path, clock: Clock, endless: None
) -> None:
    info = client(tmp_path, clock, per_minute=600, timeout_s=0.1)
    threads, fds = threading.active_count(), len(os.listdir("/dev/fd"))

    for _ in range(100):
        clock.now += 61
        with pytest.raises(InfoUnavailable, match=r"within 0\.1 s"):
            info.post({"type": "l2Book", "coin": "BTC"})
    time.sleep(2)

    assert threading.active_count() <= threads + 1
    assert len(os.listdir("/dev/fd")) <= fds


@pytest.mark.parametrize("endless", ["header"], indirect=True)
def test_a_read_still_closing_makes_the_next_call_busy(
    tmp_path: Path, clock: Clock, endless: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(info_module._Call, "cut", lambda self: None)
    info = client(tmp_path, clock, per_minute=600, timeout_s=0.1)
    with pytest.raises(InfoUnavailable, match="within"):
        info.post({"type": "l2Book", "coin": "BTC"})
    before = spent(tmp_path)

    with pytest.raises(InfoUnavailable, match="busy"):
        info.post({"type": "l2Book", "coin": "BTC"})

    assert spent(tmp_path) == before


def test_each_read_waits_only_for_the_time_left(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    peer, port = _peer("stall")
    point_testnet_at(f"http://127.0.0.1:{port}/info")
    monkeypatch.setattr(info_module._Call, "cut", lambda self: None)
    info = client(tmp_path, clock, timeout_s=0.5)
    start = time.monotonic()
    try:
        with pytest.raises(InfoUnavailable, match="within"):
            info.post({"type": "l2Book", "coin": "BTC"})
        elapsed = time.monotonic() - start
    finally:
        peer.kill()
        peer.wait()

    assert elapsed < 0.7


@pytest.mark.parametrize("endless", ["tls"], indirect=True)
def test_a_socket_whose_deadline_passes_mid_setup_is_closed(
    tmp_path: Path, clock: Clock, endless: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    used: list[socket.socket] = []
    tighten = info_module._Call.tighten

    def late_for_tls(call: Any) -> None:
        if isinstance(call._socks[-1], ssl.SSLSocket):
            used.append(call._socks[-1])
            raise call.late()
        tighten(call)

    monkeypatch.setattr(info_module._Call, "tighten", late_for_tls)

    with pytest.raises(InfoUnavailable, match="within"):
        client(tmp_path, clock, timeout_s=1.0).post({"type": "l2Book", "coin": "BTC"})

    assert len(used) == 1 and used[0].fileno() == -1


@pytest.fixture()
def blackhole() -> Iterator[tuple[str, int]]:
    server = socket.create_server(("127.0.0.1", 0), backlog=1)
    address = server.getsockname()
    queued: list[socket.socket] = []
    try:
        for _ in range(16):
            probe = socket.socket()
            probe.settimeout(0.2)
            try:
                probe.connect(address)
            except TimeoutError:
                probe.close()
                break
            queued.append(probe)
        else:
            pytest.fail("the loopback listen queue never filled")
        yield address
    finally:
        for probe in queued:
            probe.close()
        server.close()


def _resolve_to(
    monkeypatch: pytest.MonkeyPatch, port: int, lookup: Callable[[], list[tuple[str, int]]]
) -> list[str]:
    asked: list[str] = []

    def getaddrinfo(host: str, *args: Any, **kwargs: Any) -> list[Any]:
        asked.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", a) for a in lookup()]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    point_testnet_at(f"http://info.invalid:{port}/info")
    return asked


def _port(url: str) -> int:
    return int(url.rsplit(":", 1)[1].split("/")[0])


def test_a_blackholed_first_address_leaves_time_for_the_next(
    tmp_path: Path,
    clock: Clock,
    fake: FakeInfo,
    blackhole: tuple[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(fake.url)
    asked = _resolve_to(monkeypatch, port, lambda: [blackhole, ("127.0.0.1", port)])
    start = time.monotonic()

    answer = client(tmp_path, clock, timeout_s=2.0).post({"type": "l2Book", "coin": "BTC"})

    assert answer == json.loads(recorded("l2Book"))
    assert time.monotonic() - start < 2.0
    assert asked == ["info.invalid"]


def test_a_stuck_lookup_keeps_each_read_to_its_deadline_and_never_busy(
    tmp_path: Path, clock: Clock, fake: FakeInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(fake.url)
    landed = threading.Event()

    def stuck() -> list[tuple[str, int]]:
        landed.wait(10)
        return [("127.0.0.1", port)]

    asked = _resolve_to(monkeypatch, port, stuck)
    info = client(tmp_path, clock, timeout_s=0.3)
    threads = threading.active_count()
    try:
        for _ in range(5):
            start = time.monotonic()
            with pytest.raises(InfoUnavailable, match=r"within 0\.3 s"):
                info.post({"type": "l2Book", "coin": "BTC"})
            assert time.monotonic() - start < 0.5
        assert asked == ["info.invalid"]
        assert threading.active_count() <= threads + 1
        landed.set()

        assert info.post({"type": "l2Book", "coin": "BTC"}) == json.loads(recorded("l2Book"))
    finally:
        landed.set()


def test_each_read_looks_the_host_up_again(
    tmp_path: Path, clock: Clock, fake: FakeInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(fake.url)
    asked = _resolve_to(monkeypatch, port, lambda: [("127.0.0.1", port)])
    info = client(tmp_path, clock)

    info.post({"type": "l2Book", "coin": "BTC"})
    info.post({"type": "l2Book", "coin": "BTC"})

    assert asked == ["info.invalid", "info.invalid"]


def test_a_failed_lookup_is_unreachable_and_costs_the_base_weight(
    tmp_path: Path, clock: Clock, fake: FakeInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown() -> list[tuple[str, int]]:
        raise socket.gaierror(socket.EAI_NONAME, "unknown host")

    _resolve_to(monkeypatch, _port(fake.url), unknown)

    with pytest.raises(InfoUnavailable, match="unreachable"):
        client(tmp_path, clock).post({"type": "userFills", "user": "0x"})

    assert spent(tmp_path) == 20
    assert fake.requests == []


def test_headroom_lets_a_request_past_the_minute_once_and_no_further(
    tmp_path: Path, clock: Clock
) -> None:
    weights = budget(tmp_path, clock, 60)
    weights.settle(weights.reserve(60), 60)

    with pytest.raises(InfoRateLimited):
        weights.reserve(2)
    weights.settle(weights.reserve(2, headroom=60), 2)
    weights.settle(weights.reserve(58, headroom=60), 58)
    with pytest.raises(InfoRateLimited):
        weights.reserve(1, headroom=60)
