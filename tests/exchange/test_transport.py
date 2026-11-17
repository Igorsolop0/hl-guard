import http.client
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from eth_account.signers.local import LocalAccount

from hl_guard.exchange import halt as halt_module
from hl_guard.exchange import network
from hl_guard.exchange import transport as transport_module
from hl_guard.exchange.actions import Action, build_action
from hl_guard.exchange.halt import HaltFlag
from hl_guard.exchange.network import Network
from hl_guard.exchange.signer import Signer, SignerError
from hl_guard.exchange.transport import ExchangeTransport, TransportError

TESTNET_URL = "https://api.hyperliquid-testnet.xyz/exchange"
OTHER = Network(
    name="othernet",
    exchange_url="https://exchange.othernet.invalid/exchange",
    info_url="https://info.othernet.invalid/info",
    phantom_source="a",
)


class _Resp:
    def __init__(self, body: Any = None, status: int = 200, raw: bytes | None = None) -> None:
        self._body = raw if raw is not None else json.dumps(body).encode()
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _Raising(_Resp):
    def __init__(self, exc: BaseException) -> None:
        super().__init__({})
        self._exc = exc

    def read(self) -> bytes:
        raise self._exc


@pytest.fixture()
def two_networks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        network, "NETWORKS", MappingProxyType({**network.NETWORKS, "othernet": OTHER})
    )


@pytest.fixture()
def signer(agent: LocalAccount, master: str) -> Signer:
    return Signer(agent, master, network="testnet")


@pytest.fixture()
def halt(durable_dir: Path) -> HaltFlag:
    return HaltFlag(durable_dir / "data")


@pytest.fixture()
def tx(signer: Signer, halt: HaltFlag) -> ExchangeTransport:
    return ExchangeTransport(signer, "testnet", halt)


def _stub(monkeypatch: pytest.MonkeyPatch, response: Callable[..., Any]) -> None:
    monkeypatch.setattr(transport_module._OPENER, "open", response)


def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, lambda *a, **k: pytest.fail("must not reach the network"))


@pytest.mark.parametrize("name", ["live", "paper", "mainnet", "TESTNET", ""])
def test_transport_refuses_a_network_that_is_not_enabled(
    signer: Signer, halt: HaltFlag, name: str
) -> None:
    with pytest.raises(TransportError, match="refused"):
        ExchangeTransport(signer, name, halt)


@pytest.mark.parametrize(
    "url",
    [
        "https://api.hyperliquid.xyz/exchange",
        "http://api.hyperliquid-testnet.xyz/exchange",
        "https://api.hyperliquid-testnet.xyz/info",
        "https://evil.example.com/exchange",
    ],
)
def test_transport_refuses_any_url_but_the_pinned_one(
    signer: Signer, halt: HaltFlag, url: str
) -> None:
    with pytest.raises(TransportError, match="pinned"):
        ExchangeTransport(signer, "testnet", halt, url=url)


def test_transport_accepts_the_pinned_url_spelled_out(signer: Signer, halt: HaltFlag) -> None:
    ExchangeTransport(signer, "testnet", halt, url=TESTNET_URL)


def test_transport_refuses_when_the_halt_flag_is_present(
    tx: ExchangeTransport,
    halt: HaltFlag,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    halt.path.parent.mkdir(parents=True)
    halt.path.write_text("operator halt\n")
    calls: list[int] = []
    original = tx._signer.sign_action

    def probe(action: Action, nonce: int, expires_after: int | None = None) -> Any:
        calls.append(nonce)
        return original(action, nonce, expires_after)

    monkeypatch.setattr(tx._signer, "sign_action", probe)
    _no_network(monkeypatch)
    with pytest.raises(TransportError, match="halt flag"):
        tx.send(order_action(), nonce=0)
    assert calls == []
    assert tx.last_request is None


def test_an_unreadable_halt_flag_counts_as_halted(
    tx: ExchangeTransport,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    def denied(*a: object, **k: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(halt_module, "os", SimpleNamespace(lstat=denied))
    _no_network(monkeypatch)
    with pytest.raises(TransportError, match="halt flag"):
        tx.send(order_action(), nonce=0)


def test_transport_posts_sdk_shape_signature_only(
    tx: ExchangeTransport,
    agent: LocalAccount,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: float | None = None) -> _Resp:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        return _Resp({"status": "ok", "response": {"type": "order"}})

    _stub(monkeypatch, fake_urlopen)
    expires = 1757300000000
    assert tx.send(order_action(), nonce=5, expires_after=expires)["status"] == "ok"
    assert captured["url"] == TESTNET_URL
    body = captured["body"]
    assert set(body) == {"action", "nonce", "signature", "vaultAddress", "expiresAfter"}
    assert body["vaultAddress"] is None
    assert body["expiresAfter"] == expires
    assert body["nonce"] == 5
    assert set(body["signature"]) == {"r", "s", "v"}
    assert list(body["action"]) == ["type", "orders", "grouping"]
    key_text = agent.key.hex().removeprefix("0x")
    for blob in (json.dumps(tx.request_view()), json.dumps(body), json.dumps(tx.last_response)):
        assert key_text not in blob


@pytest.mark.parametrize(
    ("status", "unknown"), [(400, False), (429, False), (500, True), (503, True)]
)
def test_transport_http_error_raises_not_returns(
    tx: ExchangeTransport,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
    status: int,
    unknown: bool,
) -> None:
    _stub(monkeypatch, lambda r, timeout=None: _Resp({"error": "boom"}, status=status))
    with pytest.raises(TransportError) as excinfo:
        tx.send(order_action(), nonce=0)
    assert excinfo.value.outcome_unknown is unknown
    assert tx.last_response is not None
    assert tx.last_response["outcome_unknown"] is unknown


def test_transport_unknown_outcome_catches_http_client_errors(
    tx: ExchangeTransport,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    _stub(monkeypatch, lambda r, timeout=None: _Resp({"status": "ok"}))
    tx.send(order_action(), nonce=0)
    assert tx.last_response is not None
    assert tx.last_response["body"]["status"] == "ok"

    _stub(monkeypatch, lambda r, timeout=None: _Raising(http.client.IncompleteRead(b"")))
    with pytest.raises(TransportError) as excinfo:
        tx.send(order_action(), nonce=1)
    assert excinfo.value.outcome_unknown is True
    assert tx.last_response["outcome_unknown"] is True
    assert tx.last_response["error"] == "IncompleteRead"


def test_transport_read_timeout_is_outcome_unknown(
    tx: ExchangeTransport,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    _stub(monkeypatch, lambda r, timeout=None: _Raising(TimeoutError("read timed out")))
    with pytest.raises(TransportError) as excinfo:
        tx.send(order_action(), nonce=0)
    assert excinfo.value.outcome_unknown is True
    assert tx.last_response is not None
    assert tx.last_response["outcome_unknown"] is True


def test_transport_rejects_forbidden_action_before_any_post(
    tx: ExchangeTransport, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_network(monkeypatch)
    with pytest.raises(SignerError):
        tx.send({"type": "agentSendAsset", "destination": "0x" + "00" * 20, "amount": "1"}, 0)


def test_transport_non_json_2xx_is_outcome_unknown(
    tx: ExchangeTransport,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    _stub(monkeypatch, lambda r, timeout=None: _Resp(raw=b"<html>proxy error page</html>"))
    with pytest.raises(TransportError) as excinfo:
        tx.send(order_action(), nonce=0)
    assert excinfo.value.outcome_unknown is True
    assert tx.last_response is not None
    assert tx.last_response["outcome_unknown"] is True


def test_transport_200_status_err_raises_not_returns(
    tx: ExchangeTransport,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    body = {"status": "err", "response": "User or API Wallet 0xabc does not exist."}
    _stub(monkeypatch, lambda r, timeout=None: _Resp(body))
    with pytest.raises(TransportError, match="exchange rejected") as excinfo:
        tx.send(order_action(), nonce=0)
    assert excinfo.value.outcome_unknown is False
    assert excinfo.value.response == body
    assert tx.last_response is not None
    assert tx.last_response["outcome_unknown"] is False
    assert tx.last_response["body"]["status"] == "err"


def test_transport_posts_to_the_table_url_of_its_network(
    tx: ExchangeTransport,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    seen: list[str] = []

    def record(req: Any, timeout: float | None = None) -> _Resp:
        seen.append(req.full_url)
        return _Resp({"status": "ok"})

    _stub(monkeypatch, record)
    tx.send(order_action(), nonce=1)
    assert seen == [TESTNET_URL]
    assert tx.last_request is not None
    assert tx.last_request["url"] == TESTNET_URL


def test_transport_refuses_a_signer_of_another_network(
    agent: LocalAccount, master: str, halt: HaltFlag, two_networks: None
) -> None:
    with network.bound_network("othernet"):
        other_signer = Signer(agent, master, network="othernet")
    with pytest.raises(TransportError, match="refusing to pair"):
        ExchangeTransport(other_signer, "testnet", halt)
    test_signer = Signer(agent, master, network="testnet")
    with network.bound_network("othernet"), pytest.raises(TransportError, match="refusing to pair"):
        ExchangeTransport(test_signer, "othernet", halt)


def test_transport_refuses_the_other_networks_url(
    signer: Signer, halt: HaltFlag, two_networks: None
) -> None:
    with pytest.raises(TransportError, match="pinned"):
        ExchangeTransport(signer, "testnet", halt, url=OTHER.exchange_url)


def test_each_network_posts_only_to_its_own_url(
    agent: LocalAccount,
    master: str,
    halt: HaltFlag,
    monkeypatch: pytest.MonkeyPatch,
    two_networks: None,
    order_action: Callable[[], Action],
) -> None:
    seen: list[str] = []

    def record(req: Any, timeout: float | None = None) -> _Resp:
        seen.append(req.full_url)
        return _Resp({"status": "ok"})

    _stub(monkeypatch, record)
    for name in ("testnet", "othernet"):
        with network.bound_network(name):
            signer = Signer(agent, master, network=name)
            ExchangeTransport(signer, name, halt).send(order_action(), nonce=1)
    assert seen == [TESTNET_URL, OTHER.exchange_url]


def test_a_signer_rebound_after_construction_is_refused_at_send(
    tx: ExchangeTransport,
    signer: Signer,
    monkeypatch: pytest.MonkeyPatch,
    two_networks: None,
    order_action: Callable[[], Action],
) -> None:
    _no_network(monkeypatch)
    signer._network = OTHER
    with pytest.raises(TransportError, match="signer network changed"):
        tx.send(order_action(), nonce=1)


def test_a_row_swapped_in_the_signer_is_refused_at_send(
    tx: ExchangeTransport,
    signer: Signer,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    _no_network(monkeypatch)
    signer._network = replace(network.NETWORKS["testnet"], phantom_source="a")
    with pytest.raises(SignerError, match="differs from the network table"):
        tx.send(order_action(), nonce=1)
    assert tx.last_request is None


def test_a_network_disabled_after_construction_is_refused_at_send(
    tx: ExchangeTransport,
    monkeypatch: pytest.MonkeyPatch,
    order_action: Callable[[], Action],
) -> None:
    _no_network(monkeypatch)
    with (
        network.bound_network("mainnet", allow_mainnet=True),
        pytest.raises(TransportError, match="network changed"),
    ):
        tx.send(order_action(), nonce=1)


class _LocalExchange(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.hits: list[tuple[str, str]] = []
        self.url = f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server: _LocalExchange

    def _serve(self) -> None:
        self.server.hits.append((self.command, self.path))
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        status, headers, body = self.server.routes[self.path]
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _serve

    def log_message(self, format: str, *args: Any) -> None:
        return None


@pytest.fixture()
def local_exchange(monkeypatch: pytest.MonkeyPatch) -> Iterator[_LocalExchange]:
    server = _LocalExchange()
    row = replace(OTHER, name="localnet", exchange_url=f"{server.url}/exchange")
    monkeypatch.setattr(
        network, "NETWORKS", MappingProxyType({**network.NETWORKS, "localnet": row})
    )
    network.enable_network("localnet")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture()
def local_tx(
    local_exchange: _LocalExchange, agent: LocalAccount, master: str, halt: HaltFlag
) -> ExchangeTransport:
    return ExchangeTransport(Signer(agent, master, network="localnet"), "localnet", halt)


OK_BODY = json.dumps({"status": "ok", "response": {"type": "order"}}).encode()


def test_the_local_exchange_answers_a_plain_post(
    local_exchange: _LocalExchange,
    local_tx: ExchangeTransport,
    order_action: Callable[[], Action],
) -> None:
    local_exchange.routes["/exchange"] = (200, {}, OK_BODY)
    assert local_tx.send(order_action(), nonce=1)["status"] == "ok"
    assert local_exchange.hits == [("POST", "/exchange")]


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirect_is_not_followed_and_the_outcome_is_unknown(
    local_exchange: _LocalExchange,
    local_tx: ExchangeTransport,
    order_action: Callable[[], Action],
    code: int,
) -> None:
    local_exchange.routes["/exchange"] = (code, {"Location": "/landed"}, OK_BODY)
    local_exchange.routes["/landed"] = (200, {}, OK_BODY)
    with pytest.raises(TransportError) as excinfo:
        local_tx.send(order_action(), nonce=1)
    assert excinfo.value.outcome_unknown is True
    assert local_exchange.hits == [("POST", "/exchange")]
    assert local_tx.last_response is not None
    assert local_tx.last_response["http_status"] == code
    assert local_tx.last_response["outcome_unknown"] is True


@pytest.mark.parametrize(
    ("code", "unknown"), [(400, False), (422, False), (500, True), (502, True)]
)
def test_a_real_http_error_carries_the_parsed_body(
    local_exchange: _LocalExchange,
    local_tx: ExchangeTransport,
    order_action: Callable[[], Action],
    code: int,
    unknown: bool,
) -> None:
    body = {"status": "err", "response": "bad request"}
    local_exchange.routes["/exchange"] = (code, {}, json.dumps(body).encode())
    with pytest.raises(TransportError, match=f"HTTP {code}") as excinfo:
        local_tx.send(order_action(), nonce=1)
    assert excinfo.value.outcome_unknown is unknown
    assert excinfo.value.response == body
    assert local_tx.last_response is not None
    assert local_tx.last_response["outcome_unknown"] is unknown


def test_a_real_500_with_a_non_json_body_is_outcome_unknown(
    local_exchange: _LocalExchange,
    local_tx: ExchangeTransport,
    order_action: Callable[[], Action],
) -> None:
    local_exchange.routes["/exchange"] = (500, {}, b"<html>upstream down</html>")
    with pytest.raises(TransportError) as excinfo:
        local_tx.send(order_action(), nonce=1)
    assert excinfo.value.outcome_unknown is True
    assert excinfo.value.response is None


def _raw_order(**over: Any) -> Action:
    wire = {
        "a": 0,
        "b": True,
        "p": "1",
        "s": "0",
        "r": False,
        "t": {"limit": {"tif": "Gtc"}},
        "c": "0x" + "1" * 32,
    }
    return {"type": "order", "orders": [{**wire, **over}], "grouping": "na"}


@pytest.mark.parametrize(
    "action",
    [
        _raw_order(a=2**64),
        _raw_order(a=-1),
        _raw_order(a=10_000),
        _raw_order(c="not-hex"),
        {"type": "cancel", "cancels": [{"a": 0, "o": 2**64}]},
        {"type": "scheduleCancel", "time": 2**64},
    ],
)
def test_send_raises_signer_error_for_a_refused_action_before_any_post(
    local_exchange: _LocalExchange, local_tx: ExchangeTransport, action: Action
) -> None:
    local_exchange.routes["/exchange"] = (200, {}, OK_BODY)
    with pytest.raises(SignerError) as excinfo:
        local_tx.send(action, nonce=1)
    assert not isinstance(excinfo.value, TransportError)
    assert local_exchange.hits == []
    assert local_tx.last_request is None


def test_send_raises_signer_error_when_encoding_fails_and_transport_error_for_an_outcome(
    local_exchange: _LocalExchange,
    local_tx: ExchangeTransport,
    order_action: Callable[[], Action],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import msgpack

    local_exchange.routes["/exchange"] = (500, {}, b"down")
    with monkeypatch.context() as patch:

        def _overflow(*args: Any, **kwargs: Any) -> Any:
            raise OverflowError("Integer value out of range")

        patch.setattr(msgpack, "packb", _overflow)
        with pytest.raises(SignerError):
            local_tx.send(order_action(), nonce=1)
    assert local_exchange.hits == []
    with pytest.raises(TransportError) as excinfo:
        local_tx.send(order_action(), nonce=2)
    assert not isinstance(excinfo.value, SignerError)
    assert excinfo.value.outcome_unknown is True
    assert local_exchange.hits == [("POST", "/exchange")]


def _halted(halt: HaltFlag) -> None:
    halt.path.parent.mkdir(parents=True, exist_ok=True)
    halt.path.write_text("operator halt\n")


def _reduce_only(r: bool = True, second_r: bool | None = None) -> Action:
    wire = {
        "a": 1,
        "b": False,
        "p": "50000",
        "s": "0.0002",
        "r": r,
        "t": {"limit": {"tif": "Ioc"}},
        "c": "0x" + "1" * 32,
    }
    orders = [wire] if second_r is None else [wire, {**wire, "r": second_r, "c": "0x" + "2" * 32}]
    return build_action("order", {"orders": orders})


@pytest.mark.parametrize(
    "action",
    [
        lambda: _reduce_only(),
        lambda: _reduce_only(second_r=True),
        lambda: build_action("cancel", {"cancels": [{"a": 1, "o": 7}]}),
        lambda: build_action(
            "cancelByCloid", {"cancels": [{"asset": 1, "cloid": "0x" + "3" * 32}]}
        ),
    ],
    ids=["reduce-only order", "reduce-only batch", "cancel", "cancel by cloid"],
)
def test_a_halt_still_lets_risk_reducing_actions_out(
    tx: ExchangeTransport, halt: HaltFlag, monkeypatch: pytest.MonkeyPatch, action: Any
) -> None:
    _halted(halt)
    _stub(monkeypatch, lambda *a, **k: _Resp({"status": "ok", "response": {"type": "order"}}))
    assert tx.send(action(), nonce=3)["status"] == "ok"


@pytest.mark.parametrize(
    "action",
    [
        lambda: _reduce_only(r=False),
        lambda: _reduce_only(second_r=False),
        lambda: build_action("updateLeverage", {"asset": 1, "isCross": False, "leverage": 2}),
        lambda: build_action("updateIsolatedMargin", {"asset": 1, "isBuy": True, "ntli": 1}),
        lambda: build_action("scheduleCancel", {"time": 1}),
    ],
    ids=["opening order", "mixed batch", "leverage", "margin", "schedule cancel"],
)
def test_a_halt_refuses_every_other_action(
    tx: ExchangeTransport, halt: HaltFlag, monkeypatch: pytest.MonkeyPatch, action: Any
) -> None:
    _halted(halt)
    _no_network(monkeypatch)
    with pytest.raises(TransportError, match="halt flag"):
        tx.send(action(), nonce=3)
