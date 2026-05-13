"""Notabene Travel Rule Protocol (TRP) adapter.

Posts our IVMS 101 envelope to Notabene's TRP-shaped endpoint at
``https://api.notabene.id/v1/transactions``.

Notabene's production transport between VASPs is mTLS: both sides hold
client certs issued via Notabene's directory, and the directory handles
the public-key exchange between counterparty VASPs. mTLS requires a real
client cert+key pair obtained from Notabene's onboarding flow.

For environments without an mTLS handshake configured (most demo /
sandbox setups), this adapter falls back to HTTPS + Bearer authentication
against the same endpoint shape. The decision is made at request time:

  - If ``NOTABENE_CLIENT_CERT_PATH`` AND ``NOTABENE_CLIENT_KEY_PATH`` are
    both set and both files exist, an SSLContext is built with the cert
    and key, and that context is used on the urllib opener.
  - Otherwise the request uses the default SSLContext and authenticates
    with ``Authorization: Bearer <NOTABENE_API_KEY>``. The body is signed
    with HMAC-SHA256 over ``ts + METHOD + path + body`` using the API
    secret.

Environment variables:

  NOTABENE_API_KEY            API key issued by Notabene (Bearer token).
  NOTABENE_API_SECRET         Shared secret for the HMAC body signature.
  NOTABENE_BASE_URL           (optional) Override base URL. Default is
                              the production endpoint.
  NOTABENE_CLIENT_CERT_PATH   (optional) Path to PEM client cert for mTLS.
  NOTABENE_CLIENT_KEY_PATH    (optional) Path to PEM client key for mTLS.
  NOTABENE_CA_BUNDLE_PATH     (optional) Path to PEM CA bundle to pin
                              Notabene's server cert. Default uses system.
  NOTABENE_TIMEOUT            (optional) Request timeout in seconds.

Without ``NOTABENE_API_KEY``+``NOTABENE_API_SECRET`` set, instantiation
fails and the factory falls back to the stub.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from .base import InboundIvms, TransportResult, validated_http_base_url

ENV_VARS = (
    "NOTABENE_API_KEY",
    "NOTABENE_API_SECRET",
    "NOTABENE_BASE_URL",
    "NOTABENE_CLIENT_CERT_PATH",
    "NOTABENE_CLIENT_KEY_PATH",
    "NOTABENE_CA_BUNDLE_PATH",
    "NOTABENE_TIMEOUT",
)

DEFAULT_BASE_URL = "https://api.notabene.id"
DEFAULT_TIMEOUT = 8.0


class NotabeneAdapter:
    """Notabene TRP adapter. See module docstring."""

    name = "notabene"

    def __init__(self) -> None:
        self.api_key = os.environ.get("NOTABENE_API_KEY", "").strip()
        self.api_secret = os.environ.get("NOTABENE_API_SECRET", "").strip()
        self.base_url = validated_http_base_url(
            "NOTABENE_BASE_URL", os.environ.get("NOTABENE_BASE_URL", DEFAULT_BASE_URL)
        )
        self.client_cert_path = os.environ.get("NOTABENE_CLIENT_CERT_PATH", "").strip()
        self.client_key_path = os.environ.get("NOTABENE_CLIENT_KEY_PATH", "").strip()
        self.ca_bundle_path = os.environ.get("NOTABENE_CA_BUNDLE_PATH", "").strip()
        try:
            self.timeout = float(os.environ.get("NOTABENE_TIMEOUT", str(DEFAULT_TIMEOUT)))
        except ValueError:
            self.timeout = DEFAULT_TIMEOUT

        if not self.api_key or not self.api_secret:
            raise RuntimeError(
                "notabene_not_configured: set NOTABENE_API_KEY + "
                "NOTABENE_API_SECRET to enable the Notabene TRP adapter"
            )
        self.mtls_active = bool(
            self.client_cert_path
            and self.client_key_path
            and os.path.exists(self.client_cert_path)
            and os.path.exists(self.client_key_path)
        )
        self.configured = True

    # ------------------------------------------------------------------
    # IVMS -> TRP envelope translation
    # ------------------------------------------------------------------
    def _ivms_to_trp_envelope(
        self,
        ivms_message: dict,
        signature: str,
        public_key: str,
        counterparty_vasp_id: str,
    ) -> dict[str, Any]:
        """Wrap our IVMS message in a TRP-shaped envelope.

        TRP (the OpenVASP Travel Rule Protocol Notabene champions) puts
        the IVMS payload under ``ivms101`` and adds a transport-layer
        wrapper with the originator/beneficiary VASP DIDs, the asset/
        amount, and an originator signature block. We don't speak the
        full DID layer — for the demo we pass the raw vasp_id strings;
        in production Notabene's directory resolves them to DIDs.
        """
        transaction = ivms_message.get("transaction", {}) or {}
        originating_vasp = ivms_message.get("originatingVASP", {}) or {}
        return {
            "asset": {
                "slip0044": _slip44_for_asset(str(transaction.get("transferAsset") or "")),
                "symbol": transaction.get("transferAsset") or "",
            },
            "amount": str(transaction.get("transferAmount") or "0"),
            "originatorVASPdid": originating_vasp.get("vaspId") or "",
            "beneficiaryVASPdid": counterparty_vasp_id,
            "originator": ivms_message.get("originator") or {},
            "beneficiary": ivms_message.get("beneficiary") or {},
            "ivms101": ivms_message,
            "originatorProof": {
                "scheme": "Ed25519",
                "publicKey": public_key,
                "signature": signature,
            },
            "trpVersion": "3.1.0",
        }

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _build_ssl_context(self) -> ssl.SSLContext:
        """Build the SSLContext for outbound calls.

        If mTLS env vars are set and files exist, we attach the client
        cert and key. Otherwise we use the default verified context.
        Optionally pins the CA bundle via ``NOTABENE_CA_BUNDLE_PATH``.
        """
        ctx = ssl.create_default_context(cafile=self.ca_bundle_path or None)
        if self.mtls_active:
            ctx.load_cert_chain(
                certfile=self.client_cert_path,
                keyfile=self.client_key_path,
            )
        return ctx

    def _signed_headers(self, method: str, path: str, body_bytes: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        signing_input = (
            ts.encode("ascii") + method.upper().encode("ascii") + path.encode("ascii") + body_bytes
        )
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-Notabene-Signature": sig,
            "X-Notabene-Timestamp": ts,
            "X-TRP-Version": "3.1.0",
        }

    def _post_json(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], int]:
        body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        url = self.base_url + path
        headers = self._signed_headers("POST", path, body_bytes)
        req = urllib.request.Request(  # noqa: S310 - base_url is http(s)-validated.
            url, data=body_bytes, method="POST", headers=headers
        )
        ctx = self._build_ssl_context()
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:  # noqa: S310
                raw = resp.read()
                rtt_ms = int((time.time() - t0) * 1000)
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    payload = {"raw": raw.decode("utf-8", "replace")}
                return resp.status, payload, rtt_ms
        except urllib.error.HTTPError as e:
            rtt_ms = int((time.time() - t0) * 1000)
            raw = b""
            raw = e.read()
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                payload = {"raw": raw.decode("utf-8", "replace")}
            return e.code, payload, rtt_ms

    # ------------------------------------------------------------------
    # Protocol surface
    # ------------------------------------------------------------------
    def post_ivms(
        self,
        ivms_message: dict,
        signature: str,
        public_key: str,
        counterparty_vasp_id: str,
    ) -> TransportResult:
        body = self._ivms_to_trp_envelope(ivms_message, signature, public_key, counterparty_vasp_id)
        path = "/v1/transactions"
        try:
            status_code, payload, rtt_ms = self._post_json(path, body)
        except (
            urllib.error.URLError,
            ssl.SSLError,
            TimeoutError,
            OSError,
        ) as exc:
            return TransportResult(
                delivered=False,
                counterparty_id=counterparty_vasp_id,
                counterparty_ack_id=None,
                status="unreachable",
                error=f"notabene_network:{exc!r}",
                response_payload=None,
                rtt_ms=0,
            )

        # Notabene returns ``state`` field with values:
        #   ACCEPTED, PENDING_REVIEW, ACTION_REQUIRED, REJECTED.
        provider_state = str(payload.get("state") or payload.get("status") or "").upper()
        ack_id = payload.get("id") or payload.get("transferId") or payload.get("transactionId")
        if 200 <= status_code < 300:
            if provider_state == "ACCEPTED":
                return TransportResult(
                    delivered=True,
                    counterparty_id=counterparty_vasp_id,
                    counterparty_ack_id=str(ack_id) if ack_id else None,
                    status="accepted",
                    error=None,
                    response_payload=payload,
                    rtt_ms=rtt_ms,
                )
            if provider_state in ("PENDING_REVIEW", "ACTION_REQUIRED"):
                return TransportResult(
                    delivered=False,
                    counterparty_id=counterparty_vasp_id,
                    counterparty_ack_id=str(ack_id) if ack_id else None,
                    status="pending",
                    error=None,
                    response_payload=payload,
                    rtt_ms=rtt_ms,
                )
            if provider_state == "REJECTED":
                return TransportResult(
                    delivered=False,
                    counterparty_id=counterparty_vasp_id,
                    counterparty_ack_id=str(ack_id) if ack_id else None,
                    status="rejected",
                    error=f"notabene_rejected:{payload.get('rejectionReason')!r}",
                    response_payload=payload,
                    rtt_ms=rtt_ms,
                )
            return TransportResult(
                delivered=False,
                counterparty_id=counterparty_vasp_id,
                counterparty_ack_id=str(ack_id) if ack_id else None,
                status="unknown",
                error=f"notabene_unrecognized_2xx:{provider_state!r}",
                response_payload=payload,
                rtt_ms=rtt_ms,
            )
        if 400 <= status_code < 500:
            return TransportResult(
                delivered=False,
                counterparty_id=counterparty_vasp_id,
                counterparty_ack_id=None,
                status="rejected",
                error=f"notabene_http_{status_code}:{payload.get('message') or payload!r}",
                response_payload=payload,
                rtt_ms=rtt_ms,
            )
        return TransportResult(
            delivered=False,
            counterparty_id=counterparty_vasp_id,
            counterparty_ack_id=None,
            status="unreachable",
            error=f"notabene_http_{status_code}",
            response_payload=payload,
            rtt_ms=rtt_ms,
        )

    def fetch_inbox(self, since_ts: int) -> list[InboundIvms]:
        path = f"/v1/inbound?since={int(since_ts)}"
        url = self.base_url + path
        headers = self._signed_headers("GET", path, b"")
        req = urllib.request.Request(url, method="GET", headers=headers)  # noqa: S310
        ctx = self._build_ssl_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:  # noqa: S310
                raw = resp.read()
                payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return []
        out: list[InboundIvms] = []
        for entry in payload.get("transfers", []) or []:
            try:
                out.append(
                    InboundIvms(
                        inbox_id=str(entry.get("id") or ""),
                        from_vasp_id=entry.get("originatorVASPdid") or None,
                        ivms_message=entry.get("ivms101") or entry,
                        signature=(entry.get("originatorProof") or {}).get("signature"),
                        received_at=int(entry.get("createdAt") or 0),
                        raw=entry,
                    )
                )
            except Exception:  # noqa: S112 - skip malformed provider inbox entries.
                continue
        return out

    def health_check(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": bool(self.api_key and self.api_secret),
            "base_url": self.base_url,
            "mtls_active": self.mtls_active,
            "client_cert_path": self.client_cert_path or None,
            "timeout_s": self.timeout,
            "env_vars": list(ENV_VARS),
        }


# SLIP-0044 coin-type registry — partial; covers the assets the rest of
# the codebase recognizes. Notabene's TRP envelope expects this id on the
# asset block. If we ever add a new asset symbol to the order engine,
# add the slip44 here too.
_SLIP44 = {
    "BTC": 0,
    "ETH": 60,
    "USDT": 60,  # USDT on ETH chain
    "USDC": 60,  # USDC on ETH chain
    "BNB": 714,
    "SOL": 501,
    "ZETH": 60,
    "ZUSDT": 60,
}


def _slip44_for_asset(symbol: str) -> int | None:
    return _SLIP44.get((symbol or "").upper())
