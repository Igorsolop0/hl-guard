from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from hl_guard.exchange.actions import Action, reduces_risk
from hl_guard.exchange.halt import HaltFlag
from hl_guard.exchange.network import NetworkError, network_for
from hl_guard.exchange.signer import Signer


class TransportError(RuntimeError):
    """A refused or failed exchange POST.

    ``outcome_unknown`` marks a failure after the request may have reached the
    server: a 3xx, a 5xx, or no usable answer. ``response`` holds the parsed
    body when the exchange sent JSON.
    """

    def __init__(
        self, message: str, *, outcome_unknown: bool = False, response: Any = None
    ) -> None:
        super().__init__(message)
        self.outcome_unknown = outcome_unknown
        self.response = response


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is never followed, so it surfaces as an ``HTTPError``."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


class ExchangeTransport:
    def __init__(
        self,
        signer: Signer,
        network: str,
        halt: HaltFlag,
        *,
        url: str | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if not isinstance(signer, Signer):
            raise TransportError("signer must be a Signer instance")
        try:
            row = network_for(network)
        except NetworkError as exc:
            raise TransportError(f"network {network!r} refused: {exc}") from None
        if signer.network != row.name:
            raise TransportError(
                f"signer is bound to network {signer.network!r}, the transport to "
                f"{row.name!r}: refusing to pair them"
            )
        if url is not None and url != row.exchange_url:
            parsed = urlparse(url)
            raise TransportError(
                f"url {url!r} refused: the endpoint is pinned to {row.exchange_url!r} "
                f"(got scheme={parsed.scheme!r} host={parsed.hostname!r} path={parsed.path!r})"
            )
        self._signer = signer
        self._network = row.name
        self._halt = halt
        self._timeout_s = timeout_s
        self.last_request: dict[str, Any] | None = None
        self.last_response: dict[str, Any] | None = None

    def send(self, action: Action, nonce: int, expires_after: int | None = None) -> Any:
        """Sign and POST one allowlisted action. Returns the parsed 2xx body.

        Raises ``SignerError`` when the signer refuses the action, before any
        POST, and ``TransportError`` for a halt, a network change, or the
        exchange's outcome. A halt lets cancels and reduce-only orders out.
        """
        try:
            pinned_url = network_for(self._network).exchange_url
        except NetworkError:
            raise TransportError("network changed: refusing to send") from None
        if self._signer.network != self._network:
            raise TransportError("signer network changed: refusing to send")
        if self._halt.present() and not reduces_risk(action):
            raise TransportError(
                f"halt flag {self._halt.path} present: only cancels and reduce-only orders go out"
            )

        canonical, signature = self._signer.sign_action(action, nonce, expires_after)
        self.last_request = {
            "url": pinned_url,
            "action": canonical,
            "nonce": nonce,
            "signature": signature,
            "ts": time.time(),
        }
        self.last_response = None
        payload = {
            "action": canonical,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": expires_after,
        }
        request = urllib.request.Request(
            pinned_url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with _OPENER.open(request, timeout=self._timeout_s) as resp:
                status, text = resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            status, text = exc.code, _error_body(exc)
        except Exception as exc:
            self.last_response = {
                "outcome_unknown": True,
                "error": exc.__class__.__name__,
                "ts": time.time(),
            }
            raise TransportError(
                f"exchange POST outcome unknown ({exc.__class__.__name__}): "
                "reconcile via /info before retry",
                outcome_unknown=True,
            ) from None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if not 200 <= status < 300:
            unknown = not 400 <= status < 500
            self._record(
                status, unknown, **({"raw": text[:2000]} if parsed is None else {"body": parsed})
            )
            raise TransportError(
                f"exchange POST HTTP {status}: {text[:300]}"
                + (" (outcome unknown)" if unknown else ""),
                outcome_unknown=unknown,
                response=parsed,
            )
        if parsed is None:
            self._record(status, True, raw=text[:2000])
            raise TransportError(
                f"non-JSON response (HTTP {status}) (outcome unknown)", outcome_unknown=True
            )
        self._record(status, False, body=parsed)
        if isinstance(parsed, dict) and parsed.get("status") == "err":
            raise TransportError(
                f"exchange rejected: {str(parsed.get('response'))[:300]}", response=parsed
            )
        return parsed

    def _record(self, status: int, unknown: bool, **detail: Any) -> None:
        self.last_response = {
            "http_status": status,
            **detail,
            "outcome_unknown": unknown,
            "ts": time.time(),
        }

    def request_view(self) -> dict[str, Any] | None:
        """A deep copy of the last request view (signature only, no key)."""
        if self.last_request is None:
            return None
        view: dict[str, Any] = json.loads(json.dumps(self.last_request))
        return view
