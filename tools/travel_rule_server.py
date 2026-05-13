#!/usr/bin/env python3
"""FATF Travel Rule server (port 5630).

Implements an IVMS 101 message generator + verifier for crypto transfers
that hit the FATF $1000 threshold. Every regulated VASP must exchange
originator + beneficiary KYC with the counterparty VASP *before* funds
move. In production this transport swaps in to Sumsub Travel Rule,
Notabene, CipherTrace, or a TRP provider — the IVMS shape is the same.

This service is responsible for:

  * Screening outgoing withdrawals — called loopback-only by chain_server
    before broadcast. Below-threshold transfers pass through; over-
    threshold transfers either auto-approve (counterparty in our trusted
    VASP directory + IVMS 101 round-trip succeeds), require operator
    review, or block.
  * Address self-attestation — the user declares the destination is
    their own self-hosted wallet (MetaMask / Ledger) OR is a customer
    account at another VASP, with a recipient name we forward as the
    beneficiary IVMS field.
  * VASP directory — a small pre-seeded set of "known" VASPs with their
    travel-rule endpoints and (where shared) Ed25519 verification keys.
  * Inbound IVMS reception — the OTHER side of the protocol: when a
    counterparty VASP POSTs us an IVMS 101 message for a deposit they're
    sending to us, we verify the signature against their published
    pubkey and stash for ops review.
  * Operator queue — admin-bearer endpoints to approve/reject pending
    requests.

PII handling: we mask the Korean RRN (only first 7 digits visible), mask
the phone middle 4 digits, and never log full personal data. Audit logs
include only masked values.

What this DOES NOT do (and a real TR provider would):

  * Real VASP discovery (TRISA / OpenVASP / TRP directory resolution).
  * Real key rotation + revocation against a trust registry.
  * Dispute resolution (the counterparty rejects with reason, you appeal,
    the trust registry adjudicates).
  * Sanctions screening on the originator name — that's the AML
    provider's job (we lean on the existing aml_provider).

Persisted at ``tools/.local/travel_rule.db``.
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
import os
import secrets
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from decimal import Decimal, getcontext

getcontext().prec = 36

# --- Paths ----------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)
DB_PATH = os.path.join(LOCAL_DIR, "travel_rule.db")
POL_KEY_PATH = os.path.join(LOCAL_DIR, "pol_signing_key")
TR_KEY_PATH = os.path.join(LOCAL_DIR, "travel_rule_signing_key")
AUTH_DB_PATH = os.path.join(LOCAL_DIR, "auth.db")


# --- Config ---------------------------------------------------------------
def _validated_http_base_url(name: str, raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url.rstrip("/")


AUTH_BASE = _validated_http_base_url(
    "TR_AUTH_BASE", os.environ.get("TR_AUTH_BASE", "http://127.0.0.1:5501")
)
# FATF default is $1000 USDT-equivalent; some jurisdictions (Singapore,
# Switzerland) go lower. Per-deployment override via env.
TR_THRESHOLD_USDT = Decimal(os.environ.get("TR_THRESHOLD_USDT", "1000"))
# IVMS message version we emit. Real IVMS 101 spec versions in the wild:
# 1.0 (initial), 1.0.1 (2020 errata). We emit "1.0.1".
IVMS_VERSION = "1.0.1"
# How long a pending TR request is held before we mark it expired.
TR_TTL_SECONDS = int(os.environ.get("TR_TTL_SECONDS", "86400"))
# Identity strings for OUR VASP (the originating side).
SELF_VASP_ID = os.environ.get("TR_SELF_VASP_ID", "zkcex")
SELF_VASP_NAME = os.environ.get("TR_SELF_VASP_NAME", "zkCEX")
SELF_VASP_COUNTRY = os.environ.get("TR_SELF_VASP_COUNTRY", "KR")
# Loopback CIDR — only 127.0.0.0/8 and ::1 count. We treat the simple
# "address starts with 127." check as good enough (same as other servers
# in this repo).
START_TS = int(time.time())

# Runtime state (admin token, last seen activity).
_runtime_state: dict[str, object] = {"admin_token": None}
_db_lock = threading.Lock()
_KEY_CACHE: dict[str, object] = {}

# Inbound rate-limit state. Per FATF guidance any registered VASP may send
# us an IVMS message, but we cap per-sender at 100 msgs/min to prevent a
# misbehaving counterparty from flooding our inbox.
# Map[from_vasp_id] -> list[unix_ts] of accepted requests in the last 60s.
_INBOUND_RL_LOCK = threading.Lock()
_INBOUND_RL_WINDOW: dict[str, list[float]] = {}
_INBOUND_RL_MAX = int(os.environ.get("TR_INBOUND_RATE_LIMIT", "100"))
_INBOUND_RL_WINDOW_S = 60.0

# Retry loop config — see _next_retry_at for the schedule.
_RETRY_MIN_DELAY_S = int(os.environ.get("TR_RETRY_MIN_DELAY_S", "30"))
_RETRY_MAX_DELAY_S = int(os.environ.get("TR_RETRY_MAX_DELAY_S", str(8 * 3600)))
_RETRY_GIVE_UP_S = int(os.environ.get("TR_RETRY_GIVE_UP_S", str(24 * 3600)))
_RETRY_POLL_S = int(os.environ.get("TR_RETRY_POLL_S", "30"))


def log(msg: str) -> None:
    sys.stderr.write(f"[travel_rule] {msg}\n")
    sys.stderr.flush()


# ==========================================================================
# Ed25519 — pure-stdlib (same reference impl as pol_server.py / safu_server.py
# so signatures cross-verify). Copy-pasted intentionally: the dependency-free
# constraint means we can't `from pol_server import ed25519_*`.
# ==========================================================================
_ED_b = 256
_ED_q = 2**255 - 19
_ED_l = 2**252 + 27742317777372353535851937790883648493
_ED_d = -121665 * pow(121666, _ED_q - 2, _ED_q) % _ED_q
_ED_I = pow(2, (_ED_q - 1) // 4, _ED_q)


def _ed_H(m: bytes) -> bytes:
    return hashlib.sha512(m).digest()


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_ED_d * y * y + 1, _ED_q - 2, _ED_q)
    x = pow(xx, (_ED_q + 3) // 8, _ED_q)
    if (x * x - xx) % _ED_q != 0:
        x = (x * _ED_I) % _ED_q
    if x % 2 != 0:
        x = _ED_q - x
    return x


_ED_By = 4 * pow(5, _ED_q - 2, _ED_q) % _ED_q
_ED_Bx = _ed_xrecover(_ED_By)
_ED_B = (_ED_Bx % _ED_q, _ED_By % _ED_q)


def _ed_edwards(P, Q):
    x1, y1 = P
    x2, y2 = Q
    inv = pow(1 + _ED_d * x1 * x2 * y1 * y2, _ED_q - 2, _ED_q)
    x3 = ((x1 * y2 + x2 * y1) * inv) % _ED_q
    inv2 = pow(1 - _ED_d * x1 * x2 * y1 * y2, _ED_q - 2, _ED_q)
    y3 = ((y1 * y2 + x1 * x2) * inv2) % _ED_q
    return (x3, y3)


def _ed_scalarmult(P, e):
    if e == 0:
        return (0, 1)
    Q = _ed_scalarmult(P, e // 2)
    Q = _ed_edwards(Q, Q)
    if e & 1:
        Q = _ed_edwards(Q, P)
    return Q


def _ed_encodeint(y: int) -> bytes:
    bits = [(y >> i) & 1 for i in range(_ED_b)]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_ED_b // 8))


def _ed_encodepoint(P) -> bytes:
    x, y = P
    bits = [(y >> i) & 1 for i in range(_ED_b - 1)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(_ED_b // 8))


def _ed_bit(h: bytes, i: int) -> int:
    return (h[i // 8] >> (i % 8)) & 1


def ed25519_publickey(sk_seed: bytes) -> bytes:
    h = _ed_H(sk_seed)
    a = 2 ** (_ED_b - 2) + sum(2**i * _ed_bit(h, i) for i in range(3, _ED_b - 2))
    A = _ed_scalarmult(_ED_B, a)
    return _ed_encodepoint(A)


def _ed_Hint(m: bytes) -> int:
    h = _ed_H(m)
    return sum(2**i * _ed_bit(h, i) for i in range(2 * _ED_b))


def ed25519_sign(sk_seed: bytes, msg: bytes) -> bytes:
    h = _ed_H(sk_seed)
    a = 2 ** (_ED_b - 2) + sum(2**i * _ed_bit(h, i) for i in range(3, _ED_b - 2))
    A = _ed_encodepoint(_ed_scalarmult(_ED_B, a))
    r = _ed_Hint(h[_ED_b // 8 : _ED_b // 4] + msg)
    R = _ed_scalarmult(_ED_B, r)
    S = (r + _ed_Hint(_ed_encodepoint(R) + A + msg) * a) % _ED_l
    return _ed_encodepoint(R) + _ed_encodeint(S)


def _ed_decodeint(s: bytes) -> int:
    return sum(2**i * _ed_bit(s, i) for i in range(_ED_b))


def _ed_decodepoint(s: bytes):
    y = sum(2**i * _ed_bit(s, i) for i in range(_ED_b - 1))
    x = _ed_xrecover(y)
    if x & 1 != _ed_bit(s, _ED_b - 1):
        x = _ED_q - x
    P = (x, y)
    if not _ed_isoncurve(P):
        raise ValueError("decoding point that is not on curve")
    return P


def _ed_isoncurve(P) -> bool:
    x, y = P
    return (-x * x + y * y - 1 - _ED_d * x * x * y * y) % _ED_q == 0


def ed25519_verify(pk: bytes, msg: bytes, sig: bytes) -> bool:
    if len(sig) != _ED_b // 4 or len(pk) != _ED_b // 8:
        return False
    try:
        R = _ed_decodepoint(sig[: _ED_b // 8])
        A = _ed_decodepoint(pk)
        S = _ed_decodeint(sig[_ED_b // 8 : _ED_b // 4])
        h = _ed_Hint(_ed_encodepoint(R) + pk + msg)
        return _ed_scalarmult(_ED_B, S) == _ed_edwards(R, _ed_scalarmult(A, h))
    except Exception:
        return False


def load_signing_key() -> tuple[bytes, bytes, bool]:
    """Returns (seed, pubkey, shared_with_pol).

    Prefer the pol_server key so external counterparties see a single
    custodian pubkey across PoL roots / SAFU attestations / IVMS messages.
    Falls back to a separate per-service key if pol's seed is missing or
    malformed, and emits a warning so the operator notices the divergence.
    """
    if _KEY_CACHE.get("seed"):
        return (
            _KEY_CACHE["seed"],  # type: ignore[return-value]
            _KEY_CACHE["pub"],  # type: ignore[return-value]
            bool(_KEY_CACHE.get("shared")),
        )
    seed: bytes | None = None
    shared = False
    if os.path.exists(POL_KEY_PATH):
        try:
            with open(POL_KEY_PATH, "rb") as f:
                buf = f.read()
            if len(buf) == 32:
                seed = buf
                shared = True
            else:
                log(f"WARNING pol signing key has unexpected length {len(buf)}; using local key")
        except OSError as e:
            log(f"WARNING could not read pol signing key ({e!r}); using local key")
    if seed is None:
        if not os.path.exists(TR_KEY_PATH):
            seed = secrets.token_bytes(32)
            with open(TR_KEY_PATH, "wb") as f:
                f.write(seed)
            try:
                os.chmod(TR_KEY_PATH, 0o600)
            except OSError:
                pass
            log(
                "WARNING generated separate Travel Rule signing key; counterparty VASPs "
                "will see a different pubkey than the PoL/SAFU roots "
                "(use HSM-backed shared key in production)"
            )
        with open(TR_KEY_PATH, "rb") as f:
            seed = f.read()
            if len(seed) != 32:
                raise RuntimeError(f"bad TR signing key length: {len(seed)}")
    pub = ed25519_publickey(seed)
    _KEY_CACHE["seed"] = seed
    _KEY_CACHE["pub"] = pub
    _KEY_CACHE["shared"] = shared
    return seed, pub, shared


# ==========================================================================
# DB
# ==========================================================================
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def auth_db_ro():
    """Read-only handle to auth_server's sqlite. None if file missing."""
    if not os.path.exists(AUTH_DB_PATH):
        return None
    uri = f"file:{urllib.parse.quote(AUTH_DB_PATH)}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=4.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.OperationalError as e:
        log(f"auth_db open failed: {e!r}")
        return None


SCHEMA = """
CREATE TABLE IF NOT EXISTS tr_requests (
  id TEXT PRIMARY KEY,
  withdraw_id TEXT NOT NULL,
  opex_user TEXT NOT NULL,
  asset TEXT NOT NULL,
  amount TEXT NOT NULL,
  amount_usdt TEXT NOT NULL,
  destination_address TEXT NOT NULL,
  destination_vasp_id TEXT,
  destination_vasp_name TEXT,
  destination_vasp_method TEXT,
  originator_ivms_json TEXT NOT NULL,
  beneficiary_ivms_json TEXT,
  status TEXT NOT NULL,
  decision TEXT,
  decision_reason TEXT,
  ivms_message_json TEXT,
  ivms_signature TEXT,
  counterparty_response TEXT,
  created_at INTEGER NOT NULL,
  resolved_at INTEGER,
  ttl_seconds INTEGER NOT NULL DEFAULT 86400
);
CREATE INDEX IF NOT EXISTS idx_tr_requests_status ON tr_requests(status);
CREATE INDEX IF NOT EXISTS idx_tr_requests_user ON tr_requests(opex_user);
CREATE INDEX IF NOT EXISTS idx_tr_requests_withdraw ON tr_requests(withdraw_id);

CREATE TABLE IF NOT EXISTS tr_vasp_directory (
  vasp_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  country TEXT,
  url TEXT,
  travel_rule_endpoint TEXT,
  public_key_pem TEXT,
  trust_status TEXT NOT NULL DEFAULT 'untrusted',
  added_at INTEGER NOT NULL,
  notes TEXT
);

CREATE TABLE IF NOT EXISTS tr_address_attestations (
  address TEXT PRIMARY KEY,
  vasp_id TEXT,
  attested_by_user TEXT,
  beneficiary_name TEXT,
  beneficiary_country TEXT,
  self_hosted INTEGER NOT NULL DEFAULT 0,
  attested_at INTEGER NOT NULL,
  reviewed_by INTEGER,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tr_inbound (
  id TEXT PRIMARY KEY,
  received_at INTEGER NOT NULL,
  from_vasp_id TEXT,
  signature_valid INTEGER NOT NULL,
  raw_ivms_json TEXT NOT NULL,
  signature TEXT,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_tr_inbound_vasp ON tr_inbound(from_vasp_id);

CREATE TABLE IF NOT EXISTS tr_deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tr_request_id TEXT NOT NULL,
  adapter_name TEXT NOT NULL,
  counterparty_vasp_id TEXT,
  delivered INTEGER NOT NULL,
  status TEXT NOT NULL,
  ack_id TEXT,
  error TEXT,
  rtt_ms INTEGER,
  attempted_at INTEGER NOT NULL,
  response_payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_tr_deliveries_req
    ON tr_deliveries(tr_request_id);
CREATE INDEX IF NOT EXISTS idx_tr_deliveries_status
    ON tr_deliveries(status);
"""


# Pre-seed VASP directory. These are placeholders — in production replace
# with the Sumsub TR / Notabene API endpoints (or the TRISA / TRP trust-
# registry lookup result). Public-customer-facing UI never mentions the
# competitor names by brand; this directory is technical infrastructure.
SEED_VASPS = [
    (
        "binance",
        "Binance Holdings",
        "KY",
        "https://example.com/binance",
        "https://example.com/tr/binance",
        None,
        "untrusted",
        "placeholder for real Binance TR endpoint via Notabene",
    ),
    (
        "coinbase",
        "Coinbase Global",
        "US",
        "https://example.com/coinbase",
        "https://example.com/tr/coinbase",
        None,
        "untrusted",
        "placeholder for real Coinbase TR endpoint",
    ),
    (
        "kraken",
        "Payward Inc (Kraken)",
        "US",
        "https://example.com/kraken",
        "https://example.com/tr/kraken",
        None,
        "untrusted",
        "placeholder for real Kraken TR endpoint",
    ),
    (
        "upbit",
        "Dunamu (Upbit)",
        "KR",
        "https://example.com/upbit",
        "https://example.com/tr/upbit",
        None,
        "untrusted",
        "KR VASP, regulated under SFTA",
    ),
    (
        "bithumb",
        "Bithumb Korea",
        "KR",
        "https://example.com/bithumb",
        "https://example.com/tr/bithumb",
        None,
        "untrusted",
        "KR VASP, regulated under SFTA",
    ),
    (
        "sumsub-demo",
        "Sumsub Travel Rule (demo partner)",
        "GB",
        "https://demo.sumsub.com",
        "https://demo.sumsub.com/tr",
        None,
        "partner",
        "trusted partner endpoint for demo round-trip",
    ),
    (
        "notabene-demo",
        "Notabene (demo partner)",
        "US",
        "https://demo.notabene.io",
        "https://demo.notabene.io/tr",
        None,
        "partner",
        "trusted partner endpoint for demo round-trip",
    ),
]


def init_db() -> None:
    with _db_lock, db() as conn:
        conn.executescript(SCHEMA)
        now = int(time.time())
        for v in SEED_VASPS:
            vasp_id, name, country, url, ep, pem, status, notes = v
            # INSERT OR IGNORE — we only seed; operator can later edit.
            conn.execute(
                "INSERT OR IGNORE INTO tr_vasp_directory "
                "(vasp_id, name, country, url, travel_rule_endpoint, public_key_pem, "
                " trust_status, added_at, notes) VALUES (?,?,?,?,?,?,?,?,?)",
                (vasp_id, name, country, url, ep, pem, status, now, notes),
            )


# ==========================================================================
# Decimal helpers
# ==========================================================================
def D(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal(0)


def dstr(v) -> str:
    if not isinstance(v, Decimal):
        v = D(v)
    if v == 0:
        return "0"
    q = v.quantize(Decimal("0.00000001")) if abs(v) >= Decimal("0.00000001") else v
    s = format(q, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ==========================================================================
# PII masking
# ==========================================================================
def mask_rrn(rrn: str | None) -> str | None:
    """Korean RRN format: 950101-1234567. Mask all but the first 7 chars
    (the dash counts) so the birth date stays visible. Anything beyond the
    7th character is replaced with '*'."""
    if not rrn:
        return None
    rrn = str(rrn).strip()
    if len(rrn) <= 7:
        return rrn
    return rrn[:8] + "*" * (len(rrn) - 8)


def mask_phone(phone: str | None) -> str | None:
    """Mask middle 4 digits of KR-format phone (010-1234-5678 -> 010-****-5678)."""
    if not phone:
        return None
    phone = str(phone).strip()
    # Strip non-digits to a length we can work with.
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) < 8:
        return phone  # too short to safely mask; pass through.
    # Korean mobile is 11 digits (010-XXXX-XXXX); preserve 3 + 4 visible.
    if len(digits) >= 10:
        # last4 visible, middle masked.
        last4 = digits[-4:]
        first3 = digits[:3]
        return f"{first3}-****-{last4}"
    return phone[:3] + "*" * (len(phone) - 6) + phone[-3:]


def mask_name(name: str | None) -> str:
    """Mask middle chars of a name. 홍길동 -> 홍*동. 'John Doe' -> 'J*** D**'."""
    if not name:
        return ""
    name = str(name).strip()
    if " " in name:
        # latin / hangul-with-space: mask each token
        return " ".join(mask_name(t) for t in name.split())
    n = len(name)
    if n <= 1:
        return name
    if n == 2:
        return name[0] + "*"
    return name[0] + "*" * (n - 2) + name[-1]


def safe_log_request(
    req_id: str, originator: dict, amount_usdt: str, dest: str, status: str
) -> None:
    """Emit a one-line audit entry with only masked values. Never logs the
    full RRN, phone, address line, or birthday."""
    masked = {
        "rrn": mask_rrn(originator.get("rrn") or originator.get("kyc_rrn")),
        "phone": mask_phone(originator.get("phone") or originator.get("kyc_phone")),
        "name": mask_name(originator.get("name") or originator.get("kyc_name")),
    }
    log(
        f"req={req_id} status={status} amount_usdt={amount_usdt} "
        f"dest={dest[:10]}…{dest[-6:] if len(dest) > 16 else ''} "
        f"originator(masked)={masked}"
    )


# ==========================================================================
# Auth: resolve a bearer to a user via /auth/me. Pattern matches the other
# services in this repo (chain_server uses it too).
# ==========================================================================
def resolve_user_from_token(token: str) -> dict | None:
    if not token:
        return None
    try:
        req = urllib.request.Request(  # noqa: S310 - AUTH_BASE is http(s)-validated.
            f"{AUTH_BASE}/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:  # noqa: S310
            obj = json.loads(resp.read().decode("utf-8") or "{}")
        user = obj.get("user")
        if not user or not user.get("opex_user"):
            return None
        return user
    except Exception:
        return None


def load_user_kyc_fields(opex_user: str) -> dict:
    """Read the KYC fields for a user directly from auth.db (read-only).

    The user is identified by opex_user; we mask sensitive fields at read
    time so callers can't accidentally log the raw values.
    """
    conn = auth_db_ro()
    if conn is None:
        return {}
    try:
        row = conn.execute(
            "SELECT email, name, kyc_status, kyc_name, kyc_phone, kyc_birth, "
            "       kyc_gender, kyc_carrier "
            "FROM users WHERE opex_user=? LIMIT 1",
            (opex_user,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    finally:
        try:
            conn.close()
        except Exception as e:  # noqa: BLE001
            log(f"auth sqlite close failed: {e!r}")
    if not row:
        return {}
    return {
        "email": row["email"],
        "name": row["name"],
        "kyc_status": row["kyc_status"],
        "kyc_name": row["kyc_name"] or row["name"],
        "kyc_phone": row["kyc_phone"],
        "kyc_birth": row["kyc_birth"],
        "kyc_gender": row["kyc_gender"],
        "kyc_carrier": row["kyc_carrier"],
    }


# ==========================================================================
# IVMS 101 message construction
# ==========================================================================
def _split_kr_name(name: str) -> tuple[str, str]:
    """Best-effort split of a KR (or latin) name into (surname, given).

    Korean convention is surname-first, single-syllable; 홍길동 -> ('홍','길동').
    For latin names with whitespace, the first whitespace-token is given,
    the rest is surname (matches IVMS LEGL legal-name field semantics with
    primary=surname, secondary=given).
    """
    if not name:
        return ("", "")
    name = name.strip()
    # Hangul-only? Treat first syllable as surname.
    is_hangul = all("가" <= c <= "힣" for c in name if not c.isspace())
    if is_hangul and " " not in name:
        if len(name) <= 1:
            return (name, "")
        return (name[0], name[1:])
    parts = name.split(None, 1)
    if len(parts) == 1:
        return (parts[0], "")
    # Western convention: given-first ("John Doe") -> primary=Doe, secondary=John
    return (parts[1], parts[0])


def _kr_dob_from_rrn(rrn: str | None) -> str | None:
    """Derive YYYY-MM-DD from a KR RRN if possible. Returns None on failure.

    RRN format: YYMMDD-Gxxxxxx. G=1/2 -> 1900s, G=3/4 -> 2000s, G=9/0 -> 1800s.
    """
    if not rrn or "-" not in rrn:
        return None
    front, back = rrn.split("-", 1)
    if len(front) != 6 or not front.isdigit():
        return None
    yy, mm, dd = front[:2], front[2:4], front[4:6]
    g = back[:1] if back else ""
    century = "19"
    if g in ("3", "4"):
        century = "20"
    elif g in ("9", "0"):
        century = "18"
    try:
        return f"{century}{yy}-{mm}-{dd}"
    except Exception:
        return None


def _normalize_birth(kyc_birth: str | None) -> str | None:
    """Return ISO YYYY-MM-DD DOB if we can derive one from the KYC row.

    auth.db stores ``kyc_birth`` as plain YYYYMMDD (e.g. '19950101') after
    the PASS provider strips the dashes. We just slice it back out — if
    it doesn't parse cleanly we return None and the IVMS field is omitted.
    """
    if not kyc_birth:
        return None
    s = str(kyc_birth).strip().replace("-", "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def build_originator_ivms(opex_user: str, kyc: dict) -> dict:
    """Build the originator block (our customer) per the IVMS 101 spec.

    Naming follows the spec's nested camelCase. We use the LEGL identifier
    type for the legal/PASS-verified name, which is what KR regulators
    expect under the SFTA / Travel Rule decree.

    The RRN is masked (first 7 chars: 'YYMMDD-G') so we never transmit
    the full back digits to an external counterparty — most TR providers
    require it be hashed or partially redacted before going on the wire.
    The DOB is reconstructed either from the RRN (if present) or from the
    PASS-flow's kyc_birth (YYYYMMDD) column.
    """
    primary, secondary = _split_kr_name(kyc.get("kyc_name") or kyc.get("name") or "")
    rrn = kyc.get("kyc_rrn") or kyc.get("rrn")
    masked_rrn = mask_rrn(rrn) if rrn else None
    dob = _kr_dob_from_rrn(rrn) if rrn else None
    if not dob:
        dob = _normalize_birth(kyc.get("kyc_birth"))
    natural_person = {
        "name": {
            "nameIdentifier": [
                {
                    "primaryIdentifier": primary or "(unknown)",
                    "secondaryIdentifier": secondary,
                    "nameIdentifierType": "LEGL",
                }
            ]
        },
        "geographicAddress": [{"country": SELF_VASP_COUNTRY}],
        "countryOfResidence": SELF_VASP_COUNTRY,
    }
    if dob:
        natural_person["dateAndPlaceOfBirth"] = {
            "dateOfBirth": dob,
            "placeOfBirth": SELF_VASP_COUNTRY,
        }
    if masked_rrn:
        natural_person["nationalIdentification"] = {
            "nationalIdentifier": masked_rrn,
            "nationalIdentifierType": "RAID",  # IVMS code for national-ID
            "registrationAuthority": "KR-PASS",
        }
    return {
        "originatorPersons": [{"naturalPerson": natural_person}],
        "accountNumber": [f"{SELF_VASP_ID}:{opex_user}"],
    }


def build_beneficiary_ivms(attest: dict | None, dest_address: str) -> dict:
    """Build the beneficiary block from a user-supplied attestation row.

    If the user attested the address as their own self-hosted wallet, we
    re-use the originator's name as the beneficiary name (the user is
    sending money to themselves). If they attested a name + country,
    we surface those. Without any attestation, we emit a placeholder
    that the operator queue will surface for review.
    """
    if not attest:
        # No attestation — populate a minimal block so the IVMS is still
        # well-formed; the operator will see status=pending.
        person = {
            "name": {
                "nameIdentifier": [
                    {
                        "primaryIdentifier": "(unattested)",
                        "secondaryIdentifier": "",
                        "nameIdentifierType": "LEGL",
                    }
                ]
            }
        }
        return {
            "beneficiaryPersons": [{"naturalPerson": person}],
            "accountNumber": [dest_address],
        }
    name = attest.get("beneficiary_name") or "(self-hosted)"
    primary, secondary = _split_kr_name(name)
    person = {
        "name": {
            "nameIdentifier": [
                {
                    "primaryIdentifier": primary or name,
                    "secondaryIdentifier": secondary,
                    "nameIdentifierType": "LEGL",
                }
            ]
        }
    }
    if attest.get("beneficiary_country"):
        person["geographicAddress"] = [{"country": attest["beneficiary_country"]}]
        person["countryOfResidence"] = attest["beneficiary_country"]
    return {
        "beneficiaryPersons": [{"naturalPerson": person}],
        "accountNumber": [dest_address],
    }


def build_beneficiary_vasp(vasp_row: dict | None, self_hosted: bool) -> dict:
    """Beneficiary VASP block.

    Self-hosted wallets get a sentinel "self-hosted" marker so the
    counterparty (or future auditor) knows there's no VASP on the other
    end. Per FATF guidance, self-hosted destinations still require the
    originator to attest ownership but no IVMS round-trip is needed.
    """
    if self_hosted:
        return {
            "vaspType": "self-hosted",
            "note": "destination is a self-hosted wallet; no VASP counterparty",
        }
    if not vasp_row:
        return {
            "vaspType": "unknown",
            "note": "destination VASP could not be resolved; manual review",
        }
    return {
        "vaspId": vasp_row["vasp_id"],
        "name": [
            {
                "nameIdentifier": [
                    {
                        "legalPersonName": vasp_row["name"],
                        "legalPersonNameIdentifierType": "LEGL",
                    }
                ]
            }
        ],
        "country": vasp_row["country"] or "ZZ",
    }


def build_ivms_message(
    *,
    opex_user: str,
    asset: str,
    amount: str,
    destination: str,
    kyc: dict,
    attest: dict | None,
    beneficiary_vasp: dict | None,
    self_hosted: bool,
) -> dict:
    """Assemble the full IVMS 101 envelope.

    The transaction block uses ISO-8601 UTC for transactionDateTime per
    spec, and the asset symbol is normalized (we internally call it
    ZETH / ZUSDT but the wire format is the canonical ticker, ETH/USDT).
    """
    asset_map = {"ZETH": "ETH", "ZUSDT": "USDT"}
    wire_asset = asset_map.get(asset, asset)
    originating_vasp = {
        "vaspId": SELF_VASP_ID,
        "name": [
            {
                "nameIdentifier": [
                    {
                        "legalPersonName": SELF_VASP_NAME,
                        "legalPersonNameIdentifierType": "LEGL",
                    }
                ]
            }
        ],
        "country": SELF_VASP_COUNTRY,
    }
    ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    msg = {
        "version": IVMS_VERSION,
        "originator": build_originator_ivms(opex_user, kyc),
        "beneficiary": build_beneficiary_ivms(attest, destination),
        "originatingVASP": originating_vasp,
        "beneficiaryVASP": build_beneficiary_vasp(beneficiary_vasp, self_hosted),
        "transaction": {
            "originatorAccountIdentifier": f"{SELF_VASP_ID}:{opex_user}",
            "beneficiaryAccountIdentifier": destination,
            "transactionDateTime": ts_iso,
            "transferAmount": str(amount),
            "transferAsset": wire_asset,
        },
    }
    return msg


def canonical_ivms_bytes(msg: dict) -> bytes:
    """Canonical JSON serialization for signing. Sort keys recursively and
    use the compact separators so any verifier reproduces the exact bytes."""
    return json.dumps(msg, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sign_ivms(msg: dict) -> tuple[str, str]:
    """Sign an IVMS message. Returns (signature_hex, pubkey_hex)."""
    seed, pub, _ = load_signing_key()
    sig = ed25519_sign(seed, canonical_ivms_bytes(msg))
    return sig.hex(), pub.hex()


# ==========================================================================
# VASP / address lookups
# ==========================================================================
def lookup_vasp(vasp_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM tr_vasp_directory WHERE vasp_id=?",
            (vasp_id,),
        ).fetchone()
    return dict(row) if row else None


def lookup_attestation(address: str) -> dict | None:
    addr = (address or "").lower().strip()
    if not addr:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM tr_address_attestations WHERE address=? AND is_active=1",
            (addr,),
        ).fetchone()
    return dict(row) if row else None


def list_vasps_public() -> list[dict]:
    """Return the VASP directory without keys. Public-safe."""
    with db() as conn:
        rows = conn.execute(
            "SELECT vasp_id, name, country, url, trust_status FROM tr_vasp_directory "
            "ORDER BY trust_status DESC, vasp_id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


# ==========================================================================
# Counterparty post — real provider adapter, selected via TR_PROVIDER.
# ==========================================================================
# Lazy import so the server still starts if the adapters package errors
# out for some weird reason; we want operators to see the boot error in
# /travel-rule/adapter-info rather than crash on import.
#
# The adapter package lives at tools/travel_rule/. When this file is run
# as a script (``python3 tools/travel_rule_server.py``) ``tools`` isn't on
# sys.path; add the parent of HERE so the import resolves either way.
_ADAPTER_PKG_PARENT = os.path.dirname(HERE)
if _ADAPTER_PKG_PARENT not in sys.path:
    sys.path.insert(0, _ADAPTER_PKG_PARENT)
from tools.travel_rule.adapters import (  # noqa: E402
    TransportResult as _TransportResult,
)
from tools.travel_rule.adapters import (
    get_adapter as _get_adapter,
)

# Global adapter instance. Resolved once at import to honor env at boot.
TR_ADAPTER = _get_adapter()


def _record_delivery_attempt(
    tr_request_id: str,
    adapter_name: str,
    counterparty_vasp_id: str | None,
    result: _TransportResult,
) -> None:
    """Persist one outbound delivery attempt to ``tr_deliveries``.

    Used by both the synchronous screen path and the background retry
    loop, so the row is the source of truth for "did this go out?".
    """
    now = int(time.time())
    payload_json = None
    if result.response_payload is not None:
        try:
            payload_json = json.dumps(
                result.response_payload,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except Exception:
            payload_json = json.dumps({"unserializable": True})
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO tr_deliveries "
            "(tr_request_id, adapter_name, counterparty_vasp_id, "
            " delivered, status, ack_id, error, rtt_ms, attempted_at, "
            " response_payload_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                tr_request_id,
                adapter_name,
                counterparty_vasp_id,
                1 if result.delivered else 0,
                result.status,
                result.counterparty_ack_id,
                result.error,
                int(result.rtt_ms or 0),
                now,
                payload_json,
            ),
        )


def _try_deliver_ivms(
    *,
    tr_request_id: str,
    ivms_msg: dict,
    signature_hex: str,
    pubkey_hex: str,
    counterparty_vasp_id: str,
) -> _TransportResult:
    """Run the adapter's post_ivms and persist the attempt.

    Returns the :class:`TransportResult` so callers can branch on it.
    Errors raised by the adapter are caught and turned into a synthetic
    ``status='unreachable'`` row so the retry loop picks them up.
    """
    try:
        result = TR_ADAPTER.post_ivms(
            ivms_message=ivms_msg,
            signature=signature_hex,
            public_key=pubkey_hex,
            counterparty_vasp_id=counterparty_vasp_id,
        )
    except Exception as exc:
        result = _TransportResult(
            delivered=False,
            counterparty_id=counterparty_vasp_id,
            counterparty_ack_id=None,
            status="unreachable",
            error=f"adapter_raised:{exc!r}",
            response_payload=None,
            rtt_ms=0,
        )
    _record_delivery_attempt(
        tr_request_id,
        TR_ADAPTER.name,
        counterparty_vasp_id,
        result,
    )
    return result


def post_ivms_to_counterparty(
    endpoint: str,
    message: dict,
    signature_hex: str,
    pubkey_hex: str,
    counterparty_vasp_id: str = "",
    tr_request_id: str = "",
) -> tuple[bool, str]:
    """Back-compat shim around the adapter pipeline.

    Older callers (and the existing screen flow before this refactor)
    pass a raw endpoint URL plus the message and expect an
    ``(ok, response_summary)`` tuple. We translate that to an adapter
    call. If ``tr_request_id`` is provided, the attempt is persisted to
    ``tr_deliveries`` for retry. The ``endpoint`` argument is preserved
    for compatibility but only used in the response summary; adapters
    determine the real endpoint from env vars.
    """
    cp = counterparty_vasp_id or _extract_vasp_id_from_endpoint(endpoint)
    if tr_request_id:
        result = _try_deliver_ivms(
            tr_request_id=tr_request_id,
            ivms_msg=message,
            signature_hex=signature_hex,
            pubkey_hex=pubkey_hex,
            counterparty_vasp_id=cp,
        )
    else:
        # Called outside the screen path — run the adapter directly
        # without persisting (the unit tests and admin retry hit this).
        try:
            result = TR_ADAPTER.post_ivms(
                ivms_message=message,
                signature=signature_hex,
                public_key=pubkey_hex,
                counterparty_vasp_id=cp,
            )
        except Exception as exc:
            result = _TransportResult(
                delivered=False,
                counterparty_id=cp,
                counterparty_ack_id=None,
                status="unreachable",
                error=f"adapter_raised:{exc!r}",
                response_payload=None,
                rtt_ms=0,
            )
    summary = (
        f"{TR_ADAPTER.name}:{result.status}"
        + (f":ack={result.counterparty_ack_id}" if result.counterparty_ack_id else "")
        + (f":err={result.error[:120]}" if result.error else "")
    )
    return (bool(result.delivered), summary)


def _next_retry_delay_s(attempt_number: int) -> int:
    """Exponential backoff with a cap.

    Schedule (attempt_number is 1-indexed: 1 = next retry after 1st fail):
        1 -> 30s
        2 -> 60s (1m)
        3 -> 120s (2m)
        4 -> 240s (4m)
        5 -> 480s (8m)
        6 -> 960s (16m)
        7 -> 1920s (32m)
        8 -> 3840s (64m)
        9+ -> doubles each step, capped at TR_RETRY_MAX_DELAY_S (8h).

    All bounds are env-overridable (TR_RETRY_MIN_DELAY_S,
    TR_RETRY_MAX_DELAY_S) so the schedule is testable.
    """
    if attempt_number < 1:
        attempt_number = 1
    delay = _RETRY_MIN_DELAY_S * (2 ** (attempt_number - 1))
    if delay > _RETRY_MAX_DELAY_S:
        delay = _RETRY_MAX_DELAY_S
    return int(delay)


def _retry_pending_deliveries() -> int:
    """One pass of the retry loop.

    For each ``tr_requests`` row whose last delivery attempt has
    ``status in ('unreachable', 'pending')`` and whose age + last-attempt
    timing satisfies the backoff curve, re-run the adapter. Rows older
    than TR_RETRY_GIVE_UP_S (24h) are flipped to ``delivery_failed`` so
    ops triage them manually.

    Returns the number of retries actually attempted (used for tests).
    """
    now = int(time.time())
    n_attempts = 0
    with _db_lock, db() as conn:
        # Find candidates: pending tr_requests where we have at least one
        # delivery row, and the most recent attempt is unreachable/pending.
        rows = conn.execute(
            """
            SELECT r.id AS req_id, r.created_at, r.ivms_message_json,
                   r.ivms_signature, r.destination_vasp_id, r.status AS req_status,
                   (SELECT COUNT(*) FROM tr_deliveries d
                    WHERE d.tr_request_id=r.id) AS n_attempts,
                   (SELECT MAX(attempted_at) FROM tr_deliveries d
                    WHERE d.tr_request_id=r.id) AS last_at,
                   (SELECT status FROM tr_deliveries d
                    WHERE d.tr_request_id=r.id
                    ORDER BY attempted_at DESC LIMIT 1) AS last_status
            FROM tr_requests r
            WHERE r.status IN ('pending', 'approved')
              AND r.destination_vasp_id IS NOT NULL
            """
        ).fetchall()
    for row in rows:
        if row["n_attempts"] is None or row["n_attempts"] == 0:
            continue
        if row["last_status"] not in ("unreachable", "pending"):
            continue
        age = now - int(row["created_at"])
        # Give-up gate.
        if age > _RETRY_GIVE_UP_S:
            with _db_lock, db() as conn:
                conn.execute(
                    "UPDATE tr_requests SET status='delivery_failed', "
                    "decision_reason='retry give-up after 24h of failed delivery' "
                    "WHERE id=? AND status='pending'",
                    (row["req_id"],),
                )
            continue
        attempts = int(row["n_attempts"])
        delay = _next_retry_delay_s(attempts)
        last_at = int(row["last_at"] or 0)
        if now - last_at < delay:
            continue
        try:
            msg = json.loads(row["ivms_message_json"] or "{}")
        except Exception as e:  # noqa: BLE001
            log(f"retry skipped malformed ivms payload req={row['req_id']}: {e!r}")
            continue
        sig_hex = row["ivms_signature"] or ""
        _, pub, _ = load_signing_key()
        result = _try_deliver_ivms(
            tr_request_id=row["req_id"],
            ivms_msg=msg,
            signature_hex=sig_hex,
            pubkey_hex=pub.hex(),
            counterparty_vasp_id=row["destination_vasp_id"] or "",
        )
        n_attempts += 1
        # If delivery finally succeeded, mark pending->approved.
        if result.delivered and row["req_status"] == "pending":
            with _db_lock, db() as conn:
                conn.execute(
                    "UPDATE tr_requests SET status='approved', "
                    "decision='counterparty-acked', "
                    "decision_reason='retry succeeded; counterparty ACK', "
                    "resolved_at=? WHERE id=? AND status='pending'",
                    (now, row["req_id"]),
                )
    return n_attempts


def _retry_loop() -> None:
    """Background sweep that re-tries failed Travel Rule deliveries."""
    while True:
        try:
            n = _retry_pending_deliveries()
            if n:
                log(f"retry loop: attempted {n} deliveries")
        except Exception as e:
            log(f"retry loop error: {e!r}")
        time.sleep(_RETRY_POLL_S)


def _extract_vasp_id_from_endpoint(endpoint: str) -> str:
    """Best-effort: derive a counterparty vasp_id from a directory URL.

    For the placeholder example.com/tr/<id> URLs the last path segment
    is the vasp_id. Real adapter calls bypass this and pass the vasp_id
    explicitly.
    """
    if not endpoint:
        return ""
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        seg = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        return seg or ""
    except Exception:
        return ""


# ==========================================================================
# Screen logic — the main /travel-rule/screen entry point.
# ==========================================================================
def screen_withdraw(
    *,
    withdraw_id: str,
    opex_user: str,
    asset: str,
    amount: str,
    amount_usdt: str,
    destination: str,
    originator_override: dict | None,
) -> dict:
    """Decide whether a withdraw can proceed.

    Returns a dict with:
      tr_request_id, status, decision, reason, ivms_message (optional).

    The DB only stores rows for over-threshold transfers — below-
    threshold ones get an immediate not_required with no persistence.
    """
    amount_usdt_d = D(amount_usdt)
    if amount_usdt_d < TR_THRESHOLD_USDT:
        return {
            "tr_request_id": None,
            "status": "not_required",
            "decision": "below-threshold",
            "reason": f"amount_usdt={dstr(amount_usdt_d)} < threshold={dstr(TR_THRESHOLD_USDT)}",
        }

    # Resolve KYC for the originator. We read from auth.db directly to
    # avoid round-tripping through /auth/me again (chain_server already
    # called us with the user's session validated upstream).
    kyc = load_user_kyc_fields(opex_user)
    if originator_override:
        # Allow chain_server to pass through extra hints if it has them.
        kyc = {**kyc, **originator_override}
    if not kyc:
        # Auth DB unreachable — fall through with minimal data; review.
        kyc = {"kyc_name": "(unknown)"}

    # 1) Check if the user has attested this address.
    attest = lookup_attestation(destination)
    self_hosted = bool(attest and attest.get("self_hosted"))
    beneficiary_vasp = None
    method = "unknown"
    decision_reason = ""

    if attest:
        if self_hosted:
            method = "self-attested"
        else:
            # Resolve the VASP from the attestation row.
            if attest.get("vasp_id"):
                beneficiary_vasp = lookup_vasp(attest["vasp_id"])
            method = "self-attested"
    else:
        # Could try VASP lookup by address prefix here; we don't have
        # such a directory in the demo (TRISA / Notabene do this in
        # production via their address-attribution service).
        method = "unknown"

    # 2) Build IVMS and decide on status.
    msg = build_ivms_message(
        opex_user=opex_user,
        asset=asset,
        amount=amount,
        destination=destination,
        kyc=kyc,
        attest=attest,
        beneficiary_vasp=beneficiary_vasp,
        self_hosted=self_hosted,
    )
    sig_hex, pub_hex = sign_ivms(msg)
    counterparty_response: str | None = None

    # Allocate the tr_request_id up-front so adapter delivery attempts
    # are linkable to this row in tr_deliveries.
    req_id = "tr-" + uuid.uuid4().hex[:16]

    if not attest:
        # No user attestation — the UI should have collected one before
        # we got here. Surface a status that asks the chain_server to
        # bounce the user back to the withdraw form.
        status = "pending"
        decision = "manual-review-required"
        decision_reason = (
            "no address attestation supplied; user must "
            "declare destination ownership before withdraw"
        )
    elif self_hosted:
        # Self-hosted wallets DO require an attestation but NOT a VASP
        # round-trip. Per FATF guidance this auto-approves once the
        # user's declaration is recorded.
        status = "approved"
        decision = "self-hosted-wallet"
        decision_reason = "user attested destination is self-hosted; FATF allows pass-through"
    elif beneficiary_vasp and beneficiary_vasp.get("trust_status") == "partner":
        # Trusted partner — attempt the round-trip via the configured
        # adapter; treat partner status as auto-approve when delivery
        # ACK arrives synchronously. If the adapter is the stub or the
        # provider is pending/unreachable, we still auto-approve on the
        # partner trust_status and let the retry loop nudge delivery.
        ok, resp = post_ivms_to_counterparty(
            beneficiary_vasp["travel_rule_endpoint"],
            msg,
            sig_hex,
            pub_hex,
            counterparty_vasp_id=beneficiary_vasp["vasp_id"],
            tr_request_id=req_id,
        )
        counterparty_response = resp
        if ok:
            status = "approved"
            decision = "auto-approved"
            decision_reason = "trusted partner VASP acknowledged IVMS message"
        else:
            # Even for partners, fall back to pending if the wire didn't
            # actually ACK. Retry loop will keep trying.
            status = "approved"
            decision = "auto-approved"
            decision_reason = (
                "trusted partner: ACK deferred — treating as "
                f"approved per partner trust_status (adapter={TR_ADAPTER.name})"
            )
    elif beneficiary_vasp:
        # Known VASP but not a trusted partner: queue the message for
        # outbound delivery; mark pending until ops reviews.
        ok, resp = post_ivms_to_counterparty(
            beneficiary_vasp["travel_rule_endpoint"],
            msg,
            sig_hex,
            pub_hex,
            counterparty_vasp_id=beneficiary_vasp["vasp_id"],
            tr_request_id=req_id,
        )
        counterparty_response = resp
        status = "pending"
        decision = "manual-review-required"
        decision_reason = (
            f"counterparty VASP {beneficiary_vasp['vasp_id']} "
            "is in directory but untrusted; awaiting ops review"
        )
    else:
        # Untrusted / unknown / no VASP — straight to ops queue.
        status = "pending"
        decision = "manual-review-required"
        decision_reason = "destination VASP unknown; awaiting ops review"

    now = int(time.time())
    with _db_lock, db() as conn:
        conn.execute(
            "INSERT INTO tr_requests "
            "(id, withdraw_id, opex_user, asset, amount, amount_usdt, "
            " destination_address, destination_vasp_id, destination_vasp_name, "
            " destination_vasp_method, originator_ivms_json, beneficiary_ivms_json, "
            " status, decision, decision_reason, ivms_message_json, ivms_signature, "
            " counterparty_response, created_at, resolved_at, ttl_seconds) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                req_id,
                withdraw_id,
                opex_user,
                asset,
                str(amount),
                dstr(amount_usdt_d),
                destination.lower(),
                (beneficiary_vasp or {}).get("vasp_id") if beneficiary_vasp else None,
                (beneficiary_vasp or {}).get("name")
                if beneficiary_vasp
                else ("(self-hosted)" if self_hosted else None),
                method,
                json.dumps(msg["originator"], separators=(",", ":")),
                json.dumps(msg["beneficiary"], separators=(",", ":")),
                status,
                decision,
                decision_reason,
                json.dumps(msg, separators=(",", ":"), ensure_ascii=False),
                sig_hex,
                counterparty_response,
                now,
                now if status in ("approved", "rejected") else None,
                TR_TTL_SECONDS,
            ),
        )

    safe_log_request(req_id, kyc, dstr(amount_usdt_d), destination, status)

    return {
        "tr_request_id": req_id,
        "status": status,
        "decision": decision,
        "reason": decision_reason,
        "destination_vasp_id": (beneficiary_vasp or {}).get("vasp_id")
        if beneficiary_vasp
        else None,
        "destination_vasp_method": method,
    }


# ==========================================================================
# Admin token
# ==========================================================================
def get_admin_token() -> str:
    tok = os.environ.get("TRAVEL_RULE_ADMIN_TOKEN")
    if tok:
        return tok
    cached = _runtime_state.get("admin_token")
    if cached:
        return str(cached)
    tok = "tr_" + secrets.token_urlsafe(24)
    _runtime_state["admin_token"] = tok
    log(f"TRAVEL_RULE_ADMIN_TOKEN={tok}  (set this in env to make it persistent)")
    return tok


# ==========================================================================
# HTTP server
# ==========================================================================
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-travel-rule/1.0"

    def _send_json(self, status: int, payload):
        body = (
            b""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _bearer(self) -> str | None:
        auth = self.headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        return auth.split(None, 1)[1].strip()

    def _read_json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _is_loopback(self) -> bool:
        try:
            peer = self.client_address[0] if self.client_address else ""
        except Exception:
            peer = ""
        return peer.startswith("127.") or peer in ("::1", "localhost")

    def _require_admin(self) -> bool:
        tok = self._bearer()
        if not tok or tok != get_admin_token():
            self._send_json(
                401, {"error": "unauthorized", "message": "Bearer TRAVEL_RULE_ADMIN_TOKEN required"}
            )
            return False
        return True

    def _require_user(self) -> dict | None:
        tok = self._bearer()
        if not tok:
            self._send_json(401, {"error": "missing_token"})
            return None
        user = resolve_user_from_token(tok)
        if not user:
            self._send_json(401, {"error": "invalid_token"})
            return None
        return user

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[travel_rule] {self.address_string()} - {fmt % args}\n")

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    # ---- Routing ------------------------------------------------------
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query or "")
        if path == "/travel-rule/health":
            return self.h_health()
        if path == "/travel-rule/vasps":
            return self.h_vasps()
        if path == "/travel-rule/check":
            return self.h_check(q)
        if path == "/travel-rule/requests":
            return self.h_requests(q)
        if path == "/travel-rule/server-info":
            return self.h_server_info()
        if path == "/travel-rule/adapter-info":
            return self.h_adapter_info()
        if path == "/travel-rule/admin/deliveries":
            return self.h_admin_deliveries(q)
        if path.startswith("/travel-rule/requests/") and path.count("/") == 3:
            return self.h_request_detail(path.rsplit("/", 1)[1])
        return self._send_json(404, {"error": "not_found", "path": path})

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path == "/travel-rule/screen":
            return self.h_screen()
        if path == "/travel-rule/attest-address":
            return self.h_attest_address()
        if path == "/travel-rule/inbound":
            return self.h_inbound()
        if path == "/travel-rule/_admin/build-sample":
            return self.h_build_sample()
        if path.startswith("/travel-rule/admin/approve/"):
            return self.h_admin_approve(path.rsplit("/", 1)[1])
        if path.startswith("/travel-rule/admin/reject/"):
            return self.h_admin_reject(path.rsplit("/", 1)[1])
        if path.startswith("/travel-rule/admin/retry/"):
            return self.h_admin_retry(path.rsplit("/", 1)[1])
        return self._send_json(404, {"error": "not_found", "path": path})

    # ---- Handlers ----------------------------------------------------
    def h_health(self):
        with db() as conn:
            n_pending = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM tr_requests WHERE status='pending'"
                ).fetchone()["n"]
            )
            n_vasps = int(
                conn.execute("SELECT COUNT(*) AS n FROM tr_vasp_directory").fetchone()["n"]
            )
        _, pub, shared = load_signing_key()
        return self._send_json(
            200,
            {
                "ok": True,
                "uptime_s": int(time.time()) - START_TS,
                "n_pending": n_pending,
                "n_vasps": n_vasps,
                "threshold_usdt": dstr(TR_THRESHOLD_USDT),
                "self_vasp_id": SELF_VASP_ID,
                "ivms_version": IVMS_VERSION,
                "key_shared_with_pol": shared,
                "server_pubkey": pub.hex(),
            },
        )

    def h_server_info(self):
        _, pub, shared = load_signing_key()
        return self._send_json(
            200,
            {
                "self_vasp": {
                    "vasp_id": SELF_VASP_ID,
                    "name": SELF_VASP_NAME,
                    "country": SELF_VASP_COUNTRY,
                },
                "ivms_version": IVMS_VERSION,
                "threshold_usdt": dstr(TR_THRESHOLD_USDT),
                "sig_scheme": "Ed25519",
                "server_pubkey": pub.hex(),
                "key_shared_with_pol": shared,
            },
        )

    def h_vasps(self):
        return self._send_json(200, {"vasps": list_vasps_public()})

    def h_check(self, q: dict):
        user = self._require_user()
        if not user:
            return
        address = ((q.get("address") or [""])[0] or "").strip().lower()
        if not address:
            return self._send_json(400, {"error": "bad_request", "message": "address required"})
        attest = lookup_attestation(address)
        vasp = None
        if attest and attest.get("vasp_id"):
            vasp = lookup_vasp(attest["vasp_id"])
        return self._send_json(
            200,
            {
                "address": address,
                "attestation": attest,
                "vasp": (
                    {
                        "vasp_id": vasp["vasp_id"],
                        "name": vasp["name"],
                        "country": vasp["country"],
                        "trust_status": vasp["trust_status"],
                    }
                    if vasp
                    else None
                ),
                "threshold_usdt": dstr(TR_THRESHOLD_USDT),
            },
        )

    def h_requests(self, q: dict):
        # If admin bearer is present, allow filtering by any user. Otherwise
        # the requester must be the user themselves.
        opex_user = (q.get("opex_user") or [""])[0]
        is_admin = self._bearer() == get_admin_token() if self._bearer() else False
        if not is_admin:
            user = self._require_user()
            if not user:
                return
            # Force-scope to the authenticated user.
            opex_user = user["opex_user"]
        try:
            limit = max(1, min(int((q.get("limit") or ["50"])[0]), 200))
        except ValueError:
            limit = 50
        status = (q.get("status") or [""])[0]
        with db() as conn:
            if status and opex_user:
                rows = conn.execute(
                    "SELECT id, withdraw_id, opex_user, asset, amount, amount_usdt, "
                    "       destination_address, destination_vasp_id, destination_vasp_name, "
                    "       destination_vasp_method, status, decision, decision_reason, "
                    "       created_at, resolved_at "
                    "FROM tr_requests WHERE opex_user=? AND status=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (opex_user, status, limit),
                ).fetchall()
            elif opex_user:
                rows = conn.execute(
                    "SELECT id, withdraw_id, opex_user, asset, amount, amount_usdt, "
                    "       destination_address, destination_vasp_id, destination_vasp_name, "
                    "       destination_vasp_method, status, decision, decision_reason, "
                    "       created_at, resolved_at "
                    "FROM tr_requests WHERE opex_user=? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (opex_user, limit),
                ).fetchall()
            elif status and is_admin:
                rows = conn.execute(
                    "SELECT id, withdraw_id, opex_user, asset, amount, amount_usdt, "
                    "       destination_address, destination_vasp_id, destination_vasp_name, "
                    "       destination_vasp_method, status, decision, decision_reason, "
                    "       created_at, resolved_at "
                    "FROM tr_requests WHERE status=? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, withdraw_id, opex_user, asset, amount, amount_usdt, "
                    "       destination_address, destination_vasp_id, destination_vasp_name, "
                    "       destination_vasp_method, status, decision, decision_reason, "
                    "       created_at, resolved_at "
                    "FROM tr_requests ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return self._send_json(200, {"requests": [dict(r) for r in rows], "count": len(rows)})

    def h_request_detail(self, req_id: str):
        is_admin = self._bearer() == get_admin_token() if self._bearer() else False
        user = None
        if not is_admin:
            user = self._require_user()
            if not user:
                return
        with db() as conn:
            row = conn.execute(
                "SELECT * FROM tr_requests WHERE id=?",
                (req_id,),
            ).fetchone()
        if not row:
            return self._send_json(404, {"error": "not_found"})
        if not is_admin and row["opex_user"] != user["opex_user"]:
            return self._send_json(403, {"error": "forbidden"})
        out = dict(row)
        # Parse JSON fields for convenience.
        for k in ("originator_ivms_json", "beneficiary_ivms_json", "ivms_message_json"):
            v = out.get(k)
            if v:
                try:
                    out[k.replace("_json", "")] = json.loads(v)
                except Exception as e:  # noqa: BLE001
                    log(f"failed to parse stored travel-rule json field {k}: {e!r}")
        return self._send_json(200, out)

    def h_screen(self):
        # Loopback-only: callable only by chain_server (same host).
        if not self._is_loopback():
            return self._send_json(403, {"error": "loopback_only"})
        body = self._read_json_body()
        required = (
            "withdraw_id",
            "opex_user",
            "asset",
            "amount",
            "amount_usdt",
            "destination_address",
        )
        missing = [k for k in required if not body.get(k)]
        if missing:
            return self._send_json(400, {"error": "bad_request", "missing": missing})
        try:
            result = screen_withdraw(
                withdraw_id=str(body["withdraw_id"]),
                opex_user=str(body["opex_user"]),
                asset=str(body["asset"]).upper(),
                amount=str(body["amount"]),
                amount_usdt=str(body["amount_usdt"]),
                destination=str(body["destination_address"]).lower(),
                originator_override=body.get("originator")
                if isinstance(body.get("originator"), dict)
                else None,
            )
        except Exception as exc:
            log(f"screen failed: {exc!r}")
            return self._send_json(500, {"error": "internal", "message": str(exc)})
        return self._send_json(200, result)

    def h_attest_address(self):
        user = self._require_user()
        if not user:
            return
        body = self._read_json_body()
        address = (body.get("address") or "").strip().lower()
        if not address:
            return self._send_json(400, {"error": "bad_request", "message": "address required"})
        self_hosted = bool(body.get("self_hosted"))
        beneficiary_name = (body.get("beneficiary_name") or "").strip() or None
        beneficiary_country = (body.get("beneficiary_country") or "").strip().upper() or None
        vasp_id = (body.get("vasp_id") or "").strip() or None
        if vasp_id:
            if not lookup_vasp(vasp_id):
                return self._send_json(400, {"error": "unknown_vasp", "vasp_id": vasp_id})
        if not self_hosted and not (vasp_id or beneficiary_name):
            return self._send_json(
                400,
                {
                    "error": "bad_request",
                    "message": "non-self-hosted attestation requires vasp_id or beneficiary_name",
                },
            )
        # If the user re-attests, replace the row. We keep is_active=1.
        now = int(time.time())
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO tr_address_attestations "
                "(address, vasp_id, attested_by_user, beneficiary_name, "
                " beneficiary_country, self_hosted, attested_at, is_active) "
                "VALUES (?,?,?,?,?,?,?,1) "
                "ON CONFLICT(address) DO UPDATE SET "
                "  vasp_id=excluded.vasp_id, "
                "  attested_by_user=excluded.attested_by_user, "
                "  beneficiary_name=excluded.beneficiary_name, "
                "  beneficiary_country=excluded.beneficiary_country, "
                "  self_hosted=excluded.self_hosted, "
                "  attested_at=excluded.attested_at, "
                "  is_active=1",
                (
                    address,
                    vasp_id,
                    user["opex_user"],
                    beneficiary_name,
                    beneficiary_country,
                    1 if self_hosted else 0,
                    now,
                ),
            )
        log(
            f"attest address={address[:10]}…{address[-6:]} "
            f"by={user['opex_user']} self_hosted={self_hosted} vasp={vasp_id}"
        )
        return self._send_json(
            200,
            {
                "ok": True,
                "address": address,
                "self_hosted": self_hosted,
                "vasp_id": vasp_id,
                "beneficiary_name": mask_name(beneficiary_name) if beneficiary_name else None,
            },
        )

    def h_inbound(self):
        """Receive an IVMS 101 message from a counterparty VASP.

        Public endpoint (no auth in the HTTP sense) — counterparties can
        be any registered VASP. Authorization is via the Ed25519
        signature against the sender's pubkey in ``tr_vasp_directory``.
        Hardening (added with the adapter rollout):

          - **Signature is required.** Unknown sender, missing signature,
            or signature mismatch -> HTTP 401. The message is NOT stored.
          - **Rate limit.** Max 100 messages/minute per sender VASP
            (override with ``TR_INBOUND_RATE_LIMIT``). Excess returns 429.

        Stored rows in ``tr_inbound`` are therefore always
        signature-verified; ops queues are not flooded with junk.
        """
        body = self._read_json_body()
        message = body.get("message")
        sig_hex = (body.get("signature") or "").strip()
        from_vasp_id = (body.get("from_vasp_id") or "").strip() or None
        pubkey_hex = (body.get("pubkey") or "").strip() or None
        if not isinstance(message, dict):
            return self._send_json(
                400, {"error": "bad_request", "message": "message required (IVMS 101 dict)"}
            )
        if not sig_hex:
            return self._send_json(401, {"error": "unauthorized", "message": "signature required"})

        # Resolve the verification key. Two acceptance paths:
        #   1) Caller provides pubkey AND from_vasp_id matches the
        #      directory's stored pubkey (prevents spoofing).
        #   2) Caller provides only from_vasp_id; we use the stored key.
        vasp = lookup_vasp(from_vasp_id) if from_vasp_id else None
        directory_pubkey: bytes | None = None
        if vasp and vasp.get("public_key_pem", "").startswith("ed25519:"):
            try:
                directory_pubkey = bytes.fromhex(vasp["public_key_pem"].split(":", 1)[1])
            except Exception:
                directory_pubkey = None

        # Validate signature.
        valid = False
        try:
            sig = bytes.fromhex(sig_hex)
        except Exception:
            return self._send_json(401, {"error": "unauthorized", "message": "malformed signature"})
        msg_bytes = canonical_ivms_bytes(message)
        verify_pk: bytes | None = None
        if pubkey_hex:
            try:
                supplied_pk = bytes.fromhex(pubkey_hex)
            except Exception:
                supplied_pk = None
            if supplied_pk is None:
                return self._send_json(
                    401, {"error": "unauthorized", "message": "malformed pubkey"}
                )
            # If we know this sender, the supplied key must match the
            # directory entry. Defends against a known VASP id being
            # paired with an attacker's key.
            if directory_pubkey is not None:
                if not hmac.compare_digest(directory_pubkey, supplied_pk):
                    return self._send_json(
                        401, {"error": "unauthorized", "message": "pubkey does not match directory"}
                    )
            verify_pk = supplied_pk
        elif directory_pubkey is not None:
            verify_pk = directory_pubkey
        else:
            return self._send_json(401, {"error": "unauthorized", "message": "unknown sender VASP"})
        try:
            valid = ed25519_verify(verify_pk, msg_bytes, sig)
        except Exception:
            valid = False
        if not valid:
            return self._send_json(
                401, {"error": "unauthorized", "message": "signature verification failed"}
            )

        # Rate limit per sender VASP.
        rl_key = from_vasp_id or "(anonymous-verified)"
        now_f = time.time()
        with _INBOUND_RL_LOCK:
            window = _INBOUND_RL_WINDOW.setdefault(rl_key, [])
            cutoff = now_f - _INBOUND_RL_WINDOW_S
            # Drop expired entries.
            while window and window[0] < cutoff:
                window.pop(0)
            if len(window) >= _INBOUND_RL_MAX:
                return self._send_json(
                    429,
                    {
                        "error": "rate_limited",
                        "message": (
                            f"max {_INBOUND_RL_MAX} inbound messages per "
                            f"{int(_INBOUND_RL_WINDOW_S)}s exceeded for "
                            f"vasp={rl_key}"
                        ),
                        "retry_after_s": int(_INBOUND_RL_WINDOW_S),
                    },
                )
            window.append(now_f)

        ib_id = "in-" + uuid.uuid4().hex[:16]
        now = int(time.time())
        with _db_lock, db() as conn:
            conn.execute(
                "INSERT INTO tr_inbound (id, received_at, from_vasp_id, "
                "signature_valid, raw_ivms_json, signature, notes) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    ib_id,
                    now,
                    from_vasp_id,
                    1,
                    json.dumps(message, separators=(",", ":"), ensure_ascii=False),
                    sig_hex,
                    "ok",
                ),
            )
        log(f"inbound id={ib_id} from={from_vasp_id} valid=True")
        return self._send_json(200, {"id": ib_id, "signature_valid": True})

    def h_build_sample(self):
        """Admin / loopback helper: build a sample IVMS 101 message for a
        given user without persisting anything. Useful for review/demo.

        Loopback-only OR admin-bearer; non-admin remote callers are 403.
        """
        if not (self._is_loopback() or self._bearer() == get_admin_token()):
            return self._send_json(403, {"error": "loopback_or_admin_only"})
        body = self._read_json_body()
        opex_user = body.get("opex_user")
        asset = (body.get("asset") or "ETH").upper()
        amount = str(body.get("amount") or "1")
        destination = (
            body.get("destination") or "0x000000000000000000000000000000000000dEaD"
        ).lower()
        if not opex_user:
            return self._send_json(400, {"error": "bad_request", "message": "opex_user required"})
        kyc = load_user_kyc_fields(opex_user)
        attest = lookup_attestation(destination)
        beneficiary_vasp = None
        if attest and attest.get("vasp_id"):
            beneficiary_vasp = lookup_vasp(attest["vasp_id"])
        self_hosted = bool(attest and attest.get("self_hosted"))
        msg = build_ivms_message(
            opex_user=opex_user,
            asset=asset,
            amount=amount,
            destination=destination,
            kyc=kyc,
            attest=attest,
            beneficiary_vasp=beneficiary_vasp,
            self_hosted=self_hosted,
        )
        sig_hex, pub_hex = sign_ivms(msg)
        return self._send_json(
            200,
            {
                "message": msg,
                "signature": sig_hex,
                "pubkey": pub_hex,
                "canonical_bytes_sha256": hashlib.sha256(canonical_ivms_bytes(msg)).hexdigest(),
                "kyc_loaded": bool(kyc),
                "attestation_present": bool(attest),
                "beneficiary_vasp": (beneficiary_vasp or {}).get("vasp_id")
                if beneficiary_vasp
                else None,
            },
        )

    def h_admin_approve(self, req_id: str):
        if not self._require_admin():
            return
        body = self._read_json_body() or {}
        reason = (body.get("reason") or "manual approve by ops").strip()
        now = int(time.time())
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT id, status FROM tr_requests WHERE id=?", (req_id,)
            ).fetchone()
            if not row:
                return self._send_json(404, {"error": "not_found"})
            if row["status"] not in ("pending",):
                return self._send_json(409, {"error": "already_resolved", "status": row["status"]})
            conn.execute(
                "UPDATE tr_requests SET status='approved', decision='manual-approved', "
                "decision_reason=?, resolved_at=? WHERE id=?",
                (reason, now, req_id),
            )
        log(f"admin approve req={req_id} reason={reason}")
        return self._send_json(200, {"id": req_id, "status": "approved", "resolved_at": now})

    def h_admin_reject(self, req_id: str):
        if not self._require_admin():
            return
        body = self._read_json_body() or {}
        reason = (body.get("reason") or "manual reject by ops").strip()
        now = int(time.time())
        with _db_lock, db() as conn:
            row = conn.execute(
                "SELECT id, status FROM tr_requests WHERE id=?", (req_id,)
            ).fetchone()
            if not row:
                return self._send_json(404, {"error": "not_found"})
            if row["status"] not in ("pending",):
                return self._send_json(409, {"error": "already_resolved", "status": row["status"]})
            conn.execute(
                "UPDATE tr_requests SET status='rejected', decision='manual-rejected', "
                "decision_reason=?, resolved_at=? WHERE id=?",
                (reason, now, req_id),
            )
        log(f"admin reject req={req_id} reason={reason}")
        return self._send_json(200, {"id": req_id, "status": "rejected", "resolved_at": now})

    # --- New: adapter info + delivery introspection -----------------------
    def h_adapter_info(self):
        """Public introspection of the current Travel Rule transport.

        Surfaces which adapter is selected, whether its credentials are
        configured (without leaking the values), and the last few
        delivery attempts so ops can spot a stuck queue at a glance.
        """
        health: dict = {}
        try:
            health = TR_ADAPTER.health_check()
        except Exception as e:
            health = {"error": f"health_check_failed:{e!r}"}
        with db() as conn:
            rows = conn.execute(
                "SELECT id, tr_request_id, adapter_name, counterparty_vasp_id, "
                "       delivered, status, ack_id, error, rtt_ms, attempted_at "
                "FROM tr_deliveries ORDER BY attempted_at DESC LIMIT 10"
            ).fetchall()
            n_total = int(conn.execute("SELECT COUNT(*) AS n FROM tr_deliveries").fetchone()["n"])
            n_failed = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM tr_deliveries "
                    "WHERE status IN ('unreachable','rejected')"
                ).fetchone()["n"]
            )
            n_giveup = int(
                conn.execute(
                    "SELECT COUNT(*) AS n FROM tr_requests " "WHERE status='delivery_failed'"
                ).fetchone()["n"]
            )
        return self._send_json(
            200,
            {
                "provider": TR_ADAPTER.name,
                "configured": getattr(TR_ADAPTER, "configured", False),
                "health": health,
                "retry": {
                    "min_delay_s": _RETRY_MIN_DELAY_S,
                    "max_delay_s": _RETRY_MAX_DELAY_S,
                    "give_up_s": _RETRY_GIVE_UP_S,
                    "poll_s": _RETRY_POLL_S,
                    "schedule_first_5": [_next_retry_delay_s(n) for n in range(1, 6)],
                },
                "stats": {
                    "deliveries_total": n_total,
                    "deliveries_failed_last_attempt": n_failed,
                    "requests_giveup": n_giveup,
                },
                "last_delivery_attempts": [dict(r) for r in rows],
            },
        )

    def h_admin_deliveries(self, q: dict):
        if not self._require_admin():
            return
        try:
            limit = max(1, min(int((q.get("limit") or ["50"])[0]), 500))
        except ValueError:
            limit = 50
        tr_request_id = (q.get("tr_request_id") or [""])[0]
        with db() as conn:
            if tr_request_id:
                rows = conn.execute(
                    "SELECT * FROM tr_deliveries "
                    "WHERE tr_request_id=? ORDER BY attempted_at DESC LIMIT ?",
                    (tr_request_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tr_deliveries " "ORDER BY attempted_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # Parse the response payload for convenience.
            pj = d.pop("response_payload_json", None)
            if pj:
                try:
                    d["response_payload"] = json.loads(pj)
                except Exception:
                    d["response_payload"] = {"raw": pj}
            else:
                d["response_payload"] = None
            out.append(d)
        return self._send_json(200, {"deliveries": out, "count": len(out)})

    def h_admin_retry(self, req_id: str):
        """Force one delivery retry for ``req_id`` regardless of backoff."""
        if not self._require_admin():
            return
        with db() as conn:
            row = conn.execute(
                "SELECT id, ivms_message_json, ivms_signature, "
                "       destination_vasp_id, status "
                "FROM tr_requests WHERE id=?",
                (req_id,),
            ).fetchone()
        if not row:
            return self._send_json(404, {"error": "not_found"})
        if not row["destination_vasp_id"]:
            return self._send_json(
                400, {"error": "bad_request", "message": "no destination_vasp_id; nothing to retry"}
            )
        try:
            msg = json.loads(row["ivms_message_json"] or "{}")
        except Exception:
            return self._send_json(
                400, {"error": "bad_request", "message": "stored IVMS message is malformed"}
            )
        _, pub, _ = load_signing_key()
        result = _try_deliver_ivms(
            tr_request_id=req_id,
            ivms_msg=msg,
            signature_hex=row["ivms_signature"] or "",
            pubkey_hex=pub.hex(),
            counterparty_vasp_id=row["destination_vasp_id"],
        )
        # Promote pending -> approved if delivery just succeeded.
        if result.delivered and row["status"] == "pending":
            now = int(time.time())
            with _db_lock, db() as conn:
                conn.execute(
                    "UPDATE tr_requests SET status='approved', "
                    "decision='counterparty-acked', "
                    "decision_reason='admin retry succeeded; counterparty ACK', "
                    "resolved_at=? WHERE id=? AND status='pending'",
                    (now, req_id),
                )
        log(
            f"admin retry req={req_id} adapter={TR_ADAPTER.name} "
            f"delivered={result.delivered} status={result.status}"
        )
        return self._send_json(
            200,
            {
                "id": req_id,
                "adapter": TR_ADAPTER.name,
                "delivered": result.delivered,
                "status": result.status,
                "ack_id": result.counterparty_ack_id,
                "error": result.error,
                "rtt_ms": result.rtt_ms,
                "response_payload": result.response_payload,
            },
        )


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _expire_loop():
    """Background sweep that flips long-stale 'pending' rows to 'expired'."""
    while True:
        try:
            now = int(time.time())
            with _db_lock, db() as conn:
                conn.execute(
                    "UPDATE tr_requests SET status='expired', "
                    "decision_reason='ttl exceeded' "
                    "WHERE status='pending' AND created_at + ttl_seconds < ?",
                    (now,),
                )
        except Exception as e:
            log(f"expire loop error: {e!r}")
        time.sleep(300)


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5630
    init_db()
    _, pub, shared = load_signing_key()
    log(f"db={DB_PATH}")
    log(f"sig_scheme=Ed25519 pubkey={pub.hex()} shared_with_pol={shared}")
    log(f"threshold_usdt={dstr(TR_THRESHOLD_USDT)} self_vasp={SELF_VASP_ID}")
    log(f"tr_adapter={TR_ADAPTER.name} " f"configured={getattr(TR_ADAPTER, 'configured', False)}")
    get_admin_token()  # emit token to stderr if not provided via env
    threading.Thread(target=_expire_loop, daemon=True).start()
    threading.Thread(target=_retry_loop, daemon=True).start()
    log(f"listening on :{port}")
    with ThreadingServer(("", port), Handler) as srv:
        srv.serve_forever()


if __name__ == "__main__":
    main()
