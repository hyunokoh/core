"""Integration test: signup -> login flow against a live auth_server.

Prerequisites (provided by tools/run_integration_tests.sh):
  - auth_server.py running on 127.0.0.1:5501.
  - A Postgres instance reachable as configured by POSTGRES_DSN, OR
    auth_server defaulting to sqlite for the run.

The test is intentionally minimal — it covers the happy path that gates
nearly every other user-touching endpoint.
"""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request

import pytest

AUTH_BASE = os.environ.get("AUTH_BASE", "http://127.0.0.1:5501")
pytestmark = pytest.mark.integration


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


def _post(path: str, body: dict) -> tuple[int, dict]:
    req = _http_request(
        AUTH_BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _http_urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def _health_ok() -> bool:
    try:
        with _http_urlopen(AUTH_BASE + "/health", timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


@pytest.fixture(scope="module", autouse=True)
def _require_server():
    if not _health_ok():
        pytest.skip(f"auth_server not reachable at {AUTH_BASE}; skipping integration")


def test_signup_then_login_roundtrip():
    # Unique email per test run so we never collide with previous runs.
    suffix = secrets.token_hex(4)
    email = f"it-{suffix}@example.com"
    pw = "TestPass123!"

    code, body = _post(
        "/auth/signup",
        {
            "email": email,
            "password": pw,
            "name": "IT User",
        },
    )
    assert code == 201, body
    assert "token" in body
    assert body["user"]["email"] == email

    # Login with the same credentials.
    code, body = _post("/auth/login", {"email": email, "password": pw})
    assert code == 200, body
    # Either a session token (no 2FA) or a 2fa_required marker is acceptable.
    assert "token" in body or body.get("twofa_required") is True


def test_login_rejects_wrong_password():
    suffix = secrets.token_hex(4)
    email = f"it-bad-{suffix}@example.com"
    pw = "TestPass123!"
    _post("/auth/signup", {"email": email, "password": pw, "name": "X"})

    code, body = _post("/auth/login", {"email": email, "password": "wrong-pw-99"})
    assert code == 401
    assert body.get("error") == "invalid_credentials"
