from __future__ import annotations

import html
import ipaddress
import json
import re
import secrets
import socket
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from string import Template
from typing import Any
from urllib.parse import parse_qs, urlsplit

from hl_guard.exchange.network import Network
from hl_guard.keys.agent_key import AGENT_NAME

DOMAIN_CHAIN_ID = 0x66EEE
HYPERLIQUID_CHAIN = {"testnet": "Testnet", "mainnet": "Mainnet"}
MAX_VALID_DAYS = 180
DEFAULT_VALID_DAYS = 30
WIRE_NAME_MAX = 90
DAY_MS = 86_400_000
RESULT_BYTES_MAX = 64 * 1024
PRIMARY_TYPE = "HyperliquidTransaction:ApproveAgent"
APPROVE_AGENT_FIELDS = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "agentAddress", "type": "address"},
    {"name": "agentName", "type": "string"},
    {"name": "nonce", "type": "uint64"},
]
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_NAME = re.compile(r"[A-Za-z0-9_-]+")


class ApproveAgentError(ValueError):
    pass


def check_days(days: int) -> None:
    if type(days) is not int or not 1 <= days <= MAX_VALID_DAYS:
        raise ApproveAgentError(
            f"days must be an integer from 1 to {MAX_VALID_DAYS}, got {days!r}: the exchange "
            f"caps an agent at {MAX_VALID_DAYS} days and hl-guard always sets an expiry"
        )


def check_host(host: str) -> str:
    if "%" in host:
        raise ApproveAgentError(f"the approve page host takes no IPv6 zone id, got {host!r}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise ApproveAgentError(
            f"the approve page takes no IPv4-mapped IPv6 host, got {host!r}; "
            f"use {address.ipv4_mapped} instead"
        )
    if address is None or not address.is_loopback:
        raise ApproveAgentError(
            f"the approve page binds only to a loopback address such as 127.0.0.1, got {host!r}"
        )
    return address.compressed


def check_port(port: int) -> None:
    if type(port) is not int or not 0 <= port <= 65535:
        raise ApproveAgentError(f"port must be an integer from 0 to 65535, got {port!r}")


def expiry_text(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M UTC")


@dataclass(frozen=True)
class Approval:
    network: Network
    master: str
    agent_address: str
    agent_name: str
    nonce: int
    valid_until_ms: int

    def __post_init__(self) -> None:
        if self.network.name not in HYPERLIQUID_CHAIN:
            raise ApproveAgentError(f"unknown network {self.network.name!r}")
        for label, value in (("master", self.master), ("agent_address", self.agent_address)):
            if not isinstance(value, str) or not _ADDRESS.fullmatch(value):
                raise ApproveAgentError(f"{label} must be 0x and 40 hex digits, got {value!r:.60}")
        if not isinstance(self.agent_name, str) or not _NAME.fullmatch(self.agent_name):
            raise ApproveAgentError(
                "agent_name must be non-empty ASCII letters, digits, '-' or '_', "
                f"got {self.agent_name!r:.60}"
            )
        if type(self.nonce) is not int or not 0 <= self.nonce < 2**64:
            raise ApproveAgentError(f"nonce must be a uint64, got {self.nonce!r}")
        until = self.valid_until_ms
        if type(until) is not int or until <= self.nonce:
            raise ApproveAgentError(f"valid_until must be a ms time after the nonce, got {until!r}")
        if until > self.nonce + MAX_VALID_DAYS * DAY_MS:
            raise ApproveAgentError(
                f"valid_until {until} is past the exchange cap of {MAX_VALID_DAYS} days"
            )
        if len(self.wire_name) > WIRE_NAME_MAX:
            raise ApproveAgentError(
                f"agent_name too long: the wire name {self.wire_name!r} exceeds "
                f"{WIRE_NAME_MAX} chars"
            )

    @property
    def wire_name(self) -> str:
        return f"{self.agent_name} valid_until {self.valid_until_ms}"

    @property
    def days(self) -> int:
        return (self.valid_until_ms - self.nonce) // DAY_MS


def approval(
    network: Network,
    master: str,
    agent_address: str,
    *,
    days: int,
    now_ms: int,
    agent_name: str = AGENT_NAME,
) -> Approval:
    check_days(days)
    return Approval(network, master, agent_address, agent_name, now_ms, now_ms + days * DAY_MS)


def typed_data(a: Approval) -> dict[str, Any]:
    return {
        "domain": {
            "name": "HyperliquidSignTransaction",
            "version": "1",
            "chainId": DOMAIN_CHAIN_ID,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        },
        "types": {
            PRIMARY_TYPE: APPROVE_AGENT_FIELDS,
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": PRIMARY_TYPE,
        "message": {
            "hyperliquidChain": HYPERLIQUID_CHAIN[a.network.name],
            "agentAddress": a.agent_address,
            "agentName": a.wire_name,
            "nonce": a.nonce,
            "signatureChainId": hex(DOMAIN_CHAIN_ID),
        },
    }


@dataclass(frozen=True)
class Accepted:
    valid_until_ms: int


@dataclass(frozen=True)
class Refused:
    reason: str
    by_exchange: bool = False


Outcome = Accepted | Refused


class _Server6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


class _Phase(Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    SENDING = "sending"
    DONE = "done"


_STEPS = {"/claim": (_Phase.OPEN, _Phase.CLAIMED), "/send": (_Phase.CLAIMED, _Phase.SENDING)}


class ApprovePage:
    def __init__(self, a: Approval, host: str = "127.0.0.1", port: int = 0) -> None:
        self._approval = a
        self._host = check_host(host)
        self._port = port
        self._token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._phase = _Phase.OPEN
        self._outcome: Outcome | None = None
        self._done = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def authority(self) -> str:
        host = f"[{self._host}]" if ":" in self._host else self._host
        return f"{host}:{self._port}"

    @property
    def url(self) -> str:
        return f"http://{self.authority}/approve?t={self._token}"

    def start(self) -> None:
        server_class = _Server6 if ":" in self._host else ThreadingHTTPServer
        httpd = server_class((self._host, self._port), _handler(self))
        self._httpd = httpd
        self._port = httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True)
        self._thread.start()

    def wait(self, timeout_s: float) -> Outcome | None:
        self._done.wait(timeout_s)
        return self._outcome

    def close(self) -> None:
        with self._lock:
            self._phase = _Phase.DONE
            httpd, thread = self._httpd, self._thread
            self._httpd = self._thread = None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(5)

    def _token_ok(self, query: str) -> bool:
        values = parse_qs(query).get("t", [])
        return len(values) == 1 and secrets.compare_digest(
            values[0].encode("utf-8", "replace"), self._token.encode()
        )

    def _advance(self, step: str) -> bool:
        before, after = _STEPS[step]
        with self._lock:
            if self._phase is not before:
                return False
            self._phase = after
        return True

    def _settle(self, outcome: Outcome) -> bool:
        with self._lock:
            if self._phase is not _Phase.SENDING:
                return False
            self._phase = _Phase.DONE
            self._outcome = outcome
        self._done.set()
        return True

    def _judge(self, account: str, response: dict[str, Any]) -> Outcome:
        if account.lower() != self._approval.master.lower():
            return Refused(
                f"the wallet account {account} is not the master {self._approval.master}"
            )
        if response.get("status") == "ok":
            return Accepted(self._approval.valid_until_ms)
        return Refused(f"the exchange answered {json.dumps(response)[:300]}", by_exchange=True)

    def _page(self, nonce: str) -> bytes:
        a = self._approval
        td = typed_data(a)
        network = a.network.name
        return _PAGE.substitute(
            network=html.escape(network),
            chain=html.escape(HYPERLIQUID_CHAIN[network]),
            agent=html.escape(a.agent_address),
            master=html.escape(a.master),
            expiry=html.escape(expiry_text(a.valid_until_ms)),
            days=a.days,
            typed_pre=html.escape(json.dumps(td, indent=2)),
            typed_json=_script_json(td),
            master_json=_script_json(a.master),
            exchange_json=_script_json(a.network.exchange_url),
            nonce=nonce,
        ).encode()

    def _csp(self, nonce: str) -> str:
        parts = urlsplit(self._approval.network.exchange_url)
        return (
            f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
            f"connect-src 'self' {parts.scheme}://{parts.netloc}; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'; img-src data:"
        )


def _script_json(value: object) -> str:
    return json.dumps(value).replace("<", "\\u003c")


def _handler(page: ApprovePage) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        timeout = 10

        def _foreign_host(self) -> bool:
            if self.headers.get_all("Host") == [page.authority]:
                return False
            self.send_error(403, "the Host header does not name this page")
            return True

        def do_GET(self) -> None:
            if self._foreign_host():
                return
            url = urlsplit(self.path)
            if url.path != "/approve":
                self.send_error(404)
                return
            if not page._token_ok(url.query):
                self.send_error(403, "missing or wrong token")
                return
            if page._phase is _Phase.DONE:
                self.send_error(403, "this approval link was already used")
                return
            nonce = secrets.token_urlsafe(16)
            body = page._page(nonce)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", page._csp(nonce))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            if self._foreign_host():
                return
            url = urlsplit(self.path)
            if url.path not in ("/claim", "/send", "/result"):
                self.send_error(404)
                return
            if not page._token_ok(url.query):
                self.send_error(403, "missing or wrong token")
                return
            if self.headers.get_content_type() != "application/json":
                self.send_error(415, "the body must be application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.send_error(400, "bad Content-Length")
                return
            if not 0 <= length <= RESULT_BYTES_MAX:
                self.send_error(413)
                return
            try:
                data = json.loads(self.rfile.read(length))
            except ValueError:
                self.send_error(400, "the body is not JSON")
                return
            if url.path in _STEPS:
                if not page._advance(url.path):
                    self.send_error(403, "this approval link was already used or hl-guard stopped")
                    return
                self._reply({"ok": True})
                return
            account = data.get("account") if isinstance(data, dict) else None
            response = data.get("response") if isinstance(data, dict) else None
            if not isinstance(account, str) or not _ADDRESS.fullmatch(account):
                self.send_error(400, "account must be 0x and 40 hex digits")
                return
            if not isinstance(response, dict):
                self.send_error(400, "response must be the exchange's JSON object")
                return
            outcome = page._judge(account, response)
            if not page._settle(outcome):
                self.send_error(403, "this approval link was already used")
                return
            self._reply({"accepted": isinstance(outcome, Accepted)})

        def _reply(self, answer: dict[str, bool]) -> None:
            body = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return Handler


_PAGE = Template("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hl-guard · approve agent ($network)</title>
<style>
 body { font-family: -apple-system, system-ui, sans-serif; background: #101418; color: #d8dee6;
        max-width: 680px; margin: 40px auto; padding: 0 16px; }
 .card { background: #1a2027; border: 1px solid #2c3540; border-radius: 10px;
         padding: 20px 24px; margin-bottom: 16px; }
 .addr { font-family: ui-monospace, monospace; word-break: break-all; color: #7fd1a8; }
 .warn { color: #e0b060; }
 button { background: #2d6a4f; color: #fff; border: 0; border-radius: 6px;
          padding: 12px 22px; font-size: 15px; cursor: pointer; }
 button:disabled { background: #444; cursor: not-allowed; }
 #wallets button { display: flex; align-items: center; gap: 8px; margin: 8px 0 0; }
 #wallets img { width: 24px; height: 24px; }
 pre { background: #0c1014; padding: 12px; border-radius: 6px; overflow-x: auto; font-size: 12px; }
 #status { margin-top: 12px; }
</style>
</head>
<body>
<h2>Approve the hl-guard agent on Hyperliquid $chain</h2>
<div class="card">
 <div>Agent address (the terminal printed the same one):</div>
 <div class="addr" id="agent">$agent</div>
 <p>Expires <b id="expiry">$expiry</b> ($days days)</p>
 <p>Master wallet: <span class="addr">$master</span></p>
 <p>Chain: <b>Hyperliquid $chain</b></p>
 <p class="warn">The agent can place and cancel orders for the master account until it expires.
 It cannot move funds out.</p>
 <button id="approve">Sign with wallet</button>
 <div id="wallets"></div>
 <div id="status"></div>
</div>
<div class="card">
 <details><summary>Raw typed data</summary>
 <pre id="typed">$typed_pre</pre></details>
</div>
<script nonce="$nonce">
const typedData = $typed_json;
const master = $master_json;
const exchangeUrl = $exchange_json;
const statusEl = document.getElementById('status');
const button = document.getElementById('approve');
const typedPre = document.getElementById('typed');
const token = new URLSearchParams(location.search).get('t');

function show(text) {
  statusEl.textContent = text;
}

async function postJson(url, body) {
  return fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
}

async function local(path, body) {
  return postJson(path + '?t=' + encodeURIComponent(token), body);
}

async function allowed(path) {
  try {
    return (await local(path, {})).ok;
  } catch {
    return false;
  }
}

async function report(body) {
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      if ((await local('/result', body)).ok) return true;
    } catch {
    }
  }
  return false;
}

const WALLET_WAIT = 'Waiting for the wallet to connect... If no wallet window opened, click ' +
  'the wallet extension icon in the toolbar; in MetaMask, close the side panel and open the ' +
  'extension from the toolbar. If another wallet opened instead, reload the page and choose ' +
  'the wallet, or turn off the other wallet extensions.';

const announced = [];
window.addEventListener('eip6963:announceProvider', (event) => {
  const wallet = event.detail;
  if (!wallet || !wallet.info || !wallet.provider) return;
  if (announced.some((seen) => seen.info.uuid === wallet.info.uuid)) return;
  announced.push(wallet);
});
window.dispatchEvent(new Event('eip6963:requestProvider'));
const walletsEl = document.getElementById('wallets');
let chosen = null;

function showChoice() {
  walletsEl.replaceChildren();
  for (const wallet of announced) {
    const choice = document.createElement('button');
    const icon = String(wallet.info.icon || '');
    if (icon.startsWith('data:image/')) {
      const image = document.createElement('img');
      image.src = icon;
      image.alt = '';
      choice.appendChild(image);
    }
    const name = document.createElement('span');
    name.textContent = String(wallet.info.name || 'Unnamed wallet');
    choice.appendChild(name);
    choice.onclick = () => {
      chosen = wallet.provider;
      walletsEl.replaceChildren();
      return button.onclick();
    };
    walletsEl.appendChild(choice);
  }
  show('This browser has ' + announced.length + ' wallets. Choose the one that holds the ' +
    'master wallet ' + master + '.');
}

function walletProvider() {
  if (chosen) return chosen;
  if (announced.length === 1) return announced[0].provider;
  if (announced.length > 1) return null;
  return window.ethereum || undefined;
}

button.onclick = async () => {
  const wallet = walletProvider();
  if (wallet === null) {
    showChoice();
    return;
  }
  if (!wallet) {
    show('No browser wallet found. Open this link in the browser that holds the master wallet.');
    return;
  }
  button.disabled = true;
  let claimed = false;
  show(WALLET_WAIT);
  const noAnswer = setTimeout(() => {
    show('No answer from the wallet yet. ' + WALLET_WAIT);
    button.disabled = false;
  }, 20000);
  try {
    let accounts;
    try {
      accounts = await wallet.request({method: 'eth_requestAccounts'});
    } finally {
      clearTimeout(noAnswer);
    }
    button.disabled = true;
    const account = accounts[0] || '';
    if (account.toLowerCase() !== master.toLowerCase()) {
      show('The wallet account ' + account + ' is not the master ' + master +
        '. Switch the wallet to the master account and reload. Nothing was signed.');
      button.disabled = false;
      return;
    }
    if (!(await allowed('/claim'))) {
      show('hl-guard no longer waits for this link, or another tab used it. Nothing was signed. ' +
        'Run the command again for a new link.');
      return;
    }
    claimed = true;
    const chainId = parseInt(await wallet.request({method: 'eth_chainId'}), 16);
    typedData.domain.chainId = chainId;
    typedData.message.signatureChainId = '0x' + chainId.toString(16);
    typedPre.textContent = JSON.stringify(typedData, null, 2);
    show('Waiting for the wallet, chain id ' + chainId + '...');
    const hex = await wallet.request({
      method: 'eth_signTypedData_v4',
      params: [account, JSON.stringify(typedData)],
    });
    const raw = hex.slice(2);
    const v = parseInt(raw.slice(128, 130), 16);
    const body = {
      action: Object.assign({}, typedData.message, {type: 'approveAgent'}),
      nonce: typedData.message.nonce,
      signature: {r: '0x' + raw.slice(0, 64), s: '0x' + raw.slice(64, 128), v: v < 27 ? v + 27 : v},
      vaultAddress: null,
    };
    if (!(await allowed('/send'))) {
      show('hl-guard stopped waiting while the wallet was open, so the signature was not sent ' +
        'to the exchange. Run the command again for a new link.');
      return;
    }
    show('Sending the approval to the exchange, chain id ' + chainId + '...');
    let answer;
    try {
      answer = await (await postJson(exchangeUrl, body)).json();
    } catch (e) {
      answer = {status: 'err', response: String((e && e.message) || e)};
    }
    if (answer === null || typeof answer !== 'object' || Array.isArray(answer)) {
      answer = {status: 'err', response: JSON.stringify(answer)};
    }
    const noted = await report({account: account, response: answer});
    const verdict = answer.status === 'ok' ? 'Agent approved.' : 'The exchange refused it.';
    show(verdict + ' Chain id ' + chainId + '. Exchange answer: ' + JSON.stringify(answer) +
      (noted ? '. The terminal shows what comes next.' : '. hl-guard did not record it.'));
  } catch (e) {
    const used = '. Nothing was sent to the exchange. Run the command again for a new link.';
    show('Error: ' + ((e && e.message) || e) + (claimed ? used : ''));
    button.disabled = claimed;
  }
};
</script>
</body>
</html>
""")
