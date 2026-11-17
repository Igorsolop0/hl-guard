from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import textwrap
from collections.abc import Iterator
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from hyperliquid.utils.signing import recover_user_from_user_signed_action, user_signed_payload

from hl_guard.exchange.network import NETWORKS, Network
from hl_guard.keys.agent_key import AGENT_NAME, KeySlot, write_key
from hl_guard.keys.approve_page import (
    DAY_MS,
    DEFAULT_VALID_DAYS,
    MAX_VALID_DAYS,
    PRIMARY_TYPE,
    Accepted,
    Approval,
    ApproveAgentError,
    ApprovePage,
    Refused,
    _handler,
    approval,
    typed_data,
)

from .page_client import embedded_typed_data, get, post, result, sending, step

AGENT = "0x" + "ab" * 20
MASTER = "0x" + "cd" * 20
NOW = 1_757_300_000_000
TESTNET = NETWORKS["testnet"]
FIELDS = [
    {"name": "hyperliquidChain", "type": "string"},
    {"name": "agentAddress", "type": "address"},
    {"name": "agentName", "type": "string"},
    {"name": "nonce", "type": "uint64"},
]


def make(**overrides: Any) -> Approval:
    kwargs: dict[str, Any] = {"days": DEFAULT_VALID_DAYS, "now_ms": NOW} | overrides
    return approval(TESTNET, MASTER, AGENT, **kwargs)


@pytest.fixture()
def page() -> Iterator[ApprovePage]:
    served = ApprovePage(make())
    served.start()
    yield served
    served.close()


def base(page: ApprovePage) -> str:
    return page.url.split("/approve")[0]


def token(page: ApprovePage) -> str:
    return page.url.split("?t=")[1]


def test_typed_data_fields_and_defaults() -> None:
    td = typed_data(make())
    valid_until = NOW + DEFAULT_VALID_DAYS * DAY_MS
    assert td["message"] == {
        "hyperliquidChain": "Testnet",
        "agentAddress": AGENT,
        "agentName": f"{AGENT_NAME} valid_until {valid_until}",
        "nonce": NOW,
        "signatureChainId": "0x66eee",
    }
    assert td["primaryType"] == "HyperliquidTransaction:ApproveAgent"
    assert td["domain"] == {
        "name": "HyperliquidSignTransaction",
        "version": "1",
        "chainId": 421614,
        "verifyingContract": "0x0000000000000000000000000000000000000000",
    }
    assert td["types"][PRIMARY_TYPE] == FIELDS
    assert typed_data(approval(NETWORKS["mainnet"], MASTER, AGENT, days=1, now_ms=NOW))["message"][
        "hyperliquidChain"
    ] == ("Mainnet")


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"agent_address": "0x1234"}, "address"),
        ({"agent_address": "0x</script><script>alert(1)</script>padpad!"}, "hex"),
        ({"agent_address": "0x" + "zz" * 20}, "hex"),
        ({"agent_name": ""}, "agent_name"),
        ({"agent_name": "bad name!"}, "agent_name"),
        ({"nonce": -1}, "nonce"),
        ({"nonce": 2**64}, "nonce"),
        ({"valid_until_ms": NOW - 1}, "valid_until"),
        ({"valid_until_ms": NOW + 181 * DAY_MS}, "cap of 180 days"),
    ],
)
def test_approval_rejects_bad_inputs(overrides: dict[str, Any], match: str) -> None:
    fields: dict[str, Any] = {
        "network": TESTNET,
        "master": MASTER,
        "agent_address": AGENT,
        "agent_name": AGENT_NAME,
        "nonce": NOW,
        "valid_until_ms": NOW + DAY_MS,
    } | overrides
    with pytest.raises(ApproveAgentError, match=match):
        Approval(**fields)


def test_typed_data_hashes_like_the_sdk_payload() -> None:
    a = make()
    wire = typed_data(a)["message"]
    sdk = user_signed_payload(PRIMARY_TYPE, FIELDS, dict(wire))
    wallet = Account.from_key("0x" + "33" * 32)
    ours = wallet.sign_message(encode_typed_data(full_message=typed_data(a)))
    theirs = wallet.sign_message(encode_typed_data(full_message=sdk))
    assert ours.signature == theirs.signature


def test_the_expiry_in_the_name_changes_the_signed_hash() -> None:
    wallet = Account.from_key("0x" + "33" * 32)
    one, two = make(days=30), make(days=31)
    s1 = wallet.sign_message(encode_typed_data(full_message=typed_data(one)))
    s2 = wallet.sign_message(encode_typed_data(full_message=typed_data(two)))
    assert s1.signature != s2.signature


def test_the_full_typed_data_recovers_the_signer() -> None:
    wallet = Account.create()
    signable = encode_typed_data(full_message=typed_data(make()))
    sig = wallet.sign_message(signable)
    assert Account.recover_message(signable, signature=sig.signature) == wallet.address


def test_the_page_binds_loopback_and_carries_a_long_token(page: ApprovePage) -> None:
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/approve\?t=[\w-]{43}", page.url)
    assert ":0/" not in page.url


def test_the_page_shows_the_address_never_the_key(tmp_path: Path) -> None:
    slot = KeySlot(tmp_path / "keys" / "agent.key")
    address = write_key(slot.key_path, overwrite=False)
    secret = slot.key_path.read_text().strip()
    served = ApprovePage(approval(TESTNET, MASTER, address, days=30, now_ms=NOW))
    served.start()
    try:
        status, _, body = get(served.url)
    finally:
        served.close()
    assert status == 200
    assert address in body and MASTER in body
    assert secret not in body and secret.removeprefix("0x") not in body
    assert "30 days" in body and "HyperliquidTransaction:ApproveAgent" in body


def test_the_page_needs_its_token(page: ApprovePage) -> None:
    assert get(base(page) + "/approve")[0] == 403
    assert get(base(page) + "/approve?t=wrong")[0] == 403
    assert get(base(page) + f"/approve?t={token(page)}&t={token(page)}")[0] == 403
    assert get(base(page) + f"/admin?t={token(page)}")[0] == 404
    status, _, body = get(page.url)
    assert status == 200 and AGENT in body and "valid_until" in body
    assert post(base(page) + "/result?t=wrong", result(MASTER, {"status": "ok"})) == 403
    assert page.wait(0) is None


@pytest.mark.parametrize("days", [0, -1, 181, 200])
def test_days_outside_one_to_180_are_refused(days: int) -> None:
    with pytest.raises(ApproveAgentError, match="180 days"):
        make(days=days)


def test_the_constants() -> None:
    assert DEFAULT_VALID_DAYS == 30
    assert MAX_VALID_DAYS == 180


def test_embedded_json_cannot_close_the_script(page: ApprovePage) -> None:
    body = get(page.url)[2]
    script = body.split("<script", 1)[1].split("</script>", 1)[0]
    assert "</" not in script
    assert embedded_typed_data(body) == typed_data(make())
    pre = body.split('<pre id="typed">', 1)[1].split("</pre>", 1)[0]
    assert "<" not in pre and "&quot;HyperliquidSignTransaction&quot;" in pre


def test_embedded_text_is_escaped_for_the_script() -> None:
    hostile = Network("testnet", "https://ex.test/</script><b>", TESTNET.info_url, "b")
    served = ApprovePage(approval(hostile, MASTER, AGENT, days=1, now_ms=NOW))
    served.start()
    try:
        body = get(served.url)[2]
    finally:
        served.close()
    line = re.search(r"const exchangeUrl = (.*);\n", body)
    assert line and "<" not in line.group(1)
    assert json.loads(line.group(1)) == "https://ex.test/</script><b>"


def test_the_page_shows_the_expiry_date_and_day_count() -> None:
    a = approval(TESTNET, MASTER, AGENT, days=30, now_ms=1_767_225_600_000)
    served = ApprovePage(a)
    served.start()
    try:
        body = get(served.url)[2]
    finally:
        served.close()
    assert "2026-01-31 00:00 UTC" in body and "(30 days)" in body
    assert body.count("<button") == 1 and "Sign with wallet" in body
    assert "<title>hl-guard · approve agent (testnet)</title>" in body


@pytest.mark.parametrize(
    ("length", "body", "code"),
    [
        ("-1", b"{}", 413),
        (str(64 * 1024 + 1), b"{}", 413),
        ("x", b"{}", 400),
        (None, b"[1, 2]", 400),
        (None, b"{ not json", 400),
        (None, result("nope", {"status": "ok"}), 400),
        (None, result(MASTER, "ok"), 400),
        (None, json.dumps({"account": MASTER}).encode(), 400),
    ],
)
def test_the_result_post_is_hardened(
    page: ApprovePage, length: str | None, body: bytes, code: int
) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", int(base(page).rsplit(":", 1)[1]), timeout=5)
    try:
        conn.putrequest("POST", f"/result?t={token(page)}")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", length if length is not None else str(len(body)))
        conn.endheaders()
        conn.send(body)
        assert conn.getresponse().status == code
    finally:
        conn.close()
    assert page.wait(0) is None
    assert get(page.url)[0] == 200


@pytest.mark.parametrize("path", ["/claim", "/send", "/result"])
@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded", "application/jsonx"]
)
def test_every_post_must_be_json(page: ApprovePage, content_type: str | None, path: str) -> None:
    if path != "/claim":
        assert step(page.url, "/claim") == 200
    if path == "/result":
        assert step(page.url, "/send") == 200
    url = base(page) + f"{path}?t={token(page)}"
    conn = http.client.HTTPConnection("127.0.0.1", int(base(page).rsplit(":", 1)[1]), timeout=5)
    body = result(MASTER, {"status": "ok"})
    try:
        conn.putrequest("POST", url.split(base(page), 1)[1])
        if content_type is not None:
            conn.putheader("Content-Type", content_type)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        conn.send(body)
        assert conn.getresponse().status == 415
    finally:
        conn.close()
    assert page.wait(0) is None
    assert post(url, body, {"Content-Type": "application/json; charset=utf-8"}) == 200


@pytest.mark.parametrize("host", ["evil.test", "localhost:{port}", "127.0.0.1", "127.0.0.1:1"])
def test_a_foreign_host_header_is_refused(page: ApprovePage, host: str) -> None:
    port = base(page).rsplit(":", 1)[1]
    forged = {"Host": host.format(port=port)}
    assert get(page.url, forged)[0] == 403
    for path in ("/claim", "/send", "/result"):
        url = base(page) + f"{path}?t={token(page)}"
        assert post(url, result(MASTER, {"status": "ok"}), forged) == 403
    assert page.wait(0) is None
    assert sending(page.url)


def test_the_ipv6_page_checks_the_bracketed_host() -> None:
    served = ApprovePage(make(), host="::1")
    try:
        served.start()
    except OSError:
        pytest.skip("no IPv6 loopback")
    try:
        assert get(served.url)[0] == 200
        assert get(served.url, {"Host": "::1"})[0] == 403
    finally:
        served.close()


def test_a_result_that_lands_during_close_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    served = ApprovePage(make())
    served.start()
    assert sending(served.url)
    url = base(served) + f"/result?t={token(served)}"
    real = ThreadingHTTPServer.shutdown
    codes: list[int] = []

    def late_result_then_shutdown(httpd: ThreadingHTTPServer) -> None:
        codes.append(post(url, result(MASTER, {"status": "ok"})))
        real(httpd)

    monkeypatch.setattr(ThreadingHTTPServer, "shutdown", late_result_then_shutdown)
    served.close()
    assert codes == [403]
    assert served.wait(0) is None


def test_the_handler_drops_a_silent_client_after_ten_seconds(page: ApprovePage) -> None:
    assert _handler(page).timeout == 10


def test_a_wrong_wallet_account_is_refused(page: ApprovePage) -> None:
    other = "0x" + "ef" * 20
    assert sending(page.url)
    assert post(base(page) + f"/result?t={token(page)}", result(other, {"status": "ok"})) == 200
    outcome = page.wait(1)
    assert isinstance(outcome, Refused) and other in outcome.reason


def test_an_exchange_error_is_refused_with_its_text(page: ApprovePage) -> None:
    answer = {"status": "err", "response": "User or API Wallet does not exist."}
    assert sending(page.url)
    assert post(base(page) + f"/result?t={token(page)}", result(MASTER, answer)) == 200
    outcome = page.wait(1)
    assert isinstance(outcome, Refused) and "does not exist" in outcome.reason


def test_the_master_matches_case_insensitively(page: ApprovePage) -> None:
    mixed = "0x" + MASTER[2:].upper()
    assert sending(page.url)
    assert post(base(page) + f"/result?t={token(page)}", result(mixed, {"status": "ok"})) == 200
    assert page.wait(1) == Accepted(NOW + 30 * DAY_MS)


def test_the_token_is_spent_by_the_first_result(page: ApprovePage) -> None:
    url = base(page) + f"/result?t={token(page)}"
    assert sending(page.url)
    assert post(url, result(MASTER, {"status": "ok"})) == 200
    status, _, body = get(page.url)
    assert status == 403 and "already used" in body
    assert post(url, result(MASTER, {"status": "err"})) == 403
    assert page.wait(0) == Accepted(NOW + 30 * DAY_MS)


def test_each_step_is_taken_once_and_in_order(page: ApprovePage) -> None:
    url = base(page) + f"/result?t={token(page)}"
    answer = result(MASTER, {"status": "ok"})
    assert step(page.url, "/send") == 403
    assert post(url, answer) == 403
    assert step(page.url, "/claim") == 200
    assert step(page.url, "/claim") == 403
    assert post(url, answer) == 403
    assert step(page.url, "/send") == 200
    assert step(page.url, "/send") == 403
    assert page.wait(0) is None
    assert post(url, answer) == 200
    assert step(page.url, "/claim") == 403
    assert page.wait(0) == Accepted(NOW + 30 * DAY_MS)


def test_a_step_needs_the_token(page: ApprovePage) -> None:
    wrong = page.url.replace(token(page), "wrong")
    assert step(wrong, "/claim") == 403
    assert step(page.url, "/claim") == 200


@pytest.mark.parametrize("name", ["тест-агент", "agent²", "a b"])
def test_agent_names_are_ascii_only(name: str) -> None:
    with pytest.raises(ApproveAgentError, match="ASCII"):
        make(agent_name=name)


def test_the_page_sends_its_security_headers(page: ApprovePage) -> None:
    _, first, _ = get(page.url)
    _, second, _ = get(page.url)
    assert first["Content-Type"] == "text/html; charset=utf-8"
    assert first["Cache-Control"] == "no-store"
    assert first["Referrer-Policy"] == "no-referrer"
    assert first["X-Frame-Options"] == "DENY"
    csp = first["Content-Security-Policy"]
    nonce = re.search(r"script-src 'nonce-([\w-]+)'", csp)
    assert nonce
    assert csp == (
        f"default-src 'none'; script-src 'nonce-{nonce.group(1)}'; style-src 'unsafe-inline'; "
        "connect-src 'self' https://api.hyperliquid-testnet.xyz; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'; img-src data:"
    )
    assert second["Content-Security-Policy"] != csp


def test_the_inline_script_is_valid_js(page: ApprovePage, tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    body = get(page.url)[2]
    script = tmp_path / "page.js"
    script.write_text(body.split("<script", 1)[1].split(">", 1)[1].split("</script>", 1)[0])
    checked = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_the_wire_name_length_is_checked() -> None:
    with pytest.raises(ApproveAgentError, match="too long"):
        make(agent_name="a" * 70)
    assert len(make(agent_name="a" * 60).wire_name) <= 90


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.2", "localhost", "::", "example.com"])
def test_a_non_loopback_host_is_refused_before_binding(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound: list[object] = []
    monkeypatch.setattr("socketserver.TCPServer.server_bind", lambda self: bound.append(self))
    with pytest.raises(ApproveAgentError, match="loopback"):
        ApprovePage(make(), host=host)
    assert bound == []


@pytest.mark.parametrize("host", ["::1%lo0", "::1%1", "fe80::1%lo0"])
def test_a_zone_id_is_refused(host: str) -> None:
    with pytest.raises(ApproveAgentError, match="zone"):
        ApprovePage(make(), host=host)


@pytest.mark.parametrize("host", ["0:0:0:0:0:0:0:1", "0000::0001", "::1"])
def test_a_long_form_ipv6_host_serves_the_address_the_browser_sends(host: str) -> None:
    served = ApprovePage(make(), host=host)
    served.start()
    try:
        assert served.url.startswith("http://[::1]:")
        port = served.url.split("]:", 1)[1].split("/", 1)[0]
        status, _, _ = get(served.url, {"Host": f"[::1]:{port}"})
        assert status == 200
    finally:
        served.close()


def test_close_is_idempotent() -> None:
    served = ApprovePage(make())
    served.close()
    served.start()
    served.close()
    served.close()


NODE_DRIVER = textwrap.dedent(
    """
    const fs = require('fs');
    const [script, pageUrl, hex] = process.argv.slice(2);
    const page = new URL(pageUrl);
    const els = {};
    const element = (tag) => ({
      tag, children: [],
      appendChild(child) { this.children.push(child); },
      replaceChildren() { this.children = []; },
    });
    globalThis.document = {
      getElementById: (id) => (els[id] ||= Object.assign(element('div'), {id: id})),
      createElement: element,
    };
    globalThis.location = {search: page.search};
    const asked = [];
    const askedBy = {};
    const wallet = (name) => ({request: async ({method, params}) => {
      asked.push({method, params});
      (askedBy[name] ||= []).push(method);
      if (method === 'eth_requestAccounts') return [process.env.WALLET];
      if (method === 'eth_chainId') return '0x1';
      if (method === 'eth_signTypedData_v4' && process.env.HOLD) {
        console.log('signing');
        await new Promise((resume) => process.stdin.once('data', resume));
        process.stdin.pause();
      }
      if (method === 'eth_signTypedData_v4') return hex;
      throw new Error('unexpected ' + method);
    }});
    globalThis.window = new EventTarget();
    if (process.env.WALLET && !process.env.ANNOUNCE) window.ethereum = wallet('window.ethereum');
    if (process.env.ANNOUNCE) {
      window.ethereum = wallet('window.ethereum');
      const names = process.env.ANNOUNCE.split(',');
      window.addEventListener('eip6963:requestProvider', () => names.forEach((name, i) => {
        const info = {uuid: 'u' + i, name, icon: 'data:image/svg+xml;base64,PHN2Zy8+', rdns: name};
        window.dispatchEvent(
          new CustomEvent('eip6963:announceProvider', {detail: {info, provider: wallet(name)}}));
      }));
    }
    const realFetch = fetch;
    let posted;
    let resultFailures = Number(process.env.FAIL_RESULT || 0);
    const steps = [];
    globalThis.fetch = async (url, init) => {
      if (url.startsWith('/')) {
        steps.push(url.split('?')[0]);
        const failing = url.startsWith('/result') && resultFailures-- > 0;
        if (failing) throw new TypeError('Failed to fetch');
        return realFetch(new URL(url, page.origin), init);
      }
      posted = {url, body: JSON.parse(init.body)};
      return {json: async () => ({status: 'ok', response: {type: 'default'}})};
    };
    eval(fs.readFileSync(script, 'utf8'));
    (async () => {
      await els.approve.onclick();
      const choices = (els.wallets.children || []).map(
        (choice) => ({name: choice.children.at(-1).textContent, icon: choice.children[0].src}));
      const pick = (els.wallets.children || []).find(
        (choice) => choice.children.at(-1).textContent === process.env.PICK);
      const chooseStatus = els.status.textContent;
      if (pick) await pick.onclick();
      console.log(JSON.stringify({
        asked, askedBy, choices, chooseStatus, posted, steps,
        results: steps.filter((step) => step === '/result'),
        status: els.status.textContent,
        typed: els.typed.textContent, disabled: els.approve.disabled,
      }));
    })();
    """
)


def page_script(served: ApprovePage, tmp_path: Path) -> Path:
    body = get(served.url)[2]
    script = tmp_path / "page.js"
    script.write_text(body.split("<script", 1)[1].split(">", 1)[1].split("</script>", 1)[0])
    return script


def node_driver(tmp_path: Path, script: Path, url: str, signed_hex: str) -> list[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    driver = tmp_path / "driver.js"
    driver.write_text(NODE_DRIVER)
    return [node, str(driver), str(script), url, signed_hex]


def drive(
    served: ApprovePage, tmp_path: Path, signed_hex: str, script: Path | None = None, **env: str
) -> dict[str, Any]:
    script = script or page_script(served, tmp_path)
    ran = subprocess.run(
        node_driver(tmp_path, script, served.url, signed_hex),
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin"} | env,
    )
    assert ran.returncode == 0, ran.stderr
    out: dict[str, Any] = json.loads(ran.stdout)
    return out


def signed_for_chain_1(
    served: ApprovePage, wallet: LocalAccount, v_offset: int = 0
) -> tuple[str, dict[str, Any], int]:
    td = embedded_typed_data(get(served.url)[2])
    td["domain"]["chainId"] = 1
    td["message"]["signatureChainId"] = "0x1"
    sig = wallet.sign_message(encode_typed_data(full_message=td))
    return "0x" + sig.signature[:64].hex() + f"{sig.v - v_offset:02x}", td, sig.v


@pytest.fixture()
def wallet_page() -> Iterator[tuple[ApprovePage, LocalAccount]]:
    wallet: LocalAccount = Account.create()
    served = ApprovePage(approval(TESTNET, wallet.address.lower(), AGENT, days=30, now_ms=NOW))
    served.start()
    yield served, wallet
    served.close()


@pytest.mark.parametrize("v_offset", [0, 27], ids=["v 27 or 28", "v 0 or 1"])
def test_the_page_script_posts_a_body_the_exchange_recovers(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount], v_offset: int
) -> None:
    served, wallet = wallet_page
    signed_hex, td, v = signed_for_chain_1(served, wallet, v_offset)
    out = drive(served, tmp_path, signed_hex, WALLET=wallet.address)
    assert out["posted"]["url"] == TESTNET.exchange_url
    sent = out["posted"]["body"]
    assert sent["action"]["type"] == "approveAgent"
    assert sent["action"]["signatureChainId"] == "0x1"
    assert sent["vaultAddress"] is None and sent["nonce"] == NOW
    assert sent["signature"]["v"] == v
    assert json.loads(out["asked"][2]["params"][1]) == td
    recovered = recover_user_from_user_signed_action(
        dict(sent["action"]), sent["signature"], FIELDS, PRIMARY_TYPE, False
    )
    assert recovered == wallet.address
    assert served.wait(5) == Accepted(NOW + 30 * DAY_MS)
    assert "Agent approved" in out["status"] and "Chain id 1" in out["status"]


def test_the_page_shows_the_typed_data_the_wallet_signs(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    out = drive(served, tmp_path, signed_for_chain_1(served, wallet)[0], WALLET=wallet.address)
    assert json.loads(out["typed"]) == json.loads(out["asked"][2]["params"][1])
    assert json.loads(out["typed"])["domain"]["chainId"] == 1


def test_a_failed_result_post_is_retried_once_without_asking_the_wallet_again(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    out = drive(
        served,
        tmp_path,
        signed_for_chain_1(served, wallet)[0],
        WALLET=wallet.address,
        FAIL_RESULT="1",
    )
    assert len(out["results"]) == 2
    assert [a["method"] for a in out["asked"]].count("eth_signTypedData_v4") == 1
    assert served.wait(5) == Accepted(NOW + 30 * DAY_MS)
    assert out["disabled"] is True


def test_the_button_stays_off_once_the_exchange_was_asked(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    out = drive(
        served,
        tmp_path,
        signed_for_chain_1(served, wallet)[0],
        WALLET=wallet.address,
        FAIL_RESULT="2",
    )
    assert len(out["results"]) == 2
    assert out["posted"]["url"] == TESTNET.exchange_url
    assert out["disabled"] is True
    assert "did not record it" in out["status"]
    assert served.wait(0) is None


def test_the_page_refuses_a_wallet_on_another_account_before_signing(tmp_path: Path) -> None:
    served = ApprovePage(make())
    served.start()
    try:
        out = drive(served, tmp_path, "0x", WALLET="0x" + "ef" * 20)
    finally:
        served.close()
    assert [a["method"] for a in out["asked"]] == ["eth_requestAccounts"]
    assert out.get("posted") is None
    assert "Nothing was signed" in out["status"]
    assert out["disabled"] is False
    assert served.wait(0) is None


def test_the_page_names_a_missing_wallet(tmp_path: Path) -> None:
    served = ApprovePage(make())
    served.start()
    try:
        out = drive(served, tmp_path, "0x")
        outcome = served.wait(0.2)
    finally:
        served.close()
    assert out["asked"] == [] and out.get("posted") is None
    assert "No browser wallet found" in out["status"]
    assert outcome is None


@pytest.mark.parametrize("host", ["::ffff:127.0.0.1", "::ffff:7f00:1", "0:0:0:0:0:ffff:7f00:1"])
def test_an_ipv4_mapped_host_is_refused_for_the_plain_ipv4_form(host: str) -> None:
    with pytest.raises(ApproveAgentError, match=r"IPv4-mapped .* use 127\.0\.0\.1"):
        ApprovePage(make(), host=host)


def test_an_ipv4_mapped_public_host_is_refused() -> None:
    with pytest.raises(ApproveAgentError, match=r"IPv4-mapped .* use 192\.0\.2\.1"):
        ApprovePage(make(), host="::ffff:192.0.2.1")


def methods(out: dict[str, Any]) -> list[str]:
    return [asked["method"] for asked in out["asked"]]


def test_a_tab_left_open_after_the_command_stopped_signs_nothing(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    signed_hex = signed_for_chain_1(served, wallet)[0]
    script = page_script(served, tmp_path)
    served.close()
    out = drive(served, tmp_path, signed_hex, script, WALLET=wallet.address)
    assert "eth_signTypedData_v4" not in methods(out)
    assert out.get("posted") is None
    assert "no longer waits for this link" in out["status"]
    assert "Nothing was signed" in out["status"]
    assert out["disabled"] is True


def test_a_second_tab_signs_nothing_once_the_first_settled(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    signed_hex = signed_for_chain_1(served, wallet)[0]
    (tmp_path / "second").mkdir()
    second = page_script(served, tmp_path / "second")
    first = drive(served, tmp_path, signed_hex, WALLET=wallet.address)
    assert first["posted"]["url"] == TESTNET.exchange_url
    out = drive(served, tmp_path, signed_hex, second, WALLET=wallet.address)
    assert "eth_signTypedData_v4" not in methods(out) and out.get("posted") is None
    assert served.wait(0) == Accepted(NOW + 30 * DAY_MS)


def test_a_wallet_left_open_past_the_timeout_sends_nothing_to_the_exchange(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    signed_hex = signed_for_chain_1(served, wallet)[0]
    script = page_script(served, tmp_path)
    env = {"PATH": "/usr/bin:/bin", "WALLET": wallet.address, "HOLD": "1"}
    args = node_driver(tmp_path, script, served.url, signed_hex)
    with subprocess.Popen(
        args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env
    ) as held:
        assert held.stdin is not None and held.stdout is not None
        assert held.stdout.readline() == "signing\n"
        served.close()
        rest, _ = held.communicate("go\n", timeout=30)
    out: dict[str, Any] = json.loads(rest.strip().splitlines()[-1])
    assert methods(out).count("eth_signTypedData_v4") == 1
    assert out.get("posted") is None
    assert "the signature was not sent" in out["status"]
    assert served.wait(0) is None


SILENT_WALLET_DRIVER = textwrap.dedent(
    """
    const fs = require('fs');
    const els = {};
    globalThis.document = {getElementById: (id) => (els[id] ||= {id: id})};
    globalThis.location = {search: '?t=x'};
    const asked = [];
    globalThis.window = Object.assign(new EventTarget(), {ethereum: {request: ({method}) => {
      asked.push(method);
      return new Promise(() => {});
    }}});
    const timers = [];
    globalThis.setTimeout = (run, ms) => timers.push({run, ms});
    globalThis.clearTimeout = () => {};
    eval(fs.readFileSync(process.argv[2], 'utf8'));
    els.approve.onclick();
    const waiting = {status: els.status.textContent, disabled: els.approve.disabled};
    timers.forEach((timer) => timer.run());
    console.log(JSON.stringify({
      asked, waiting, ms: timers.map((timer) => timer.ms),
      after: {status: els.status.textContent, disabled: els.approve.disabled},
    }));
    """
)


def test_the_page_says_it_waits_for_the_wallet_and_hints_when_it_stays_silent(
    page: ApprovePage, tmp_path: Path
) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    driver = tmp_path / "silent.js"
    driver.write_text(SILENT_WALLET_DRIVER)
    ran = subprocess.run(
        [node, str(driver), str(page_script(page, tmp_path))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stderr
    out = json.loads(ran.stdout)
    assert out["asked"] == ["eth_requestAccounts"]
    assert out["waiting"]["status"].startswith("Waiting for the wallet to connect")
    assert "close the side panel" in out["waiting"]["status"]
    assert out["waiting"]["disabled"] is True
    assert out["ms"] == [20000]
    assert out["after"]["status"].startswith("No answer from the wallet yet.")
    assert out["after"]["disabled"] is False


def test_one_announced_wallet_gets_every_request(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    signed_hex = signed_for_chain_1(served, wallet)[0]
    out = drive(served, tmp_path, signed_hex, WALLET=wallet.address, ANNOUNCE="MetaMask")
    assert out["choices"] == []
    assert out["askedBy"] == {
        "MetaMask": ["eth_requestAccounts", "eth_chainId", "eth_signTypedData_v4"]
    }
    assert served.wait(5) == Accepted(NOW + 30 * DAY_MS)


def test_two_announced_wallets_ask_for_a_choice_and_only_the_chosen_one_is_asked(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    signed_hex = signed_for_chain_1(served, wallet)[0]
    out = drive(
        served,
        tmp_path,
        signed_hex,
        WALLET=wallet.address,
        ANNOUNCE="Phantom,MetaMask",
        PICK="MetaMask",
    )
    assert [c["name"] for c in out["choices"]] == ["Phantom", "MetaMask"]
    assert all(c["icon"].startswith("data:image/") for c in out["choices"])
    assert "2 wallets" in out["chooseStatus"]
    assert out["askedBy"] == {
        "MetaMask": ["eth_requestAccounts", "eth_chainId", "eth_signTypedData_v4"]
    }
    assert out["posted"]["url"] == TESTNET.exchange_url
    assert served.wait(5) == Accepted(NOW + 30 * DAY_MS)


def test_two_announced_wallets_ask_none_before_the_choice(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    out = drive(served, tmp_path, "0x", WALLET=wallet.address, ANNOUNCE="Phantom,MetaMask")
    assert len(out["choices"]) == 2
    assert out["askedBy"] == {}
    assert out.get("posted") is None
    assert out.get("disabled") is not True
    assert served.wait(0) is None


def test_with_no_announced_wallet_the_page_falls_back_to_window_ethereum(
    tmp_path: Path, wallet_page: tuple[ApprovePage, LocalAccount]
) -> None:
    served, wallet = wallet_page
    out = drive(served, tmp_path, signed_for_chain_1(served, wallet)[0], WALLET=wallet.address)
    assert out["choices"] == []
    assert out["askedBy"] == {
        "window.ethereum": ["eth_requestAccounts", "eth_chainId", "eth_signTypedData_v4"]
    }
    assert served.wait(5) == Accepted(NOW + 30 * DAY_MS)
