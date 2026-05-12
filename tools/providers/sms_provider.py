"""SMS provider interface.

In production this dispatches the 6-digit OTP through a KISA-approved Korean
SMS gateway (NICE 비밀스미스, SKT MO/MT, LG U+ SMS API, KCB, KMC). In demo it
just logs the (phone, code) pair to stderr -- which preserves the existing
`__demo_code` flow used by auth_server's /kyc/start.

Swap by setting:
    SMS_PROVIDER_URL  -- e.g. https://api.example-sms.kr/v1/send
    SMS_PROVIDER_KEY  -- bearer token / api key issued by the gateway

Failure mode is FAIL-CLOSED: if SMS_PROVIDER_URL is configured but unreachable,
this returns ok=False so the caller can present a 502 sms_unavailable instead
of pretending the message went out.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _optional_http_url(name: str, raw_url: str) -> str:
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return ""
    return _validated_http_url(raw_url, name=name)


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


SMS_PROVIDER_URL = _optional_http_url("SMS_PROVIDER_URL", os.environ.get("SMS_PROVIDER_URL", ""))
SMS_PROVIDER_KEY = os.environ.get("SMS_PROVIDER_KEY", "")
SMS_FROM = os.environ.get("SMS_FROM", "0212340000")  # caller-id; provider configures whitelist


def send_code(phone: str, code: str) -> dict:
    """Send the OTP. Returns
    {"ok": bool, "provider_message_id": str, "sent_at": int, "error": str|None}
    """
    if SMS_PROVIDER_URL:
        return _call_real(phone=phone, code=code)
    return _stub_send(phone=phone, code=code)


def _stub_send(*, phone, code):
    sys.stderr.write(f"[sms-stub] -> {phone}: KYC code {code}\n")
    return {
        "ok": True,
        "provider_message_id": f"stub-{int(time.time()*1000)}",
        "sent_at": int(time.time()),
        "error": None,
    }


def _call_real(*, phone, code):
    body = {
        "to": phone,
        "from": SMS_FROM,
        "text": f"[zkCEX] 인증번호 {code}. 5분 이내 입력 / Code valid 5min.",
        "type": "SMS",
    }
    headers = {"Content-Type": "application/json"}
    if SMS_PROVIDER_KEY:
        headers["Authorization"] = f"Bearer {SMS_PROVIDER_KEY}"
    req = _http_request(
        url=SMS_PROVIDER_URL,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with _http_urlopen(req, timeout=3.0) as r:
            data = json.loads(r.read().decode())
            return {
                "ok": True,
                "provider_message_id": str(data.get("messageId") or data.get("id") or ""),
                "sent_at": int(time.time()),
                "error": None,
            }
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ) as e:
        sys.stderr.write(f"[sms] provider unreachable: {type(e).__name__}: {e}\n")
        return {
            "ok": False,
            "provider_message_id": "",
            "sent_at": int(time.time()),
            "error": f"sms-unreachable: {type(e).__name__}: {e}",
        }
