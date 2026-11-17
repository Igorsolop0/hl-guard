import json
from pathlib import Path
from typing import Any

import pytest
from eth_account import Account
from eth_account.messages import encode_typed_data

from hl_guard.cli.__main__ import main
from hl_guard.exchange import transport
from hl_guard.exchange.probe import EXIT_REFUSED, EXIT_UNVERIFIED, EXIT_VERIFIED, probe_sign
from hl_guard.exchange.signer import action_hash, l1_payload


class _Resp:
    status = 200

    def __init__(self, body: Any) -> None:
        self._body = json.dumps(body).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _fake_exchange(monkeypatch: pytest.MonkeyPatch, source: str = "b") -> list[Any]:
    posted: list[Any] = []

    def urlopen(request: Any, timeout: float | None = None) -> _Resp:
        body = json.loads(request.data.decode())
        posted.append(body)
        digest = action_hash(body["action"], body["nonce"], body["expiresAfter"])
        signable = encode_typed_data(
            full_message=l1_payload({"source": source, "connectionId": digest})
        )
        sig = body["signature"]
        vrs = (sig["v"], int(sig["r"], 16), int(sig["s"], 16))
        recovered = Account.recover_message(signable, vrs=vrs).lower()
        return _Resp(
            {"status": "err", "response": f"User or API Wallet {recovered} does not exist."}
        )

    monkeypatch.setattr(transport._OPENER, "open", urlopen)
    return posted


def test_probe_verifies_when_the_exchange_recovers_the_agent(
    monkeypatch: pytest.MonkeyPatch, durable_dir: Path
) -> None:
    posted = _fake_exchange(monkeypatch)
    code, report = probe_sign("testnet", durable_dir)
    assert code == EXIT_VERIFIED
    assert report["signature_verified"] is True
    assert report["recovered_address"] == report["agent_address"].lower()
    assert len(posted) == 1
    order = posted[0]["action"]["orders"][0]
    assert order["s"] == "0"
    assert posted[0]["action"]["type"] == "order"


def test_probe_reports_a_signature_the_exchange_recovers_to_another_address(
    monkeypatch: pytest.MonkeyPatch, durable_dir: Path
) -> None:
    _fake_exchange(monkeypatch, source="a")
    code, report = probe_sign("testnet", durable_dir)
    assert code == EXIT_UNVERIFIED
    assert report["signature_verified"] is False


def test_probe_refuses_mainnet_without_posting(
    monkeypatch: pytest.MonkeyPatch, durable_dir: Path
) -> None:
    posted = _fake_exchange(monkeypatch)
    code, report = probe_sign("mainnet", durable_dir)
    assert code == EXIT_REFUSED
    assert "testnet only" in report["refused"]
    assert posted == []


def test_probe_refuses_while_halted_without_posting(
    monkeypatch: pytest.MonkeyPatch, durable_dir: Path
) -> None:
    posted = _fake_exchange(monkeypatch)
    (durable_dir / "HALT").write_text("stop\n")
    code, report = probe_sign("testnet", durable_dir)
    assert code == EXIT_REFUSED
    assert "halt flag" in report["refused"]
    assert posted == []


def test_probe_refuses_an_unpinned_url_without_posting(
    monkeypatch: pytest.MonkeyPatch, durable_dir: Path
) -> None:
    posted = _fake_exchange(monkeypatch)
    code, report = probe_sign("testnet", durable_dir, url="https://evil.example.com/exchange")
    assert code == EXIT_REFUSED
    assert "pinned" in report["refused"]
    assert posted == []


def test_cli_prints_the_report_and_exits_with_its_code(
    monkeypatch: pytest.MonkeyPatch, durable_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_exchange(monkeypatch)
    with pytest.raises(SystemExit) as exit_info:
        main(["probe-sign", "--network", "testnet", "--data-dir", str(durable_dir)])
    assert exit_info.value.code == EXIT_VERIFIED
    report = json.loads(capsys.readouterr().out)
    assert report["signature_verified"] is True


def test_probe_refuses_a_temp_data_dir_without_posting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    posted = _fake_exchange(monkeypatch)
    code, report = probe_sign("testnet", tmp_path)
    assert code == EXIT_REFUSED
    assert "temp" in report["refused"]
    assert posted == []


@pytest.mark.parametrize(
    "template",
    ["User or API Wallet {addr}ff does not exist.", "hash 0x00{bare} seen"],
)
def test_probe_ignores_an_address_glued_into_a_longer_hex_run(
    monkeypatch: pytest.MonkeyPatch, durable_dir: Path, template: str
) -> None:
    def urlopen(request: Any, timeout: float | None = None) -> _Resp:
        body = json.loads(request.data.decode())
        digest = action_hash(body["action"], body["nonce"], body["expiresAfter"])
        signable = encode_typed_data(
            full_message=l1_payload({"source": "b", "connectionId": digest})
        )
        sig = body["signature"]
        vrs = (sig["v"], int(sig["r"], 16), int(sig["s"], 16))
        addr = Account.recover_message(signable, vrs=vrs).lower()
        text = template.format(addr=addr, bare=addr.removeprefix("0x"))
        return _Resp({"status": "err", "response": text})

    monkeypatch.setattr(transport._OPENER, "open", urlopen)
    code, report = probe_sign("testnet", durable_dir)
    assert code == EXIT_UNVERIFIED
    assert report["recovered_address"] is None


def test_probe_exits_refused_when_the_signer_refuses(
    monkeypatch: pytest.MonkeyPatch, durable_dir: Path
) -> None:
    from hl_guard.exchange import signer

    posted = _fake_exchange(monkeypatch)

    def refuse(*args: object, **kwargs: object) -> None:
        raise signer.SignerError("refused for the test")

    monkeypatch.setattr(signer.Signer, "sign_action", refuse)
    code, report = probe_sign("testnet", durable_dir)
    assert code == EXIT_REFUSED
    assert "signer refused" in report["refused"]
    assert posted == []
