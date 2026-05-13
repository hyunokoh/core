#!/usr/bin/env python3
"""Real-time bridge: zkCEX wallet balances -> zkPoL ledger_change_event.

Polls the wallet API once per second for every user known to ``auth.db``,
diffs against the previous balance snapshot kept in ``tools/.local/zkpol_bridge.db``
(SQLite), and inserts a ``deposit`` or ``withdrawal`` row into the zkPoL
``ledger_change_event`` table for each balance change.

zkPoL's MariaDB sits inside the ``zkpol-mariadb`` Docker container; we shell
out to ``docker exec ... mariadb`` to run statements (no pip dependencies
required). Balances are scaled to 8-decimal-place integers to match the
precision the existing pol_server uses.

HTTP endpoints (port 5504, exposed by serve_homepage.py via /bridge/):
  GET  /bridge/health            -> public, last-tick stats
  POST /bridge/sync-now          -> public, force an immediate tick
  GET  /bridge/state?user=...    -> Bearer auth, returns per-asset state
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import socketserver
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from typing import Any

# --------------------------------------------------------------------------
# Paths & constants
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DIR = os.path.join(HERE, ".local")
os.makedirs(LOCAL_DIR, exist_ok=True)

AUTH_DB_PATH = os.path.join(LOCAL_DIR, "auth.db")
STATE_DB_PATH = os.path.join(LOCAL_DIR, "zkpol_bridge.db")


def _validated_http_url(raw_url: str, *, name: str = "url") -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    return raw_url


def _validated_http_base_url(name: str, raw_url: str) -> str:
    return _validated_http_url(raw_url, name=name).rstrip("/")


def _http_request(url: str, **kwargs) -> urllib.request.Request:
    return urllib.request.Request(_validated_http_url(url), **kwargs)  # noqa: S310


def _http_urlopen(target, **kwargs):
    if isinstance(target, str):
        target = _validated_http_url(target)
    return urllib.request.urlopen(target, **kwargs)  # noqa: S310


WALLET_BASE = _validated_http_base_url(
    "WALLET_BASE", os.environ.get("WALLET_BASE", "http://127.0.0.1:8091")
)
ZKPOL_BASE = _validated_http_base_url(
    "ZKPOL_BASE", os.environ.get("ZKPOL_BASE", "http://127.0.0.1:21011")
)
# anchor_indexer (port 5707) — best-effort POST so the on-chain explorer can
# show "pending" markers for changes whose CommitmentPosted hasn't landed yet.
ANCHOR_BASE = _validated_http_base_url(
    "ANCHOR_BASE", os.environ.get("ANCHOR_BASE", "http://127.0.0.1:5707")
)
MARIADB_CONTAINER = os.environ.get("ZKPOL_MARIADB_CONTAINER", "zkpol-mariadb")
MARIADB_USER = os.environ.get("ZKPOL_MARIADB_USER", "app")
MARIADB_PASSWORD = os.environ.get("ZKPOL_MARIADB_PASSWORD", "app-password")
MARIADB_DB = os.environ.get("ZKPOL_MARIADB_DB", "zk_pol")

# Tokens we mirror into zkPoL. Maps the wallet API's `asset` field to
# zkPoL's token_id. The wallet API returns assets like "ZETH" / "ZUSDT"
# (testnet wrappers), which we map to "ETH" / "USDT" in zkPoL.
ASSET_TO_TOKEN = {
    "ETH": "ETH",
    "ZETH": "ETH",
    "USDT": "USDT",
    "ZUSDT": "USDT",
}

POLL_INTERVAL = float(os.environ.get("BRIDGE_POLL_INTERVAL", "1.0"))
SCALE = 10**8  # 8-dp integer scaling matching pol_server


def log(msg: str) -> None:
    sys.stderr.write(f"[zkpol-bridge] {msg}\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------
# State store (SQLite)
# --------------------------------------------------------------------------
def _open_state_db() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_DB_PATH, timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bridge_state (
            opex_user      TEXT NOT NULL,
            asset          TEXT NOT NULL,
            balance_scaled INTEGER NOT NULL,
            last_event_at  INTEGER NOT NULL,
            PRIMARY KEY (opex_user, asset)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bridge_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    return conn


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO bridge_meta (key, value) VALUES (?, ?)",
        (key, value),
    )


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM bridge_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


# --------------------------------------------------------------------------
# Auth.db (read-only)
# --------------------------------------------------------------------------
@contextmanager
def _auth_db_ro():
    uri = f"file:{urllib.parse.quote(AUTH_DB_PATH)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _list_users() -> list[dict[str, Any]]:
    if not os.path.exists(AUTH_DB_PATH):
        return []
    try:
        with _auth_db_ro() as conn:
            rows = conn.execute(
                "SELECT id, opex_user, email FROM users "
                "WHERE opex_user IS NOT NULL AND opex_user != '' "
                "ORDER BY id ASC"
            ).fetchall()
        return [
            {"id": int(r["id"]), "opex_user": r["opex_user"], "email": r["email"]} for r in rows
        ]
    except sqlite3.OperationalError:
        return []


def _verify_bearer(token: str) -> dict[str, Any] | None:
    """Resolve an Authorization: Bearer <token> against auth.db sessions."""
    if not token or not os.path.exists(AUTH_DB_PATH):
        return None
    try:
        now = int(time.time())
        with _auth_db_ro() as conn:
            row = conn.execute(
                "SELECT u.id, u.opex_user, u.email "
                "FROM sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token=? AND s.expires_at > ?",
                (token, now),
            ).fetchone()
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "opex_user": row["opex_user"],
            "email": row["email"],
        }
    except sqlite3.OperationalError:
        return None


# --------------------------------------------------------------------------
# Wallet API
# --------------------------------------------------------------------------
def _fetch_wallets(opex_user: str) -> list[dict[str, Any]]:
    url = f"{WALLET_BASE}/v1/owner/{urllib.parse.quote(opex_user)}/wallets"
    try:
        with _http_urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:  # noqa: BLE001
        log(f"wallet fetch skipped for {opex_user}: {e!r}")
        return []


def _addr_key_for(opex_user: str) -> str:
    """Mirror zkPoL's ``address_key_bytes`` = sha256(account_id_bytes).

    Returns a 0x-prefixed 64-char hex string. The anchor_indexer uses the
    same recipe so pending changes get matched to on-chain CommitmentPosted
    events for the right account.
    """
    import hashlib

    return "0x" + hashlib.sha256(opex_user.encode("utf-8")).hexdigest()


def _log_pending_to_anchor(opex_user: str, asset: str, signed_delta: int) -> None:
    """Best-effort POST to anchor_indexer's loopback "log-pending" endpoint.

    A failure here must NOT break the bridge — the anchor service is optional
    and runs in degraded mode by default. We swallow every error.
    """
    try:
        body = json.dumps(
            {
                "opex_user": opex_user,
                "asset": asset,
                "delta": str(signed_delta),
                "expected_addr_key": _addr_key_for(opex_user),
            }
        ).encode("utf-8")
        req = _http_request(
            f"{ANCHOR_BASE}/anchor/internal/log-pending",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _http_urlopen(req, timeout=1.5) as resp:
            resp.read()  # discard
    except Exception as e:  # noqa: BLE001
        log(f"anchor pending marker skipped: {e!r}")


def _scale(balance_raw: Any) -> int:
    try:
        d = Decimal(str(balance_raw))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    if d < 0:
        return 0
    scaled = (d * Decimal(SCALE)).to_integral_value()
    n = int(scaled)
    return max(n, 0)


def _wallet_liability_scaled(wallet: dict) -> int:
    """Return total customer liability for one wallet row.

    The wallet API exposes available, locked, and pending-withdraw balances
    separately. zkPoL must track the user's total claim, so locked order funds
    and pending withdraws remain part of liabilities until they actually leave
    custody.
    """
    total = Decimal(0)
    for key in ("balance", "locked", "withdraw"):
        try:
            value = Decimal(str(wallet.get(key, 0) or 0))
        except (InvalidOperation, ValueError, TypeError):
            value = Decimal(0)
        if value > 0:
            total += value
    return _scale(total)


# --------------------------------------------------------------------------
# zkPoL DB writes (via docker exec mariadb CLI)
# --------------------------------------------------------------------------
class MariaDBUnavailable(RuntimeError):
    pass


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _mariadb_exec(sql: str, *, timeout: float = 5.0) -> str:
    """Run a SQL statement inside the zkpol-mariadb container.

    Returns stdout. Raises MariaDBUnavailable if docker / container missing.
    """
    if not _docker_available():
        raise MariaDBUnavailable("docker CLI not on PATH")
    docker = shutil.which("docker") or "docker"
    cmd = [
        docker,
        "exec",
        "-i",
        MARIADB_CONTAINER,
        "mariadb",
        f"-u{MARIADB_USER}",
        f"-p{MARIADB_PASSWORD}",
        "-D",
        MARIADB_DB,
        "--batch",
        "--skip-column-names",
        "-e",
        sql,
    ]
    try:
        out = subprocess.run(  # noqa: S603 - executable is resolved; SQL is constructed below.
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MariaDBUnavailable(f"mariadb exec timed out: {exc}") from exc
    except FileNotFoundError as exc:
        raise MariaDBUnavailable(f"docker not found: {exc}") from exc
    if out.returncode != 0:
        err = (out.stderr or "").strip()
        if "No such container" in err or "is not running" in err:
            raise MariaDBUnavailable(err or "container missing")
        raise MariaDBUnavailable(f"mariadb exec failed: {err}")
    return out.stdout or ""


_TRACE_COL_AVAILABLE: bool | None = None


def _has_trace_column() -> bool:
    """Detect once whether the ledger_change_event table has a transaction_trace_id column.

    Older mariadb volumes (pre-migration v12) may not have it; the bridge
    works on either schema by omitting the column when it's absent.
    """
    global _TRACE_COL_AVAILABLE
    if _TRACE_COL_AVAILABLE is not None:
        return _TRACE_COL_AVAILABLE
    try:
        out = _mariadb_exec("SHOW COLUMNS FROM ledger_change_event LIKE 'transaction_trace_id';")
        _TRACE_COL_AVAILABLE = "transaction_trace_id" in (out or "")
    except MariaDBUnavailable:
        _TRACE_COL_AVAILABLE = False
    return _TRACE_COL_AVAILABLE


def _mariadb_insert_event(
    *,
    account_id: str,
    token_id: str,
    balance_scaled: int,
    delta: int,
    event_type: str,
    occurred_at_iso: str,
    transaction_trace_id: str,
) -> int | None:
    """Insert a row into ledger_change_event. Returns the new id or None on failure."""

    # Use a HEX-escape-safe approach: we don't accept user-supplied strings
    # for token_id / event_type, but account_id is the user's opex_user.
    # opex_user is sanitized at signup (alnum+_-), so we still escape just in case.
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "''")

    if _has_trace_column():
        sql = (
            "INSERT INTO ledger_change_event "  # noqa: S608
            "(transaction_trace_id, account_id, token_id, balance, delta, event_type, occurred_at) "
            f"VALUES ('{esc(transaction_trace_id)}', '{esc(account_id)}', '{esc(token_id)}', "
            f"{int(balance_scaled)}, {int(delta)}, '{esc(event_type)}', '{esc(occurred_at_iso)}'); "
            "SELECT LAST_INSERT_ID();"
        )
    else:
        sql = (
            "INSERT INTO ledger_change_event "  # noqa: S608
            "(account_id, token_id, balance, delta, event_type, occurred_at) "
            f"VALUES ('{esc(account_id)}', '{esc(token_id)}', "
            f"{int(balance_scaled)}, {int(delta)}, '{esc(event_type)}', '{esc(occurred_at_iso)}'); "
            "SELECT LAST_INSERT_ID();"
        )
    try:
        out = _mariadb_exec(sql, timeout=6.0).strip()
    except MariaDBUnavailable:
        raise
    if not out:
        return None
    last_line = out.strip().splitlines()[-1].strip()
    try:
        return int(last_line)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Bridge tick (the main loop body)
# --------------------------------------------------------------------------
class TickStats:
    __slots__ = (
        "ok",
        "events_inserted",
        "users_tracked",
        "assets_tracked",
        "last_event_id",
        "last_tick_at",
        "last_error",
        "took_ms",
    )

    def __init__(self) -> None:
        self.ok = False
        self.events_inserted = 0
        self.users_tracked = 0
        self.assets_tracked = 0
        self.last_event_id: int | None = None
        self.last_tick_at: float = 0.0
        self.last_error: str | None = None
        self.took_ms: int = 0


_LAST_TICK = TickStats()
_TICK_LOCK = threading.Lock()


def _now_iso_3() -> str:
    """ISO-8601 UTC timestamp with millisecond precision (TIMESTAMP(3) friendly)."""
    t = time.gmtime()
    ms = int((time.time() % 1) * 1000)
    return time.strftime("%Y-%m-%d %H:%M:%S", t) + f".{ms:03d}"


def _tick(stats_target: TickStats | None = None) -> TickStats:
    """One bridge tick. Walks all users, diffs balances, writes events.

    Fail-soft: a single user's wallet fetch / DB write failure does NOT
    crash the loop. Errors are recorded in the per-tick stats.
    """
    started = time.time()
    target = stats_target or TickStats()
    target.events_inserted = 0
    target.last_error = None

    users = _list_users()
    target.users_tracked = len(users)

    if not users:
        target.ok = True
        target.last_tick_at = time.time()
        target.took_ms = int((time.time() - started) * 1000)
        return target

    state_conn = _open_state_db()
    try:
        # snapshot existing state into memory
        state: dict[tuple[str, str], int] = {}
        for r in state_conn.execute("SELECT opex_user, asset, balance_scaled FROM bridge_state"):
            state[(r["opex_user"], r["asset"])] = int(r["balance_scaled"])

        assets_seen: set[str] = set()
        last_event_id_in_tick: int | None = None
        mariadb_dead = False

        for u in users:
            opex = u["opex_user"]
            wallets = _fetch_wallets(opex)
            for w in wallets:
                asset_raw = (w.get("asset") or "").upper()
                token_id = ASSET_TO_TOKEN.get(asset_raw)
                if token_id is None:
                    continue
                assets_seen.add(token_id)
                bal_scaled = _wallet_liability_scaled(w)
                key = (opex, token_id)
                prev = state.get(key)
                if prev is None:
                    delta = bal_scaled  # initial seeding
                    if delta == 0:
                        # no event needed for a zero-balance user; still record state
                        state_conn.execute(
                            "INSERT OR REPLACE INTO bridge_state "
                            "(opex_user, asset, balance_scaled, last_event_at) VALUES (?,?,?,?)",
                            (opex, token_id, bal_scaled, int(time.time())),
                        )
                        state[key] = bal_scaled
                        continue
                    event_type = "deposit"
                elif bal_scaled == prev:
                    continue
                else:
                    delta = bal_scaled - prev
                    event_type = "deposit" if delta > 0 else "withdrawal"

                if mariadb_dead:
                    continue
                trace = f"bridge-{int(time.time()*1000)}-{opex}-{token_id}"
                # zkPoL derives the previous value as `balance - delta`.
                # Deposits therefore use a positive delta and withdrawals use
                # a negative delta; event_type is kept for audit readability.
                try:
                    ev_id = _mariadb_insert_event(
                        account_id=opex,
                        token_id=token_id,
                        balance_scaled=bal_scaled,
                        delta=delta,
                        event_type=event_type,
                        occurred_at_iso=_now_iso_3(),
                        transaction_trace_id=trace,
                    )
                except MariaDBUnavailable as exc:
                    target.last_error = f"mariadb_unavailable: {exc}"
                    mariadb_dead = True
                    continue
                if ev_id is not None:
                    last_event_id_in_tick = ev_id
                target.events_inserted += 1
                # Notify the anchor indexer so the UI can show this change
                # as "pending on-chain confirmation" until the matching
                # CommitmentPosted event lands.
                _log_pending_to_anchor(opex, token_id, delta)
                state_conn.execute(
                    "INSERT OR REPLACE INTO bridge_state "
                    "(opex_user, asset, balance_scaled, last_event_at) VALUES (?,?,?,?)",
                    (opex, token_id, bal_scaled, int(time.time())),
                )
                state[key] = bal_scaled

        target.assets_tracked = len(assets_seen)
        target.ok = not mariadb_dead
        if last_event_id_in_tick is not None:
            target.last_event_id = last_event_id_in_tick
            _meta_set(state_conn, "last_event_id", str(last_event_id_in_tick))
        else:
            saved = _meta_get(state_conn, "last_event_id")
            if saved:
                try:
                    target.last_event_id = int(saved)
                except ValueError:
                    pass
        target.last_tick_at = time.time()
        _meta_set(state_conn, "last_tick_at", str(target.last_tick_at))
    finally:
        state_conn.close()

    target.took_ms = int((time.time() - started) * 1000)
    return target


def _tick_safe() -> TickStats:
    with _TICK_LOCK:
        try:
            return _tick(_LAST_TICK)
        except Exception as exc:  # fail-soft
            _LAST_TICK.ok = False
            _LAST_TICK.last_error = f"{type(exc).__name__}: {exc}"
            _LAST_TICK.last_tick_at = time.time()
            return _LAST_TICK


def _bridge_loop(stop_event: threading.Event) -> None:
    sys.stderr.write(
        f"[zkpol-bridge] loop start interval={POLL_INTERVAL}s "
        f"wallet={WALLET_BASE} mariadb_container={MARIADB_CONTAINER}\n"
    )
    while not stop_event.is_set():
        _tick_safe()
        stop_event.wait(POLL_INTERVAL)


# --------------------------------------------------------------------------
# HTTP endpoints
# --------------------------------------------------------------------------
class BridgeHandler(http.server.BaseHTTPRequestHandler):
    server_version = "zkpol-bridge/1.0"

    def _json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _bearer(self) -> str | None:
        h = self.headers.get("Authorization") or ""
        if h.startswith("Bearer "):
            return h[7:].strip()
        return None

    def do_OPTIONS(self):  # noqa: N802 (stdlib API)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path, _, query = self.path.partition("?")
        # The reverse proxy at :5500 forwards as /bridge/<x>; but when called
        # directly we accept either /bridge/health or /health.
        suffix = path
        if suffix.startswith("/bridge/"):
            suffix = suffix[len("/bridge") :]  # leave the leading slash on the rest
        if suffix == "/health":
            stats = _LAST_TICK
            return self._json(
                200,
                {
                    "ok": stats.ok,
                    "last_tick_at": stats.last_tick_at,
                    "last_tick_at_iso": (
                        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stats.last_tick_at))
                        if stats.last_tick_at
                        else None
                    ),
                    "n_users_tracked": stats.users_tracked,
                    "n_assets": stats.assets_tracked,
                    "last_event_id": stats.last_event_id,
                    "took_ms": stats.took_ms,
                    "last_error": stats.last_error,
                    "wallet_base": WALLET_BASE,
                    "mariadb_container": MARIADB_CONTAINER,
                    "transport": "docker-exec-mariadb-cli",
                },
            )
        if suffix == "/state":
            tok = self._bearer()
            user = _verify_bearer(tok or "")
            if not user:
                return self._json(401, {"error": "unauthorized"})
            qs = urllib.parse.parse_qs(query)
            req_user = (qs.get("user") or [user["opex_user"]])[0]
            # Demo simplification: only allow self-lookup unless caller is admin (none defined).
            if req_user != user["opex_user"]:
                return self._json(403, {"error": "forbidden"})
            try:
                state_conn = _open_state_db()
                rows = state_conn.execute(
                    "SELECT asset, balance_scaled, last_event_at FROM bridge_state "
                    "WHERE opex_user=? ORDER BY asset ASC",
                    (req_user,),
                ).fetchall()
                state_conn.close()
            except sqlite3.OperationalError as exc:
                return self._json(500, {"error": "state_db", "message": str(exc)})
            return self._json(
                200,
                [
                    {
                        "asset": r["asset"],
                        "balance_scaled": int(r["balance_scaled"]),
                        "last_event_at": int(r["last_event_at"]),
                    }
                    for r in rows
                ],
            )
        return self._json(404, {"error": "not_found", "path": self.path})

    def do_POST(self):  # noqa: N802
        suffix = self.path.split("?", 1)[0]
        if suffix.startswith("/bridge/"):
            suffix = suffix[len("/bridge") :]
        if suffix == "/sync-now":
            t0 = time.time()
            stats = _tick_safe()
            return self._json(
                200,
                {
                    "ok": stats.ok,
                    "events_inserted": stats.events_inserted,
                    "took_ms": int((time.time() - t0) * 1000),
                    "last_event_id": stats.last_event_id,
                    "last_error": stats.last_error,
                    "n_users_tracked": stats.users_tracked,
                },
            )
        return self._json(404, {"error": "not_found", "path": self.path})

    def log_message(self, fmt, *args):  # quiet — match stdlib idiom
        sys.stderr.write(f"[zkpol-bridge] {self.address_string()} - {fmt % args}\n")


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5504
    stop = threading.Event()
    th = threading.Thread(target=_bridge_loop, args=(stop,), daemon=True)
    th.start()

    server = _ThreadingServer(("127.0.0.1", port), BridgeHandler)
    sys.stderr.write(
        f"[zkpol-bridge] listening on :{port}  (zkpol={ZKPOL_BASE} wallet={WALLET_BASE})\n"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("[zkpol-bridge] shutting down\n")
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
