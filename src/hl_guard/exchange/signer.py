from __future__ import annotations

import re
from typing import Any

import msgpack
from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from eth_keys.backends import get_backend_class
from eth_keys.backends.coincurve import CoinCurveECCBackend
from eth_utils.crypto import keccak

from hl_guard.exchange.actions import Action, ActionConstructionError, build_action
from hl_guard.exchange.network import NetworkError, network_for

Signature = dict[str, Any]
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


class SignerError(RuntimeError):
    """Signing failed or was refused."""


def action_hash(action: Action, nonce: int, expires_after: int | None) -> bytes:
    """keccak(msgpack(action) ++ nonce ++ 0 (no vault) [++ 0 ++ expires_after])."""
    data: bytes = msgpack.packb(action)
    data += nonce.to_bytes(8, "big")
    data += b"\x00"
    if expires_after is not None:
        data += b"\x00" + expires_after.to_bytes(8, "big")
    return keccak(data)


def l1_payload(phantom_agent: dict[str, Any]) -> dict[str, Any]:
    """The EIP-712 message for an L1 action: chain id 1337, zero verifier."""
    return {
        "domain": {
            "chainId": 1337,
            "name": "Exchange",
            "verifyingContract": "0x0000000000000000000000000000000000000000",
            "version": "1",
        },
        "types": {
            "Agent": [
                {"name": "source", "type": "string"},
                {"name": "connectionId", "type": "bytes32"},
            ],
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
        },
        "primaryType": "Agent",
        "message": phantom_agent,
    }


def _check_u64(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise SignerError(f"{name} must be a non-negative int")
    if value >= 2**64:
        raise SignerError(f"{name} must fit in 64 bits")


class Signer:
    """Signs allowlisted L1 actions with an agent key for one network.

    ``account_address`` is the master address the agent trades for.
    """

    def __init__(self, agent: LocalAccount, account_address: str, *, network: str) -> None:
        try:
            self._network = network_for(network)
        except NetworkError as exc:
            raise SignerError(str(exc)) from None
        if not isinstance(agent, LocalAccount):
            raise SignerError("agent must be an eth_account LocalAccount")
        if not isinstance(account_address, str) or not _ADDRESS_RE.fullmatch(account_address):
            raise SignerError(
                f"account_address (master) must be 0x and 40 hex digits: {account_address!r}"
            )
        self._agent = agent
        self._account_address = account_address

    @property
    def agent_address(self) -> str:
        return str(self._agent.address)

    @property
    def account_address(self) -> str:
        return self._account_address

    @property
    def network(self) -> str:
        return self._network.name

    def sign_action(
        self, action: Action, nonce: int, expires_after: int | None = None
    ) -> tuple[Action, Signature]:
        """Sign one ``build_action`` dict. Returns ``(canonical_action, {r, s, v})``.

        The canonical action is the exact object that was hashed and is what
        goes on the wire. Anything that is not a canonical build is refused
        before the key is touched.
        """
        if not isinstance(action, dict) or "type" not in action:
            raise SignerError("signer accepts only build_action() dicts")
        _check_u64("nonce", nonce)
        if expires_after is not None:
            _check_u64("expires_after", expires_after)
        try:
            canonical = build_action(
                action["type"], {k: v for k, v in action.items() if k != "type"}
            )
        except ActionConstructionError:
            raise SignerError(
                f"action failed allowlist re-validation: {action['type']!r}"
            ) from None
        if canonical != action or list(canonical) != list(action):
            raise SignerError("action dict is not in canonical build_action form (mutated?)")
        try:
            current = network_for(self._network.name)
        except NetworkError as exc:
            raise SignerError(str(exc)) from None
        if current != self._network:
            raise SignerError("signer network row differs from the network table")
        backend = get_backend_class()
        if not issubclass(backend, CoinCurveECCBackend):
            raise SignerError(
                "refusing to sign: eth_keys is not on libsecp256k1 (coincurve); "
                f"it resolved {backend.__name__}"
            )
        try:
            digest = action_hash(canonical, nonce, expires_after)
            phantom_agent = {"source": self._network.phantom_source, "connectionId": digest}
            signable = encode_typed_data(full_message=l1_payload(phantom_agent))
        except (OverflowError, ValueError) as exc:
            raise SignerError(
                f"refusing to sign: encoding failed ({exc.__class__.__name__}: {exc})"
            ) from None
        signed = self._agent.sign_message(signable)
        return canonical, {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v}
