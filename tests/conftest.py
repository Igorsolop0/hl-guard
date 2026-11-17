import os
import socket
import urllib.request
from typing import Any

import pytest

LOOPBACK = (None, "127.0.0.1", "::1")
INET = (socket.AF_INET, socket.AF_INET6)


def _check(host: object) -> None:
    if host not in LOOPBACK:
        raise OSError(f"tests reach only loopback addresses, not {host!r}")


def _check_peer(sock: Any, address: Any) -> None:
    if sock.family in INET:
        _check(address[0])


resolve = socket.getaddrinfo
by_name, by_name_ex = socket.gethostbyname, socket.gethostbyname_ex
by_addr, name_info = socket.gethostbyaddr, socket.getnameinfo
connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
sendto, sendmsg = socket.socket.sendto, socket.socket.sendmsg


def loopback_resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
    _check(host)
    return resolve(host, *args, **kwargs)


def loopback_by_name(host: Any) -> Any:
    _check(host)
    return by_name(host)


def loopback_by_name_ex(host: Any) -> Any:
    _check(host)
    return by_name_ex(host)


def loopback_by_addr(host: Any) -> Any:
    _check(host)
    return by_addr(host)


def loopback_name_info(address: Any, flags: int) -> Any:
    _check(address[0])
    return name_info(address, flags)


def loopback_connect(sock: Any, address: Any) -> None:
    _check_peer(sock, address)
    connect(sock, address)


def loopback_connect_ex(sock: Any, address: Any) -> int:
    _check_peer(sock, address)
    return connect_ex(sock, address)


def loopback_sendto(sock: Any, data: Any, *args: Any) -> int:
    _check_peer(sock, args[-1])
    return sendto(sock, data, *args)


def loopback_sendmsg(sock: Any, buffers: Any, *args: Any) -> int:
    if len(args) == 3:
        _check_peer(sock, args[2])
    return sendmsg(sock, buffers, *args)


for variable in [name for name in os.environ if name.lower().endswith("_proxy")]:
    del os.environ[variable]
urllib.request.getproxies = dict
socket.getaddrinfo = loopback_resolve
socket.gethostbyname = loopback_by_name
socket.gethostbyname_ex = loopback_by_name_ex
socket.gethostbyaddr = loopback_by_addr
socket.getnameinfo = loopback_name_info
socket.socket.connect = loopback_connect  # type: ignore[method-assign]
socket.socket.connect_ex = loopback_connect_ex  # type: ignore[method-assign]
socket.socket.sendto = loopback_sendto  # type: ignore[method-assign]
socket.socket.sendmsg = loopback_sendmsg  # type: ignore[method-assign]


@pytest.fixture()
def env_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies_environment)
