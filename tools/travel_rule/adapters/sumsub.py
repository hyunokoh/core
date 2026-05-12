"""Sumsub Travel Rule adapter.

Posts our internal IVMS 101 envelope to Sumsub's Travel Rule API at
``https://api.sumsub.com/resources/travelRule/transactions``. Sumsub
re-maps IVMS to its provider-internal schema and is responsible for
finding the beneficiary VASP and ACK-ing the message.

Environment variables (all required when ``TR_PROVIDER=sumsub``):

  SUMSUB_TR_APP_TOKEN   App token issued by Sumsub for the TR product.
                        Sent verbatim in the ``X-App-Token`` header.
  SUMSUB_TR_APP_SECRET  Shared secret used to HMAC-SHA256-sign each
                        request body + path + timestamp. NEVER logged.
  SUMSUB_TR_BASE_URL    (optional) override the API base; default is
                        the production URL. Set to a sandbox URL for
                        non-prod environments.
  SUMSUB_TR_TIMEOUT     (optional) request timeout in seconds. Default 8.

Request signature scheme per Sumsub docs:
    X-App-Access-Sig = hex(HMAC_SHA256(secret, ts + METHOD + path + body))
    X-App-Access-Ts  = unix-seconds string

Response shape (paraphrased from
https://docs.sumsub.com/reference/travel-rule):
    {
      "id": "<tr_id>",
      "status": "ACCEPTED" | "PENDING" | "REJECTED",
      "reasons": [...],
      "inboxUrl": "...",
      "createdAt": "..."
    }

This adapter is stdlib-only on purpose: it imports nothing beyond
``urllib``, ``json``, ``hmac``, and ``hashlib`` so it can run in the same
dependency-free environment as the rest of the repo.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .base import InboundIvms, TransportResult, validated_http_base_url

# Env vars consumed by this adapter (mirrored in adapter-info for docs).
ENV_VARS = (
    "SUMSUB_TR_APP_TOKEN",
    "SUMSUB_TR_APP_SECRET",
    "SUMSUB_TR_BASE_URL",
    "SUMSUB_TR_TIMEOUT",
)

DEFAULT_BASE_URL = "https://api.sumsub.com"
DEFAULT_TIMEOUT = 8.0


class SumsubAdapter:
    """Sumsub Travel Rule adapter. See module docstring for env vars."""

    name = "sumsub"

    def __init__(self) -> None:
        self.app_token = os.environ.get("SUMSUB_TR_APP_TOKEN", "").strip()
        self.app_secret = os.environ.get("SUMSUB_TR_APP_SECRET", "").strip()
        self.base_url = validated_http_base_url(
            "SUMSUB_TR_BASE_URL", os.environ.get("SUMSUB_TR_BASE_URL", DEFAULT_BASE_URL)
        )
        try:
            self.timeout = float(os.environ.get("SUMSUB_TR_TIMEOUT", str(DEFAULT_TIMEOUT)))
        except ValueError:
            self.timeout = DEFAULT_TIMEOUT

        if not self.app_token or not self.app_secret:
            raise RuntimeError(
                "sumsub_not_configured: set SUMSUB_TR_APP_TOKEN + "
                "SUMSUB_TR_APP_SECRET to enable the Sumsub Travel Rule adapter"
            )
        self.configured = True

    # ------------------------------------------------------------------
    # IVMS -> Sumsub field translation
    # ------------------------------------------------------------------
    def _ivms_to_sumsub_payload(
        self,
        ivms_message: dict,
        signature: str,
        counterparty_vasp_id: str,
    ) -> dict[str, Any]:
        """Translate our IVMS 101 envelope to the Sumsub TR payload shape.

        We pass the original IVMS verbatim under ``ivms101`` so Sumsub's
        validators see the canonical bytes, AND we also expand the most
        frequently-indexed fields (party names, transaction value) at
        the top level so the Sumsub dashboard renders them correctly
        without a deep JSON-pointer walk.
        """
        originator = ivms_message.get("originator", {}) or {}
        beneficiary = ivms_message.get("beneficiary", {}) or {}
        originating_vasp = ivms_message.get("originatingVASP", {}) or {}
        ivms_message.get("beneficiaryVASP", {}) or {}
        transaction = ivms_message.get("transaction", {}) or {}

        def _first_name(party_block: dict) -> dict[str, str]:
            persons = (
                party_block.get("originatorPersons") or party_block.get("beneficiaryPersons") or []
            )
            if not persons:
                return {"primary": "", "secondary": ""}
            np_ = persons[0].get("naturalPerson") or {}
            ids = (np_.get("name") or {}).get("nameIdentifier") or []
            if not ids:
                return {"primary": "", "secondary": ""}
            return {
                "primary": str(ids[0].get("primaryIdentifier") or ""),
                "secondary": str(ids[0].get("secondaryIdentifier") or ""),
            }

        return {
            "originatorVaspId": originating_vasp.get("vaspId") or "",
            "beneficiaryVaspId": counterparty_vasp_id,
            "transaction": {
                "amount": transaction.get("transferAmount") or "0",
                "asset": transaction.get("transferAsset") or "",
                "originatorAccount": transaction.get("originatorAccountIdentifier") or "",
                "beneficiaryAccount": transaction.get("beneficiaryAccountIdentifier") or "",
                "timestamp": transaction.get("transactionDateTime") or "",
            },
            "originatorParty": _first_name(originator),
            "beneficiaryParty": _first_name(beneficiary),
            # Verbatim IVMS — Sumsub's validators verify against the spec.
            "ivms101": ivms_message,
            # Originator-side signature so the counterparty can verify our
            # message bytes were not mutated by Sumsub's intermediation.
            "originatorSignature": {
                "scheme": "Ed25519",
                "signature": signature,
            },
        }

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    def _signed_headers(self, method: str, path: str, body_bytes: bytes) -> dict[str, str]:
        """Compute the Sumsub HMAC headers for one request.

        Per https://docs.sumsub.com/reference/authentication the signature
        is HMAC-SHA256 over ``ts + METHOD + path + body`` using the app
        secret. We use ``hmac.compare_digest`` for verification elsewhere;
        signing is plain ``.hexdigest()``.
        """
        ts = str(int(time.time()))
        signing_input = (
            ts.encode("ascii") + method.upper().encode("ascii") + path.encode("ascii") + body_bytes
        )
        sig = hmac.new(
            self.app_secret.encode("utf-8"),
            signing_input,
            hashlib.sha256,
        ).hexdigest()
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-App-Token": self.app_token,
            "X-App-Access-Sig": sig,
            "X-App-Access-Ts": ts,
        }

    def _post_json(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any], int]:
        body_bytes = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        url = self.base_url + path
        headers = self._signed_headers("POST", path, body_bytes)
        req = urllib.request.Request(  # noqa: S310 - base_url is http(s)-validated.
            url, data=body_bytes, method="POST", headers=headers
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
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
        body = self._ivms_to_sumsub_payload(ivms_message, signature, counterparty_vasp_id)
        # Sumsub also wants the originator pubkey so they can echo it to
        # the beneficiary VASP. Send as a sibling field.
        body["originatorPublicKey"] = {
            "scheme": "Ed25519",
            "publicKey": public_key,
        }
        path = "/resources/travelRule/transactions"
        try:
            status_code, payload, rtt_ms = self._post_json(path, body)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return TransportResult(
                delivered=False,
                counterparty_id=counterparty_vasp_id,
                counterparty_ack_id=None,
                status="unreachable",
                error=f"sumsub_network:{exc!r}",
                response_payload=None,
                rtt_ms=0,
            )

        # Map HTTP / provider status to our DeliveryStatus.
        provider_status = str(payload.get("status") or "").upper()
        ack_id = payload.get("id") or payload.get("trId") or payload.get("transactionId")
        if 200 <= status_code < 300:
            if provider_status == "ACCEPTED":
                return TransportResult(
                    delivered=True,
                    counterparty_id=counterparty_vasp_id,
                    counterparty_ack_id=str(ack_id) if ack_id else None,
                    status="accepted",
                    error=None,
                    response_payload=payload,
                    rtt_ms=rtt_ms,
                )
            if provider_status == "PENDING":
                return TransportResult(
                    delivered=False,
                    counterparty_id=counterparty_vasp_id,
                    counterparty_ack_id=str(ack_id) if ack_id else None,
                    status="pending",
                    error=None,
                    response_payload=payload,
                    rtt_ms=rtt_ms,
                )
            if provider_status == "REJECTED":
                return TransportResult(
                    delivered=False,
                    counterparty_id=counterparty_vasp_id,
                    counterparty_ack_id=str(ack_id) if ack_id else None,
                    status="rejected",
                    error=f"sumsub_rejected:{payload.get('reasons')!r}",
                    response_payload=payload,
                    rtt_ms=rtt_ms,
                )
            return TransportResult(
                delivered=False,
                counterparty_id=counterparty_vasp_id,
                counterparty_ack_id=str(ack_id) if ack_id else None,
                status="unknown",
                error=f"sumsub_unrecognized_2xx:{provider_status!r}",
                response_payload=payload,
                rtt_ms=rtt_ms,
            )
        # Non-2xx: rejected (4xx) vs unreachable (5xx).
        if 400 <= status_code < 500:
            return TransportResult(
                delivered=False,
                counterparty_id=counterparty_vasp_id,
                counterparty_ack_id=None,
                status="rejected",
                error=f"sumsub_http_{status_code}:{payload.get('description') or payload.get('error') or payload!r}",
                response_payload=payload,
                rtt_ms=rtt_ms,
            )
        return TransportResult(
            delivered=False,
            counterparty_id=counterparty_vasp_id,
            counterparty_ack_id=None,
            status="unreachable",
            error=f"sumsub_http_{status_code}",
            response_payload=payload,
            rtt_ms=rtt_ms,
        )

    def fetch_inbox(self, since_ts: int) -> list[InboundIvms]:
        """Pull pending inbound TR messages from Sumsub.

        In production Sumsub pushes via webhook to ``/travel-rule/inbound``
        rather than expecting a poll. We expose the pull path here for
        operators recovering from a webhook outage. Returns [] on error.
        """
        path = f"/resources/travelRule/inbox?since={int(since_ts)}"
        body_bytes = b""
        url = self.base_url + path
        headers = self._signed_headers("GET", path, body_bytes)
        req = urllib.request.Request(url, method="GET", headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read()
                payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return []
        out: list[InboundIvms] = []
        for entry in payload.get("items", []) or []:
            try:
                out.append(
                    InboundIvms(
                        inbox_id=str(entry.get("id") or ""),
                        from_vasp_id=str(entry.get("originatorVaspId") or "") or None,
                        ivms_message=entry.get("ivms101") or entry,
                        signature=(entry.get("originatorSignature") or {}).get("signature"),
                        received_at=int(entry.get("receivedAt") or 0),
                        raw=entry,
                    )
                )
            except Exception:  # noqa: S112 - skip malformed provider inbox entries.
                continue
        return out

    def health_check(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "configured": bool(self.app_token and self.app_secret),
            "base_url": self.base_url,
            "timeout_s": self.timeout,
            "env_vars": list(ENV_VARS),
        }
