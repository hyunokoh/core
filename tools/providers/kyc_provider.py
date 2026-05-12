"""KYC (본인인증) provider interface.

Two parallel flows are supported:

1. Korean PASS-style 휴대폰 본인인증 (the original ``verify_identity``):
   In production this calls a 통신사 본인확인 service (NICE checkplus, KCB,
   KMC) that verifies a Korean RRN + carrier + name match. In demo it only
   validates the field formats and returns verified=True. Swap with
   ``KYC_PROVIDER_URL`` / ``KYC_PROVIDER_KEY``.

2. Sumsub WebSDK (``sumsub_*`` helpers below):
   The production-grade global flow used by Binance/Coinbase/Kraken. The
   browser drives an iframe that handles passport / liveness / address proof,
   and Sumsub posts the result back to our webhook asynchronously. Swap with
   ``SUMSUB_APP_TOKEN`` + ``SUMSUB_APP_SECRET``. If those env vars are not
   set, the sumsub_* functions raise ``RuntimeError("sumsub_not_configured")``
   so the auth server can return 503 and the frontend can fall back to PASS.

Failure mode for the PASS path is FAIL-CLOSED: if KYC_PROVIDER_URL is
configured but the upstream is unreachable or returns garbage, this returns
verified=False with a reason so the caller can present a 400
kyc_provider_rejected instead of letting an unverified user through.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _optional_http_base_url(name: str, raw_url: str) -> str:
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return ""
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


KYC_PROVIDER_URL = _optional_http_base_url(
    "KYC_PROVIDER_URL", os.environ.get("KYC_PROVIDER_URL", "")
)
KYC_PROVIDER_KEY = os.environ.get("KYC_PROVIDER_KEY", "")

_RRN_FRONT_RE = re.compile(r"^\d{6}$")
_RRN_BACK1_RE = re.compile(r"^[1-4]$")
_PHONE_RE = re.compile(r"^010\d{8}$")
_NAME_RE = re.compile(r"^[A-Za-z가-힣 ]{2,16}$")
_CARRIERS = {"SKT", "KT", "LGU", "SKT_MVNO", "KT_MVNO", "LGU_MVNO"}


def verify_identity(*, name: str, rrn_front: str, rrn_back1: str, phone: str, carrier: str) -> dict:
    """Returns
    {"verified": bool, "provider_request_id": str,
     "verified_at": int, "reason": str|None}
    """
    if KYC_PROVIDER_URL:
        return _call_real(
            name=name,
            rrn_front=rrn_front,
            rrn_back1=rrn_back1,
            phone=phone,
            carrier=carrier,
        )
    return _stub_verify(
        name=name,
        rrn_front=rrn_front,
        rrn_back1=rrn_back1,
        phone=phone,
        carrier=carrier,
    )


def _stub_verify(*, name, rrn_front, rrn_back1, phone, carrier):
    """Format-only check. Real provider would also confirm 통신사 + 이름 + 주민번호 일치."""
    if not _NAME_RE.match(name or ""):
        return _fail("invalid_name_format")
    if not _RRN_FRONT_RE.match(rrn_front or ""):
        return _fail("invalid_rrn_front_format")
    if not _RRN_BACK1_RE.match(rrn_back1 or ""):
        return _fail("invalid_rrn_back1_format")
    if not _PHONE_RE.match(phone or ""):
        return _fail("invalid_phone_format")
    if (carrier or "").upper() not in _CARRIERS:
        return _fail("invalid_carrier")
    return {
        "verified": True,
        "provider_request_id": f"stub-{int(time.time()*1000)}",
        "verified_at": int(time.time()),
        "reason": None,
    }


def _fail(reason):
    return {
        "verified": False,
        "provider_request_id": "",
        "verified_at": int(time.time()),
        "reason": reason,
    }


def _call_real(*, name, rrn_front, rrn_back1, phone, carrier):
    body = {
        "name": name,
        "rrn_front": rrn_front,
        # rrn_back1 is the leading gender/century digit; the rest is
        # intentionally NOT sent because we don't store it locally.
        "rrn_back1": rrn_back1,
        "phone": phone,
        "carrier": carrier,
    }
    headers = {"Content-Type": "application/json"}
    if KYC_PROVIDER_KEY:
        headers["Authorization"] = f"Bearer {KYC_PROVIDER_KEY}"
    req = _http_request(
        url=f"{KYC_PROVIDER_URL}/verify",
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with _http_urlopen(req, timeout=3.0) as r:
            data = json.loads(r.read().decode())
            verified = bool(data.get("verified", False))
            return {
                "verified": verified,
                "provider_request_id": str(data.get("request_id", "")),
                "verified_at": int(data.get("verified_at", int(time.time()))),
                "reason": None if verified else str(data.get("reason", "rejected")),
            }
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ) as e:
        # Fail-closed.
        return {
            "verified": False,
            "provider_request_id": "",
            "verified_at": int(time.time()),
            "reason": f"kyc-unreachable: {type(e).__name__}: {e}",
        }


# =====================================================================
#  Sumsub WebSDK adapter
#  Docs:
#    https://docs.sumsub.com/docs/websdk-integration
#    https://docs.sumsub.com/reference
#  Auth: HMAC-SHA256 over (timestamp + httpMethod + path + body) with
#  app_secret, sent as headers:
#      X-App-Token:        <app_token>
#      X-App-Access-Sig:   <hex sha256 hmac>
#      X-App-Access-Ts:    <unix seconds>
# =====================================================================


# Read each call so a process started without env can pick up env that was
# set later in the same shell (e.g. for tests). Cheap; just os.environ lookups.
def _sumsub_cfg():
    return {
        "token": os.environ.get("SUMSUB_APP_TOKEN", ""),
        "secret": os.environ.get("SUMSUB_APP_SECRET", ""),
        "base": os.environ.get("SUMSUB_BASE", "https://api.sumsub.com").rstrip("/"),
        "level": os.environ.get("SUMSUB_LEVEL", "basic-kyc-level"),
    }


def is_sumsub_configured() -> bool:
    """True iff both SUMSUB_APP_TOKEN and SUMSUB_APP_SECRET are set."""
    cfg = _sumsub_cfg()
    return bool(cfg["token"]) and bool(cfg["secret"])


def _require_sumsub() -> dict:
    cfg = _sumsub_cfg()
    if not cfg["token"] or not cfg["secret"]:
        raise RuntimeError("sumsub_not_configured")
    return cfg


def sumsub_signed_request(method: str, path: str, body: bytes = b"") -> dict:
    """Issue a Sumsub REST call with HMAC headers. Returns parsed JSON.

    ``path`` must include the leading ``/`` and any query string -- the path
    used in the signature is exactly the path+query that goes on the wire.
    Raises ``RuntimeError("sumsub_not_configured")`` if no credentials.
    Raises ``RuntimeError`` with the upstream HTTP status / body on non-2xx.
    """
    cfg = _require_sumsub()
    method = method.upper()
    ts = str(int(time.time()))
    msg = ts.encode() + method.encode() + path.encode() + (body or b"")
    sig = hmac.new(cfg["secret"].encode(), msg, hashlib.sha256).hexdigest()

    url = cfg["base"] + path
    headers = {
        "X-App-Token": cfg["token"],
        "X-App-Access-Sig": sig,
        "X-App-Access-Ts": ts,
        "Accept": "application/json",
    }
    if body:
        headers["Content-Type"] = "application/json"
    req = _http_request(url=url, data=body if body else None, headers=headers, method=method)
    try:
        with _http_urlopen(req, timeout=8.0) as r:
            raw = r.read()
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {"_raw": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            err_body = ""
        raise RuntimeError(f"sumsub_http_{e.code}: {err_body[:400]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"sumsub_unreachable: {e.reason}") from e


def sumsub_create_applicant(
    *, external_user_id: str, email: str | None = None, country_iso: str | None = None
) -> dict:
    """Create or fetch a Sumsub applicant for the given external_user_id.

    We use ``external_user_id`` = ``opex_user`` (e.g. ``u-42``) so we can map
    webhook callbacks back to a local user row.

    Returns the applicant payload from Sumsub, including ``id``.

    If an applicant with this externalUserId already exists Sumsub returns 409
    -- in that case we do a GET by externalUserId and return that instead.
    """
    if not external_user_id:
        raise RuntimeError("external_user_id_required")
    cfg = _require_sumsub()
    payload = {
        "externalUserId": external_user_id,
    }
    if email:
        payload["email"] = email
    if country_iso:
        payload["country"] = country_iso  # ISO 3166-1 alpha-3 expected

    body = json.dumps(payload).encode("utf-8")
    path = f"/resources/applicants?levelName={urllib.parse.quote(cfg['level'])}"
    try:
        return sumsub_signed_request("POST", path, body)
    except RuntimeError as e:
        # On 409 (already exists) fall through to GET by externalUserId.
        msg = str(e)
        if "sumsub_http_409" not in msg and "alreadyExists" not in msg:
            raise
        return sumsub_get_applicant(external_user_id=external_user_id)


def sumsub_get_applicant(*, external_user_id: str) -> dict:
    """Fetch the latest applicant state by externalUserId.

    Returns ``{}`` if the applicant doesn't exist yet.
    """
    if not external_user_id:
        raise RuntimeError("external_user_id_required")
    _require_sumsub()
    path = f"/resources/applicants/-;externalUserId={urllib.parse.quote(external_user_id)}/one"
    try:
        return sumsub_signed_request("GET", path)
    except RuntimeError as e:
        if "sumsub_http_404" in str(e):
            return {}
        raise


def sumsub_access_token(*, external_user_id: str, ttl_seconds: int = 600) -> str:
    """Get a one-shot WebSDK access token. Browser passes this to launchWebSdk().

    Tokens are short-lived (default 10 min). The WebSDK calls a refresh
    callback to get a new one when needed.
    """
    if not external_user_id:
        raise RuntimeError("external_user_id_required")
    cfg = _require_sumsub()
    qs = urllib.parse.urlencode(
        {
            "userId": external_user_id,
            "levelName": cfg["level"],
            "ttlInSecs": int(ttl_seconds),
        }
    )
    path = f"/resources/accessTokens?{qs}"
    data = sumsub_signed_request("POST", path, b"")
    tok = data.get("token") or ""
    if not tok:
        raise RuntimeError(f"sumsub_token_missing: {data!r}")
    return tok


def sumsub_verify_webhook(*, signature: str, timestamp: str, body: bytes) -> bool:
    """HMAC-verify a webhook payload using SUMSUB_APP_SECRET.

    Sumsub signs ``timestamp + body`` with the same app_secret used for REST.
    Returns False on any malformed input rather than raising, so the caller
    just sends back a 401.

    Uses ``hmac.compare_digest`` to avoid timing leaks.
    """
    if not is_sumsub_configured():
        return False
    if not signature or not timestamp or body is None:
        return False
    secret = _sumsub_cfg()["secret"].encode()
    try:
        ts_bytes = str(timestamp).encode()
    except Exception:  # noqa: BLE001
        return False
    msg = ts_bytes + (body or b"")
    expected = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.lower())


def sumsub_review_answer_to_kyc_status(answer: str | None) -> str:
    """Map Sumsub reviewAnswer to our local kyc_status column.

    GREEN  -> verified
    RED    -> rejected
    others (YELLOW, None, ...) -> pending
    """
    a = (answer or "").upper()
    if a == "GREEN":
        return "verified"
    if a == "RED":
        return "rejected"
    return "pending"
