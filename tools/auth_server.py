#!/usr/bin/env python3
"""Auth + Korean PASS-style KYC simulation server for zkCEX.

Stdlib-only HTTP server that listens on a port (default 5501) and is fronted by
the static homepage proxy on :5500. The browser sees same-origin URLs like
``http://localhost:5500/auth/login`` and ``http://localhost:5500/kyc/start``.

Persistence: SQLite at ``tools/.local/auth.db``.
Password hashing: stdlib ``hashlib.scrypt`` (n=2**14, r=8, p=1, dklen=64).

This is a demo. SMS is **not** sent — the 6-digit code is logged to stderr
and also returned in the response under ``__demo_code`` so the UI can autofill
it. RRN digits 2-7 are never stored; only the gender code (digit 1 of the
backside) is kept to derive birth-century + gender.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import secrets
import socketserver
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# --- Paths -----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
# providers/ is a sibling package next to this file -- ensure importable
# regardless of the cwd the server was launched from.
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import auth_db as _auth_db  # noqa: E402
from providers import geo_provider, kyc_provider, sms_provider  # noqa: E402

# OpenTelemetry tracing (stdlib-only). Silent no-op when OTEL_ENABLED=0 or
# the collector is unreachable. Set the service name BEFORE importing otel
# (module-level constants are captured at import time).
os.environ.setdefault("OTEL_SERVICE_NAME", "zkcex-auth")
try:
    from otel.shim import install as _otel_install  # noqa: E402
    from otel.shim import server_span as _otel_server_span
except Exception:  # noqa: BLE001 - never block service startup

    def _otel_install():
        pass

    def _otel_server_span(_h):
        class _N:
            def __enter__(self):
                class _S:
                    def set_attribute(self, *a, **kw):
                        pass

                return _S()

            def __exit__(self, *a):
                return False

        return _N()


LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "auth.db")

# Backend is chosen at startup via AUTH_DB_BACKEND. SQLite remains the default
# so legacy invocations behave identically. Set AUTH_DB_BACKEND=postgres to
# switch to the production-track Postgres backend (see auth_db/POSTGRES_PROD.md).
AUTH_DB = _auth_db.AuthDB()
SERVER_VERSION = "0.4.0"


# --- Config ----------------------------------------------------------------
def _validated_http_base_url(name: str, raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url.rstrip("/")


WALLET_BASE = _validated_http_base_url(
    "WALLET_BASE", os.environ.get("WALLET_BASE", "http://127.0.0.1:8091")
)
SESSION_TTL = 30 * 24 * 3600  # 30 days
KYC_TTL = 5 * 60  # 5 minutes
MAX_KYC_ATTEMPTS = 3
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
# Some Python builds (notably macOS system Python 3.9) lack hashlib.scrypt
# because the linked OpenSSL was compiled without it. Detect once at startup
# and fall back to PBKDF2-HMAC-SHA512 with a high iteration count, which is
# also OWASP-acceptable and pure stdlib. The hashed blob is prefixed with a
# 1-byte algo tag so verification picks the right routine.
_HAS_SCRYPT = hasattr(hashlib, "scrypt")
PBKDF2_ITERS = 600_000  # OWASP 2024+ recommendation for SHA-512
PBKDF2_DKLEN = 64
ALGO_SCRYPT = b"\x01"
ALGO_PBKDF2 = b"\x02"

CARRIERS = {"SKT", "KT", "LGU", "SKT_MVNO", "KT_MVNO", "LGU_MVNO"}

PUSH_BASE = _validated_http_base_url(
    "PUSH_BASE", os.environ.get("PUSH_BASE", "http://127.0.0.1:5580")
)

# --- WebAuthn / Passkey config --------------------------------------------
# The Relying Party (RP) identifier is the registrable domain that owns the
# credentials -- ``localhost`` for the demo, the public DNS name in
# production. The origin is the exact ``scheme://host[:port]`` the browser
# sees; clientDataJSON.origin must match this byte-for-byte.
WEBAUTHN_RP_ID = os.environ.get("WEBAUTHN_RP_ID", "localhost")
WEBAUTHN_RP_NAME = "zkCEX"
WEBAUTHN_ORIGIN = os.environ.get("WEBAUTHN_ORIGIN", "http://localhost:5500")
WEBAUTHN_CHALLENGE_TTL = 5 * 60  # 5 minutes
WEBAUTHN_CHALLENGE_BYTES = 32

# Detect cryptography lib at import. The signature-verify path on
# /authenticate/finish degrades to 501 if absent; everything else (challenge
# issuance, COSE parsing, sign_count check, listing/deleting credentials)
# works regardless. Production should always have it installed.
try:
    from cryptography.hazmat.primitives import hashes as _crypto_hashes  # noqa: F401
    from cryptography.hazmat.primitives.asymmetric import ec as _crypto_ec
    from cryptography.hazmat.primitives.asymmetric import padding as _crypto_padding
    from cryptography.hazmat.primitives.asymmetric import rsa as _crypto_rsa  # noqa: F401

    _HAS_CRYPTOGRAPHY = True
except Exception:  # noqa: BLE001
    _HAS_CRYPTOGRAPHY = False

# Geo-blocking. Toggled by env at server boot — see providers/geo_provider.py
# for blocklist + trusted-proxy parsing. The admin endpoint
# /auth/geo/log is gated by this token; if unset, the endpoint stays open
# only to loopback callers (same belt-and-braces as api_key_server's verify
# route).
GEO_ADMIN_TOKEN = os.environ.get("GEO_ADMIN_TOKEN", "")


def _push_notify(opex_user: str, payload: dict) -> None:
    """Best-effort push fan-out via push_server.

    Wrapped tight so a missing push server never blocks a KYC verification.
    """
    if not opex_user:
        return
    try:
        body = json.dumps({"opex_user": opex_user, "payload": payload}).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - PUSH_BASE is http(s)-validated.
            f"{PUSH_BASE}/push/send",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=2).read()  # noqa: S310
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[auth] push notify failed: {e!r}\n")


NOTIF_BASE = _validated_http_base_url(
    "NOTIF_BASE", os.environ.get("NOTIF_BASE", "http://127.0.0.1:5691")
)


def _notif_send(
    opex_user: str, ntype: str, category: str, title: str, body: str, metadata: dict | None = None
) -> None:
    """Best-effort fan-out to notification_center (inbox + email + push)."""
    if not opex_user:
        return
    try:
        payload = {
            "opex_user": opex_user,
            "type": ntype,
            "category": category,
            "title": title,
            "body": body,
            "metadata": metadata or {},
        }
        req = urllib.request.Request(  # noqa: S310 - NOTIF_BASE is http(s)-validated.
            f"{NOTIF_BASE}/notifications/send",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=1).read()  # noqa: S310
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[auth] notification send failed: {e!r}\n")


REFERRAL_BASE = _validated_http_base_url(
    "REFERRAL_BASE", os.environ.get("REFERRAL_BASE", "http://127.0.0.1:5695")
)


def _apply_referral_loopback(code: str, opex_user: str, client_ip: str | None) -> bool:
    """Forward a signup's referral_code to referral.py's loopback endpoint.

    Returns True on applied:true, False otherwise (invalid code, self-referral,
    referral.py down, etc.). Never raises — the caller treats the result as a
    best-effort attribution.
    """
    if not code or not opex_user:
        return False
    redacted = None
    if client_ip:
        # Strip the last octet for IPv4 — the same convention used by the geo
        # audit log so we don't store raw IPs anywhere outside the proxy.
        parts = client_ip.split(".")
        if len(parts) == 4:
            redacted = ".".join(parts[:3]) + ".0/24"
        else:
            redacted = client_ip[:24]
    try:
        body = json.dumps(
            {"code": code, "opex_user": opex_user, "signup_ip_redacted": redacted}
        ).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - REFERRAL_BASE is http(s)-validated.
            f"{REFERRAL_BASE}/referral/internal/record-signup",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8") or "{}")
            return bool(data.get("applied"))
    except Exception as e:
        sys.stderr.write(f"[auth] referral apply failed: {e!r}\n")
        return False


def _record_kyc_to_referral(opex_user: str) -> None:
    """Fire-and-forget: tell referral.py that this user just completed KYC."""
    if not opex_user:
        return
    try:
        body = json.dumps({"referee_opex_user": opex_user}).encode("utf-8")
        req = urllib.request.Request(  # noqa: S310 - REFERRAL_BASE is http(s)-validated.
            f"{REFERRAL_BASE}/referral/internal/record-kyc",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=2).read()  # noqa: S310
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[auth] referral kyc record failed: {e!r}\n")


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NAME_RE = re.compile(r"^.{1,32}$")  # post-strip
DIGITS6 = re.compile(r"^\d{6}$")
DIGIT1 = re.compile(r"^[1-4]$")
PHONE_RE = re.compile(r"^010\d{8}$")
KYC_NAME_RE = re.compile(r"^[A-Za-z가-힣 ]{2,16}$")  # 한글 또는 라틴, 2-16자


# --- DB --------------------------------------------------------------------
# The persistence layer is now behind ``auth_db.AuthDB``. The two backends
# (SQLite + Postgres) expose identical methods, so the handler code below is
# backend-agnostic. ``init_db`` just runs the idempotent CREATE TABLE script.
def init_db():
    AUTH_DB.init_schema()


# --- 2FA / TOTP ------------------------------------------------------------
# RFC 6238 TOTP + RFC 4226 HOTP, stdlib only. Authenticator app compatible
# (Google Authenticator, Authy, 1Password, Bitwarden, ...): SHA-1, 6 digits,
# 30 s period, ±30 s clock-drift window.
TOTP_DIGITS = 6
TOTP_PERIOD = 30
TOTP_WINDOW = 1  # ±N steps -> ±N*period seconds tolerance

# Recovery code alphabet: base32 minus visual ambiguity (0/O, 1/I/L).
RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
RECOVERY_LEN = 10
RECOVERY_COUNT = 10
RECOVERY_PBKDF2_ITERS = 200_000

# Rate-limit: 5 wrong codes in 15 min -> 30 min lock.
TOTP_FAIL_WINDOW_SEC = 15 * 60
TOTP_FAIL_THRESHOLD = 5
TOTP_LOCK_SECONDS = 30 * 60
TOTP_SETUP_MAX_ATTEMPTS = 5


def _normalize_totp_secret(s: str) -> str:
    return (s or "").upper().replace(" ", "").replace("-", "")


def verify_totp(secret_b32: str, code: str, window: int = TOTP_WINDOW) -> tuple[bool, int | None]:
    """Verify a 6-digit TOTP code with ±window 30s intervals tolerance.

    Returns (ok, counter). ``counter`` is the step index that matched, so the
    caller can persist it for replay protection.
    """
    if not secret_b32 or not isinstance(code, str):
        return False, None
    try:
        key = base64.b32decode(_normalize_totp_secret(secret_b32), casefold=True)
    except Exception:
        return False, None
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return False, None
    counter = int(time.time()) // TOTP_PERIOD
    target = int(code)
    for offset in range(-window, window + 1):
        c = counter + offset
        if c < 0:
            continue
        msg = struct.pack(">Q", c)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        o = h[-1] & 0x0F
        v = ((h[o] & 0x7F) << 24) | (h[o + 1] << 16) | (h[o + 2] << 8) | h[o + 3]
        if hmac.compare_digest(f"{v % 1_000_000:06d}", f"{target:06d}"):
            return True, c
    return False, None


def gen_totp_secret() -> str:
    """20 random bytes, base32-encoded with padding stripped."""
    raw = secrets.token_bytes(20)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def gen_recovery_codes() -> list[str]:
    """Ten 10-char codes from the visually unambiguous alphabet."""
    codes = []
    for _ in range(RECOVERY_COUNT):
        codes.append("".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LEN)))
    return codes


def hash_recovery_code(code: str) -> tuple[bytes, bytes]:
    """PBKDF2-SHA256 hash. Returns (salt, derived). Used for both create and verify."""
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", code.encode("ascii"), salt, RECOVERY_PBKDF2_ITERS, 32)
    return salt, derived


def verify_recovery_code(code: str, salt: bytes, expected: bytes) -> bool:
    derived = hashlib.pbkdf2_hmac(
        "sha256", code.encode("ascii"), salt, RECOVERY_PBKDF2_ITERS, len(expected)
    )
    return hmac.compare_digest(derived, expected)


def build_otpauth_url(email: str, secret_b32: str) -> str:
    label = urllib.parse.quote(f"zkCEX:{email}", safe="")
    params = urllib.parse.urlencode(
        {
            "secret": secret_b32,
            "issuer": "zkCEX",
            "algorithm": "SHA1",
            "digits": str(TOTP_DIGITS),
            "period": str(TOTP_PERIOD),
        }
    )
    return f"otpauth://totp/{label}?{params}"


def render_qr_svg(payload: str) -> str:
    """Render a base64-encoded SVG QR code for ``payload``.

    Pure stdlib implementation that builds a QR code "visual placeholder" --
    a 21x21 module grid derived from a SHA-256 of the payload, plus the
    classic three finder squares in the corners. Real authenticator apps
    cannot scan this image (a true QR encoder is ~600 LOC); callers should
    fall back to the otpauth:// URL displayed below the image and the
    "tap to copy" affordance. The SVG still gives the user a visual cue
    that something is rendered, and the QR-format finder pattern keeps the
    UX honest about what's there.

    Returns: base64-encoded SVG bytes, ready to drop into a data: URL.
    """
    n = 21
    cell = 8  # px per module
    margin = cell * 2

    # Deterministic dot pattern derived from SHA-256 of the payload. Two
    # rounds of hashing give us 64 bytes -> 512 bits, more than enough for
    # the 21*21 = 441 cells.
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    digest += hashlib.sha256(b"qr2:" + digest).digest()
    bits = []
    for byte in digest:
        for shift in range(8):
            bits.append((byte >> shift) & 1)
    grid = [[0] * n for _ in range(n)]
    idx = 0
    for y in range(n):
        for x in range(n):
            grid[y][x] = bits[idx % len(bits)]
            idx += 1

    # Finder pattern (top-left, top-right, bottom-left). Each is a 7x7
    # outer black square with a 5x5 white inset and a 3x3 black core.
    def stamp_finder(ox: int, oy: int):
        for y in range(7):
            for x in range(7):
                if x == 0 or x == 6 or y == 0 or y == 6:
                    grid[oy + y][ox + x] = 1
                elif x in (1, 5) or y in (1, 5):
                    grid[oy + y][ox + x] = 0
                else:
                    grid[oy + y][ox + x] = 1
        # 1-module quiet zone (white) around each finder.
        for y in range(-1, 8):
            for x in range(-1, 8):
                if x in (-1, 7) or y in (-1, 7):
                    yy, xx = oy + y, ox + x
                    if 0 <= xx < n and 0 <= yy < n:
                        grid[yy][xx] = 0

    stamp_finder(0, 0)
    stamp_finder(n - 7, 0)
    stamp_finder(0, n - 7)

    size = n * cell + 2 * margin
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
        f'height="{size}" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">',
        f'<rect width="{size}" height="{size}" fill="#ffffff"/>',
    ]
    for y in range(n):
        for x in range(n):
            if grid[y][x]:
                px = margin + x * cell
                py = margin + y * cell
                parts.append(
                    f'<rect x="{px}" y="{py}" width="{cell}" ' f'height="{cell}" fill="#000000"/>'
                )
    parts.append("</svg>")
    svg = "".join(parts).encode("utf-8")
    return base64.b64encode(svg).decode("ascii")


# ============================================================================
# WebAuthn helpers
# ============================================================================
# All inputs/outputs that travel over the wire are base64url (the WebAuthn
# spec mandates it). Internally we work with ``bytes`` and convert at the
# edges via these two helpers.
def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    s = s.strip()
    if not s:
        return b""
    # Browsers may send standard or URL-safe variants; tolerate either.
    s = s.replace("-", "+").replace("_", "/")
    # Re-pad to a multiple of 4 so the stdlib decoder doesn't choke.
    pad = (-len(s)) % 4
    return base64.b64decode(s + ("=" * pad))


# ---- minimal CBOR decoder (WebAuthn subset) -------------------------------
# WebAuthn uses CBOR (RFC 8949) in two places: the attestationObject sent
# during register, and the COSE-encoded public key inside the attestedCredentialData.
# We implement the small subset that actually appears there:
#   major 0: unsigned int (0..23 inline, 24/25/26/27 followed by 1/2/4/8 bytes)
#   major 1: negative int (same shape; value = -(n+1))
#   major 2: byte string
#   major 3: text string (UTF-8)
#   major 4: array
#   major 5: map
#   major 7 / 22: null
# This is enough for ES256/RS256 keys + attObj parsing. ~80 LOC.
class _CborError(Exception):
    pass


def _cbor_decode(data: bytes) -> tuple:
    """Decode the first CBOR item from ``data``. Returns (value, bytes_consumed)."""
    val, n = _cbor_decode_at(data, 0)
    return val, n


def _cbor_decode_at(data: bytes, pos: int):
    if pos >= len(data):
        raise _CborError("truncated")
    ib = data[pos]
    pos += 1
    major = ib >> 5
    minor = ib & 0x1F
    if minor < 24:
        arg = minor
    elif minor == 24:
        arg = data[pos]
        pos += 1
    elif minor == 25:
        arg = int.from_bytes(data[pos : pos + 2], "big")
        pos += 2
    elif minor == 26:
        arg = int.from_bytes(data[pos : pos + 4], "big")
        pos += 4
    elif minor == 27:
        arg = int.from_bytes(data[pos : pos + 8], "big")
        pos += 8
    else:
        raise _CborError(f"unsupported minor {minor}")

    if major == 0:
        return arg, pos
    if major == 1:
        return -1 - arg, pos
    if major == 2:
        chunk = bytes(data[pos : pos + arg])
        if len(chunk) != arg:
            raise _CborError("truncated bytestring")
        return chunk, pos + arg
    if major == 3:
        chunk = bytes(data[pos : pos + arg])
        if len(chunk) != arg:
            raise _CborError("truncated string")
        return chunk.decode("utf-8", "replace"), pos + arg
    if major == 4:
        out = []
        for _ in range(arg):
            v, pos = _cbor_decode_at(data, pos)
            out.append(v)
        return out, pos
    if major == 5:
        out = {}
        for _ in range(arg):
            k, pos = _cbor_decode_at(data, pos)
            v, pos = _cbor_decode_at(data, pos)
            out[k] = v
        return out, pos
    if major == 7:
        if minor == 22:  # null
            return None, pos
        if minor == 20:  # false
            return False, pos
        if minor == 21:  # true
            return True, pos
    raise _CborError(f"unsupported major {major}")


def _parse_attestation_object(att_obj_bytes: bytes) -> dict:
    """Parse the CBOR attestationObject blob sent by the authenticator.

    The top-level is a 3-key map: ``fmt`` (str), ``attStmt`` (map),
    ``authData`` (bytes). The interesting bits for us are inside authData.
    """
    obj, _ = _cbor_decode(att_obj_bytes)
    if not isinstance(obj, dict):
        raise ValueError("attestationObject is not a CBOR map")
    fmt = obj.get("fmt") or "none"
    auth_data = obj.get("authData") or b""
    if not isinstance(auth_data, (bytes, bytearray)):
        raise ValueError("authData is not a byte string")
    return {"fmt": fmt, "authData": bytes(auth_data), "attStmt": obj.get("attStmt") or {}}


def _parse_auth_data(auth_data: bytes, *, expect_attested: bool) -> dict:
    """Decode the authenticatorData fixed layout (W3C WebAuthn L2 §6.1)."""
    if len(auth_data) < 37:
        raise ValueError("authData too short")
    rp_id_hash = auth_data[:32]
    flags = auth_data[32]
    sign_count = int.from_bytes(auth_data[33:37], "big")
    out = {
        "rp_id_hash": rp_id_hash,
        "flags": flags,
        "user_present": bool(flags & 0x01),
        "user_verified": bool(flags & 0x04),
        "at_included": bool(flags & 0x40),
        "ed_included": bool(flags & 0x80),
        "sign_count": sign_count,
    }
    pos = 37
    if expect_attested:
        if not out["at_included"]:
            raise ValueError("authData missing attested-credential-data flag")
        if len(auth_data) < pos + 18:
            raise ValueError("authData too short for AAGUID+credIdLen")
        aaguid = auth_data[pos : pos + 16]
        pos += 16
        cred_id_len = int.from_bytes(auth_data[pos : pos + 2], "big")
        pos += 2
        if len(auth_data) < pos + cred_id_len:
            raise ValueError("authData too short for credentialId")
        cred_id = auth_data[pos : pos + cred_id_len]
        pos += cred_id_len
        # The remainder of authData is the COSE-encoded public key, possibly
        # followed by extensions (which we ignore). _cbor_decode stops at the
        # end of the first CBOR item so the leftover is harmless.
        cose_remainder = auth_data[pos:]
        cose_key, consumed = _cbor_decode(cose_remainder)
        out["aaguid"] = bytes(aaguid)
        out["credential_id"] = bytes(cred_id)
        out["cose_public_key"] = cose_key
        out["cose_public_key_bytes"] = bytes(cose_remainder[:consumed])
    return out


def _aaguid_hex(aaguid: bytes) -> str:
    """Format a 16-byte AAGUID as a dashed UUID string (RFC 4122)."""
    if not aaguid or len(aaguid) != 16:
        return ""
    h = aaguid.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ---- AAGUID lookup --------------------------------------------------------
# Authenticator-model registry. Embedded for offline lookup; this is the
# infrastructure-vendor identifier the authenticator announces so we can
# show the user "iPhone (Touch ID)" instead of a hex string. Real registry
# is much larger -- this is the long tail of common dev/desktop hardware.
# Format: AAGUID (lower-case dashed) -> friendly name.
_AAGUID_NAMES = {
    "00000000-0000-0000-0000-000000000000": "Platform authenticator",
    "08987058-cadc-4b81-b6e1-30de50dcbe96": "Windows Hello",
    "9ddd1817-af5a-4672-a2b9-3e3dd95000a9": "Windows Hello",
    "6028b017-b1d4-4c02-b4b3-afcdafc96bb2": "Windows Hello",
    "ee882879-721c-4913-9775-3dfcce97072a": "1Password",
    "adce0002-35bc-c60a-648b-0b25f1f05503": "Chrome on Mac",
    "fbfc3007-154e-4ecc-8c0b-6e020557d7bd": "iCloud Keychain",
    "dd4ec289-e01d-41c9-bb89-70fa845d4bf2": "iCloud Keychain",
    "bada5566-a7aa-401f-bd96-45619a55120d": "1Password",
    "cb69481e-8ff7-4039-93ec-0a2729a154a8": "YubiKey 5 Series",
    "f8a011f3-8c0a-4d15-8006-17111f9edc7d": "YubiKey 5 NFC",
    "2fc0579f-8113-47ea-b116-bb5a8db9202a": "YubiKey 5 NFC",
    "73bb0cd4-e502-49b8-9c6f-b59445bf720b": "YubiKey 5 NFC FIPS",
    "fa2b99dc-9e39-4257-8f92-4a30d23c4118": "YubiKey 5 NFC",
    "c5ef55ff-ad9a-4b9f-b580-adebafe026d0": "YubiKey 5Ci",
    "85203421-48f9-4355-9bc8-8a53846e5083": "YubiKey 5Ci FIPS",
    "ec31b4cc-2acc-4b8e-9c01-bade00ccbe26": "YubiKey Bio",
    "b92c3f9a-c014-4056-887f-140a2501163b": "YubiKey 5 Nano",
    "149a2021-8ef6-4133-96b8-81f8d5b7f1f5": "Yubico Security Key NFC",
    "b6ede29c-3772-412c-8a78-539c1f4c62d2": "Yubico Security Key NFC FIPS",
    "f56f58b3-d711-4afc-ba7d-6ac05f88cb19": "Yubico Security Key",
    "0bb43545-fd2c-4185-87dd-feb0b2916ace": "Yubico Security Key Enterprise",
    "454e5346-4944-4ffd-6c93-8e9267193e9a": "Yubico Authenticator",
    "fa1f7a1b-d92d-491b-bb56-7ca5715f1f60": "Solo 1",
    "8876631b-d4a0-427f-5773-0ec71c9e0279": "SoloKeys Solo",
    "8c97a730-3f7b-41a6-87d6-1e9b62bda6f0": "SoloKeys",
    "39a5647e-1853-446c-a1f6-a79bae9f5bc7": "Vasco SecureClick",
    "9876631b-d4a0-427f-5773-0ec71c9e0279": "SoloKeys (HW)",
    "ad784498-1902-3f54-b99a-10bb7dbd9588": "Apple Touch ID (laptop)",
    "9f0d8150-baa5-4c00-9299-ad62c8bb4e87": "Apple",
    "39a5647e-1853-446c-a1f6-a79b9febd6c2": "Apple Face ID",
    "53414d53-554e-4700-0000-000000000000": "Samsung Pass",
    "53414d53-554e-4700-0001-000000000000": "Samsung Pass",
    "b93fd961-f2e6-462f-b122-82002247de78": "Android (Play Services)",
    "0ea242b4-43c4-4a1b-8b17-dd6d0b6baec6": "Android KeyStore",
    "39a5647e-1853-446c-a1f6-a79bae9fbcad": "Android",
    "8836336a-f590-0921-301d-46427531eee6": "Feitian BioPass",
    "77010bd7-212a-4fc9-b236-d2ca5e9d4084": "Feitian BioPass FIDO2",
    "12ded745-4bed-47d4-abaa-e713f51d6393": "Feitian",
    "ee041bce-25e5-4cdb-8f86-897fd6418464": "Feitian ePass FIDO2",
    "833b721a-ff5f-4d00-bb2e-bdda3ec01e29": "Feitian ePass",
    "85e02fa1-3a45-4dfb-9c0a-bd99f4d3a30e": "Trezor",
    "f4c63eff-d26c-4248-801c-3736c7eaa93a": "Trezor T",
    "e1a96183-8770-49b1-9f76-bbe2ee48d0d8": "Nitrokey",
    "08e6e7c2-ef96-49da-a263-f9c6fcad1c44": "Nitrokey Pro 2",
    "b84e4048-15dc-4dd0-8640-f4f60813c8af": "Nitrokey FIDO2",
    "73402251-f2a8-4f03-873e-3cb6db604b03": "OnlyKey",
    "8c39ee69-be91-44c0-9bcc-94f76f4d7a14": "Token2 PIN+",
    "f0a3bcd6-2ce4-4d7b-bd54-d2ffd6cfdb91": "Token2 T2F2",
    "62e54e98-c209-4df3-b692-de71bb6a8528": "Google Titan",
    "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4": "Google Titan (T1, T2)",
    "fec067a1-f1d0-4c5e-b4c0-cd9167747033": "Google Titan v2",
    "30b5035e-d297-4fc1-b00b-addc96ba6a98": "OnlyKey DUO",
    "97e6a830-c952-4740-95fc-7c78dc97ce47": "WiSeKey",
}


def _aaguid_friendly_name(aaguid_hex: str) -> str | None:
    """Look up a friendly name for an AAGUID; falls back to a generic label."""
    key = (aaguid_hex or "").lower()
    if not key:
        return None
    name = _AAGUID_NAMES.get(key)
    if name:
        return name
    if key == "00000000-0000-0000-0000-000000000000":
        return "Platform authenticator"
    return None


# ---- COSE -> verification key ---------------------------------------------
# COSE key shape (RFC 8152):
#   ES256: {1:2 (kty=EC2), 3:-7, -1:1 (P-256), -2:<x>, -3:<y>}
#   RS256: {1:3 (kty=RSA), 3:-257, -1:<n>, -2:<e>}
# These helpers only run when _HAS_CRYPTOGRAPHY is true.
def _cose_load_verifier(cose_key: dict):
    """Return a callable ``verify(signature_bytes, message_bytes) -> bool``."""
    if not _HAS_CRYPTOGRAPHY:
        raise RuntimeError("cryptography library is required")
    if not isinstance(cose_key, dict):
        raise ValueError("COSE key is not a map")
    kty = cose_key.get(1)
    alg = cose_key.get(3)
    if kty == 2 and alg == -7:
        x = cose_key.get(-2)
        y = cose_key.get(-3)
        if not isinstance(x, (bytes, bytearray)) or not isinstance(y, (bytes, bytearray)):
            raise ValueError("ES256 key missing x/y")
        # Build a P-256 public key from the raw uncompressed (x||y) coords.
        public_numbers = _crypto_ec.EllipticCurvePublicNumbers(
            int.from_bytes(bytes(x), "big"),
            int.from_bytes(bytes(y), "big"),
            _crypto_ec.SECP256R1(),
        )
        pubkey = public_numbers.public_key()

        def _verify(sig: bytes, msg: bytes) -> bool:
            # WebAuthn sends the DER-encoded ECDSA signature exactly as
            # produced by the platform; cryptography expects the same.
            try:
                pubkey.verify(sig, msg, _crypto_ec.ECDSA(_crypto_hashes.SHA256()))
                return True
            except Exception:  # noqa: BLE001
                return False

        return _verify

    if kty == 3 and alg == -257:
        n = cose_key.get(-1)
        e = cose_key.get(-2)
        if not isinstance(n, (bytes, bytearray)) or not isinstance(e, (bytes, bytearray)):
            raise ValueError("RS256 key missing n/e")
        public_numbers = _crypto_rsa.RSAPublicNumbers(
            int.from_bytes(bytes(e), "big"),
            int.from_bytes(bytes(n), "big"),
        )
        pubkey = public_numbers.public_key()

        def _verify(sig: bytes, msg: bytes) -> bool:
            try:
                pubkey.verify(sig, msg, _crypto_padding.PKCS1v15(), _crypto_hashes.SHA256())
                return True
            except Exception:  # noqa: BLE001
                return False

        return _verify

    raise ValueError(f"unsupported COSE alg/kty: kty={kty!r} alg={alg!r}")


def _rp_id_hash(rp_id: str) -> bytes:
    return hashlib.sha256(rp_id.encode("ascii")).digest()


def _is_loopback_addr(remote: str) -> bool:
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except Exception:
        return False


# --- Crypto / token helpers ------------------------------------------------
def hash_pw(password: str, salt: bytes) -> bytes:
    """Return ``algo_tag || raw_hash``. Picks scrypt when available, else PBKDF2."""
    pw_bytes = password.encode("utf-8")
    if _HAS_SCRYPT:
        raw = hashlib.scrypt(
            pw_bytes,
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=SCRYPT_DKLEN,
        )
        return ALGO_SCRYPT + raw
    raw = hashlib.pbkdf2_hmac("sha512", pw_bytes, salt, PBKDF2_ITERS, PBKDF2_DKLEN)
    return ALGO_PBKDF2 + raw


def verify_pw(password: str, salt: bytes, expected: bytes) -> bool:
    if not expected:
        return False
    tag, raw = expected[:1], expected[1:]
    pw_bytes = password.encode("utf-8")
    if tag == ALGO_SCRYPT and _HAS_SCRYPT:
        got = hashlib.scrypt(
            pw_bytes,
            salt=salt,
            n=SCRYPT_N,
            r=SCRYPT_R,
            p=SCRYPT_P,
            dklen=len(raw),
        )
    elif tag == ALGO_PBKDF2:
        got = hashlib.pbkdf2_hmac("sha512", pw_bytes, salt, PBKDF2_ITERS, len(raw))
    else:
        return False
    return hmac.compare_digest(got, raw)


def make_token() -> str:
    return secrets.token_hex(32)


def mask_phone(phone: str) -> str:
    # 010-1234-5678 -> 010-****-5678
    if not phone or len(phone) != 11:
        return phone or ""
    return f"{phone[0:3]}-****-{phone[7:]}"


# --- Validation helpers ----------------------------------------------------
def validate_signup(body: dict) -> tuple[str | None, str, str, str]:
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    name = (body.get("name") or "").strip()
    if not EMAIL_RE.match(email):
        return ("invalid_email", "", "", "")
    if len(password) < 8 or not re.search(r"\d", password) or not re.search(r"[A-Za-z]", password):
        return ("invalid_password", "", "", "")
    if not NAME_RE.match(name) or len(name) < 1:
        return ("invalid_name", "", "", "")
    return (None, email, password, name)


def validate_kyc_start(body: dict) -> tuple[str | None, dict]:
    carrier = (body.get("carrier") or "").upper().strip()
    name = (body.get("name") or "").strip()
    rrn_front = (body.get("rrn_front") or "").strip()
    rrn_back1 = (body.get("rrn_back1") or "").strip()
    phone = re.sub(r"[^\d]", "", body.get("phone") or "")
    if carrier not in CARRIERS:
        return ("invalid_carrier", {})
    if not KYC_NAME_RE.match(name):
        return ("invalid_name", {})
    if not DIGITS6.match(rrn_front):
        return ("invalid_rrn_front", {})
    # Sanity check the date-of-birth (loose — allow invalid feb 30 etc., but
    # at least require a plausible month / day).
    _yy, mm, dd = int(rrn_front[0:2]), int(rrn_front[2:4]), int(rrn_front[4:6])
    if not (1 <= mm <= 12) or not (1 <= dd <= 31):
        return ("invalid_rrn_front", {})
    if not DIGIT1.match(rrn_back1):
        return ("invalid_rrn_back1", {})
    if not PHONE_RE.match(phone):
        return ("invalid_phone", {})
    return (
        None,
        {
            "carrier": carrier,
            "name": name,
            "rrn_front": rrn_front,
            "rrn_back1": rrn_back1,
            "phone": phone,
        },
    )


# --- Demo seeder -----------------------------------------------------------
def seed_demo_funds(opex_user: str) -> None:
    """Best-effort: 10 zkETH + 10 zkUSDT to <opex_user>_MAIN. Never raises."""
    ts = int(time.time() * 1000)
    deposits = [
        ("ETH", f"demo-eth-{ts}"),
        ("USDT", f"demo-usd-{ts}"),
    ]
    for asset, ref in deposits:
        path = (
            f"/deposit/10_test-ethereum_{asset}/"
            f"{urllib.parse.quote(opex_user)}_MAIN"
            f"?description=demo-seed&transferRef={urllib.parse.quote(ref)}"
        )
        url = WALLET_BASE + path
        try:
            req = urllib.request.Request(  # noqa: S310 - WALLET_BASE is http(s)-validated.
                url, method="POST", data=b""
            )
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                resp.read()
            sys.stderr.write(f"[seed] {opex_user} +10 {asset} OK\n")
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[seed] {opex_user} +10 {asset} FAILED: {e}\n")


# --- Session helpers -------------------------------------------------------
def issue_session(user_id: int) -> tuple[str, int]:
    """Mint a fresh token for ``user_id`` and revoke any previous sessions."""
    token = make_token()
    now = int(time.time())
    expires = now + SESSION_TTL
    # Single-active-token policy: wipe other sessions for this user.
    AUTH_DB.create_session(user_id=user_id, token=token, expires_at=expires)
    _ = now  # kept for symmetry with the prior code path
    return token, expires


def session_user(token: str | None):
    """Return the user dict for a bearer token, or None if invalid/expired."""
    if not token:
        return None
    return AUTH_DB.lookup_session(token)


def user_dict(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "opex_user": row["opex_user"],
        "kyc_status": row["kyc_status"] or "none",
    }


# --- HTTP machinery --------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-auth/1.0"

    # ---- helpers ---------------------------------------------------------
    def _send_json(self, status: int, payload: dict | None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self._response_code = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    # ---- Geo-blocking ---------------------------------------------------
    def _client_ip(self) -> str:
        """Return the canonical client IP for this request.

        Trusts XFF / X-Real-IP only when the immediate peer is on the
        GEO_TRUSTED_PROXIES list (default: 127.0.0.0/8 — the homepage proxy).
        """
        remote = ""
        try:
            remote = self.client_address[0] if self.client_address else ""
        except Exception:  # noqa: BLE001
            remote = ""
        return geo_provider.resolve_client_ip(
            remote_addr=remote,
            x_forwarded_for=self.headers.get("X-Forwarded-For"),
            x_real_ip=self.headers.get("X-Real-IP"),
        )

    def _geo_check(self, *, endpoint: str) -> dict:
        """Resolve country for the current request and decide allow/block.

        Always returns a dict {ip, country, country_name, source, blocked,
        reason}. When GEO_BLOCK_ENABLED=0 the decision is forced to
        ``blocked=False`` but the lookup still runs (so /auth/geo/whoami
        keeps working as a diagnostic).
        """
        ip = self._client_ip()
        info = geo_provider.lookup_country(ip)
        country = info.country_iso2 if info else None
        country_name = info.country_name if info else None
        source = info.source if info else None
        if geo_provider.is_enforcement_enabled():
            blocked, reason = geo_provider.is_blocked(country)
        else:
            blocked, reason = (False, None)
        verdict = {
            "ip": ip,
            "country": country,
            "country_name": country_name,
            "source": source,
            "blocked": blocked,
            "reason": reason,
        }
        # Persist the decision (last octet redacted) so the admin diagnostics
        # endpoint can show recent blocks/allows. Best-effort.
        try:
            AUTH_DB.insert_geo_decision(
                ts=int(time.time()),
                ip_redacted=geo_provider.redact_ip(ip),
                country=country,
                endpoint=endpoint,
                decision="block" if blocked else "allow",
                reason=reason,
            )
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[geo] audit insert failed: {e}\n")
        return verdict

    def _geo_enforce(self, *, endpoint: str) -> bool:
        """Run the geo check and, if blocked, write a 451 + return True.

        Returns True when the request was blocked and a response has been
        written (caller must return immediately). Returns False when the
        request is allowed to proceed.
        """
        verdict = self._geo_check(endpoint=endpoint)
        if verdict["blocked"]:
            self._send_json(
                451,
                {
                    "error": "geo_blocked",
                    "country": verdict["country"],
                    "country_name": verdict["country_name"],
                    "reason": verdict["reason"],
                    "message": (
                        "Service is not available from "
                        f"{verdict['country_name'] or verdict['country']}. "
                        "Please contact support if you believe this is an error."
                    ),
                },
            )
            return True
        return False

    def _require_user(self):
        row = session_user(self._bearer())
        if not row:
            self._send_json(401, {"error": "unauthorized"})
            return None
        return row

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[auth] {self.address_string()} - {fmt % args}\n")

    # ---- CORS ------------------------------------------------------------
    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Opex-User")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---- routing ---------------------------------------------------------
    def _route(self):
        path = urllib.parse.urlsplit(self.path).path
        method = self.command
        # Auth
        if method == "POST" and path == "/auth/signup":
            return self.h_signup
        if method == "POST" and path == "/auth/login":
            return self.h_login
        if method == "GET" and path == "/auth/me":
            return self.h_me
        if method == "POST" and path == "/auth/logout":
            return self.h_logout
        if method == "GET" and path == "/auth/session/peek":
            return self.h_peek
        if method == "GET" and path in ("/auth/health", "/health"):
            return self.h_health
        # Internal endpoint used by pol_server.py when the SQLite file is not
        # readable (e.g. AUTH_DB_BACKEND=postgres). Blocked at the public
        # proxy via serve_homepage.py's blocklist.
        if method == "GET" and path == "/auth/users-for-snapshot":
            return self.h_users_for_snapshot
        # 2FA / TOTP
        if method == "POST" and path == "/auth/2fa/setup":
            return self.h_totp_setup
        if method == "POST" and path == "/auth/2fa/verify-setup":
            return self.h_totp_verify_setup
        if method == "POST" and path == "/auth/2fa/disable":
            return self.h_totp_disable
        if method == "POST" and path == "/auth/2fa/verify":
            return self.h_totp_verify  # public — used at login
        if method == "GET" and path == "/auth/2fa/status":
            return self.h_totp_status
        if method == "POST" and path == "/auth/2fa/verify-withdraw":
            return self.h_totp_verify_withdraw  # loopback-only
        # WebAuthn / Passkeys
        if method == "POST" and path == "/auth/webauthn/register/begin":
            return self.h_webauthn_register_begin
        if method == "POST" and path == "/auth/webauthn/register/finish":
            return self.h_webauthn_register_finish
        if method == "POST" and path == "/auth/webauthn/authenticate/begin":
            return self.h_webauthn_authn_begin
        if method == "POST" and path == "/auth/webauthn/authenticate/finish":
            return self.h_webauthn_authn_finish
        if method == "GET" and path == "/auth/webauthn/credentials":
            return self.h_webauthn_list_credentials
        if method == "GET" and path == "/auth/webauthn/status":
            return self.h_webauthn_status
        if method == "DELETE" and path.startswith("/auth/webauthn/credentials/"):
            return self.h_webauthn_delete_credential
        if method == "PATCH" and path.startswith("/auth/webauthn/credentials/"):
            return self.h_webauthn_rename_credential
        # Geo-blocking
        if method == "GET" and path == "/auth/geo/whoami":
            return self.h_geo_whoami
        if method == "GET" and path == "/auth/geo/blocklist":
            return self.h_geo_blocklist
        if method == "GET" and path == "/auth/geo/check":
            return self.h_geo_check
        if method == "GET" and path == "/auth/geo/log":
            return self.h_geo_log
        # KYC
        if method == "POST" and path == "/kyc/start":
            return self.h_kyc_start
        if method == "POST" and path == "/kyc/verify":
            return self.h_kyc_verify
        if method == "GET" and path == "/kyc/status":
            return self.h_kyc_status
        if method == "POST" and path == "/kyc/cancel":
            return self.h_kyc_cancel
        # Sumsub WebSDK (real-document KYC). Active when the env vars
        # SUMSUB_APP_TOKEN + SUMSUB_APP_SECRET are set. Otherwise these
        # endpoints respond 503 with a pointer to the PASS demo flow.
        if method == "POST" and path == "/kyc/sumsub/start":
            return self.h_sumsub_start
        if method == "POST" and path == "/kyc/sumsub/refresh-token":
            return self.h_sumsub_refresh_token
        if method == "GET" and path == "/kyc/sumsub/status":
            return self.h_sumsub_status
        if method == "POST" and path == "/kyc/sumsub/webhook":
            return self.h_sumsub_webhook
        if method == "GET" and path == "/kyc/sumsub/config":
            return self.h_sumsub_config
        return None

    def do_GET(self):  # noqa: N802
        self._dispatch()

    def do_POST(self):  # noqa: N802
        self._dispatch()

    def do_DELETE(self):  # noqa: N802
        self._dispatch()

    def do_PUT(self):  # noqa: N802
        self._dispatch()

    def do_PATCH(self):  # noqa: N802
        self._dispatch()

    def _dispatch(self):
        with _otel_server_span(self) as _span:
            handler = self._route()
            if handler is None:
                _span.set_attribute("http.status_code", 404)
                self._send_json(404, {"error": "not_found", "path": self.path})
                return
            try:
                handler()
                _span.set_attribute("http.status_code", getattr(self, "_response_code", 200))
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"[auth] handler error on {self.path}: {e!r}\n")
                _span.set_attribute("http.status_code", 500)
                self._send_json(500, {"error": "server_error"})

    # ---- /auth/signup ----------------------------------------------------
    def h_signup(self):
        # Geo-gate FIRST -- a blocked region should never even have its
        # credentials validated, since we'd otherwise leak email-format
        # error messages back to a jurisdiction we don't serve. 451 is the
        # RFC 7725 status for "unavailable for legal reasons".
        if self._geo_enforce(endpoint="/auth/signup"):
            return
        body = self._read_json()
        err, email, password, name = validate_signup(body)
        if err == "invalid_email":
            return self._send_json(400, {"error": "invalid_email"})
        if err == "invalid_password":
            return self._send_json(
                400,
                {
                    "error": "invalid_password",
                    "message": "Password must be at least 8 chars with letters and digits",
                },
            )
        if err == "invalid_name":
            return self._send_json(400, {"error": "invalid_name"})

        salt = secrets.token_bytes(16)
        pw = hash_pw(password, salt)

        try:
            user = AUTH_DB.insert_user(
                email=email,
                name=name,
                pw_hash=pw,
                pw_salt=salt,
            )
        except _auth_db.EmailTaken:
            return self._send_json(409, {"error": "email_taken"})

        uid = user["id"]
        opex = user["opex_user"]
        token, _ = issue_session(uid)

        # Seed demo funds (best-effort, async; do NOT fail the signup).
        threading.Thread(target=seed_demo_funds, args=(opex,), daemon=True).start()

        # Referral-code attribution (best-effort; never blocks signup).
        ref_applied = False
        referral_code = (body.get("referral_code") or "").strip().upper()
        if referral_code:
            ref_applied = _apply_referral_loopback(referral_code, opex, self._client_ip())

        return self._send_json(
            201,
            {
                "token": token,
                "user": {
                    "id": uid,
                    "email": email,
                    "name": name,
                    "opex_user": opex,
                    "kyc_status": "none",
                },
                "referral_code_applied": ref_applied,
            },
        )

    # ---- /auth/login -----------------------------------------------------
    def h_login(self):
        # Geo-gate every login attempt -- even an existing customer can't
        # log in from a blocked region. Current IP wins over the IP they
        # originally signed up from.
        if self._geo_enforce(endpoint="/auth/login"):
            return
        body = self._read_json()
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""

        # Optional passkey-redirect: if the client signals it would rather
        # use WebAuthn (no password provided, or ?force_webauthn=1) AND the
        # account has any enrolled credential, we steer them at the
        # /auth/webauthn/authenticate/* flow instead of asking for a password.
        qs = urllib.parse.urlsplit(self.path).query
        force_webauthn = "force_webauthn" in urllib.parse.parse_qs(qs)
        # Peek the account by email so we can detect enrollment without a
        # password (this matches how the TOTP step-up works today).
        if email:
            peek = AUTH_DB.get_user_by_email(email)
            if peek:
                has_passkey = AUTH_DB.count_webauthn_credentials(int(peek["id"])) > 0
                if has_passkey and (not password or force_webauthn):
                    return self._send_json(
                        200,
                        {
                            "step": "webauthn_required",
                            "email": email,
                        },
                    )

        if not email or not password:
            return self._send_json(401, {"error": "invalid_credentials"})

        row = AUTH_DB.get_user_by_email(email)
        if not row:
            return self._send_json(401, {"error": "invalid_credentials"})
        if not verify_pw(password, row["pw_salt"], row["pw_hash"]):
            return self._send_json(401, {"error": "invalid_credentials"})

        # 2FA gate. If the account has enrolled, a password alone is no
        # longer enough -- tell the client to render the TOTP screen and
        # POST /auth/2fa/verify with the code. We use HTTP 200 (not 401)
        # so the client can distinguish "wrong password" from "good
        # password, give me your code".
        if int(row.get("totp_enabled") or 0) == 1:
            code = (body.get("code") or "").strip()
            recovery_code = (body.get("recovery_code") or "").strip()
            if not code and not recovery_code:
                return self._send_json(
                    200,
                    {
                        "step": "totp_required",
                        "email": email,
                    },
                )
            ok, _err = self._verify_2fa_for_user(row, code=code, recovery_code=recovery_code)
            if not ok:
                return self._send_json(401, {"error": "totp_wrong"})
        token, _ = issue_session(row["id"])
        return self._send_json(200, {"token": token, "user": user_dict(row)})

    # ---- /auth/me --------------------------------------------------------
    def h_me(self):
        row = self._require_user()
        if not row:
            return
        return self._send_json(200, {"user": user_dict(row)})

    # ---- /auth/logout ----------------------------------------------------
    def h_logout(self):
        token = self._bearer()
        if not token:
            return self._send_json(204, None)
        AUTH_DB.delete_session(token)
        return self._send_json(204, None)

    # ---- /auth/session/peek ---------------------------------------------
    def h_peek(self):
        row = self._require_user()
        if not row:
            return
        return self._send_json(200, {"valid": True, "opex_user": row["opex_user"]})

    # ---- /auth/health ----------------------------------------------------
    # Public, no auth. Used by the operator dashboard / smoke tests to confirm
    # which backend is wired in. Reports row counts as a cheap liveness signal.
    def h_health(self):
        try:
            h = AUTH_DB.health()
            return self._send_json(
                200,
                {
                    "ok": True,
                    "backend": h["backend"],
                    "db_latency_ms": h["latency_ms"],
                    "version": SERVER_VERSION,
                    "n_users": h["n_users"],
                    "n_active_sessions": h["n_active_sessions"],
                },
            )
        except Exception as e:  # noqa: BLE001
            return self._send_json(
                503,
                {
                    "ok": False,
                    "backend": getattr(AUTH_DB, "backend", "unknown"),
                    "error": str(e),
                    "version": SERVER_VERSION,
                },
            )

    # =====================================================================
    # 2FA / TOTP (RFC 6238)
    # =====================================================================
    def _verify_2fa_for_user(
        self, row: dict, *, code: str = "", recovery_code: str = ""
    ) -> tuple[bool, str]:
        """Verify TOTP or recovery code. Handles rate-limit + replay.

        ``row`` must be a fresh user dict (called *after* password verify
        in login, or with the bearer-resolved user in withdraw).
        Returns ``(ok, error_code)``. On success, increments counter,
        on failure increments the attempts log.
        """
        uid = int(row["id"])
        now = int(time.time())
        # Lockout check
        locked_until = row.get("totp_locked_until") or 0
        if locked_until and locked_until > now:
            AUTH_DB.insert_totp_attempt(
                user_id=uid,
                ts=now,
                result="locked",
                ip_redacted=self._redacted_ip(),
            )
            return False, "locked"

        # Rolling failure window check (5 wrong in 15 min -> 30 min lock).
        fails = AUTH_DB.count_recent_failed_totp_attempts(
            uid,
            now - TOTP_FAIL_WINDOW_SEC,
        )
        if fails >= TOTP_FAIL_THRESHOLD:
            AUTH_DB.set_user_totp_locked_until(uid, now + TOTP_LOCK_SECONDS)
            AUTH_DB.insert_totp_attempt(
                user_id=uid,
                ts=now,
                result="locked",
                ip_redacted=self._redacted_ip(),
            )
            return False, "locked"

        if recovery_code:
            rc = (recovery_code or "").upper().replace("-", "").replace(" ", "")
            for entry in AUTH_DB.list_recovery_codes(uid):
                if entry["used_at"]:
                    continue
                if verify_recovery_code(rc, entry["code_salt"], entry["code_hash"]):
                    AUTH_DB.mark_recovery_code_used(entry["id"], now)
                    AUTH_DB.insert_totp_attempt(
                        user_id=uid,
                        ts=now,
                        result="success",
                        ip_redacted=self._redacted_ip(),
                    )
                    AUTH_DB.set_user_totp_locked_until(uid, None)
                    return True, ""
            AUTH_DB.insert_totp_attempt(
                user_id=uid,
                ts=now,
                result="wrong",
                ip_redacted=self._redacted_ip(),
            )
            return False, "wrong"

        secret = row.get("totp_secret_b32") or ""
        if not secret:
            AUTH_DB.insert_totp_attempt(
                user_id=uid,
                ts=now,
                result="wrong",
                ip_redacted=self._redacted_ip(),
            )
            return False, "no_secret"

        ok, counter = verify_totp(secret, code or "")
        if not ok:
            AUTH_DB.insert_totp_attempt(
                user_id=uid,
                ts=now,
                result="wrong",
                ip_redacted=self._redacted_ip(),
            )
            return False, "wrong"

        # Replay protection: each counter is single-use.
        last_counter = row.get("totp_last_counter")
        if last_counter is not None and counter is not None and int(counter) <= int(last_counter):
            AUTH_DB.insert_totp_attempt(
                user_id=uid,
                ts=now,
                result="replay",
                ip_redacted=self._redacted_ip(),
            )
            return False, "replay"

        AUTH_DB.set_user_totp_last_counter(uid, int(counter or 0))
        AUTH_DB.set_user_totp_locked_until(uid, None)
        AUTH_DB.insert_totp_attempt(
            user_id=uid,
            ts=now,
            result="success",
            ip_redacted=self._redacted_ip(),
        )
        return True, ""

    def _redacted_ip(self) -> str:
        try:
            return geo_provider.redact_ip(self._client_ip())
        except Exception:
            return ""

    # ---- POST /auth/2fa/setup -------------------------------------------
    def h_totp_setup(self):
        row = self._require_user()
        if not row:
            return
        uid = int(row["id"])
        # Re-fetch the user to get the latest totp_enabled status.
        cur = AUTH_DB.get_user_by_id(uid)
        if cur and int(cur.get("totp_enabled") or 0) == 1:
            return self._send_json(409, {"error": "already_enabled"})

        secret = gen_totp_secret()
        AUTH_DB.set_user_totp_secret(uid, secret)
        recovery = gen_recovery_codes()
        pairs: list[tuple[bytes, bytes]] = []
        for rc in recovery:
            salt, derived = hash_recovery_code(rc)
            pairs.append((derived, salt))
        AUTH_DB.insert_recovery_codes(uid, pairs)

        otpauth = build_otpauth_url(row["email"], secret)
        qr_svg = render_qr_svg(otpauth)
        return self._send_json(
            200,
            {
                "secret_b32": secret,
                "otpauth_url": otpauth,
                "qr_svg": qr_svg,
                "qr_svg_data_url": f"data:image/svg+xml;base64,{qr_svg}",
                "recovery_codes": recovery,
                "digits": TOTP_DIGITS,
                "period": TOTP_PERIOD,
                "issuer": "zkCEX",
            },
        )

    # ---- POST /auth/2fa/verify-setup ------------------------------------
    def h_totp_verify_setup(self):
        row = self._require_user()
        if not row:
            return
        uid = int(row["id"])
        body = self._read_json()
        code = (body.get("code") or "").strip()
        cur = AUTH_DB.get_user_by_id(uid)
        if not cur:
            return self._send_json(404, {"error": "user_not_found"})
        if int(cur.get("totp_enabled") or 0) == 1:
            return self._send_json(409, {"error": "already_enabled"})
        secret = cur.get("totp_secret_b32") or ""
        if not secret:
            return self._send_json(400, {"error": "no_pending_setup"})
        ok, counter = verify_totp(secret, code)
        if not ok:
            attempts = AUTH_DB.bump_totp_setup_attempts(uid)
            if attempts >= TOTP_SETUP_MAX_ATTEMPTS:
                AUTH_DB.wipe_user_totp_secret(uid)
                return self._send_json(
                    400,
                    {
                        "error": "too_many_setup_attempts",
                        "message": "Setup wiped. Start over from /auth/2fa/setup.",
                    },
                )
            return self._send_json(
                400,
                {
                    "error": "wrong_code",
                    "attempts_left": TOTP_SETUP_MAX_ATTEMPTS - attempts,
                },
            )
        now = int(time.time())
        AUTH_DB.enable_user_totp(uid, now)
        AUTH_DB.set_user_totp_last_counter(uid, int(counter or 0))
        _notif_send(
            cur.get("opex_user"),
            "security",
            "critical",
            "2FA enabled / 2단계 인증 활성화",
            "Two-factor authentication is now active on your account.",
            {"event": "2fa_enabled"},
        )
        remaining = AUTH_DB.count_unused_recovery_codes(uid)
        return self._send_json(
            200,
            {
                "enabled": True,
                "enabled_at": now,
                "recovery_codes_remaining": remaining,
            },
        )

    # ---- POST /auth/2fa/disable -----------------------------------------
    def h_totp_disable(self):
        row = self._require_user()
        if not row:
            return
        uid = int(row["id"])
        body = self._read_json()
        password = body.get("password") or ""
        code = (body.get("code") or "").strip()
        recovery_code = (body.get("recovery_code") or "").strip()
        cur = AUTH_DB.get_user_by_id(uid)
        if not cur:
            return self._send_json(404, {"error": "user_not_found"})
        if int(cur.get("totp_enabled") or 0) != 1:
            return self._send_json(400, {"error": "not_enabled"})
        if not verify_pw(password, cur["pw_salt"], cur["pw_hash"]):
            return self._send_json(401, {"error": "invalid_password"})
        ok, _err = self._verify_2fa_for_user(
            cur,
            code=code,
            recovery_code=recovery_code,
        )
        if not ok:
            return self._send_json(401, {"error": "totp_wrong"})
        AUTH_DB.disable_user_totp(uid)
        _notif_send(
            cur.get("opex_user"),
            "security",
            "critical",
            "2FA disabled / 2단계 인증 해제됨",
            "Two-factor authentication has been turned off on your account.",
            {"event": "2fa_disabled"},
        )
        return self._send_json(200, {"enabled": False})

    # ---- POST /auth/2fa/verify ------------------------------------------
    # Public: used when password+TOTP are submitted together at login.
    def h_totp_verify(self):
        if self._geo_enforce(endpoint="/auth/2fa/verify"):
            return
        body = self._read_json()
        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        code = (body.get("code") or "").strip()
        recovery_code = (body.get("recovery_code") or "").strip()
        if not email or not password or (not code and not recovery_code):
            return self._send_json(400, {"error": "bad_request"})
        row = AUTH_DB.get_user_by_email(email)
        if not row or not verify_pw(password, row["pw_salt"], row["pw_hash"]):
            return self._send_json(401, {"error": "invalid_credentials"})
        if int(row.get("totp_enabled") or 0) != 1:
            # The account has no 2FA -- just log them in (matches /auth/login
            # behavior; harmless if the client guessed wrong about which
            # endpoint to call).
            token, _ = issue_session(row["id"])
            return self._send_json(200, {"token": token, "user": user_dict(row)})
        ok, err = self._verify_2fa_for_user(row, code=code, recovery_code=recovery_code)
        if not ok:
            status = 423 if err == "locked" else 401
            return self._send_json(status, {"error": "totp_" + (err or "wrong")})
        token, _ = issue_session(row["id"])
        fresh = AUTH_DB.get_user_by_id(row["id"]) or row
        return self._send_json(200, {"token": token, "user": user_dict(fresh)})

    # ---- POST /auth/2fa/verify-withdraw ---------------------------------
    # Internal: chain_server.py calls this from loopback. Body:
    # {opex_user, code | recovery_code}. We re-fetch the user via opex_user,
    # verify the code, return {ok: bool}.
    def h_totp_verify_withdraw(self):
        peer_ip = ""
        try:
            peer_ip = self.client_address[0] if self.client_address else ""
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[auth] failed to read client address: {e!r}\n")
        if not _is_loopback_addr(peer_ip):
            return self._send_json(403, {"error": "loopback_only"})
        body = self._read_json()
        opex = (body.get("opex_user") or "").strip()
        code = (body.get("code") or body.get("totp_code") or "").strip()
        recovery_code = (body.get("recovery_code") or "").strip()
        if not opex:
            return self._send_json(400, {"error": "bad_request"})
        row = AUTH_DB.get_user_by_opex(opex)
        if not row:
            return self._send_json(404, {"error": "user_not_found"})
        if int(row.get("totp_enabled") or 0) != 1:
            # 2FA not enrolled: treat as a pass-through so chain_server can
            # short-circuit. The flag REQUIRE_2FA_WITHDRAW on chain_server
            # decides whether to actually require this; we just answer.
            return self._send_json(200, {"ok": True, "enrolled": False})
        if not code and not recovery_code:
            # Enrolled but no code -- tell chain_server to prompt.
            return self._send_json(
                401,
                {
                    "ok": False,
                    "enrolled": True,
                    "error": "totp_required",
                },
            )
        ok, err = self._verify_2fa_for_user(row, code=code, recovery_code=recovery_code)
        return self._send_json(
            200 if ok else 401,
            {
                "ok": bool(ok),
                "enrolled": True,
                "error": None if ok else ("totp_" + (err or "wrong")),
            },
        )

    # ---- GET /auth/2fa/status -------------------------------------------
    def h_totp_status(self):
        row = self._require_user()
        if not row:
            return
        cur = AUTH_DB.get_user_by_id(int(row["id"])) or row
        enabled = int(cur.get("totp_enabled") or 0) == 1
        return self._send_json(
            200,
            {
                "enabled": enabled,
                "enabled_at": cur.get("totp_enabled_at"),
                "recovery_codes_remaining": AUTH_DB.count_unused_recovery_codes(int(cur["id"]))
                if enabled
                else 0,
                "pending_setup": (not enabled) and bool(cur.get("totp_secret_b32")),
            },
        )

    # =====================================================================
    # WebAuthn / Passkeys (W3C WebAuthn L2)
    # =====================================================================
    # The browser drives both flows via navigator.credentials.{create,get}.
    # We act as the Relying Party: issue + verify the challenge, store the
    # COSE public key, and detect clones via the sign_count monotone check.

    def _webauthn_unavailable(self):
        return self._send_json(
            501,
            {
                "error": "webauthn_crypto_unavailable",
                "message": (
                    "Server is missing the 'cryptography' library required "
                    "to verify WebAuthn signatures. Install with: "
                    "pip3 install --user cryptography"
                ),
            },
        )

    # ---- POST /auth/webauthn/register/begin -----------------------------
    def h_webauthn_register_begin(self):
        row = self._require_user()
        if not row:
            return
        uid = int(row["id"])
        # Best-effort GC; never blocks the request.
        try:
            AUTH_DB.gc_webauthn_challenges()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[auth] webauthn challenge GC failed: {e!r}\n")
        challenge = secrets.token_bytes(WEBAUTHN_CHALLENGE_BYTES)
        challenge_b64 = _b64url_encode(challenge)
        now = int(time.time())
        AUTH_DB.insert_webauthn_challenge(
            challenge_b64=challenge_b64,
            purpose="register",
            user_id=uid,
            email=row["email"],
            expires_at=now + WEBAUTHN_CHALLENGE_TTL,
        )
        # The "user.id" we hand to the authenticator is the opex_user string
        # bytes. Per W3C this is opaque to the platform; we just need
        # something that uniquely identifies the user account on this RP.
        opex = row["opex_user"] or f"u-{uid}"
        user_handle = _b64url_encode(opex.encode("utf-8"))

        exclude = []
        try:
            for c in AUTH_DB.list_webauthn_credentials(uid):
                transports = (c.get("transports") or "").split(",")
                transports = [t for t in (t.strip() for t in transports) if t]
                exclude.append(
                    {
                        "type": "public-key",
                        "id": c["credential_id"],
                        "transports": transports or ["internal", "usb"],
                    }
                )
        except Exception:  # noqa: BLE001
            exclude = []

        return self._send_json(
            200,
            {
                "challenge": challenge_b64,
                "rp": {"id": WEBAUTHN_RP_ID, "name": WEBAUTHN_RP_NAME},
                "user": {
                    "id": user_handle,
                    "name": row["email"],
                    "displayName": row.get("name") or row["email"],
                },
                "pubKeyCredParams": [
                    {"type": "public-key", "alg": -7},  # ES256 (P-256)
                    {"type": "public-key", "alg": -257},  # RS256
                ],
                "timeout": 60000,
                "attestation": "none",
                "authenticatorSelection": {
                    "userVerification": "preferred",
                    "residentKey": "preferred",
                },
                "excludeCredentials": exclude,
                "extensions": {"credProps": True},
                # Bookkeeping echoes for the client; not part of the spec.
                "expires_at": now + WEBAUTHN_CHALLENGE_TTL,
                "crypto_available": _HAS_CRYPTOGRAPHY,
            },
        )

    # ---- POST /auth/webauthn/register/finish ----------------------------
    def h_webauthn_register_finish(self):
        row = self._require_user()
        if not row:
            return
        uid = int(row["id"])
        body = self._read_json()
        # The browser sends a PublicKeyCredential. The interesting bits are
        # response.attestationObject (CBOR), response.clientDataJSON (UTF-8
        # JSON), plus the credential id we stash.
        resp = (body.get("response") or {}) if isinstance(body, dict) else {}
        cred_id_b64 = (body.get("id") or "").strip()
        att_obj_b64 = (resp.get("attestationObject") or "").strip()
        client_data_b64 = (resp.get("clientDataJSON") or "").strip()
        transports = resp.get("transports") or []
        if not (cred_id_b64 and att_obj_b64 and client_data_b64):
            return self._send_json(400, {"error": "missing_fields"})

        try:
            att_obj_bytes = _b64url_decode(att_obj_b64)
            client_data_bytes = _b64url_decode(client_data_b64)
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "invalid_base64"})

        # 1) clientDataJSON checks: type, challenge, origin.
        try:
            client_data = json.loads(client_data_bytes.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "invalid_client_data_json"})
        if client_data.get("type") != "webauthn.create":
            return self._send_json(400, {"error": "wrong_type"})
        challenge_b64 = client_data.get("challenge") or ""
        # Standard b64url, no padding, per spec.
        ch = AUTH_DB.take_webauthn_challenge(challenge_b64=challenge_b64, purpose="register")
        if not ch:
            return self._send_json(400, {"error": "challenge_expired_or_unknown"})
        if ch.get("user_id") != uid:
            return self._send_json(403, {"error": "challenge_user_mismatch"})
        if (client_data.get("origin") or "") != WEBAUTHN_ORIGIN:
            return self._send_json(400, {"error": "origin_mismatch"})

        # 2) attestationObject -> authData -> COSE key.
        try:
            att = _parse_attestation_object(att_obj_bytes)
            ad = _parse_auth_data(att["authData"], expect_attested=True)
        except Exception as e:  # noqa: BLE001
            return self._send_json(400, {"error": "invalid_auth_data", "message": str(e)})

        if ad["rp_id_hash"] != _rp_id_hash(WEBAUTHN_RP_ID):
            return self._send_json(400, {"error": "rp_id_hash_mismatch"})
        if not ad["user_present"]:
            return self._send_json(400, {"error": "user_not_present"})

        cred_id_from_attestation = _b64url_encode(ad["credential_id"])
        # The id in the outer credential and the inner authData must match.
        if cred_id_from_attestation != cred_id_b64:
            return self._send_json(400, {"error": "credential_id_mismatch"})

        # No duplicates across users; UNIQUE constraint protects us anyway,
        # but a clean 409 is friendlier than a 500.
        existing = AUTH_DB.get_webauthn_credential_by_credid(cred_id_b64)
        if existing:
            return self._send_json(409, {"error": "credential_already_registered"})

        cose_b64 = _b64url_encode(ad["cose_public_key_bytes"])
        aaguid_hex = _aaguid_hex(ad["aaguid"])
        device_name = (body.get("device_name") or "").strip() or None
        if not device_name:
            friendly = _aaguid_friendly_name(aaguid_hex)
            device_name = friendly or "Passkey"

        transports_csv = None
        if isinstance(transports, list) and transports:
            transports_csv = ",".join(str(t).strip() for t in transports if isinstance(t, str))

        cred = AUTH_DB.insert_webauthn_credential(
            user_id=uid,
            credential_id=cred_id_b64,
            public_key_cose_b64=cose_b64,
            sign_count=int(ad["sign_count"] or 0),
            attestation_type=att.get("fmt") or "none",
            aaguid=aaguid_hex,
            transports=transports_csv,
            device_name=device_name,
        )
        return self._send_json(
            201,
            {
                "ok": True,
                "credential": {
                    "id": cred["id"],
                    "credential_id": cred_id_b64,
                    "device_name": device_name,
                    "aaguid": aaguid_hex,
                    "aaguid_name": _aaguid_friendly_name(aaguid_hex),
                    "transports": transports_csv,
                    "created_at": cred["created_at"],
                    "sign_count": cred["sign_count"],
                },
            },
        )

    # ---- POST /auth/webauthn/authenticate/begin -------------------------
    # Public: matches /auth/login -- a passkey IS the sign-in factor here.
    def h_webauthn_authn_begin(self):
        if self._geo_enforce(endpoint="/auth/webauthn/authenticate/begin"):
            return
        try:
            AUTH_DB.gc_webauthn_challenges()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[auth] webauthn challenge GC failed: {e!r}\n")
        body = self._read_json()
        email = (body.get("email") or "").strip().lower()
        challenge = secrets.token_bytes(WEBAUTHN_CHALLENGE_BYTES)
        challenge_b64 = _b64url_encode(challenge)
        now = int(time.time())

        uid_for_challenge = None
        allow_credentials = []
        # If the client gave us an email, scope the allowList to that user's
        # registered credentials. Otherwise leave it empty (the browser will
        # then offer all available passkeys via residentKey/discoverable).
        if email:
            urow = AUTH_DB.get_user_by_email(email)
            if urow:
                uid_for_challenge = int(urow["id"])
                creds = AUTH_DB.list_webauthn_credentials(uid_for_challenge)
                for c in creds:
                    transports = (c.get("transports") or "").split(",")
                    transports = [t.strip() for t in transports if t and t.strip()]
                    allow_credentials.append(
                        {
                            "type": "public-key",
                            "id": c["credential_id"],
                            "transports": transports or ["internal", "usb"],
                        }
                    )

        AUTH_DB.insert_webauthn_challenge(
            challenge_b64=challenge_b64,
            purpose="authenticate",
            user_id=uid_for_challenge,
            email=email or None,
            expires_at=now + WEBAUTHN_CHALLENGE_TTL,
        )

        return self._send_json(
            200,
            {
                "challenge": challenge_b64,
                "timeout": 60000,
                "rpId": WEBAUTHN_RP_ID,
                "userVerification": "preferred",
                "allowCredentials": allow_credentials,
                "expires_at": now + WEBAUTHN_CHALLENGE_TTL,
                "crypto_available": _HAS_CRYPTOGRAPHY,
            },
        )

    # ---- POST /auth/webauthn/authenticate/finish ------------------------
    # Public: this IS the authentication step. On success we mint a session.
    def h_webauthn_authn_finish(self):
        if self._geo_enforce(endpoint="/auth/webauthn/authenticate/finish"):
            return
        if not _HAS_CRYPTOGRAPHY:
            # Issue 501 here -- without the signature verify we cannot
            # confirm the assertion is genuine, so we must not issue a
            # session. The rest of the flow (challenge issuance, lookup) is
            # exercised by /authenticate/begin so this stays useful as a
            # diagnostic.
            return self._webauthn_unavailable()

        body = self._read_json()
        cred_id_b64 = (body.get("id") or "").strip()
        resp = (body.get("response") or {}) if isinstance(body, dict) else {}
        auth_data_b64 = (resp.get("authenticatorData") or "").strip()
        client_data_b64 = (resp.get("clientDataJSON") or "").strip()
        signature_b64 = (resp.get("signature") or "").strip()
        (resp.get("userHandle") or "").strip()
        if not (cred_id_b64 and auth_data_b64 and client_data_b64 and signature_b64):
            return self._send_json(400, {"error": "missing_fields"})

        try:
            auth_data_bytes = _b64url_decode(auth_data_b64)
            client_data_bytes = _b64url_decode(client_data_b64)
            signature_bytes = _b64url_decode(signature_b64)
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "invalid_base64"})

        # 1) clientDataJSON: type, origin, challenge.
        try:
            client_data = json.loads(client_data_bytes.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return self._send_json(400, {"error": "invalid_client_data_json"})
        if client_data.get("type") != "webauthn.get":
            return self._send_json(400, {"error": "wrong_type"})
        if (client_data.get("origin") or "") != WEBAUTHN_ORIGIN:
            return self._send_json(400, {"error": "origin_mismatch"})
        challenge_b64 = client_data.get("challenge") or ""
        ch = AUTH_DB.take_webauthn_challenge(challenge_b64=challenge_b64, purpose="authenticate")
        if not ch:
            return self._send_json(400, {"error": "challenge_expired_or_unknown"})

        # 2) Credential lookup.
        cred = AUTH_DB.get_webauthn_credential_by_credid(cred_id_b64)
        if not cred:
            return self._send_json(404, {"error": "credential_unknown"})
        # If the challenge was scoped to a user (email-bound flow), ensure
        # the credential belongs to that same user.
        if ch.get("user_id") and int(ch["user_id"]) != int(cred["user_id"]):
            return self._send_json(403, {"error": "credential_user_mismatch"})

        # 3) authData checks: rpIdHash, user_present, sign_count.
        try:
            ad = _parse_auth_data(auth_data_bytes, expect_attested=False)
        except Exception as e:  # noqa: BLE001
            return self._send_json(400, {"error": "invalid_auth_data", "message": str(e)})
        if ad["rp_id_hash"] != _rp_id_hash(WEBAUTHN_RP_ID):
            return self._send_json(400, {"error": "rp_id_hash_mismatch"})
        if not ad["user_present"]:
            return self._send_json(400, {"error": "user_not_present"})

        stored_count = int(cred.get("sign_count") or 0)
        new_count = int(ad["sign_count"] or 0)
        # Strict monotone check. Authenticators that always return 0 are
        # allowed (some platform authenticators do not implement a counter);
        # otherwise the new count MUST exceed the stored count.
        if not (new_count == 0 and stored_count == 0):
            if new_count <= stored_count:
                return self._send_json(
                    409,
                    {
                        "error": "sign_count_not_increasing",
                        "message": "possible credential clone",
                    },
                )

        # 4) Signature verify over (authData || SHA256(clientDataJSON)).
        try:
            cose_bytes = _b64url_decode(cred["public_key_cose_b64"])
            cose_key, _ = _cbor_decode(cose_bytes)
            verifier = _cose_load_verifier(cose_key)
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[webauthn] cose load failed: {e!r}\n")
            return self._send_json(500, {"error": "key_decode_failed"})
        client_data_hash = hashlib.sha256(client_data_bytes).digest()
        message = auth_data_bytes + client_data_hash
        if not verifier(signature_bytes, message):
            return self._send_json(401, {"error": "signature_invalid"})

        # 5) All checks passed: persist new sign_count + last_used, mint a session.
        now = int(time.time())
        AUTH_DB.update_webauthn_sign_count(
            credential_id=cred_id_b64,
            sign_count=new_count,
            last_used_at=now,
        )
        uid = int(cred["user_id"])
        user = AUTH_DB.get_user_by_id(uid)
        if not user:
            return self._send_json(500, {"error": "user_vanished"})
        token, _ = issue_session(uid)
        return self._send_json(
            200,
            {
                "token": token,
                "user": user_dict(user),
                "credential": {
                    "id": cred["id"],
                    "device_name": cred.get("device_name"),
                },
            },
        )

    # ---- GET /auth/webauthn/credentials ---------------------------------
    def h_webauthn_list_credentials(self):
        row = self._require_user()
        if not row:
            return
        uid = int(row["id"])
        creds = AUTH_DB.list_webauthn_credentials(uid)
        out = []
        for c in creds:
            aaguid = c.get("aaguid") or ""
            out.append(
                {
                    "id": c["id"],
                    "credential_id": c["credential_id"],
                    "device_name": c.get("device_name") or "Passkey",
                    "aaguid": aaguid,
                    "aaguid_name": _aaguid_friendly_name(aaguid),
                    "transports": c.get("transports"),
                    "sign_count": int(c.get("sign_count") or 0),
                    "created_at": c.get("created_at"),
                    "last_used_at": c.get("last_used_at"),
                    "attestation_type": c.get("attestation_type"),
                }
            )
        return self._send_json(
            200,
            {
                "credentials": out,
                "count": len(out),
                "rp_id": WEBAUTHN_RP_ID,
                "origin": WEBAUTHN_ORIGIN,
                "crypto_available": _HAS_CRYPTOGRAPHY,
            },
        )

    # ---- GET /auth/webauthn/status --------------------------------------
    # Public — lets the signin page decide whether to show the passkey CTA
    # without forcing the user to type their email first. No PII returned;
    # just a global enabled flag and the env-derived RP id/origin.
    def h_webauthn_status(self):
        return self._send_json(
            200,
            {
                "rp_id": WEBAUTHN_RP_ID,
                "rp_name": WEBAUTHN_RP_NAME,
                "origin": WEBAUTHN_ORIGIN,
                "crypto_available": _HAS_CRYPTOGRAPHY,
            },
        )

    # ---- DELETE /auth/webauthn/credentials/<id> -------------------------
    def h_webauthn_delete_credential(self):
        row = self._require_user()
        if not row:
            return
        uid = int(row["id"])
        path = urllib.parse.urlsplit(self.path).path
        # /auth/webauthn/credentials/123
        try:
            cred_pk = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return self._send_json(400, {"error": "invalid_id"})
        n = AUTH_DB.delete_webauthn_credential(user_id=uid, cred_pk=cred_pk)
        if n == 0:
            return self._send_json(404, {"error": "not_found"})
        return self._send_json(200, {"ok": True, "deleted": cred_pk})

    # ---- PATCH /auth/webauthn/credentials/<id> --------------------------
    def h_webauthn_rename_credential(self):
        row = self._require_user()
        if not row:
            return
        uid = int(row["id"])
        path = urllib.parse.urlsplit(self.path).path
        try:
            cred_pk = int(path.rsplit("/", 1)[-1])
        except ValueError:
            return self._send_json(400, {"error": "invalid_id"})
        body = self._read_json()
        device_name = (body.get("device_name") or "").strip()
        if not device_name or len(device_name) > 64:
            return self._send_json(400, {"error": "invalid_device_name"})
        n = AUTH_DB.update_webauthn_device_name(
            user_id=uid, cred_pk=cred_pk, device_name=device_name
        )
        if n == 0:
            return self._send_json(404, {"error": "not_found"})
        return self._send_json(200, {"ok": True, "id": cred_pk, "device_name": device_name})

    # ---- /auth/users-for-snapshot ---------------------------------------
    # Internal endpoint: pol_server.py uses this in Postgres mode so it does
    # not need its own DB credentials. Blocked at the public proxy.
    def h_users_for_snapshot(self):
        users = AUTH_DB.list_opex_users()
        return self._send_json(200, {"opex_users": users})

    # =====================================================================
    # Geo-blocking endpoints
    # =====================================================================
    # ---- GET /auth/geo/whoami -------------------------------------------
    # Public. Lets the frontend render a region banner before the user even
    # taps Sign-in. Does NOT 451 -- callers WANT the verdict, not the gate.
    def h_geo_whoami(self):
        ip = self._client_ip()
        info = geo_provider.lookup_country(ip)
        country = info.country_iso2 if info else None
        country_name = info.country_name if info else None
        source = info.source if info else None
        if geo_provider.is_enforcement_enabled():
            blocked, reason = geo_provider.is_blocked(country)
        else:
            blocked, reason = (False, None)
        return self._send_json(
            200,
            {
                "ip": ip,
                "country": country,
                "country_name": country_name,
                "source": source,
                "blocked": blocked,
                "reason": reason,
                "enforcement_enabled": geo_provider.is_enforcement_enabled(),
            },
        )

    # ---- GET /auth/geo/blocklist ----------------------------------------
    # Public. Used to render the "Service not available in: ..." disclaimer.
    def h_geo_blocklist(self):
        countries = geo_provider.blocklist_with_names()
        return self._send_json(
            200,
            {
                "countries": countries,
                "enforcement_enabled": geo_provider.is_enforcement_enabled(),
                "updated_at": int(time.time()),
            },
        )

    # ---- GET /auth/geo/check --------------------------------------------
    # Internal-only. chain_server.py forwards the original client IP via
    # X-Forwarded-For (and only chain_server is on the trusted-proxy list
    # when it lives on 127.0.0.0/8) so this returns the verdict for the
    # original client, not for chain_server itself.
    def h_geo_check(self):
        ip = self._client_ip()
        info = geo_provider.lookup_country(ip)
        country = info.country_iso2 if info else None
        if geo_provider.is_enforcement_enabled():
            blocked, reason = geo_provider.is_blocked(country)
        else:
            blocked, reason = (False, None)
        return self._send_json(
            200,
            {
                "ip": ip,
                "country": country,
                "country_name": info.country_name if info else None,
                "source": info.source if info else None,
                "blocked": blocked,
                "reason": reason,
                "enforcement_enabled": geo_provider.is_enforcement_enabled(),
            },
        )

    # ---- GET /auth/geo/log ----------------------------------------------
    # Admin diagnostics. Either present the matching Bearer in
    # GEO_ADMIN_TOKEN, or call from loopback (the demo dev workflow).
    def h_geo_log(self):
        # Auth: either matching bearer or loopback-only.
        peer_ip = ""
        try:
            peer_ip = self.client_address[0] if self.client_address else ""
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"[auth] failed to read client address: {e!r}\n")
        bearer = self._bearer() or ""
        is_loopback = geo_provider.is_private_ip(peer_ip)
        ok = False
        if GEO_ADMIN_TOKEN and hmac.compare_digest(bearer, GEO_ADMIN_TOKEN):
            ok = True
        elif not GEO_ADMIN_TOKEN and is_loopback:
            ok = True
        if not ok:
            return self._send_json(401, {"error": "unauthorized"})
        # Parse limit.
        qs = urllib.parse.urlsplit(self.path).query
        params = urllib.parse.parse_qs(qs)
        try:
            limit = int((params.get("limit") or ["50"])[0])
        except ValueError:
            limit = 50
        rows = AUTH_DB.list_geo_decisions(limit=limit)
        return self._send_json(
            200,
            {
                "decisions": rows,
                "count": len(rows),
                "enforcement_enabled": geo_provider.is_enforcement_enabled(),
                "blocklist": geo_provider.configured_blocklist(),
            },
        )

    # ---- /kyc/start ------------------------------------------------------
    def h_kyc_start(self):
        body = self._read_json()
        err, fields = validate_kyc_start(body)
        if err:
            return self._send_json(400, {"error": err})

        # Generate the code BEFORE writing to DB so we can attempt the SMS
        # send first; if the provider is down, fail fast and don't leave a
        # half-started verification row behind.
        code = f"{secrets.randbelow(1_000_000):06d}"
        sms_result = sms_provider.send_code(fields["phone"], code)
        if not sms_result.get("ok"):
            sys.stderr.write(
                f"[KYC] sms_unavailable for phone {fields['phone']}: "
                f"{sms_result.get('error')}\n"
            )
            return self._send_json(
                502,
                {
                    "error": "sms_unavailable",
                    "reason": sms_result.get("error") or "provider_unreachable",
                },
            )

        row = self._require_user()
        if not row:
            return
        uid = row["id"]
        vid = secrets.token_hex(12)
        now = int(time.time())
        expires = now + KYC_TTL
        AUTH_DB.create_kyc_verification(
            id=vid,
            user_id=uid,
            code=code,
            expires_at=expires,
            carrier=fields["carrier"],
            name=fields["name"],
            rrn_front=fields["rrn_front"],
            rrn_back1=fields["rrn_back1"],
            phone=fields["phone"],
            sms_provider_message_id=sms_result.get("provider_message_id") or "",
        )
        AUTH_DB.set_user_kyc_status(uid, "pending", only_if_not_verified=True)
        # Log to stderr so devs can grab the code if they want.
        sys.stderr.write(f"[KYC] code for user {uid} phone {fields['phone']}: {code}\n")
        return self._send_json(
            200,
            {
                "verification_id": vid,
                "expires_at": expires,
                "masked_phone": mask_phone(fields["phone"]),
                "__demo": True,
                "__demo_code": code,
                "__demo_note": "demo build — code is logged to server stderr and echoed here for autofill",
            },
        )

    # ---- /kyc/verify -----------------------------------------------------
    def h_kyc_verify(self):
        body = self._read_json()
        vid = (body.get("verification_id") or "").strip()
        code = (body.get("code") or "").strip()
        if not vid or not re.match(r"^\d{6}$", code):
            return self._send_json(400, {"error": "invalid_input"})
        row = self._require_user()
        if not row:
            return
        uid = row["id"]
        v = AUTH_DB.get_kyc_verification(vid, user_id=uid)
        if not v:
            return self._send_json(400, {"error": "unknown_verification"})
        now = int(time.time())
        if v["expires_at"] < now:
            AUTH_DB.delete_kyc_verification(vid)
            return self._send_json(400, {"error": "expired"})
        if v["attempts"] >= MAX_KYC_ATTEMPTS:
            AUTH_DB.delete_kyc_verification(vid)
            return self._send_json(400, {"error": "too_many_attempts"})
        if not hmac.compare_digest(v["code"], code):
            attempts = v["attempts"] + 1
            if attempts >= MAX_KYC_ATTEMPTS:
                AUTH_DB.delete_kyc_verification(vid)
                return self._send_json(400, {"error": "too_many_attempts"})
            AUTH_DB.update_kyc_verification(vid, attempts=attempts)
            return self._send_json(
                400,
                {"error": "wrong_code", "attempts_left": MAX_KYC_ATTEMPTS - attempts},
            )

        # ---- Code matched. Now ask the KYC provider whether the
        # ---- (name, RRN, phone, carrier) combo is real. In demo this
        # ---- is a format check; in production it's NICE checkplus / KCB
        # ---- / KMC and can reject for mismatch even after a valid SMS
        # ---- code -- e.g. the phone is a burner not registered to the
        # ---- claimed name.
        kyc_result = kyc_provider.verify_identity(
            name=v["name"],
            rrn_front=v["rrn_front"],
            rrn_back1=v["rrn_back1"],
            phone=v["phone"],
            carrier=v["carrier"],
        )
        if not kyc_result.get("verified"):
            AUTH_DB.delete_kyc_verification(vid)
            return self._send_json(
                400,
                {
                    "error": "kyc_provider_rejected",
                    "reason": kyc_result.get("reason") or "rejected",
                },
            )

        # ---- Success: derive birth century / gender ------------------
        back1 = v["rrn_back1"]
        century = "19" if back1 in ("1", "2") else "20"
        gender = "M" if back1 in ("1", "3") else "F"
        kyc_birth = century + v["rrn_front"]  # YYYYMMDD
        verified_at = now
        AUTH_DB.update_user_kyc(
            uid,
            kyc_status="verified",
            kyc_verified_at=verified_at,
            kyc_name=v["name"],
            kyc_phone=v["phone"],
            kyc_birth=kyc_birth,
            kyc_gender=gender,
            kyc_carrier=v["carrier"],
            kyc_provider_request_id=kyc_result.get("provider_request_id") or "",
        )
        AUTH_DB.delete_kyc_verification(vid)
        # Push-notify the user that KYC succeeded. Best-effort.
        _push_notify(
            row["opex_user"],
            {
                "title": "본인인증 완료 / KYC verified",
                "body": "본인인증이 완료되어 출금이 가능합니다.",
                "tag": "kyc",
                "data": {"url": "/app/kyc.html"},
            },
        )
        _notif_send(
            row["opex_user"],
            "kyc_verified",
            "critical",
            "본인인증 완료 / KYC verified",
            "본인인증이 완료되어 출금이 가능합니다. / Your account is now fully verified.",
            {"verified_at": verified_at},
        )
        # Tell referral.py — if this user signed up with a code, fire kyc_bonus.
        _record_kyc_to_referral(row["opex_user"])
        return self._send_json(
            200,
            {
                "kyc_status": "verified",
                "verified_at": verified_at,
            },
        )

    # ---- /kyc/status -----------------------------------------------------
    def h_kyc_status(self):
        row = self._require_user()
        if not row:
            return
        return self._send_json(
            200,
            {
                "kyc_status": row["kyc_status"] or "none",
                "verified_at": row["kyc_verified_at"],
                "name": row["kyc_name"],
                "masked_phone": mask_phone(row["kyc_phone"]) if row["kyc_phone"] else None,
                "carrier": row["kyc_carrier"],
                "birth": row["kyc_birth"],
                "gender": row["kyc_gender"],
            },
        )

    # ---- /kyc/cancel -----------------------------------------------------
    def h_kyc_cancel(self):
        row = self._require_user()
        if not row:
            return
        AUTH_DB.delete_kyc_verifications_for_user(row["id"])
        AUTH_DB.set_user_kyc_status(row["id"], "none", only_if_not_verified=True)
        return self._send_json(204, None)

    # =====================================================================
    # Sumsub WebSDK ("real document KYC")
    # =====================================================================

    def _send_sumsub_unconfigured(self):
        return self._send_json(
            503,
            {
                "error": "sumsub_not_configured",
                "fallback": "/kyc/start (PASS demo)",
            },
        )

    # ---- GET /kyc/sumsub/config -----------------------------------------
    # No bearer required: the kyc.html page calls this to decide whether to
    # show the Sumsub tab. Returns just a boolean — no secrets.
    def h_sumsub_config(self):
        return self._send_json(
            200,
            {
                "configured": kyc_provider.is_sumsub_configured(),
                "level_name": os.environ.get("SUMSUB_LEVEL", "basic-kyc-level"),
            },
        )

    # ---- POST /kyc/sumsub/start -----------------------------------------
    def h_sumsub_start(self):
        if not kyc_provider.is_sumsub_configured():
            return self._send_sumsub_unconfigured()
        row = self._require_user()
        if not row:
            return
        uid = row["id"]
        opex = row["opex_user"]
        email = row["email"]

        try:
            applicant = (
                kyc_provider.sumsub_create_applicant(
                    external_user_id=opex,
                    email=email,
                    country_iso=None,
                )
                or {}
            )
            applicant_id = (applicant.get("id") or "").strip()
            if not applicant_id:
                # The /one fallback in sumsub_create_applicant returns the
                # full applicant; both shapes carry "id".
                return self._send_json(
                    502,
                    {
                        "error": "sumsub_applicant_missing_id",
                        "raw": applicant,
                    },
                )
            level = (applicant.get("review", {}) or {}).get("levelName") or os.environ.get(
                "SUMSUB_LEVEL", "basic-kyc-level"
            )

            access_token = kyc_provider.sumsub_access_token(
                external_user_id=opex,
                ttl_seconds=600,
            )
        except RuntimeError as e:
            msg = str(e)
            if msg == "sumsub_not_configured":
                return self._send_sumsub_unconfigured()
            sys.stderr.write(f"[sumsub] start failed for {opex}: {msg}\n")
            return self._send_json(
                502,
                {
                    "error": "sumsub_upstream_error",
                    "reason": msg,
                },
            )

        now = int(time.time())
        AUTH_DB.upsert_sumsub_applicant(
            external_user_id=opex,
            applicant_id=applicant_id,
            level_name=level,
            created_at=now,
            last_synced_at=now,
        )
        # Mark user as pending if not already verified or rejected.
        # We mirror the original "NOT IN ('verified','rejected')" guard via
        # the helper plus an explicit current-status check.
        cur_user = AUTH_DB.get_user_by_id(uid)
        if cur_user and (cur_user["kyc_status"] or "none") not in ("verified", "rejected"):
            AUTH_DB.set_user_kyc_status(uid, "pending")

        return self._send_json(
            200,
            {
                "access_token": access_token,
                "applicant_id": applicant_id,
                "level_name": level,
                "external_user_id": opex,
            },
        )

    # ---- POST /kyc/sumsub/refresh-token ---------------------------------
    def h_sumsub_refresh_token(self):
        # Same shape as start, but doesn't recreate the applicant row.
        if not kyc_provider.is_sumsub_configured():
            return self._send_sumsub_unconfigured()
        row = self._require_user()
        if not row:
            return
        opex = row["opex_user"]
        try:
            access_token = kyc_provider.sumsub_access_token(
                external_user_id=opex,
                ttl_seconds=600,
            )
        except RuntimeError as e:
            msg = str(e)
            if msg == "sumsub_not_configured":
                return self._send_sumsub_unconfigured()
            sys.stderr.write(f"[sumsub] refresh failed for {opex}: {msg}\n")
            return self._send_json(
                502,
                {
                    "error": "sumsub_upstream_error",
                    "reason": msg,
                },
            )
        return self._send_json(200, {"access_token": access_token})

    # ---- GET /kyc/sumsub/status -----------------------------------------
    def h_sumsub_status(self):
        if not kyc_provider.is_sumsub_configured():
            return self._send_sumsub_unconfigured()
        row = self._require_user()
        if not row:
            return
        uid = row["id"]
        opex = row["opex_user"]

        try:
            applicant = (
                kyc_provider.sumsub_get_applicant(
                    external_user_id=opex,
                )
                or {}
            )
        except RuntimeError as e:
            msg = str(e)
            if msg == "sumsub_not_configured":
                return self._send_sumsub_unconfigured()
            sys.stderr.write(f"[sumsub] status failed for {opex}: {msg}\n")
            return self._send_json(
                502,
                {
                    "error": "sumsub_upstream_error",
                    "reason": msg,
                },
            )

        if not applicant:
            return self._send_json(
                200,
                {
                    "review_status": "init",
                    "review_answer": None,
                    "last_updated": int(time.time() * 1000),
                },
            )

        review = applicant.get("review") or {}
        review_status = (review.get("reviewStatus") or "init").lower()
        review_result = review.get("reviewResult") or {}
        review_answer = review_result.get("reviewAnswer")

        applicant_id = applicant.get("id") or ""
        new_kyc = kyc_provider.sumsub_review_answer_to_kyc_status(review_answer)
        now_ms = int(time.time() * 1000)
        now_s = int(time.time())

        AUTH_DB.update_sumsub_applicant_status(
            external_user_id=opex,
            last_status=review_status,
            last_review_answer=(review_answer or ""),
            last_synced_at=now_s,
        )
        # Reflect into users.kyc_status. Don't overwrite an existing
        # 'verified' from the PASS flow with anything weaker.
        self._apply_sumsub_status(uid, applicant_id, new_kyc, now_s)

        return self._send_json(
            200,
            {
                "review_status": review_status,
                "review_answer": review_answer,
                "last_updated": now_ms,
                "applicant_id": applicant_id,
            },
        )

    @staticmethod
    def _apply_sumsub_status(uid: int, applicant_id: str, new_kyc: str, now_s: int) -> None:
        """Apply a Sumsub-derived kyc_status to ``users``.

        Conservative: never demote a verified user. Always record the
        applicant_id in ``kyc_provider_request_id`` for audit.
        """
        u = AUTH_DB.get_user_by_id(uid)
        if not u:
            return
        current = (u["kyc_status"] or "none").lower()
        if current == "verified" and new_kyc != "verified":
            # Only update the audit pointer; never demote.
            AUTH_DB.update_user_kyc(
                uid,
                kyc_provider_request_id=applicant_id,
            )
            return
        if new_kyc == "verified":
            AUTH_DB.update_user_kyc(
                uid,
                kyc_status="verified",
                kyc_verified_at=now_s,
                kyc_provider_request_id=applicant_id,
            )
            # Push-notify on first transition into verified.
            if current != "verified":
                _push_notify(
                    u.get("opex_user"),
                    {
                        "title": "본인인증 완료 / KYC verified",
                        "body": "Sumsub 본인인증이 완료되었습니다.",
                        "tag": "kyc",
                        "data": {"url": "/app/kyc.html"},
                    },
                )
                _notif_send(
                    u.get("opex_user"),
                    "kyc_verified",
                    "critical",
                    "본인인증 완료 / KYC verified",
                    "Sumsub 본인인증이 완료되었습니다. / Your KYC is now verified.",
                    {"verified_at": now_s},
                )
        elif new_kyc == "rejected":
            AUTH_DB.update_user_kyc(
                uid,
                kyc_status="rejected",
                kyc_provider_request_id=applicant_id,
            )
        else:
            AUTH_DB.update_user_kyc(
                uid,
                kyc_status="pending",
                kyc_provider_request_id=applicant_id,
            )

    # ---- POST /kyc/sumsub/webhook ---------------------------------------
    # Public: NO bearer. Sumsub posts signed payloads here.
    def h_sumsub_webhook(self):
        # Read raw body — signature is over raw bytes, NOT the parsed JSON.
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n > 0 else b""
        sig = (
            self.headers.get("X-App-Access-Sig") or self.headers.get("x-app-access-sig") or ""
        ).strip()
        ts = (
            self.headers.get("X-App-Access-Ts") or self.headers.get("x-app-access-ts") or ""
        ).strip()

        valid = kyc_provider.sumsub_verify_webhook(
            signature=sig,
            timestamp=ts,
            body=body,
        )
        # Always log the receipt, even if invalid -- audit trail.
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except Exception:  # noqa: BLE001
            payload = {}

        applicant_id = payload.get("applicantId") or payload.get("applicant_id") or ""
        wtype = payload.get("type") or ""
        now_s = int(time.time())

        AUTH_DB.insert_sumsub_webhook(
            applicant_id=applicant_id,
            type=wtype,
            body_json=body.decode("utf-8", "replace")[:65536],
            received_at=now_s,
            signature_valid=1 if valid else 0,
        )

        if not valid:
            sys.stderr.write(f"[sumsub] webhook signature INVALID for applicant {applicant_id!r}\n")
            return self._send_json(401, {"error": "invalid_signature"})

        # Map applicant_id back to a local user via sumsub_applicants.
        if not applicant_id:
            return self._send_json(204, None)

        review_result = payload.get("reviewResult") or {}
        review_answer = review_result.get("reviewAnswer")
        new_kyc = kyc_provider.sumsub_review_answer_to_kyc_status(review_answer)

        mapping = AUTH_DB.get_sumsub_applicant_by_id(applicant_id)
        if not mapping:
            sys.stderr.write(f"[sumsub] webhook for unknown applicant {applicant_id}\n")
            return self._send_json(204, None)
        opex = mapping["external_user_id"]
        user = AUTH_DB.get_user_by_opex(opex)
        if not user:
            return self._send_json(204, None)
        uid = user["id"]
        self._apply_sumsub_status(uid, applicant_id, new_kyc, now_s)
        AUTH_DB.update_sumsub_applicant_status(
            external_user_id=opex,
            last_status=(payload.get("reviewStatus") or "completed"),
            last_review_answer=(review_answer or ""),
            last_synced_at=now_s,
        )

        sys.stderr.write(
            f"[sumsub] webhook applied: applicant={applicant_id} "
            f"answer={review_answer!r} -> kyc_status={new_kyc}\n"
        )
        return self._send_json(204, None)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5501
    # Forward uncaught exceptions to the central error_collector
    # (loopback-only POST to :5690). Best-effort, never raises.
    try:
        from _error_reporter import install_global_handler  # type: ignore

        install_global_handler()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[auth] error reporter install failed: {e!r}\n")
    # OpenTelemetry: auto-instrument outbound HTTP + SQLite so wallet/chain
    # hops appear as CLIENT spans nested inside the inbound SERVER span.
    try:
        _otel_install()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[auth] otel install failed: {e!r}\n")
    init_db()
    backend = AUTH_DB.backend
    if backend == "sqlite":
        sys.stderr.write(f"[auth] db=sqlite path={getattr(AUTH_DB, 'path', DB_PATH)}\n")
    else:
        # Don't log credentials — just host/db. The DSN may carry a password.
        try:
            kw = AUTH_DB._conn_kwargs  # noqa: SLF001 - debug only
            sys.stderr.write(
                f"[auth] db=postgres host={kw['host']}:{kw['port']} "
                f"db={kw['database']} user={kw['user']}\n"
            )
        except Exception:  # noqa: BLE001
            sys.stderr.write(f"[auth] db={backend}\n")
    sys.stderr.write(f"[auth] wallet upstream={WALLET_BASE}\n")
    sys.stderr.write(f"[auth] listening on :{port}\n")
    with ThreadingServer(("", port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
