from __future__ import annotations

import datetime
import functools
import json
import socket
import ssl
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from hl_guard.exchange.network import network_for
from hl_guard.server import info as info_module
from hl_guard.server.info import RESPONSE_BYTES_MAX, InfoClient, InfoUnavailable, WeightBudget

PINNED = "api.hyperliquid-testnet.xyz"
BOOK = {"coin": "BTC", "levels": [[], []], "time": 1}


@dataclass
class Issuer:
    name: x509.Name
    key: ec.EllipticCurvePrivateKey
    cert: x509.Certificate | None = None

    def sign(
        self, subject: x509.Name, key: ec.EllipticCurvePrivateKey, host: str | None
    ) -> x509.Certificate:
        now = datetime.datetime.now(datetime.UTC)
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(self.name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=host is None, path_length=None), True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
            .add_extension(
                x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), False
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=host is not None,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=host is None,
                    crl_sign=host is None,
                    encipher_only=False,
                    decipher_only=False,
                ),
                True,
            )
        )
        if host is not None:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(host)]), False
            ).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        return builder.sign(self.key, hashes.SHA256())


def _key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _name(common: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])


def _ca(common: str) -> Issuer:
    issuer = Issuer(_name(common), _key())
    issuer.cert = issuer.sign(issuer.name, issuer.key, None)
    return issuer


def _pem(path: Path, *certs: x509.Certificate) -> Path:
    path.write_bytes(b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs))
    return path


def _leaf(tmp_path: Path, label: str, issuer: Issuer | None, host: str) -> tuple[Path, Path]:
    key = _key()
    signer = issuer or Issuer(_name(host), key)
    cert = signer.sign(_name(host), key, host)
    key_path = tmp_path / f"{label}.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return _pem(tmp_path / f"{label}.pem", cert), key_path


def _head(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65536)
        if not chunk:
            break
        data += chunk
    return data


def ok(conn: ssl.SSLSocket) -> None:
    data = json.dumps(BOOK).encode()
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(data), data))


def chunked_past_the_cap(conn: ssl.SSLSocket) -> None:
    conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
    chunk = b" " * 65536
    for _ in range(RESPONSE_BYTES_MAX // len(chunk) + 1):
        conn.sendall(b"%x\r\n%s\r\n" % (len(chunk), chunk))
    conn.sendall(b"0\r\n\r\n")


@dataclass
class Tunnel:
    cert: Path
    key: Path
    answer: Callable[[ssl.SSLSocket], None] = ok
    connects: list[bytes] = field(default_factory=list)
    first_bytes: list[bytes] = field(default_factory=list)
    server_names: list[str | None] = field(default_factory=list)
    requests: list[bytes] = field(default_factory=list)

    def serve(self, conn: socket.socket) -> None:
        with conn:
            self.connects.append(_head(conn))
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.first_bytes.append(conn.recv(1, socket.MSG_PEEK))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.cert, self.key)
            context.sni_callback = lambda _sock, name, _ctx: self.server_names.append(name)
            try:
                with context.wrap_socket(conn, server_side=True) as tls:
                    head, _, body = _head(tls).partition(b"\r\n\r\n")
                    length = next(
                        int(line.split(b":", 1)[1])
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    )
                    while len(body) < length:
                        body += tls.recv(65536)
                    self.requests.append(head)
                    self.answer(tls)
            except OSError:
                pass


@pytest.fixture()
def ca() -> Issuer:
    return _ca("hl-guard test CA")


@pytest.fixture()
def trust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[Issuer], None]]:
    def trust_only(issuer: Issuer) -> None:
        assert issuer.cert is not None
        cafile = _pem(tmp_path / f"trust-{id(issuer)}.pem", issuer.cert)
        default = ssl.create_default_context
        monkeypatch.setattr(
            ssl, "create_default_context", functools.partial(default, cafile=str(cafile))
        )
        info_module._tls_context.cache_clear()

    yield trust_only
    info_module._tls_context.cache_clear()


@pytest.fixture()
def direct(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    refused: list[tuple[str, int]] = []
    lookup = socket.getaddrinfo

    def local_only(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
        if host != "127.0.0.1":
            refused.append((host, port))
            raise OSError("test refuses a connection off this machine")
        return lookup(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_only)
    return refused


@pytest.fixture()
def proxy(
    monkeypatch: pytest.MonkeyPatch, env_proxies: None
) -> Iterator[Callable[[Path, Path], Tunnel]]:
    server = socket.create_server(("127.0.0.1", 0))
    tunnels: list[Tunnel] = []

    def accept() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=tunnels[-1].serve, args=(conn,), daemon=True).start()

    for name in ("https_proxy", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", f"http://tester:s3cret@127.0.0.1:{server.getsockname()[1]}")
    threading.Thread(target=accept, daemon=True).start()

    def open_tunnel(cert: Path, key: Path) -> Tunnel:
        tunnels.append(Tunnel(cert, key))
        return tunnels[-1]

    try:
        yield open_tunnel
    finally:
        server.close()


def read_book(tmp_path: Path) -> object:
    assert network_for("testnet").info_url == f"https://{PINNED}/info"
    budget = WeightBudget(tmp_path / "info-weight.json", 300)
    return InfoClient("testnet", budget, timeout_s=5.0).post({"type": "l2Book", "coin": "BTC"})


def test_a_cert_for_the_pinned_host_from_a_trusted_ca_is_read_through_the_proxy(
    tmp_path: Path,
    ca: Issuer,
    trust: Callable[[Issuer], None],
    proxy: Callable[[Path, Path], Tunnel],
    direct: list[tuple[str, int]],
) -> None:
    trust(ca)
    tunnel = proxy(*_leaf(tmp_path, "pinned", ca, PINNED))

    assert read_book(tmp_path) == BOOK

    assert direct == []
    assert tunnel.connects[0].startswith(f"CONNECT {PINNED}:443 HTTP/1.".encode())
    assert b"\r\nProxy-Authorization: Basic dGVzdGVyOnMzY3JldA==\r\n" in tunnel.connects[0]
    assert tunnel.first_bytes == [b"\x16"]
    assert tunnel.server_names == [PINNED]
    assert tunnel.requests[0].startswith(b"POST /info HTTP/1.1\r\n")
    assert b"proxy-authorization" not in tunnel.requests[0].lower()
    assert b"s3cret" not in tunnel.requests[0]


@pytest.mark.parametrize("cert", ["self-signed", "wrong-host", "untrusted-ca"])
def test_a_cert_that_does_not_prove_the_pinned_host_is_refused(
    tmp_path: Path,
    ca: Issuer,
    trust: Callable[[Issuer], None],
    proxy: Callable[[Path, Path], Tunnel],
    direct: list[tuple[str, int]],
    cert: str,
) -> None:
    trust(_ca("another CA") if cert == "untrusted-ca" else ca)
    issuer, host = {
        "self-signed": (None, PINNED),
        "wrong-host": (ca, "evil.example"),
        "untrusted-ca": (ca, PINNED),
    }[cert]
    tunnel = proxy(*_leaf(tmp_path, cert, issuer, host))

    with pytest.raises(InfoUnavailable, match=r"unreachable \(SSLCertVerificationError\)"):
        read_book(tmp_path)

    assert direct == []
    assert len(tunnel.connects) == 1
    assert tunnel.requests == []


def test_a_chunked_answer_above_two_megabytes_is_refused(
    tmp_path: Path,
    ca: Issuer,
    trust: Callable[[Issuer], None],
    proxy: Callable[[Path, Path], Tunnel],
    direct: list[tuple[str, int]],
) -> None:
    trust(ca)
    tunnel = proxy(*_leaf(tmp_path, "pinned", ca, PINNED))
    tunnel.answer = chunked_past_the_cap

    with pytest.raises(InfoUnavailable, match="larger than 2097152"):
        read_book(tmp_path)

    assert direct == []
    assert len(tunnel.requests) == 1
