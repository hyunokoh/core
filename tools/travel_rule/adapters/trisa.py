"""TRISA (Travel Rule Information Sharing Architecture) adapter.

The TRISA reference implementation (https://trisa.io) uses gRPC over
mutual TLS with the Global Directory Service (GDS) resolving counterparty
public keys. The stdlib does not include a gRPC runtime, so the canonical
TRISA transport requires installing ``grpcio`` plus the
``trisa-protocol`` Python package (generated protobufs).

This adapter implements a TRISA-shaped HTTP fallback so the integration
shape is correct and a real gRPC swap-in is a drop-in replacement. The
HTTP fallback POSTs the TRISA "Transfer" envelope (as a JSON encoding of
the protobuf schema) to:

    https://api.trisa.io/v1/transfer

which is the JSON-gateway endpoint exposed by the official trisads
implementation behind grpc-gateway. Real production deployments should
use the gRPC channel directly:

    import grpc
    from trisa.envelope import secure_envelope_pb2
    from trisa.api.v1beta1 import trisa_pb2_grpc

    channel = grpc.secure_channel(
        target=counterparty_endpoint,
        credentials=grpc.ssl_channel_credentials(
            root_certificates=gds_ca_bundle,
            private_key=our_private_key_pem,
            certificate_chain=our_cert_chain_pem,
        ),
    )
    stub = trisa_pb2_grpc.TRISANetworkStub(channel)
    response = stub.Transfer(secure_envelope)

For now we provide the HTTP shape and document the gRPC swap-in as a
production checklist item.

Environment variables:

  TRISA_BASE_URL          (optional) Override base URL; default is the
                          TRISA public JSON gateway.
  TRISA_API_KEY           Bearer token for the JSON gateway. Required.
  TRISA_DIRECTORY_URL     (optional) GDS endpoint for VASP discovery.
                          Used by future fetch_inbox implementation.
  TRISA_CLIENT_CERT_PATH  (optional) Path to mTLS client cert.
  TRISA_CLIENT_KEY_PATH   (optional) Path to mTLS client key.
  TRISA_CA_BUNDLE_PATH    (optional) CA bundle to pin server cert.
  TRISA_TIMEOUT           (optional) Request timeout in seconds.

Production deployment checklist (NOT auto-handled here):
  1. Install ``grpcio`` and the ``trisa-protocol`` Python package.
  2. Obtain a verified TRISA certificate via the GDS verification flow
     (60-day rotation, 30-day grace).
  3. Replace the urlopen() call below with a grpc.secure_channel().Transfer().
  4. Keep the IVMS-to-envelope translation here — it doesn't change between
     gRPC and HTTP-gateway encodings.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from .base import InboundIvms, TransportResult, validated_http_base_url

ENV_VARS = (
    "TRISA_BASE_URL",
    "TRISA_API_KEY",
    "TRISA_DIRECTORY_URL",
    "TRISA_CLIENT_CERT_PATH",
    "TRISA_CLIENT_KEY_PATH",
    "TRISA_CA_BUNDLE_PATH",
    "TRISA_TIMEOUT",
)

DEFAULT_BASE_URL = "https://api.trisa.io"
DEFAULT_TIMEOUT = 8.0


class TrisaAdapter:
    """TRISA-shaped HTTP fallback adapter. See module docstring."""

    name = "trisa"

    def __init__(self) -> None:
        self.api_key = os.environ.get("TRISA_API_KEY", "").strip()
        self.base_url = validated_http_base_url(
            "TRISA_BASE_URL", os.environ.get("TRISA_BASE_URL", DEFAULT_BASE_URL)
        )
        raw_directory_url = os.environ.get("TRISA_DIRECTORY_URL", "")
        self.directory_url = (
            validated_http_base_url("TRISA_DIRECTORY_URL", raw_directory_url)
            if raw_directory_url
            else ""
        )
        self.client_cert_path = os.environ.get("TRISA_CLIENT_CERT_PATH", "").strip()
        self.client_key_path = os.environ.get("TRISA_CLIENT_KEY_PATH", "").strip()
        self.ca_bundle_path = os.environ.get("TRISA_CA_BUNDLE_PATH", "").strip()
        try:
            self.timeout = float(os.environ.get("TRISA_TIMEOUT", str(DEFAULT_TIMEOUT)))
        except ValueError:
            self.timeout = DEFAULT_TIMEOUT

        if not self.api_key:
            raise RuntimeError(
                "trisa_not_configured: set TRISA_API_KEY to enable the TRISA "
                "adapter (HTTP gateway mode). For production gRPC mode, also "
                "configure TRISA_CLIENT_CERT_PATH + TRISA_CLIENT_KEY_PATH and "
                "swap this adapter's transport call for grpcio."
            )
        self.mtls_active = bool(
            self.client_cert_path
            and self.client_key_path
            and os.path.exists(self.client_cert_path)
            and os.path.exists(self.client_key_path)
        )
        self.configured = True

    # ------------------------------------------------------------------
    # IVMS -> TRISA SecureEnvelope translation
    # ------------------------------------------------------------------
    def _ivms_to_secure_envelope(
        self,
        ivms_message: dict,
        signature: str,
        public_key: str,
        counterparty_vasp_id: str,
    ) -> dict[str, Any]:
        """Build the JSON encoding of a TRISA SecureEnvelope protobuf.

        Real TRISA secures the payload by encrypting with the counterparty
        VASP's TRISA-cert public key (the cert is fetched from the GDS).
        We don't have that pubkey in the demo environment so the payload
        block carries the cleartext IVMS plus our Ed25519 signature.
        Real deployments encrypt the ``payload.transaction`` block with
        an AES key wrapped by the counterparty's RSA pubkey, per
        https://trisa.io/spec.
        """
        # Canonical IVMS bytes (the same the originator signed).
        ivms_json = json.dumps(
            ivms_message,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

        return {
            # SecureEnvelope.id
            "id": f"trisa-{int(time.time())}-{counterparty_vasp_id}",
            # SecureEnvelope.payload (base64 of cleartext for the gateway;
            # production gRPC encrypts this with the counterparty's pubkey).
            "payload": {
                "transaction": base64.b64encode(ivms_json).decode("ascii"),
                "transactionFormat": "ivms101",
                "sentAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            "encryption": {
                "encryptionAlgorithm": "PLAINTEXT-GATEWAY",
                "hmacAlgorithm": "HMAC-SHA256",
                "publicKeySignature": signature,
            },
            "originatorVASPdid": (ivms_message.get("originatingVASP") or {}).get("vaspId") or "",
            "beneficiaryVASPdid": counterparty_vasp_id,
            "signingPublicKey": public_key,
            "signature": signature,
            "trisaVersion": "1.0",
        }

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _build_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=self.ca_bundle_path or None)
        if self.mtls_active:
            ctx.load_cert_chain(
                certfile=self.client_cert_path,
                keyfile=self.client_key_path,
            )
        return ctx

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "X-TRISA-Version": "1.0",
        }

    def _post_json(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], int]:
        body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        url = self.base_url + path
        req = urllib.request.Request(  # noqa: S310 - base_url is http(s)-validated.
            url, data=body_bytes, method="POST", headers=self._headers()
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
        envelope = self._ivms_to_secure_envelope(
            ivms_message, signature, public_key, counterparty_vasp_id
        )
        path = "/v1/transfer"
        try:
            status_code, payload, rtt_ms = self._post_json(path, envelope)
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
                error=f"trisa_network:{exc!r}",
                response_payload=None,
                rtt_ms=0,
            )

        # TRISA's Transfer response is a SecureEnvelope echoed back with
        # an updated state. State codes:
        #   COMPLETE, PENDING, REJECTED, REPAIR.
        provider_state = str(payload.get("state") or payload.get("status") or "").upper()
        ack_id = payload.get("id") or payload.get("transferId") or payload.get("transactionId")
        if 200 <= status_code < 300:
            if provider_state == "COMPLETE":
                return TransportResult(
                    delivered=True,
                    counterparty_id=counterparty_vasp_id,
                    counterparty_ack_id=str(ack_id) if ack_id else None,
                    status="accepted",
                    error=None,
                    response_payload=payload,
                    rtt_ms=rtt_ms,
                )
            if provider_state in ("PENDING", "REPAIR"):
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
                    error=f"trisa_rejected:{payload.get('error')!r}",
                    response_payload=payload,
                    rtt_ms=rtt_ms,
                )
            return TransportResult(
                delivered=False,
                counterparty_id=counterparty_vasp_id,
                counterparty_ack_id=str(ack_id) if ack_id else None,
                status="unknown",
                error=f"trisa_unrecognized_2xx:{provider_state!r}",
                response_payload=payload,
                rtt_ms=rtt_ms,
            )
        if 400 <= status_code < 500:
            return TransportResult(
                delivered=False,
                counterparty_id=counterparty_vasp_id,
                counterparty_ack_id=None,
                status="rejected",
                error=f"trisa_http_{status_code}:{payload.get('error') or payload!r}",
                response_payload=payload,
                rtt_ms=rtt_ms,
            )
        return TransportResult(
            delivered=False,
            counterparty_id=counterparty_vasp_id,
            counterparty_ack_id=None,
            status="unreachable",
            error=f"trisa_http_{status_code}",
            response_payload=payload,
            rtt_ms=rtt_ms,
        )

    def fetch_inbox(self, since_ts: int) -> list[InboundIvms]:
        """Pull pending inbound TRISA transfers.

        Real TRISA is push (gRPC server-side stream). The HTTP gateway
        exposes a polled inbox endpoint we use as a fallback. Returns
        empty on any error so the caller can keep using the webhook path.
        """
        path = f"/v1/inbox?since={int(since_ts)}"
        url = self.base_url + path
        req = urllib.request.Request(url, method="GET", headers=self._headers())  # noqa: S310
        ctx = self._build_ssl_context()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:  # noqa: S310
                raw = resp.read()
                payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return []
        out: list[InboundIvms] = []
        for env in payload.get("transfers", []) or []:
            try:
                # The payload.transaction is b64 of canonical IVMS bytes.
                tx_b64 = (env.get("payload") or {}).get("transaction") or ""
                ivms_dict: dict = {}
                if tx_b64:
                    try:
                        ivms_dict = json.loads(base64.b64decode(tx_b64).decode("utf-8"))
                    except Exception:
                        ivms_dict = {}
                out.append(
                    InboundIvms(
                        inbox_id=str(env.get("id") or ""),
                        from_vasp_id=env.get("originatorVASPdid") or None,
                        ivms_message=ivms_dict,
                        signature=env.get("signature"),
                        received_at=int(env.get("receivedAt") or 0),
                        raw=env,
                    )
                )
            except Exception:  # noqa: S112 - skip malformed provider inbox entries.
                continue
        return out

    def health_check(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": bool(self.api_key),
            "base_url": self.base_url,
            "mtls_active": self.mtls_active,
            "directory_url": self.directory_url or None,
            "transport_note": (
                "HTTP gateway mode. For real gRPC + GDS, install grpcio + "
                "trisa-protocol and swap the post_ivms transport call."
            ),
            "timeout_s": self.timeout,
            "env_vars": list(ENV_VARS),
        }
