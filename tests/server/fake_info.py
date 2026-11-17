from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Any

from hl_guard.exchange import network

FIXTURES = Path(__file__).parent / "fixtures"
ROOT = Path(__file__).parent.parent.parent
ACCOUNT = "0x" + "ab" * 20

RECORDED: dict[str, str] = {
    "metaAndAssetCtxs": "meta_and_asset_ctxs",
    "l2Book": "l2_book_btc",
    "candleSnapshot": "candles_btc_1h",
    "clearinghouseState": "clearinghouse_state",
    "frontendOpenOrders": "frontend_open_orders",
    "userFills": "user_fills",
    "activeAssetData": "active_asset_data_eth",
}

Answer = tuple[int, bytes, dict[str, str]]


def recorded(kind: str) -> bytes:
    return (FIXTURES / f"{RECORDED[kind]}.json").read_bytes()


def fresh(state: bytes) -> bytes:
    raw = json.loads(state)
    raw["time"] = time.time_ns() // 1_000_000
    return json.dumps(raw).encode()


@dataclass
class FakeInfo:
    answers: dict[str, Callable[[dict[str, Any]], Answer]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    url: str = ""
    exchange: Callable[[dict[str, Any]], Answer] | None = None

    def answer(self, body: dict[str, Any]) -> Answer:
        self.requests.append(body)
        kind = body.get("type", "")
        custom = self.answers.get(kind)
        if custom is not None:
            return custom(body)
        if kind == "clearinghouseState":
            return 200, fresh(recorded(kind)), {}
        return 200, recorded(kind), {}

    def kinds(self) -> list[str]:
        return [r["type"] for r in self.requests]


def _handler(fake: FakeInfo) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            if self.path == "/exchange" and fake.exchange is not None:
                status, data, headers = fake.exchange(body)
            else:
                status, data, headers = fake.answer(body)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            fake.requests.append({"type": f"GET {self.path}"})
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


@contextmanager
def serving(fake: FakeInfo) -> Iterator[FakeInfo]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.url = f"http://127.0.0.1:{httpd.server_address[1]}/info"
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        httpd.shutdown()
        httpd.server_close()


def point_testnet_at(url: str, exchange_url: str | None = None) -> None:
    row = network.NETWORKS["testnet"]
    testnet = network.Network(row.name, exchange_url or row.exchange_url, url, row.phantom_source)
    network.NETWORKS = MappingProxyType({**network.NETWORKS, "testnet": testnet})


def write_config(root: Path, *, data_dir: str = "data") -> Path:
    (root / "data").mkdir(mode=0o700, exist_ok=True)
    policy = root / "policy.yaml"
    shutil.copy(ROOT / "examples" / "policy.yaml", policy)
    policy.chmod(0o600)
    config = root / "config.yaml"
    config.write_text(
        f"policy: policy.yaml\ndata_dir: {data_dir}\naccount: '{ACCOUNT}'\n"
        "info_budget_file: data/info-weight.json\n"
    )
    config.chmod(0o600)
    return config
