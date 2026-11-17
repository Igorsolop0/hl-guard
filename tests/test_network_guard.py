import os
import socket
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from hl_guard.server.info import InfoClient, InfoError, WeightBudget


def test_these_tests_cannot_resolve_a_public_name() -> None:
    with pytest.raises(OSError, match="tests reach only loopback"):
        urllib.request.urlopen("https://api.hyperliquid-testnet.xyz/info", timeout=1)


PUBLIC = [(socket.AF_INET, ("192.0.2.1", 9)), (socket.AF_INET6, ("2001:db8::1", 9))]


@pytest.mark.parametrize(("family", "address"), PUBLIC)
def test_a_raw_connect_to_an_ip_literal_is_refused(
    family: socket.AddressFamily, address: tuple[str, int]
) -> None:
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        with pytest.raises(OSError, match="tests reach only loopback"):
            sock.connect(address)


def test_a_loopback_connect_still_works() -> None:
    with (
        socket.create_server(("127.0.0.1", 0)) as server,
        socket.create_connection(server.getsockname(), timeout=1),
    ):
        pass


def _refusal_at_import() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.connect_ex(("192.0.2.1", 9))
        except OSError as exc:
            return str(exc)
    return "connected"


AT_IMPORT = _refusal_at_import()


def test_code_run_while_collecting_is_guarded() -> None:
    assert AT_IMPORT == "tests reach only loopback addresses, not '192.0.2.1'"


@pytest.mark.parametrize("host", ["192.0.2.1", "localhost", "example.com"])
def test_connect_ex_reaches_only_loopback_literals(host: str) -> None:
    with socket.socket() as sock, pytest.raises(OSError, match="tests reach only loopback"):
        sock.connect_ex((host, 9))


@pytest.mark.parametrize(
    "send",
    [
        lambda udp, to: udp.sendto(b"x", to),
        lambda udp, to: udp.sendto(b"x", 0, to),
        lambda udp, to: udp.sendmsg([b"x"], [], 0, to),
    ],
)
@pytest.mark.parametrize("host", ["192.0.2.1", "localhost"])
def test_udp_sends_reach_only_loopback_literals(send: Any, host: str) -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp,
        pytest.raises(OSError, match="tests reach only loopback"),
    ):
        send(udp, (host, 9))


def test_udp_to_loopback_still_works() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server,
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client,
    ):
        server.bind(("127.0.0.1", 0))
        assert client.sendto(b"x", server.getsockname()) == 1
        assert client.sendmsg([b"y"], [], 0, server.getsockname()) == 1


@pytest.mark.parametrize(
    "lookup",
    [
        lambda: socket.gethostbyname("example.com"),
        lambda: socket.gethostbyname_ex("example.com"),
        lambda: socket.gethostbyaddr("192.0.2.1"),
        lambda: socket.getnameinfo(("192.0.2.1", 9), 0),
    ],
    ids=["gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"],
)
def test_every_resolver_entry_reaches_only_loopback(lookup: Any) -> None:
    with pytest.raises(OSError, match="tests reach only loopback"):
        lookup()


def test_loopback_lookups_still_work() -> None:
    assert socket.gethostbyname("127.0.0.1") == "127.0.0.1"
    assert socket.getnameinfo(("127.0.0.1", 9), socket.NI_NUMERICHOST | socket.NI_NUMERICSERV)


def test_no_proxy_reaches_the_tests() -> None:
    assert not [name for name in os.environ if name.lower().endswith("_proxy")]
    assert urllib.request.getproxies() == {}


@pytest.mark.parametrize("variable", ["HTTPS_PROXY", "https_proxy"])
def test_a_proxy_set_after_import_carries_no_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        monkeypatch.setenv(variable, f"http://127.0.0.1:{server.getsockname()[1]}")
        client = InfoClient("testnet", WeightBudget(tmp_path / "w.json", 600), timeout_s=1)
        with pytest.raises(InfoError):
            client.post({"type": "extraAgents", "user": "0x" + "1" * 40})
        server.settimeout(0.2)
        with pytest.raises(TimeoutError):
            conn, _ = server.accept()
            with conn:
                pytest.fail(f"the proxy got {conn.recv(300)!r}")
    assert urllib.request.getproxies() == {}
