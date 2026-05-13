#!/usr/bin/env python3
"""Backend-agnostic facade for the auth + KYC persistence layer.

Two implementations live behind ``AuthDB``:

* :class:`SqliteAuthDB`   - the historical default. Stdlib only.
* :class:`PostgresAuthDB` - opt-in production-track backend. Uses ``pg8000``,
  a pure-Python Postgres driver (no native bindings).

The choice is made at construction time via ``backend='sqlite'|'postgres'``
or, for the auth_server, the ``AUTH_DB_BACKEND`` environment variable.

Public surface (used by ``auth_server.py``):

    db = AuthDB(backend="sqlite" | "postgres")
    db.init_schema()
    db.ping() -> bool
    db.health() -> dict   # {backend, latency_ms, n_users, n_active_sessions}

    # Users
    db.get_user_by_email(email) -> dict | None
    db.get_user_by_id(user_id)  -> dict | None
    db.insert_user(...) -> dict           # raises EmailTaken on UNIQUE conflict
    db.update_user_opex(user_id, opex)    # bulk update by id
    db.update_user_kyc(user_id, **fields) # partial update of kyc_* fields
    db.set_user_kyc_status(user_id, status, *, only_if_not_verified=False)
    db.list_opex_users() -> list[str]     # used by /auth/users-for-snapshot

    # Sessions
    db.create_session(user_id, token, expires_at)  # also clears prior sessions
    db.lookup_session(token) -> dict | None        # joined user
    db.delete_session(token)
    db.delete_sessions_for_user(user_id)

    # KYC verifications
    db.create_kyc_verification(**fields)
    db.get_kyc_verification(vid) -> dict | None
    db.update_kyc_verification(vid, **fields)
    db.delete_kyc_verification(vid)
    db.delete_kyc_verifications_for_user(user_id)

    # Sumsub
    db.upsert_sumsub_applicant(...)
    db.get_sumsub_applicant_by_external(external_user_id)
    db.get_sumsub_applicant_by_id(applicant_id)
    db.update_sumsub_applicant_status(external_user_id, ...)
    db.insert_sumsub_webhook(...)

The two backends serve identical row dicts. Field names match the table
column names (so ``user["pw_salt"]`` works on either backend). Bytea/blob
payloads come back as ``bytes``.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Iterable
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(HERE, "auth_schema")

_AUTH_TABLE_COLUMNS: dict[str, set[str]] = {
    "users": {
        "id",
        "email",
        "pw_hash",
        "pw_salt",
        "name",
        "opex_user",
        "kyc_status",
        "kyc_verified_at",
        "kyc_name",
        "kyc_phone",
        "kyc_birth",
        "kyc_gender",
        "kyc_carrier",
        "created_at",
        "kyc_provider_request_id",
    },
    "sessions": {"token", "user_id", "created_at", "expires_at"},
    "kyc_verifications": {
        "id",
        "user_id",
        "code",
        "expires_at",
        "attempts",
        "carrier",
        "name",
        "rrn_front",
        "rrn_back1",
        "phone",
        "created_at",
        "sms_provider_message_id",
    },
    "sumsub_applicants": {
        "external_user_id",
        "applicant_id",
        "level_name",
        "created_at",
        "last_status",
        "last_review_answer",
        "last_synced_at",
    },
    "sumsub_webhooks": {
        "id",
        "applicant_id",
        "type",
        "body_json",
        "received_at",
        "signature_valid",
    },
    "geo_decisions": {"id", "ts", "ip_redacted", "country", "endpoint", "decision", "reason"},
    "totp_recovery_codes": {"id", "user_id", "code_hash", "code_salt", "used_at", "created_at"},
    "totp_attempts": {"id", "user_id", "attempted_at", "result", "ip_redacted"},
    "webauthn_credentials": {
        "id",
        "user_id",
        "credential_id",
        "public_key_cose_b64",
        "sign_count",
        "attestation_type",
        "aaguid",
        "transports",
        "device_name",
        "created_at",
        "last_used_at",
    },
    "webauthn_challenges": {
        "id",
        "challenge_b64",
        "purpose",
        "user_id",
        "email",
        "created_at",
        "expires_at",
        "used",
    },
}

_USER_KYC_UPDATE_COLUMNS = {
    "kyc_status",
    "kyc_verified_at",
    "kyc_name",
    "kyc_phone",
    "kyc_birth",
    "kyc_gender",
    "kyc_carrier",
    "kyc_provider_request_id",
}


def _checked_auth_table(table: str) -> str:
    if table not in _AUTH_TABLE_COLUMNS:
        raise ValueError(f"unsupported auth table: {table!r}")
    return table


def _checked_auth_columns(table: str, columns: Iterable[str]) -> list[str]:
    allowed = _AUTH_TABLE_COLUMNS[_checked_auth_table(table)]
    checked = []
    for column in columns:
        if column not in allowed:
            raise ValueError(f"unsupported auth column for {table}: {column!r}")
        checked.append(column)
    if not checked:
        raise ValueError("at least one auth column is required")
    return checked


def _checked_user_kyc_fields(fields: dict[str, Any]) -> list[str]:
    checked = []
    for column in fields:
        if column not in _USER_KYC_UPDATE_COLUMNS:
            raise ValueError(f"unsupported user KYC field: {column!r}")
        checked.append(column)
    return checked


class EmailTaken(Exception):
    """Raised when a UNIQUE(email) constraint fires on insert_user."""


# ============================================================================
# Public factory + base
# ============================================================================
def AuthDB(*, backend: str | None = None, **kwargs):  # noqa: N802 - factory
    """Construct the configured backend.

    ``backend`` defaults to ``$AUTH_DB_BACKEND`` and falls back to ``sqlite``.
    Extra kwargs (``sqlite_path`` / ``dsn``) override the env-driven defaults.

    Postgres read/write split (Patroni)
    -----------------------------------
    In a Patroni deployment, writes go to the leader (Service:
    ``patroni-primary``) and reads can be load-balanced across replicas
    (Service: ``patroni-replica``). This factory accepts an optional
    ``POSTGRES_READ_DSN`` env var (or ``read_dsn`` kwarg). When set, the
    Postgres backend will:

      * route ``ping`` / ``health`` / list-style read methods to the
        replica DSN, and
      * route everything else (INSERT/UPDATE/DELETE, session writes,
        anything with read-your-write semantics) to the primary DSN.

    Default behavior — only ``POSTGRES_DSN`` set — keeps the old single-DSN
    semantics so existing deployments are unchanged.

    See ``tools/deploy/k8s/patroni/README.md`` for the K8s Service names
    and the matching DSN format.
    """
    backend = (backend or os.environ.get("AUTH_DB_BACKEND") or "sqlite").lower()
    if backend in ("sqlite", "sqlite3"):
        return SqliteAuthDB(
            path=kwargs.get("sqlite_path")
            or os.environ.get("AUTH_SQLITE_PATH")
            or os.path.join(HERE, ".local", "auth.db"),
        )
    if backend in ("postgres", "postgresql", "pg"):
        return PostgresAuthDB(
            dsn=kwargs.get("dsn")
            or os.environ.get("POSTGRES_DSN")
            or os.environ.get("AUTH_POSTGRES_DSN")
            or "postgresql://app:app-password@127.0.0.1:5433/zkcex_auth",
            read_dsn=kwargs.get("read_dsn")
            or os.environ.get("POSTGRES_READ_DSN")
            or os.environ.get("AUTH_POSTGRES_READ_DSN"),
        )
    raise ValueError(f"unknown auth db backend: {backend!r}")


# ============================================================================
# SQLite backend
# ============================================================================
class SqliteAuthDB:
    """SQLite implementation. Same on-disk shape as the historical schema."""

    backend = "sqlite"

    def __init__(self, *, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Single global lock matches the auth_server's existing concurrency
        # model (BaseHTTPRequestHandler is per-thread; the original code took
        # a process-wide lock on every write). SQLite WAL allows concurrent
        # reads; the lock just keeps writes serialized within this process.
        self._lock = threading.Lock()

    # -------- low-level connection ----------
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    # -------- schema ----------
    def init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            with open(os.path.join(SCHEMA_DIR, "sqlite.sql"), encoding="utf-8") as f:
                conn.executescript(f.read())
            # Idempotent column additions for 2FA. SQLite ALTER has no IF
            # NOT EXISTS so we probe PRAGMA table_info first.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
            if "totp_secret_b32" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN totp_secret_b32 TEXT")
            if "totp_enabled" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN totp_enabled INTEGER NOT NULL DEFAULT 0")
            if "totp_enabled_at" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN totp_enabled_at INTEGER")
            if "totp_locked_until" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN totp_locked_until INTEGER")
            if "totp_last_counter" not in cols:
                conn.execute("ALTER TABLE users ADD COLUMN totp_last_counter INTEGER")
            if "totp_setup_attempts" not in cols:
                conn.execute(
                    "ALTER TABLE users ADD COLUMN totp_setup_attempts INTEGER NOT NULL DEFAULT 0"
                )

    # -------- health ----------
    def ping(self) -> bool:
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except Exception:  # noqa: BLE001
            return False

    def health(self) -> dict:
        t0 = time.perf_counter()
        with self._connect() as conn:
            conn.execute("SELECT 1").fetchone()
            n_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            n_sessions = conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE expires_at>?",
                (int(time.time()),),
            ).fetchone()[0]
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "backend": self.backend,
            "latency_ms": latency_ms,
            "n_users": int(n_users),
            "n_active_sessions": int(n_sessions),
        }

    # -------- users ----------
    def get_user_by_email(self, email: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            return _row_to_user(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return _row_to_user(row) if row else None

    def get_user_by_opex(self, opex_user: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE opex_user=?", (opex_user,)).fetchone()
            return _row_to_user(row) if row else None

    def insert_user(
        self, *, email: str, name: str, pw_hash: bytes, pw_salt: bytes, opex_user: str = ""
    ) -> dict:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            try:
                conn.execute("BEGIN")
                cur = conn.execute(
                    "INSERT INTO users (email, pw_hash, pw_salt, name, "
                    " opex_user, created_at) VALUES (?,?,?,?,?,?)",
                    (email, sqlite3.Binary(pw_hash), sqlite3.Binary(pw_salt), name, opex_user, now),
                )
                uid = cur.lastrowid
                if not opex_user:
                    opex_user = f"u-{uid}"
                    conn.execute(
                        "UPDATE users SET opex_user=? WHERE id=?",
                        (opex_user, uid),
                    )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError as e:
                conn.execute("ROLLBACK")
                raise EmailTaken(str(e)) from e
        return {
            "id": uid,
            "email": email,
            "name": name,
            "opex_user": opex_user,
            "kyc_status": "none",
            "created_at": now,
        }

    def update_user_opex(self, user_id: int, opex_user: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE users SET opex_user=? WHERE id=?", (opex_user, user_id))

    def update_user_kyc(self, user_id: int, **fields: Any) -> None:
        if not fields:
            return
        field_names = _checked_user_kyc_fields(fields)
        cols = ", ".join(f"{k}=?" for k in field_names)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE users SET {cols} WHERE id=?",  # noqa: S608 - fields are whitelisted.
                (*fields.values(), user_id),
            )

    def set_user_kyc_status(
        self, user_id: int, status: str, *, only_if_not_verified: bool = False
    ) -> None:
        with self._lock, self._connect() as conn:
            if only_if_not_verified:
                conn.execute(
                    "UPDATE users SET kyc_status=? " "WHERE id=? AND kyc_status!='verified'",
                    (status, user_id),
                )
            else:
                conn.execute(
                    "UPDATE users SET kyc_status=? WHERE id=?",
                    (status, user_id),
                )

    def list_opex_users(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT opex_user FROM users WHERE opex_user != '' " "ORDER BY id ASC"
            ).fetchall()
        return [r["opex_user"] for r in rows]

    # -------- sessions ----------
    def create_session(self, *, user_id: int, token: str, expires_at: int) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            conn.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) " "VALUES (?,?,?,?)",
                (token, user_id, now, expires_at),
            )

    def lookup_session(self, token: str) -> dict | None:
        if not token:
            return None
        now = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=? AND s.expires_at>?",
                (token, now),
            ).fetchone()
        return _row_to_user(row) if row else None

    def delete_session(self, token: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))

    def delete_sessions_for_user(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    # -------- KYC verifications ----------
    def create_kyc_verification(
        self,
        *,
        id: str,
        user_id: int,
        code: str,
        expires_at: int,
        carrier: str,
        name: str,
        rrn_front: str,
        rrn_back1: str,
        phone: str,
        sms_provider_message_id: str = "",
    ) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM kyc_verifications WHERE user_id=?", (user_id,))
            conn.execute(
                "INSERT INTO kyc_verifications (id, user_id, code, expires_at, "
                " attempts, carrier, name, rrn_front, rrn_back1, phone, "
                " created_at, sms_provider_message_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    id,
                    user_id,
                    code,
                    expires_at,
                    0,
                    carrier,
                    name,
                    rrn_front,
                    rrn_back1,
                    phone,
                    now,
                    sms_provider_message_id,
                ),
            )

    def get_kyc_verification(self, verification_id: str, user_id: int | None = None) -> dict | None:
        with self._connect() as conn:
            if user_id is None:
                row = conn.execute(
                    "SELECT * FROM kyc_verifications WHERE id=?",
                    (verification_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM kyc_verifications WHERE id=? AND user_id=?",
                    (verification_id, user_id),
                ).fetchone()
        return dict(row) if row else None

    def update_kyc_verification(self, verification_id: str, **fields: Any) -> None:
        if not fields:
            return
        field_names = _checked_auth_columns("kyc_verifications", fields)
        cols = ", ".join(f"{k}=?" for k in field_names)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE kyc_verifications SET {cols} WHERE id=?",  # noqa: S608
                (*fields.values(), verification_id),
            )

    def delete_kyc_verification(self, verification_id: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM kyc_verifications WHERE id=?", (verification_id,))

    def delete_kyc_verifications_for_user(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM kyc_verifications WHERE user_id=?", (user_id,))

    # -------- sumsub ----------
    def upsert_sumsub_applicant(
        self,
        *,
        external_user_id: str,
        applicant_id: str,
        level_name: str,
        created_at: int | None = None,
        last_synced_at: int | None = None,
    ) -> None:
        now = int(time.time())
        c_at = created_at if created_at is not None else now
        s_at = last_synced_at if last_synced_at is not None else now
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sumsub_applicants "
                "(external_user_id, applicant_id, level_name, created_at, "
                " last_synced_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(external_user_id) DO UPDATE SET "
                "  applicant_id=excluded.applicant_id, "
                "  level_name=excluded.level_name, "
                "  last_synced_at=excluded.last_synced_at",
                (external_user_id, applicant_id, level_name, c_at, s_at),
            )

    def get_sumsub_applicant_by_external(self, external_user_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sumsub_applicants WHERE external_user_id=?",
                (external_user_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_sumsub_applicant_by_id(self, applicant_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sumsub_applicants WHERE applicant_id=?",
                (applicant_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_sumsub_applicant_status(
        self,
        *,
        external_user_id: str,
        last_status: str,
        last_review_answer: str,
        last_synced_at: int,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE sumsub_applicants SET last_status=?, "
                " last_review_answer=?, last_synced_at=? "
                "WHERE external_user_id=?",
                (last_status, last_review_answer, last_synced_at, external_user_id),
            )

    def insert_sumsub_webhook(
        self,
        *,
        applicant_id: str,
        type: str,
        body_json: str,
        received_at: int,
        signature_valid: int,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO sumsub_webhooks "
                "(applicant_id, type, body_json, received_at, signature_valid) "
                "VALUES (?,?,?,?,?)",
                (applicant_id, type, body_json, received_at, signature_valid),
            )

    # -------- geo decisions ----------
    def insert_geo_decision(
        self,
        *,
        ts: int,
        ip_redacted: str,
        country: str | None,
        endpoint: str,
        decision: str,
        reason: str | None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO geo_decisions "
                "(ts, ip_redacted, country, endpoint, decision, reason) "
                "VALUES (?,?,?,?,?,?)",
                (ts, ip_redacted, country, endpoint, decision, reason),
            )

    def list_geo_decisions(self, *, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, ip_redacted, country, endpoint, decision, reason "
                "FROM geo_decisions ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # -------- 2FA / TOTP ----------
    def set_user_totp_secret(self, user_id: int, secret_b32: str) -> None:
        """Stash a pending secret. Does NOT flip totp_enabled."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_secret_b32=?, totp_enabled=0, "
                " totp_enabled_at=NULL, totp_setup_attempts=0, "
                " totp_last_counter=NULL WHERE id=?",
                (secret_b32, user_id),
            )
            conn.execute("DELETE FROM totp_recovery_codes WHERE user_id=?", (user_id,))

    def enable_user_totp(self, user_id: int, when: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_enabled=1, totp_enabled_at=?, "
                " totp_setup_attempts=0 WHERE id=?",
                (when, user_id),
            )

    def disable_user_totp(self, user_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_secret_b32=NULL, totp_enabled=0, "
                " totp_enabled_at=NULL, totp_locked_until=NULL, "
                " totp_last_counter=NULL, totp_setup_attempts=0 WHERE id=?",
                (user_id,),
            )
            conn.execute("DELETE FROM totp_recovery_codes WHERE user_id=?", (user_id,))

    def bump_totp_setup_attempts(self, user_id: int) -> int:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_setup_attempts=totp_setup_attempts+1 " "WHERE id=?",
                (user_id,),
            )
            row = conn.execute(
                "SELECT totp_setup_attempts FROM users WHERE id=?", (user_id,)
            ).fetchone()
        return int(row["totp_setup_attempts"]) if row else 0

    def wipe_user_totp_secret(self, user_id: int) -> None:
        """Wipe a pending (unverified) secret after too many setup misses."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_secret_b32=NULL, totp_setup_attempts=0 "
                "WHERE id=? AND totp_enabled=0",
                (user_id,),
            )

    def set_user_totp_last_counter(self, user_id: int, counter: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_last_counter=? WHERE id=?",
                (counter, user_id),
            )

    def set_user_totp_locked_until(self, user_id: int, ts: int | None) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE users SET totp_locked_until=? WHERE id=?",
                (ts, user_id),
            )

    def insert_recovery_codes(self, user_id: int, hashes: list[tuple[bytes, bytes]]) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM totp_recovery_codes WHERE user_id=?", (user_id,))
            for code_hash, code_salt in hashes:
                conn.execute(
                    "INSERT INTO totp_recovery_codes "
                    "(user_id, code_hash, code_salt, used_at, created_at) "
                    "VALUES (?,?,?,NULL,?)",
                    (user_id, sqlite3.Binary(code_hash), sqlite3.Binary(code_salt), now),
                )

    def list_recovery_codes(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, code_hash, code_salt, used_at "
                "FROM totp_recovery_codes WHERE user_id=? "
                "ORDER BY id ASC",
                (user_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("code_hash", "code_salt"):
                if isinstance(d[k], (memoryview, bytearray)):
                    d[k] = bytes(d[k])
            out.append(d)
        return out

    def mark_recovery_code_used(self, code_id: int, ts: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE totp_recovery_codes SET used_at=? WHERE id=?",
                (ts, code_id),
            )

    def count_unused_recovery_codes(self, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM totp_recovery_codes "
                "WHERE user_id=? AND used_at IS NULL",
                (user_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def insert_totp_attempt(
        self, *, user_id: int, ts: int, result: str, ip_redacted: str | None
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO totp_attempts "
                "(user_id, attempted_at, result, ip_redacted) "
                "VALUES (?,?,?,?)",
                (user_id, ts, result, ip_redacted),
            )

    def count_recent_failed_totp_attempts(self, user_id: int, since_ts: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM totp_attempts "
                "WHERE user_id=? AND attempted_at>=? "
                "AND result IN ('wrong','replay','expired')",
                (user_id, since_ts),
            ).fetchone()
        return int(row["c"]) if row else 0

    # -------- WebAuthn / passkeys ----------
    def insert_webauthn_challenge(
        self,
        *,
        challenge_b64: str,
        purpose: str,
        user_id: int | None,
        email: str | None,
        expires_at: int,
    ) -> None:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO webauthn_challenges "
                "(challenge_b64, purpose, user_id, email, created_at, expires_at, used) "
                "VALUES (?,?,?,?,?,?,0)",
                (challenge_b64, purpose, user_id, email, now, expires_at),
            )

    def take_webauthn_challenge(self, *, challenge_b64: str, purpose: str) -> dict | None:
        """Atomically look up + mark a challenge as used. Returns the row, or
        None if absent / already used / expired."""
        now = int(time.time())
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM webauthn_challenges "
                "WHERE challenge_b64=? AND purpose=? AND used=0 AND expires_at>?",
                (challenge_b64, purpose, now),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE webauthn_challenges SET used=1 WHERE id=?",
                (row["id"],),
            )
        return dict(row)

    def gc_webauthn_challenges(self) -> int:
        """Best-effort cleanup of expired challenges. Returns rows removed."""
        cutoff = int(time.time()) - 3600
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM webauthn_challenges WHERE expires_at<?",
                (cutoff,),
            )
            return cur.rowcount or 0

    def insert_webauthn_credential(
        self,
        *,
        user_id: int,
        credential_id: str,
        public_key_cose_b64: str,
        sign_count: int,
        attestation_type: str | None,
        aaguid: str | None,
        transports: str | None,
        device_name: str | None,
    ) -> dict:
        now = int(time.time())
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO webauthn_credentials "
                "(user_id, credential_id, public_key_cose_b64, sign_count, "
                " attestation_type, aaguid, transports, device_name, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    credential_id,
                    public_key_cose_b64,
                    int(sign_count or 0),
                    attestation_type,
                    aaguid,
                    transports,
                    device_name,
                    now,
                ),
            )
            cred_id = cur.lastrowid
        return {
            "id": cred_id,
            "user_id": user_id,
            "credential_id": credential_id,
            "device_name": device_name,
            "aaguid": aaguid,
            "transports": transports,
            "sign_count": int(sign_count or 0),
            "created_at": now,
            "last_used_at": None,
        }

    def list_webauthn_credentials(self, user_id: int) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM webauthn_credentials WHERE user_id=? " "ORDER BY id ASC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_webauthn_credential_by_credid(self, credential_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM webauthn_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_webauthn_credential_by_id(self, *, user_id: int, cred_pk: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM webauthn_credentials WHERE id=? AND user_id=?",
                (cred_pk, user_id),
            ).fetchone()
        return dict(row) if row else None

    def update_webauthn_sign_count(
        self, *, credential_id: str, sign_count: int, last_used_at: int
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE webauthn_credentials SET sign_count=?, last_used_at=? "
                "WHERE credential_id=?",
                (int(sign_count), int(last_used_at), credential_id),
            )

    def update_webauthn_device_name(self, *, user_id: int, cred_pk: int, device_name: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE webauthn_credentials SET device_name=? " "WHERE id=? AND user_id=?",
                (device_name, cred_pk, user_id),
            )
            return cur.rowcount or 0

    def delete_webauthn_credential(self, *, user_id: int, cred_pk: int) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM webauthn_credentials WHERE id=? AND user_id=?",
                (cred_pk, user_id),
            )
            return cur.rowcount or 0

    def count_webauthn_credentials(self, user_id: int) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM webauthn_credentials WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    # -------- migration helpers ----------
    def fetch_all(self, table: str) -> list[dict]:
        """Used by migrate_auth_db.py. Returns every row as a dict."""
        table_name = _checked_auth_table(table)
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM {table_name}").fetchall()  # noqa: S608
        return [dict(r) for r in rows]

    def truncate(self, table: str) -> None:
        table_name = _checked_auth_table(table)
        with self._lock, self._connect() as conn:
            conn.execute(f"DELETE FROM {table_name}")  # noqa: S608

    def insert_raw(self, table: str, row: dict) -> None:
        table_name = _checked_auth_table(table)
        cols = _checked_auth_columns(table_name, row.keys())
        ph = ", ".join(["?"] * len(cols))
        col_list = ", ".join(cols)
        # Normalize bytes for sqlite (we get back ``bytes`` from pg8000 BYTEA).
        vals = []
        for c in cols:
            v = row[c]
            if isinstance(v, (bytes, bytearray, memoryview)):
                vals.append(sqlite3.Binary(bytes(v)))
            else:
                vals.append(v)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO {table_name} ({col_list}) VALUES ({ph})",  # noqa: S608
                vals,
            )

    def reset_sequences(self) -> None:  # no-op for sqlite (rowid-driven).
        return


def _row_to_user(row) -> dict:
    """Coerce a sqlite3.Row from the users table into a plain dict.

    The schema has BLOB columns (pw_hash, pw_salt) — sqlite3.Row already gives
    bytes for those, so this is mostly a column-name copy.
    """
    if row is None:
        return None  # type: ignore[return-value]
    return {k: row[k] for k in row.keys()}


# ============================================================================
# Postgres backend
# ============================================================================
class PostgresAuthDB:
    """pg8000-backed implementation. Mirrors :class:`SqliteAuthDB`.

    Connection strategy: short-lived connections per call (mirrors the SQLite
    backend's ``conn = sqlite3.connect(...)`` pattern). PgBouncer in front of
    the primary makes this cheap in production. For the demo, the latency
    overhead is sub-millisecond on localhost.

    Read/write split (Patroni)
    --------------------------
    Pass ``read_dsn`` to point reads at a different host than writes. The
    intended K8s setup is ``patroni-primary`` (RW) and ``patroni-replica``
    (RO fanout). When ``read_dsn`` is None (the default), reads and writes
    share the same DSN.

    Only methods that are read-only AND tolerant of replication lag should
    use ``_read_connect``: ``ping``, ``health``, list-style enumerations
    (``list_opex_users``). Anything with read-your-write semantics
    (post-INSERT lookups, login flows checking the row just written) must
    still go through ``_connect`` against the primary or it will read
    stale state.
    """

    backend = "postgres"

    def __init__(self, *, dsn: str, read_dsn: str | None = None):
        # Lazy import so the SQLite path stays stdlib-only.
        try:
            import pg8000.dbapi  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "pg8000 is required for the postgres backend; "
                "install with: pip3 install --user pg8000"
            ) from e
        self._pg = pg8000.dbapi
        self._conn_kwargs = self._parse_dsn(dsn)
        # Read connection kwargs default to the primary's — flip to a
        # separate replica DSN only when explicitly configured. Equality
        # to the primary kwargs is fine here: pg8000 sees no difference.
        self._read_conn_kwargs = self._parse_dsn(read_dsn) if read_dsn else self._conn_kwargs
        self._lock = threading.Lock()
        self.dsn = dsn
        self.read_dsn = read_dsn

    # -------- DSN parsing ----------
    @staticmethod
    def _parse_dsn(dsn: str) -> dict:
        from urllib.parse import unquote, urlsplit

        u = urlsplit(dsn)
        if u.scheme not in ("postgres", "postgresql"):
            raise ValueError(f"not a postgres DSN: {dsn!r}")
        return {
            "user": unquote(u.username or "app"),
            "password": unquote(u.password or ""),
            "host": u.hostname or "127.0.0.1",
            "port": u.port or 5432,
            "database": (u.path or "/").lstrip("/") or "postgres",
        }

    # -------- low-level connection ----------
    def _connect(self):
        """Open a connection to the *writer* (Patroni: patroni-primary)."""
        conn = self._pg.connect(**self._conn_kwargs)
        conn.autocommit = True
        return conn

    def _read_connect(self):
        """Open a connection to the *reader* (Patroni: patroni-replica) if
        configured, otherwise the primary.

        Only use for queries that:
          * do not need to read-their-own-write, and
          * tolerate up to ``maximum_lag_on_failover`` (1 MiB WAL) of
            staleness — typically sub-second on a healthy cluster.
        """
        conn = self._pg.connect(**self._read_conn_kwargs)
        conn.autocommit = True
        return conn

    def _exec(self, sql: str, params: Iterable[Any] | None = None) -> None:
        with self._connect() as conn:
            cur = conn.cursor()
            if params:
                cur.execute(sql, tuple(params))
            else:
                cur.execute(sql)
            cur.close()

    def _fetchone(self, sql: str, params: Iterable[Any] | None = None) -> tuple | None:
        with self._connect() as conn:
            cur = conn.cursor()
            if params:
                cur.execute(sql, tuple(params))
            else:
                cur.execute(sql)
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
            cur.close()
        if row is None:
            return None
        return _zip_row(cols, row)

    def _fetchall(self, sql: str, params: Iterable[Any] | None = None) -> list[dict]:
        with self._connect() as conn:
            cur = conn.cursor()
            if params:
                cur.execute(sql, tuple(params))
            else:
                cur.execute(sql)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            cur.close()
        return [_zip_row(cols, r) for r in rows]

    # -------- schema ----------
    def init_schema(self) -> None:
        with open(os.path.join(SCHEMA_DIR, "postgres.sql"), encoding="utf-8") as f:
            sql = f.read()
        # pg8000 doesn't have an executescript; split on bare semicolons.
        # Our schema has no stored procedures so this is safe.
        with self._connect() as conn:
            cur = conn.cursor()
            for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                cur.execute(stmt)
            cur.close()

    # -------- health ----------
    def ping(self) -> bool:
        # Hit the replica when configured — verifies the read path is live
        # too, not just the primary. Falls back to primary when no read DSN.
        try:
            with self._read_connect() as conn:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    def health(self) -> dict:
        t0 = time.perf_counter()
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM users")
            n_users = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM sessions WHERE expires_at>%s",
                (int(time.time()),),
            )
            n_sessions = cur.fetchone()[0]
            cur.close()
        latency_ms = int((time.perf_counter() - t0) * 1000)
        return {
            "backend": self.backend,
            "latency_ms": latency_ms,
            "n_users": int(n_users),
            "n_active_sessions": int(n_sessions),
        }

    # -------- users ----------
    def get_user_by_email(self, email: str) -> dict | None:
        return _norm_user(self._fetchone("SELECT * FROM users WHERE email=%s", (email,)))

    def get_user_by_id(self, user_id: int) -> dict | None:
        return _norm_user(self._fetchone("SELECT * FROM users WHERE id=%s", (user_id,)))

    def get_user_by_opex(self, opex_user: str) -> dict | None:
        return _norm_user(self._fetchone("SELECT * FROM users WHERE opex_user=%s", (opex_user,)))

    def insert_user(
        self, *, email: str, name: str, pw_hash: bytes, pw_salt: bytes, opex_user: str = ""
    ) -> dict:
        now = int(time.time())
        try:
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO users (email, pw_hash, pw_salt, name, "
                    " opex_user, created_at) VALUES (%s,%s,%s,%s,%s,%s) "
                    "RETURNING id",
                    (email, bytes(pw_hash), bytes(pw_salt), name, opex_user, now),
                )
                uid = int(cur.fetchone()[0])
                if not opex_user:
                    opex_user = f"u-{uid}"
                    cur.execute(
                        "UPDATE users SET opex_user=%s WHERE id=%s",
                        (opex_user, uid),
                    )
                cur.close()
        except Exception as e:  # noqa: BLE001
            # pg8000 raises DatabaseError for unique violations; sniff sqlstate
            # 23505 from .args[0] which is a dict on pg8000.
            sqlstate = ""
            try:
                a0 = e.args[0]
                if isinstance(a0, dict):
                    sqlstate = a0.get("C") or a0.get("sqlstate") or ""
                else:
                    sqlstate = str(a0)
            except Exception:  # noqa: BLE001,S110
                pass
            if "23505" in sqlstate or "duplicate" in str(e).lower():
                raise EmailTaken(str(e)) from e
            raise
        return {
            "id": uid,
            "email": email,
            "name": name,
            "opex_user": opex_user,
            "kyc_status": "none",
            "created_at": now,
        }

    def update_user_opex(self, user_id: int, opex_user: str) -> None:
        self._exec(
            "UPDATE users SET opex_user=%s WHERE id=%s",
            (opex_user, user_id),
        )

    def update_user_kyc(self, user_id: int, **fields: Any) -> None:
        if not fields:
            return
        field_names = _checked_user_kyc_fields(fields)
        cols = ", ".join(f"{k}=%s" for k in field_names)
        self._exec(
            f"UPDATE users SET {cols} WHERE id=%s",  # noqa: S608 - fields are whitelisted.
            (*fields.values(), user_id),
        )

    def set_user_kyc_status(
        self, user_id: int, status: str, *, only_if_not_verified: bool = False
    ) -> None:
        if only_if_not_verified:
            self._exec(
                "UPDATE users SET kyc_status=%s " "WHERE id=%s AND kyc_status!='verified'",
                (status, user_id),
            )
        else:
            self._exec(
                "UPDATE users SET kyc_status=%s WHERE id=%s",
                (status, user_id),
            )

    def list_opex_users(self) -> list[str]:
        rows = self._fetchall(
            "SELECT opex_user FROM users WHERE opex_user != '' " "ORDER BY id ASC"
        )
        return [r["opex_user"] for r in rows]

    # -------- sessions ----------
    def create_session(self, *, user_id: int, token: str, expires_at: int) -> None:
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM sessions WHERE user_id=%s", (user_id,))
            cur.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) "
                "VALUES (%s,%s,%s,%s)",
                (token, user_id, now, expires_at),
            )
            cur.close()

    def lookup_session(self, token: str) -> dict | None:
        if not token:
            return None
        now = int(time.time())
        return _norm_user(
            self._fetchone(
                "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=%s AND s.expires_at>%s",
                (token, now),
            )
        )

    def delete_session(self, token: str) -> None:
        self._exec("DELETE FROM sessions WHERE token=%s", (token,))

    def delete_sessions_for_user(self, user_id: int) -> None:
        self._exec("DELETE FROM sessions WHERE user_id=%s", (user_id,))

    # -------- KYC verifications ----------
    def create_kyc_verification(
        self,
        *,
        id: str,
        user_id: int,
        code: str,
        expires_at: int,
        carrier: str,
        name: str,
        rrn_front: str,
        rrn_back1: str,
        phone: str,
        sms_provider_message_id: str = "",
    ) -> None:
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM kyc_verifications WHERE user_id=%s", (user_id,))
            cur.execute(
                "INSERT INTO kyc_verifications (id, user_id, code, expires_at, "
                " attempts, carrier, name, rrn_front, rrn_back1, phone, "
                " created_at, sms_provider_message_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    id,
                    user_id,
                    code,
                    expires_at,
                    0,
                    carrier,
                    name,
                    rrn_front,
                    rrn_back1,
                    phone,
                    now,
                    sms_provider_message_id,
                ),
            )
            cur.close()

    def get_kyc_verification(self, verification_id: str, user_id: int | None = None) -> dict | None:
        if user_id is None:
            return self._fetchone(
                "SELECT * FROM kyc_verifications WHERE id=%s",
                (verification_id,),
            )
        return self._fetchone(
            "SELECT * FROM kyc_verifications WHERE id=%s AND user_id=%s",
            (verification_id, user_id),
        )

    def update_kyc_verification(self, verification_id: str, **fields: Any) -> None:
        if not fields:
            return
        field_names = _checked_auth_columns("kyc_verifications", fields)
        cols = ", ".join(f"{k}=%s" for k in field_names)
        self._exec(
            f"UPDATE kyc_verifications SET {cols} WHERE id=%s",  # noqa: S608
            (*fields.values(), verification_id),
        )

    def delete_kyc_verification(self, verification_id: str) -> None:
        self._exec("DELETE FROM kyc_verifications WHERE id=%s", (verification_id,))

    def delete_kyc_verifications_for_user(self, user_id: int) -> None:
        self._exec("DELETE FROM kyc_verifications WHERE user_id=%s", (user_id,))

    # -------- sumsub ----------
    def upsert_sumsub_applicant(
        self,
        *,
        external_user_id: str,
        applicant_id: str,
        level_name: str,
        created_at: int | None = None,
        last_synced_at: int | None = None,
    ) -> None:
        now = int(time.time())
        c_at = created_at if created_at is not None else now
        s_at = last_synced_at if last_synced_at is not None else now
        self._exec(
            "INSERT INTO sumsub_applicants "
            "(external_user_id, applicant_id, level_name, created_at, "
            " last_synced_at) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT(external_user_id) DO UPDATE SET "
            "  applicant_id=excluded.applicant_id, "
            "  level_name=excluded.level_name, "
            "  last_synced_at=excluded.last_synced_at",
            (external_user_id, applicant_id, level_name, c_at, s_at),
        )

    def get_sumsub_applicant_by_external(self, external_user_id: str) -> dict | None:
        return self._fetchone(
            "SELECT * FROM sumsub_applicants WHERE external_user_id=%s",
            (external_user_id,),
        )

    def get_sumsub_applicant_by_id(self, applicant_id: str) -> dict | None:
        return self._fetchone(
            "SELECT * FROM sumsub_applicants WHERE applicant_id=%s",
            (applicant_id,),
        )

    def update_sumsub_applicant_status(
        self,
        *,
        external_user_id: str,
        last_status: str,
        last_review_answer: str,
        last_synced_at: int,
    ) -> None:
        self._exec(
            "UPDATE sumsub_applicants SET last_status=%s, "
            " last_review_answer=%s, last_synced_at=%s "
            "WHERE external_user_id=%s",
            (last_status, last_review_answer, last_synced_at, external_user_id),
        )

    def insert_sumsub_webhook(
        self,
        *,
        applicant_id: str,
        type: str,
        body_json: str,
        received_at: int,
        signature_valid: int,
    ) -> None:
        self._exec(
            "INSERT INTO sumsub_webhooks "
            "(applicant_id, type, body_json, received_at, signature_valid) "
            "VALUES (%s,%s,%s,%s,%s)",
            (applicant_id, type, body_json, received_at, signature_valid),
        )

    # -------- geo decisions ----------
    def insert_geo_decision(
        self,
        *,
        ts: int,
        ip_redacted: str,
        country: str | None,
        endpoint: str,
        decision: str,
        reason: str | None,
    ) -> None:
        self._exec(
            "INSERT INTO geo_decisions "
            "(ts, ip_redacted, country, endpoint, decision, reason) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (ts, ip_redacted, country, endpoint, decision, reason),
        )

    def list_geo_decisions(self, *, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        return self._fetchall(
            "SELECT ts, ip_redacted, country, endpoint, decision, reason "
            "FROM geo_decisions ORDER BY id DESC LIMIT %s",
            (limit,),
        )

    # -------- WebAuthn / passkeys ----------
    def insert_webauthn_challenge(
        self,
        *,
        challenge_b64: str,
        purpose: str,
        user_id: int | None,
        email: str | None,
        expires_at: int,
    ) -> None:
        now = int(time.time())
        self._exec(
            "INSERT INTO webauthn_challenges "
            "(challenge_b64, purpose, user_id, email, created_at, expires_at, used) "
            "VALUES (%s,%s,%s,%s,%s,%s,0)",
            (challenge_b64, purpose, user_id, email, now, expires_at),
        )

    def take_webauthn_challenge(self, *, challenge_b64: str, purpose: str) -> dict | None:
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT * FROM webauthn_challenges "
                "WHERE challenge_b64=%s AND purpose=%s AND used=0 AND expires_at>%s",
                (challenge_b64, purpose, now),
            )
            row = cur.fetchone()
            cols = [d[0] for d in cur.description] if cur.description else []
            if not row:
                cur.close()
                return None
            d = _zip_row(cols, row)
            cur.execute(
                "UPDATE webauthn_challenges SET used=1 WHERE id=%s",
                (d["id"],),
            )
            cur.close()
        return d

    def gc_webauthn_challenges(self) -> int:
        cutoff = int(time.time()) - 3600
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM webauthn_challenges WHERE expires_at<%s",
                (cutoff,),
            )
            n = cur.rowcount or 0
            cur.close()
        return n

    def insert_webauthn_credential(
        self,
        *,
        user_id: int,
        credential_id: str,
        public_key_cose_b64: str,
        sign_count: int,
        attestation_type: str | None,
        aaguid: str | None,
        transports: str | None,
        device_name: str | None,
    ) -> dict:
        now = int(time.time())
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO webauthn_credentials "
                "(user_id, credential_id, public_key_cose_b64, sign_count, "
                " attestation_type, aaguid, transports, device_name, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    user_id,
                    credential_id,
                    public_key_cose_b64,
                    int(sign_count or 0),
                    attestation_type,
                    aaguid,
                    transports,
                    device_name,
                    now,
                ),
            )
            cred_id = int(cur.fetchone()[0])
            cur.close()
        return {
            "id": cred_id,
            "user_id": user_id,
            "credential_id": credential_id,
            "device_name": device_name,
            "aaguid": aaguid,
            "transports": transports,
            "sign_count": int(sign_count or 0),
            "created_at": now,
            "last_used_at": None,
        }

    def list_webauthn_credentials(self, user_id: int) -> list[dict]:
        return self._fetchall(
            "SELECT * FROM webauthn_credentials WHERE user_id=%s ORDER BY id ASC",
            (user_id,),
        )

    def get_webauthn_credential_by_credid(self, credential_id: str) -> dict | None:
        return self._fetchone(
            "SELECT * FROM webauthn_credentials WHERE credential_id=%s",
            (credential_id,),
        )

    def get_webauthn_credential_by_id(self, *, user_id: int, cred_pk: int) -> dict | None:
        return self._fetchone(
            "SELECT * FROM webauthn_credentials WHERE id=%s AND user_id=%s",
            (cred_pk, user_id),
        )

    def update_webauthn_sign_count(
        self, *, credential_id: str, sign_count: int, last_used_at: int
    ) -> None:
        self._exec(
            "UPDATE webauthn_credentials SET sign_count=%s, last_used_at=%s "
            "WHERE credential_id=%s",
            (int(sign_count), int(last_used_at), credential_id),
        )

    def update_webauthn_device_name(self, *, user_id: int, cred_pk: int, device_name: str) -> int:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "UPDATE webauthn_credentials SET device_name=%s " "WHERE id=%s AND user_id=%s",
                (device_name, cred_pk, user_id),
            )
            n = cur.rowcount or 0
            cur.close()
        return n

    def delete_webauthn_credential(self, *, user_id: int, cred_pk: int) -> int:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM webauthn_credentials WHERE id=%s AND user_id=%s",
                (cred_pk, user_id),
            )
            n = cur.rowcount or 0
            cur.close()
        return n

    def count_webauthn_credentials(self, user_id: int) -> int:
        row = self._fetchone(
            "SELECT COUNT(*) AS c FROM webauthn_credentials WHERE user_id=%s",
            (user_id,),
        )
        return int(row["c"]) if row else 0

    # -------- migration helpers ----------
    def fetch_all(self, table: str) -> list[dict]:
        table_name = _checked_auth_table(table)
        return self._fetchall(f"SELECT * FROM {table_name}")  # noqa: S608

    def truncate(self, table: str) -> None:
        # CASCADE handles the FK from sessions -> users etc.
        table_name = _checked_auth_table(table)
        self._exec(f"TRUNCATE TABLE {table_name} RESTART IDENTITY CASCADE")  # noqa: S608

    def insert_raw(self, table: str, row: dict) -> None:
        table_name = _checked_auth_table(table)
        cols = _checked_auth_columns(table_name, row.keys())
        ph = ", ".join(["%s"] * len(cols))
        col_list = ", ".join(cols)
        vals: list[Any] = []
        for c in cols:
            v = row[c]
            if isinstance(v, (bytes, bytearray, memoryview)):
                vals.append(bytes(v))
            else:
                vals.append(v)
        self._exec(
            f"INSERT INTO {table_name} ({col_list}) VALUES ({ph})",  # noqa: S608
            vals,
        )

    def reset_sequences(self) -> None:
        """Re-align IDENTITY sequences after a bulk insert that preserved IDs.

        Without this, the next INSERT that omits ``id`` would collide with an
        existing row. We bump the sequence to MAX(id)+1 for every IDENTITY
        column we know about.
        """
        for table, col in (("users", "id"), ("sumsub_webhooks", "id")):
            table_name = _checked_auth_table(table)
            column_name = _checked_auth_columns(table_name, (col,))[0]
            with self._connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT COALESCE(MAX({column_name}), 0) FROM {table_name}"  # noqa: S608
                )
                m = int(cur.fetchone()[0])
                # pg_get_serial_sequence works for IDENTITY columns too.
                cur.execute(
                    "SELECT pg_get_serial_sequence(%s, %s)",
                    (table, col),
                )
                seq = cur.fetchone()[0]
                if seq:
                    cur.execute(
                        "SELECT setval(%s, %s, true)",
                        (seq, max(m, 1)),
                    )
                cur.close()


def _zip_row(cols: list[str], row: tuple) -> dict:
    return dict(zip(cols, row, strict=False))


def _norm_user(row: dict | None) -> dict | None:
    """Make Postgres row dicts compatible with the SQLite ones.

    pg8000 returns ``memoryview`` for BYTEA columns; the auth_server expects
    raw ``bytes``. Coerce both pw_hash and pw_salt.
    """
    if row is None:
        return None
    out = dict(row)
    for k in ("pw_hash", "pw_salt"):
        if k in out and isinstance(out[k], (memoryview, bytearray)):
            out[k] = bytes(out[k])
    return out


__all__ = [
    "AuthDB",
    "SqliteAuthDB",
    "PostgresAuthDB",
    "EmailTaken",
]
