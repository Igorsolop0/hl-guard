from __future__ import annotations

import base64
import contextlib
import fcntl
import functools
import http.client
import json
import math
import os
import secrets
import socket
import ssl
import stat
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hl_guard.exchange.network import NetworkError, network_for
from hl_guard.server.parse import CANDLES_MAX, FILLS_MAX, ShapeError

RESPONSE_BYTES_MAX = 2 * 1024 * 1024
WINDOW_S = 60.0
BACK_OFF_S = 60.0
BUDGET_BYTES_MAX = 64 * 1024
BUDGET_AHEAD_S = 120.0
CLOSE_GRACE_S = 0.5
INFO_TIMEOUT_S_MAX = 60.0
HOLD_S = INFO_TIMEOUT_S_MAX + WINDOW_S
_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Cost:
    base: int
    rows_per_weight: int = 0
    rows_max: int = 0

    def of(self, rows: int) -> int:
        return self.base + (math.ceil(rows / self.rows_per_weight) if self.rows_per_weight else 0)


COSTS: Mapping[str, Cost] = {
    "metaAndAssetCtxs": Cost(20),
    "l2Book": Cost(2),
    "candleSnapshot": Cost(20, 60, CANDLES_MAX),
    "clearinghouseState": Cost(2),
    "frontendOpenOrders": Cost(20),
    "userFills": Cost(20, 20, FILLS_MAX),
    "userFillsByTime": Cost(20, 20, FILLS_MAX),
    "activeAssetData": Cost(20),
    "orderStatus": Cost(2),
    "extraAgents": Cost(20),
}


class InfoError(Exception):
    pass


class InfoUnavailable(InfoError):
    pass


class InfoRateLimited(InfoError):
    pass


class InfoRefused(InfoError):
    pass


class WeightBudget:
    def __init__(
        self, path: Path, per_minute: int, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._path = path
        self._per_minute = per_minute
        self._clock = clock

    def reserve(self, weight: int, headroom: int = 0) -> str:
        """``headroom`` lets a risk-reducing request use that much weight beyond
        the configured minute, so spending the budget cannot block a close."""
        cap = self._per_minute + headroom
        if weight > cap:
            raise InfoRateLimited(
                f"this request can cost up to {weight} /info weight, above the configured "
                f"info_weight_per_min {self._per_minute}"
            )
        token = secrets.token_hex(16)
        with self._state() as (now, state):
            if now < state["until"]:
                raise InfoRateLimited(
                    f"hl-guard backs off /info for {math.ceil(state['until'] - now)} s more "
                    "after a 429 or an unreadable budget file"
                )
            used = sum(row[1] for row in state["spent"]) + sum(row[1] for row in state["held"])
            if used + weight > cap:
                raise InfoRateLimited(
                    f"hl-guard spent {used} of its {self._per_minute} /info weight in the last "
                    f"minute and this request can cost up to {weight}; wait before calling again"
                )
            state["held"].append([now, weight, token])
        return token

    def settle(self, token: str, weight: int) -> None:
        with self._state() as (now, state):
            state["held"] = [row for row in state["held"] if row[2] != token]
            second = math.floor(now)
            if state["spent"] and state["spent"][-1][0] == second:
                state["spent"][-1][1] += weight
            else:
                state["spent"].append([second, weight])

    def back_off(self) -> None:
        with self._state() as (now, state):
            state["until"] = max(state["until"], now + BACK_OFF_S)

    @contextmanager
    def _state(self) -> Iterator[tuple[float, dict[str, Any]]]:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise InfoRateLimited(
                f"cannot open the /info budget file {self._path}: {exc.strerror}"
            ) from None
        if not _private(os.fstat(fd)):
            os.close(fd)
            raise InfoRateLimited(
                f"the /info budget file {self._path} must be your own regular file with one "
                "link that only you can write"
            )
        with os.fdopen(fd, "r+b") as f:
            fcntl.flock(fd, fcntl.LOCK_EX)
            now = self._clock()
            state = _budget_state(f.read(BUDGET_BYTES_MAX + 1), now)
            state["spent"] = [row for row in state["spent"] if now - row[0] < WINDOW_S + 1]
            state["held"] = [row for row in state["held"] if now - row[0] < HOLD_S]
            try:
                yield now, state
            finally:
                data = json.dumps(state, separators=(",", ":")).encode()
                os.pwrite(fd, data, 0)
                os.ftruncate(fd, len(data))


def _private(st: os.stat_result) -> bool:
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == os.geteuid()
        and st.st_nlink == 1
        and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )


def _count(v: Any) -> int:
    if type(v) is not int or v < 0:
        raise ValueError(v)
    return v


def _instant(v: Any, now: float) -> float:
    if type(v) not in (int, float) or not math.isfinite(v):
        raise ValueError(v)
    return min(float(v), now)


def _budget_state(data: bytes, now: float) -> dict[str, Any]:
    if not data:
        return {"until": 0.0, "spent": [], "held": []}
    try:
        state = json.loads(data, parse_constant=_reject)
        until = state["until"]
        finite = type(until) in (int, float) and math.isfinite(until)
        if not finite or until > now + BUDGET_AHEAD_S:
            raise ValueError(until)
        spent = [[math.floor(_instant(_count(s), now)), _count(w)] for s, w in state["spent"]]
        held = [[_instant(t, now), _count(w), str(k)] for t, w, k in state["held"]]
    except (ValueError, TypeError, KeyError, RecursionError):
        return {"until": now + BACK_OFF_S, "spent": [], "held": []}
    return {"until": float(until), "spent": spent, "held": held}


def _reject(constant: str) -> Any:
    raise ValueError(constant)


@functools.cache
def _tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


class _Call:
    def __init__(self, timeout_s: float, clock: Callable[[], float]) -> None:
        self.timeout_s = timeout_s
        self.deadline = clock() + timeout_s
        self.sent = False
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False
        self._peers: list[socket.socket] = []
        self._socks: list[socket.socket] = []

    def late(self) -> InfoUnavailable:
        return InfoUnavailable(f"/info gave no full answer within {self.timeout_s} s")

    def left(self) -> float:
        left = self.deadline - self._clock()
        if left <= 0:
            raise self.late()
        return left

    def open(
        self, address: tuple[str, int], timeout: object = None, source: object = None
    ) -> socket.socket:
        addresses = _resolve(address, self)
        error = OSError(f"no address for {address[0]}")
        for i, (family, kind, proto, _, peer) in enumerate(addresses):
            wait = self.left() / (len(addresses) - i)
            sock = socket.socket(family, kind, proto)
            with self._lock:
                if self._closed:
                    sock.close()
                    raise self.late()
                self._peers.append(socket.socket(fileno=os.dup(sock.fileno())))
            try:
                sock.settimeout(wait)
                sock.connect(peer)
            except OSError as exc:
                sock.close()
                error = exc
                continue
            return self.use(sock)
        raise error

    def use(self, sock: socket.socket) -> socket.socket:
        self._socks.append(sock)
        self.tighten()
        return sock

    def tighten(self) -> None:
        self._socks[-1].settimeout(self.left())

    def close(self) -> None:
        for sock in self._socks:
            sock.close()

    def cut(self) -> None:
        with self._lock:
            for peer in self._peers:
                with contextlib.suppress(OSError):
                    peer.shutdown(socket.SHUT_RDWR)

    def release(self) -> None:
        with self._lock:
            self._closed = True
            for peer in self._peers:
                peer.close()


class _Connection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, call: _Call, tls_host: str | None) -> None:
        super().__init__(host, port)
        self._call = call
        self._tls_host = tls_host
        self._create_connection = call.open

    def connect(self) -> None:
        super().connect()
        if self._tls_host is not None:
            tls = _tls_context().wrap_socket(
                self.sock, server_hostname=self._tls_host, do_handshake_on_connect=False
            )
            self.sock = self._call.use(tls)
            tls.do_handshake()


def _route(url: str, call: _Call) -> tuple[_Connection, str, dict[str, str]]:
    target = urllib.parse.urlsplit(url)
    tls = target.scheme == "https"
    host, port = target.hostname or "", target.port or (443 if tls else 80)
    path = target.path + (f"?{target.query}" if target.query else "")
    headers = {"Content-Type": "application/json"}
    proxy = urllib.request.getproxies().get(target.scheme)
    if not proxy or urllib.request.proxy_bypass(target.netloc):
        return _Connection(host, port, call, host if tls else None), path, headers
    via = urllib.parse.urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    auth = {}
    if via.username is not None:
        user = f"{urllib.parse.unquote(via.username)}:{urllib.parse.unquote(via.password or '')}"
        auth["Proxy-Authorization"] = "Basic " + base64.b64encode(user.encode()).decode()
    conn = _Connection(via.hostname or "", via.port or 80, call, host if tls else None)
    if not tls:
        return conn, url, headers | auth
    conn.set_tunnel(host, port, headers=auth)
    return conn, path, headers


class _Lookup:
    def __init__(self, address: tuple[str, int]) -> None:
        self.addresses: list[Any] = []
        self.done = threading.Event()
        threading.Thread(
            target=self._run, args=address, name="hl-guard-lookup", daemon=True
        ).start()

    def _run(self, host: str, port: int) -> None:
        with contextlib.suppress(Exception):
            self.addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        self.done.set()


_lookup_lock = threading.Lock()
_lookups: dict[tuple[str, int], _Lookup] = {}


def _resolve(address: tuple[str, int], call: _Call) -> list[Any]:
    with _lookup_lock:
        lookup = _lookups.get(address)
        if lookup is None or lookup.done.is_set():
            lookup = _lookups[address] = _Lookup(address)
    if not lookup.done.wait(call.left()):
        raise call.late()
    return lookup.addresses


_slot_lock = threading.Lock()
_worker: threading.Thread | None = None


def _idle() -> None:
    if _worker is not None and _worker.is_alive():
        raise InfoUnavailable(
            "/info is busy: an earlier read that timed out is still closing; call again"
        )


def _start(target: Callable[[], None]) -> threading.Thread:
    global _worker
    with _slot_lock:
        _idle()
        _worker = threading.Thread(target=target, name="hl-guard-info", daemon=True)
        _worker.start()
        return _worker


class InfoClient:
    def __init__(
        self,
        network: str,
        budget: WeightBudget,
        *,
        timeout_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._network = network_for(network).name
        self.budget = budget
        self._timeout_s = timeout_s
        self._clock = clock

    def post(
        self, body: Mapping[str, Any], *, rows_max: int | None = None, headroom: int = 0
    ) -> Any:
        kind = body.get("type")
        if not isinstance(kind, str) or kind not in COSTS:
            raise InfoRefused(f"/info request type {kind!r} is not one this server sends")
        try:
            url = network_for(self._network).info_url
        except NetworkError:
            raise InfoUnavailable("network changed: refusing to read") from None
        cost = COSTS[kind]
        rows = cost.rows_max if rows_max is None else rows_max
        reserved = cost.of(rows)
        _idle()
        token = self.budget.reserve(reserved, headroom)
        call = _Call(self._timeout_s, self._clock)
        charged = None
        try:
            status, data = self._fetch(call, url, body)
            if status == 429:
                self.budget.back_off()
                raise InfoRateLimited(
                    f"/info {kind}: the exchange answered 429; hl-guard backs off for a minute"
                )
            if 300 <= status < 400:
                raise InfoUnavailable(f"/info {kind}: HTTP {status} redirect refused")
            if 400 <= status < 500:
                raise InfoRefused(f"/info {kind}: HTTP {status}")
            if not 200 <= status < 300:
                raise InfoUnavailable(f"/info {kind}: HTTP {status}")
            try:
                answer = json.loads(data)
            except (ValueError, RecursionError):
                raise InfoUnavailable(f"/info {kind}: the response is not JSON") from None
            n = len(answer) if isinstance(answer, list) else 0
            charged = cost.of(n)
            if cost.rows_per_weight and n > rows:
                raise ShapeError(kind, "", f"holds {n} items; the request reserved {rows}")
            return answer
        finally:
            if charged is None:
                charged = reserved if call.sent else cost.base
            self.budget.settle(token, charged)

    def _fetch(self, call: _Call, url: str, body: Mapping[str, Any]) -> tuple[int, bytes]:
        outcome: list[tuple[int, bytes] | InfoError] = []
        worker = _start(lambda: outcome.append(self._attempt(call, url, body)))
        worker.join(max(0.0, call.deadline - self._clock()))
        try:
            if worker.is_alive():
                call.cut()
                worker.join(CLOSE_GRACE_S)
                raise call.late()
        finally:
            call.release()
        if isinstance(outcome[0], InfoError):
            raise outcome[0]
        return outcome[0]

    def _attempt(
        self, call: _Call, url: str, body: Mapping[str, Any]
    ) -> tuple[int, bytes] | InfoError:
        try:
            return self._exchange(call, url, json.dumps(body).encode("utf-8"))
        except InfoError as exc:
            return exc
        except Exception as exc:
            if self._clock() >= call.deadline:
                return call.late()
            return InfoUnavailable(f"/info is unreachable ({type(exc).__name__})")

    def _exchange(self, call: _Call, url: str, data: bytes) -> tuple[int, bytes]:
        conn, path, headers = _route(url, call)
        try:
            conn.connect()
            call.sent = True
            conn.request("POST", path, body=data, headers=headers)
            call.tighten()
            resp = conn.getresponse()
            try:
                if not 200 <= resp.status < 300:
                    return resp.status, b""
                chunks, size = [], 0
                while not resp.isclosed():
                    call.tighten()
                    chunk = resp.read1(_CHUNK)
                    size += len(chunk)
                    if size > RESPONSE_BYTES_MAX:
                        raise InfoUnavailable(
                            f"/info response is larger than {RESPONSE_BYTES_MAX} bytes"
                        )
                    chunks.append(chunk)
                if resp.length:
                    raise InfoUnavailable(
                        f"/info response ended {resp.length} bytes short of its Content-Length"
                    )
                return resp.status, b"".join(chunks)
            finally:
                resp.close()
        finally:
            conn.close()
            call.close()
