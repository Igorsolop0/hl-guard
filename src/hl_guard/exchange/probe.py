"""``hl-guard probe-sign``: prove the signature format against the live exchange.

The probe signs a zero-size order with a throwaway agent key that exists only
in memory and posts it. That key was never approved, so the exchange answers
with an account error naming the address it recovered from the signature.
When that address is the throwaway agent's address, the exchange verified the
signature. Testnet only.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from eth_account import Account

from hl_guard.exchange.actions import build_action
from hl_guard.exchange.halt import HaltFlag, HaltPathError
from hl_guard.exchange.network import NetworkError, enable_network
from hl_guard.exchange.nonces import NonceManager
from hl_guard.exchange.signer import Signer, SignerError
from hl_guard.exchange.transport import ExchangeTransport, TransportError

_ADDRESS_RE = re.compile(r"(?<![0-9a-fA-F])0x[0-9a-fA-F]{40}(?![0-9a-fA-F])")

EXIT_VERIFIED = 0
EXIT_UNVERIFIED = 1
EXIT_REFUSED = 2


def probe_sign(network: str, data_dir: Path, url: str | None = None) -> tuple[int, dict[str, Any]]:
    """Return ``(exit_code, report)``. The report never holds key material."""
    try:
        enable_network(network)
    except NetworkError as exc:
        return EXIT_REFUSED, {"refused": f"probe-sign is testnet only: {exc}"}
    try:
        halt = HaltFlag(data_dir)
    except HaltPathError as exc:
        return EXIT_REFUSED, {"refused": str(exc)}
    agent = Account.create()
    try:
        signer = Signer(agent, agent.address, network=network)
        transport = ExchangeTransport(signer, network, halt, url=url)
    except (SignerError, TransportError) as exc:
        return EXIT_REFUSED, {"refused": str(exc)}
    action = build_action(
        "order",
        {
            "orders": [
                {
                    "a": 0,
                    "b": True,
                    "p": "1",
                    "s": "0",
                    "r": False,
                    "t": {"limit": {"tif": "Gtc"}},
                    "c": "0x" + secrets.token_hex(16),
                }
            ]
        },
    )
    nonce = NonceManager.for_agent(signer.agent_address).next_nonce(time.time_ns() // 1_000_000)
    report: dict[str, Any] = {"network": network, "agent_address": signer.agent_address}
    try:
        response = transport.send(action, nonce)
    except SignerError as exc:
        return EXIT_REFUSED, {**report, "refused": f"signer refused: {exc}"}
    except TransportError as exc:
        if transport.last_request is None:
            return EXIT_REFUSED, {**report, "refused": str(exc)}
        response = exc.response if exc.response is not None else transport.last_response
        report["error"] = str(exc)
    report["response"] = response
    recovered = _ADDRESS_RE.findall(json.dumps(response))
    report["recovered_address"] = recovered[0] if recovered else None
    report["signature_verified"] = bool(recovered) and recovered[0].lower() == (
        signer.agent_address.lower()
    )
    return (EXIT_VERIFIED if report["signature_verified"] else EXIT_UNVERIFIED), report
