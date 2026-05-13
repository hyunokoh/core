#!/usr/bin/env python3
"""Notification center service for the zkCEX demo.

Listens on :5691 by default and is reverse-proxied by ``serve_homepage.py``
under ``/notifications/``. Owns three things:

* a durable **in-app inbox** stored in SQLite
* a per-user **preferences matrix** (per-type x per-channel) plus digest /
  quiet-hours / verified email address
* a durable **email queue** with an outbox-mode default and SMTP / SES API
  back-ends, drained by a background worker

State lives in ``tools/.local/notifications.db``. The schema and routes are
documented in the README in the parent agent's brief. ``POST /notifications/
send`` is a loopback-only fan-out point that all sibling services call when
they have something worth telling the user about; everything else is bearer-
authenticated and scoped to the calling user.

The push fan-out side talks to push_server (``:5580``); the email side writes
RFC 822 files into ``.local/email_outbox/`` in stub mode, or speaks SMTP /
AWS SES SigV4 if configured. Quiet hours suppress *only* push delivery, not
in-app or email — financial / security events still hit your inbox; we just
don't buzz the phone at 2am.

The service is stdlib-only (smtplib + http.server + sqlite3) so it boots in
the same one-click way the rest of the demo does.
"""

from __future__ import annotations

import datetime as dt
import email.message
import email.utils
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import smtplib
import socketserver
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# --------------------------------------------------------------------------
# Config / paths
# --------------------------------------------------------------------------

DEFAULT_PORT = 5691
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("NOTIF_DB", os.path.join(HERE, ".local", "notifications.db"))
EMAIL_OUTBOX = os.environ.get("EMAIL_OUTBOX", os.path.join(HERE, ".local", "email_outbox"))
TEMPLATE_DIR = os.environ.get("NOTIF_TEMPLATE_DIR", os.path.join(HERE, "notification_templates"))


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


AUTH_BASE = _validated_http_base_url(
    "AUTH_BASE", os.environ.get("AUTH_BASE", "http://127.0.0.1:5501")
)
PUSH_BASE = _validated_http_base_url(
    "PUSH_BASE", os.environ.get("PUSH_BASE", "http://127.0.0.1:5580")
)
TICKER_BASE = _validated_http_base_url(
    "TICKER_BASE", os.environ.get("TICKER_BASE", "http://127.0.0.1:8094")
)
PUBLIC_BASE = _validated_http_base_url(
    "PUBLIC_BASE", os.environ.get("PUBLIC_BASE", "http://localhost:5500")
)

AUTH_TTL_SECONDS = 30
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")

EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "stub").strip().lower()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "noreply@zkcex.local").strip()
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "1").strip() != "0"

AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

VALID_TYPES = (
    "kyc_verified",
    "deposit_credited",
    "withdraw_submitted",
    "withdraw_confirmed",
    "order_filled",
    "price_alert",
    "security",
    "promo",
)
VALID_CATEGORIES = ("critical", "financial", "system", "marketing")

# Map notification.type -> preferences-table column prefix.
TYPE_TO_PREF = {
    "kyc_verified": "kyc",
    "deposit_credited": "deposit",
    "withdraw_submitted": "withdraw",
    "withdraw_confirmed": "withdraw",
    "order_filled": "order",
    "price_alert": "price_alert",
    "security": "security",
    "promo": "promo",
}

# Type -> {emoji-like glyph, jump-to URL} hints for the UI.
TYPE_HINT = {
    "kyc_verified": ("KYC", "/app/kyc.html"),
    "deposit_credited": ("DEP", "/app/wallet.html"),
    "withdraw_submitted": ("OUT", "/app/wallet.html"),
    "withdraw_confirmed": ("OUT", "/app/wallet.html"),
    "order_filled": ("ORD", "/app/wallet.html#orders"),
    "price_alert": ("PRC", "/app/trade.html"),
    "security": ("SEC", "/app/security.html"),
    "promo": ("PROMO", "/app/index.html"),
}

MAX_ACTIVE_PRICE_ALERTS = 20
ARCHIVE_RETENTION_DAYS = 90

# --------------------------------------------------------------------------
# Logging (PII-safe: redact email addresses)
# --------------------------------------------------------------------------

_EMAIL_RX = re.compile(r"([A-Za-z0-9._%+-])([A-Za-z0-9._%+-]*)(@[A-Za-z0-9.-]+)")


def _redact(s: str) -> str:
    return _EMAIL_RX.sub(lambda m: m.group(1) + "***" + m.group(3), s)


def log(*args: object) -> None:
    sys.stderr.write("[notif] " + _redact(" ".join(str(a) for a in args)) + "\n")


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------

_db_lock = threading.RLock()


def db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          opex_user TEXT NOT NULL,
          type TEXT NOT NULL,
          category TEXT NOT NULL,
          title TEXT NOT NULL,
          body TEXT NOT NULL,
          metadata_json TEXT,
          channel_inapp INTEGER NOT NULL DEFAULT 1,
          channel_push INTEGER NOT NULL DEFAULT 1,
          channel_email INTEGER NOT NULL DEFAULT 1,
          read_at INTEGER,
          archived_at INTEGER,
          created_at INTEGER NOT NULL,
          delivered_push_at INTEGER,
          delivered_email_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
          ON notifications(opex_user, read_at);
        CREATE INDEX IF NOT EXISTS idx_notifications_created
          ON notifications(created_at);

        CREATE TABLE IF NOT EXISTS notification_prefs (
          opex_user TEXT PRIMARY KEY,
          kyc_inapp INTEGER NOT NULL DEFAULT 1,
          kyc_push INTEGER NOT NULL DEFAULT 1,
          kyc_email INTEGER NOT NULL DEFAULT 1,
          deposit_inapp INTEGER NOT NULL DEFAULT 1,
          deposit_push INTEGER NOT NULL DEFAULT 1,
          deposit_email INTEGER NOT NULL DEFAULT 1,
          withdraw_inapp INTEGER NOT NULL DEFAULT 1,
          withdraw_push INTEGER NOT NULL DEFAULT 1,
          withdraw_email INTEGER NOT NULL DEFAULT 1,
          order_inapp INTEGER NOT NULL DEFAULT 1,
          order_push INTEGER NOT NULL DEFAULT 0,
          order_email INTEGER NOT NULL DEFAULT 0,
          price_alert_inapp INTEGER NOT NULL DEFAULT 1,
          price_alert_push INTEGER NOT NULL DEFAULT 1,
          price_alert_email INTEGER NOT NULL DEFAULT 0,
          security_inapp INTEGER NOT NULL DEFAULT 1,
          security_push INTEGER NOT NULL DEFAULT 1,
          security_email INTEGER NOT NULL DEFAULT 1,
          promo_inapp INTEGER NOT NULL DEFAULT 1,
          promo_push INTEGER NOT NULL DEFAULT 0,
          promo_email INTEGER NOT NULL DEFAULT 1,
          email_address TEXT,
          email_verified INTEGER NOT NULL DEFAULT 0,
          email_verify_token TEXT,
          email_verify_expires_at INTEGER,
          digest_frequency TEXT NOT NULL DEFAULT 'realtime',
          quiet_hours_start INTEGER,
          quiet_hours_end INTEGER,
          updated_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS email_queue (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          notification_id INTEGER NOT NULL,
          recipient_email TEXT NOT NULL,
          subject TEXT NOT NULL,
          body_html TEXT NOT NULL,
          body_text TEXT NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL,
          scheduled_for INTEGER NOT NULL,
          sent_at INTEGER,
          error TEXT,
          provider_message_id TEXT,
          FOREIGN KEY (notification_id) REFERENCES notifications(id)
        );
        CREATE INDEX IF NOT EXISTS idx_email_queue_status
          ON email_queue(status, scheduled_for);

        CREATE TABLE IF NOT EXISTS price_alerts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          opex_user TEXT NOT NULL,
          symbol TEXT NOT NULL,
          condition TEXT NOT NULL,
          threshold_price TEXT NOT NULL,
          status TEXT NOT NULL,
          triggered_at INTEGER,
          created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_price_alerts_active
          ON price_alerts(status, symbol);
        CREATE INDEX IF NOT EXISTS idx_price_alerts_user
          ON price_alerts(opex_user);
        """)


# --------------------------------------------------------------------------
# Bearer auth: validate against auth_server /auth/me (cached).
# --------------------------------------------------------------------------

_auth_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_auth_cache_lock = threading.Lock()


def resolve_user(token: str | None) -> dict | None:
    if not token:
        return None
    now = time.time()
    with _auth_cache_lock:
        hit = _auth_cache.get(token)
        if hit and now - hit[0] < AUTH_TTL_SECONDS:
            return hit[1]
    try:
        req = _http_request(
            f"{AUTH_BASE}/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _http_urlopen(req, timeout=4) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        log("auth/me", e.code)
        return None
    except Exception as e:
        log("auth/me transport error:", e)
        return None
    user = obj.get("user") or {}
    if not user.get("opex_user"):
        return None
    with _auth_cache_lock:
        _auth_cache[token] = (now, user)
    return user


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------


def _json(handler: http.server.BaseHTTPRequestHandler, status: int, body: Any) -> None:
    payload = json.dumps(body, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _read_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def _bearer(handler: http.server.BaseHTTPRequestHandler) -> str | None:
    h = handler.headers.get("Authorization") or ""
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


def _client_is_loopback(handler: http.server.BaseHTTPRequestHandler) -> bool:
    ip = handler.client_address[0] if handler.client_address else ""
    return ip in LOOPBACK_HOSTS or ip.startswith("127.") or ip == ""


# --------------------------------------------------------------------------
# Preferences
# --------------------------------------------------------------------------


_PREF_BOOL_COLS = [
    f"{p}_{ch}"
    for p in ("kyc", "deposit", "withdraw", "order", "price_alert", "security", "promo")
    for ch in ("inapp", "push", "email")
]


def ensure_prefs(opex_user: str) -> sqlite3.Row:
    """Insert default preferences row if missing, return the row."""
    with _db_lock, db() as c:
        row = c.execute(
            "SELECT * FROM notification_prefs WHERE opex_user=?", (opex_user,)
        ).fetchone()
        if row is None:
            now = int(time.time())
            c.execute(
                "INSERT INTO notification_prefs(opex_user, updated_at) VALUES (?, ?)",
                (opex_user, now),
            )
            row = c.execute(
                "SELECT * FROM notification_prefs WHERE opex_user=?", (opex_user,)
            ).fetchone()
    return row


def prefs_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # Don't leak the verification token to the client.
    d.pop("email_verify_token", None)
    d.pop("email_verify_expires_at", None)
    return d


def _allowed_channels(pref_row: sqlite3.Row, ntype: str, requested: list[str]) -> dict:
    """Apply user preferences to the requested channel set.

    Returns ``{inapp, push, email}`` booleans for what should actually be
    delivered. ``requested`` is the channel list the caller asked for
    (defaults to all three).
    """
    prefix = TYPE_TO_PREF.get(ntype, "promo")
    asked = (
        {c.strip().lower() for c in requested}
        if requested
        else {
            "inapp",
            "push",
            "email",
        }
    )
    return {
        "inapp": ("inapp" in asked) and bool(pref_row[f"{prefix}_inapp"]),
        "push": ("push" in asked) and bool(pref_row[f"{prefix}_push"]),
        "email": ("email" in asked) and bool(pref_row[f"{prefix}_email"]),
    }


# --------------------------------------------------------------------------
# Quiet hours: returns True if "now" sits inside the user's quiet window.
# --------------------------------------------------------------------------


def in_quiet_hours(pref_row: sqlite3.Row) -> bool:
    start = pref_row["quiet_hours_start"]
    end = pref_row["quiet_hours_end"]
    if start is None or end is None:
        return False
    try:
        start = int(start)
        end = int(end)
    except (TypeError, ValueError):
        return False
    if start == end:
        return False
    hour = dt.datetime.now().hour
    if start < end:
        return start <= hour < end
    # Wraps midnight, e.g. 22..7
    return hour >= start or hour < end


# --------------------------------------------------------------------------
# Push fan-out (best-effort, loopback to push_server).
# --------------------------------------------------------------------------


def _trigger_push(
    opex_user: str, notification_id: int, title: str, body: str, meta: dict, ntype: str
) -> None:
    if not opex_user:
        return
    _, jump_url = TYPE_HINT.get(ntype, (None, "/app/inbox.html"))
    payload = {
        "title": title,
        "body": body,
        "tag": f"notif-{ntype}",
        "data": {
            "url": jump_url,
            "notification_id": notification_id,
            "type": ntype,
            **(meta or {}),
        },
    }
    try:
        req = _http_request(
            f"{PUSH_BASE}/push/send",
            data=json.dumps({"opex_user": opex_user, "payload": payload}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        _http_urlopen(req, timeout=2).read()
    except Exception as exc:  # noqa: BLE001
        log("push fan-out skipped:", exc)


# --------------------------------------------------------------------------
# Email rendering + queue.
# --------------------------------------------------------------------------


def _read_template(name: str) -> str | None:
    path = os.path.join(TEMPLATE_DIR, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _safe_format(tmpl: str, meta: dict) -> str:
    """str.format with a sandbox dict so missing keys render as ''."""

    class _Safe(dict):
        def __missing__(self, key):  # noqa: D401
            return ""

    try:
        return tmpl.format_map(_Safe(meta or {}))
    except Exception:  # noqa: BLE001
        return tmpl


def render_email(ntype: str, title: str, body: str, meta: dict) -> tuple[str, str, str]:
    """Render ``(subject, body_text, body_html)`` for a notification.

    Templates live in ``tools/notification_templates/`` and are simple
    ``str.format``-style. Falls back to a generic envelope if no template
    exists for the type so a new notification type still produces a
    deliverable email (just less branded).
    """
    subj_t = _read_template(f"{ntype}.subject.txt")
    html_t = _read_template(f"{ntype}.html")
    text_t = _read_template(f"{ntype}.text")
    ctx = {"title": title, "body": body, **(meta or {})}
    if subj_t:
        subject = _safe_format(subj_t.strip(), ctx)
    else:
        subject = title
    if text_t:
        text = _safe_format(text_t, ctx)
    else:
        text = f"{title}\n\n{body}\n\n— zkCEX"
    if html_t:
        html = _safe_format(html_t, ctx)
    else:
        # generic, hand-rolled, no GitHub / competitor mentions.
        title_html = title.replace("<", "&lt;").replace(">", "&gt;")
        body_html = body.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
        html = (
            "<html><body style='font-family:Inter,Helvetica,sans-serif;"
            "background:#0b0f1a;color:#e6e9f0;padding:24px;'>"
            "<table style='max-width:560px;margin:0 auto;background:#11151f;"
            "border-radius:12px;padding:24px'>"
            "<tr><td><h1 style='margin:0 0 12px;font-size:20px;color:#e6e9f0'>"
            f"{title_html}</h1>"
            f"<p style='margin:0 0 16px;color:#9aa3b3;line-height:1.5'>{body_html}</p>"
            "<p style='font-size:11px;color:#5c6477;margin-top:24px'>"
            "You're receiving this email because your zkCEX account is signed up "
            "for this notification type. Manage your preferences at "
            f"<a href='{PUBLIC_BASE}/app/notification-settings.html' "
            "style='color:#2a55ff'>notification settings</a>."
            "</p></td></tr></table></body></html>"
        )
    return subject, text, html


def queue_email(
    notification_id: int,
    recipient: str,
    subject: str,
    body_text: str,
    body_html: str,
    scheduled_for: int,
) -> int:
    if not recipient:
        return 0
    with _db_lock, db() as c:
        cur = c.execute(
            "INSERT INTO email_queue(notification_id, recipient_email, subject,"
            " body_html, body_text, status, scheduled_for)"
            " VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (notification_id, recipient, subject, body_html, body_text, scheduled_for),
        )
        return cur.lastrowid


# --------------------------------------------------------------------------
# Email send backends
# --------------------------------------------------------------------------


def _build_mime(recipient: str, subject: str, body_text: str, body_html: str) -> bytes:
    msg = email.message.EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="zkcex.local")
    msg["List-Unsubscribe"] = f"<{PUBLIC_BASE}/app/notification-settings.html>"
    msg.set_content(body_text)
    msg.add_alternative(body_html, subtype="html")
    return msg.as_bytes()


def _send_via_stub(qid: int, recipient: str, subject: str, body_text: str, body_html: str) -> str:
    os.makedirs(EMAIL_OUTBOX, exist_ok=True)
    raw = _build_mime(recipient, subject, body_text, body_html)
    path = os.path.join(EMAIL_OUTBOX, f"{qid:06d}.eml")
    with open(path, "wb") as f:
        f.write(raw)
    return f"stub:{os.path.basename(path)}"


def _send_via_smtp(qid: int, recipient: str, subject: str, body_text: str, body_html: str) -> str:
    if not SMTP_HOST:
        raise RuntimeError("SMTP_HOST not configured")
    raw = _build_mime(recipient, subject, body_text, body_html)
    if SMTP_USE_TLS:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
    else:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
    try:
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.sendmail(SMTP_FROM, [recipient], raw)
    finally:
        try:
            smtp.quit()
        except smtplib.SMTPException:
            pass
    return f"smtp:{qid}"


def _ses_sigv4_send(qid: int, recipient: str, subject: str, body_text: str, body_html: str) -> str:
    """POST to AWS SES API (SendEmail) via SigV4 — stdlib-only.

    Not exercised in the demo (we default to stub). The implementation here
    is intentionally minimal: it's enough to demonstrate the auth path so an
    operator can flip ``EMAIL_PROVIDER=ses`` in production once their AWS
    credentials are wired in.
    """
    if not (AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY):
        raise RuntimeError("AWS credentials not configured")
    host = f"email.{AWS_REGION}.amazonaws.com"
    endpoint = f"https://{host}/"
    # SES Query API form-encoding.
    params = {
        "Action": "SendEmail",
        "Version": "2010-12-01",
        "Source": SMTP_FROM,
        "Destination.ToAddresses.member.1": recipient,
        "Message.Subject.Data": subject,
        "Message.Body.Text.Data": body_text,
        "Message.Body.Html.Data": body_html,
    }
    body = urllib.parse.urlencode(params)
    body_bytes = body.encode("utf-8")
    payload_hash = hashlib.sha256(body_bytes).hexdigest()
    now = dt.datetime.utcnow()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    canonical_headers = (
        f"content-type:application/x-www-form-urlencoded\nhost:{host}\nx-amz-date:{amz_date}\n"
    )
    signed_headers = "content-type;host;x-amz-date"
    canonical_request = f"POST\n/\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
    credential_scope = f"{date_stamp}/{AWS_REGION}/ses/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        + hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    )

    def _sign(k, v):
        return hmac.new(k, v.encode("utf-8"), hashlib.sha256).digest()

    k_date = _sign(("AWS4" + AWS_SECRET_ACCESS_KEY).encode("utf-8"), date_stamp)
    k_region = _sign(k_date, AWS_REGION)
    k_service = _sign(k_region, "ses")
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={AWS_ACCESS_KEY_ID}/{credential_scope},"
        f" SignedHeaders={signed_headers}, Signature={signature}"
    )
    req = _http_request(
        endpoint,
        data=body_bytes,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Host": host,
            "X-Amz-Date": amz_date,
            "Authorization": auth_header,
        },
    )
    with _http_urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    # SES returns an XML wrapper with a MessageId; we just stash a substring.
    m = re.search(r"<MessageId>(.+?)</MessageId>", raw)
    return f"ses:{m.group(1) if m else qid}"


def send_one(qid: int, recipient: str, subject: str, body_text: str, body_html: str) -> str:
    if EMAIL_PROVIDER == "smtp":
        return _send_via_smtp(qid, recipient, subject, body_text, body_html)
    if EMAIL_PROVIDER == "ses":
        return _ses_sigv4_send(qid, recipient, subject, body_text, body_html)
    return _send_via_stub(qid, recipient, subject, body_text, body_html)


# --------------------------------------------------------------------------
# Email queue worker — drain every 10s.
# --------------------------------------------------------------------------


_BACKOFF_BASE = 30  # seconds; 30, 60, 120, 240, 480 then 'failed'
_MAX_ATTEMPTS = 5


def email_worker_loop() -> None:
    log("email worker started (provider=" + EMAIL_PROVIDER + ")")
    while True:
        try:
            _email_worker_tick()
        except Exception as exc:  # noqa: BLE001
            log("email worker tick error:", exc)
        time.sleep(10)


def _email_worker_tick() -> None:
    now = int(time.time())
    with _db_lock, db() as c:
        rows = c.execute(
            "SELECT id, notification_id, recipient_email, subject, body_html, body_text,"
            "       attempt_count"
            "  FROM email_queue"
            " WHERE status='queued' AND scheduled_for <= ?"
            " ORDER BY id ASC LIMIT 50",
            (now,),
        ).fetchall()
    for r in rows:
        qid = r["id"]
        try:
            provider_msg = send_one(
                qid,
                r["recipient_email"],
                r["subject"],
                r["body_text"],
                r["body_html"],
            )
            with _db_lock, db() as c:
                c.execute(
                    "UPDATE email_queue SET status='sent', sent_at=?,"
                    " attempt_count=attempt_count+1, provider_message_id=?,"
                    " error=NULL WHERE id=?",
                    (int(time.time()), provider_msg, qid),
                )
                c.execute(
                    "UPDATE notifications SET delivered_email_at=? WHERE id=?",
                    (int(time.time()), r["notification_id"]),
                )
            log(f"email sent qid={qid} to={r['recipient_email']} via={provider_msg.split(':')[0]}")
        except Exception as exc:  # noqa: BLE001
            attempts = (r["attempt_count"] or 0) + 1
            err = str(exc)[:512]
            if attempts >= _MAX_ATTEMPTS:
                final_status = "failed"
                next_at = None
            else:
                final_status = "queued"
                next_at = int(time.time()) + _BACKOFF_BASE * (2 ** (attempts - 1))
            with _db_lock, db() as c:
                c.execute(
                    "UPDATE email_queue SET status=?, attempt_count=?,"
                    " scheduled_for=COALESCE(?, scheduled_for), error=?"
                    " WHERE id=?",
                    (final_status, attempts, next_at, err, qid),
                )
            log(
                f"email send fail qid={qid} attempt={attempts} status={final_status} err={err[:80]}"
            )


# --------------------------------------------------------------------------
# Price-alert watcher — poll /v3/ticker/24h every 5s; fire on threshold cross.
# --------------------------------------------------------------------------


def _fetch_ticker(symbol: str) -> float | None:
    try:
        url = f"{TICKER_BASE}/v3/ticker/24h?symbol={urllib.parse.quote(symbol)}"
        with _http_urlopen(url, timeout=4) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log("ticker fetch skipped:", e)
        return None
    if isinstance(obj, list) and obj:
        obj = obj[0]
    if not isinstance(obj, dict):
        return None
    try:
        return float(obj.get("lastPrice") or obj.get("price") or 0)
    except (TypeError, ValueError):
        return None


def price_alert_loop() -> None:
    log("price-alert watcher started")
    while True:
        try:
            _price_alert_tick()
        except Exception as exc:  # noqa: BLE001
            log("price-alert tick error:", exc)
        time.sleep(5)


def _price_alert_tick() -> None:
    with _db_lock, db() as c:
        rows = c.execute(
            "SELECT id, opex_user, symbol, condition, threshold_price"
            "  FROM price_alerts WHERE status='active'"
        ).fetchall()
    if not rows:
        return
    # Pull each distinct symbol once.
    seen: dict[str, float | None] = {}
    for r in rows:
        sym = r["symbol"]
        if sym not in seen:
            seen[sym] = _fetch_ticker(sym)
    for r in rows:
        last = seen.get(r["symbol"])
        if last is None:
            continue
        try:
            threshold = float(r["threshold_price"])
        except (TypeError, ValueError):
            continue
        crossed = (r["condition"] == "above" and last >= threshold) or (
            r["condition"] == "below" and last <= threshold
        )
        if not crossed:
            continue
        # Fire the notification + mark the alert triggered atomically.
        with _db_lock, db() as c:
            # Re-check inside the lock to avoid double-trigger races.
            row = c.execute("SELECT status FROM price_alerts WHERE id=?", (r["id"],)).fetchone()
            if not row or row["status"] != "active":
                continue
            c.execute(
                "UPDATE price_alerts SET status='triggered', triggered_at=? WHERE id=?",
                (int(time.time()), r["id"]),
            )
        _send_internal(
            opex_user=r["opex_user"],
            ntype="price_alert",
            category="financial",
            title=f"Price alert: {r['symbol']} {r['condition']} {r['threshold_price']}",
            body=f"{r['symbol']} is now {last:.4f} (your alert: {r['condition']} {r['threshold_price']}).",
            metadata={
                "symbol": r["symbol"],
                "condition": r["condition"],
                "threshold": r["threshold_price"],
                "last_price": f"{last:.6f}",
                "alert_id": r["id"],
            },
            channels=["inapp", "push", "email"],
        )


# --------------------------------------------------------------------------
# Core send path (used by /notifications/send and by the price-alert watcher).
# --------------------------------------------------------------------------


def _send_internal(
    opex_user: str,
    ntype: str,
    category: str,
    title: str,
    body: str,
    metadata: dict | None,
    channels: list[str] | None,
) -> dict:
    if ntype not in VALID_TYPES:
        raise ValueError(f"invalid type: {ntype}")
    if category not in VALID_CATEGORIES:
        raise ValueError(f"invalid category: {category}")
    metadata = metadata or {}
    channels = channels or ["inapp", "push", "email"]
    prefs = ensure_prefs(opex_user)
    used = _allowed_channels(prefs, ntype, channels)

    now = int(time.time())
    meta_json = json.dumps(metadata, ensure_ascii=False)[:4096]
    with _db_lock, db() as c:
        cur = c.execute(
            "INSERT INTO notifications(opex_user, type, category, title, body,"
            " metadata_json, channel_inapp, channel_push, channel_email, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                opex_user,
                ntype,
                category,
                title,
                body,
                meta_json,
                1 if used["inapp"] else 0,
                1 if used["push"] else 0,
                1 if used["email"] else 0,
                now,
            ),
        )
        nid = cur.lastrowid

    channels_used = [k for k, v in used.items() if v]

    # Push: respect quiet hours by delaying via the push_server. We just don't
    # fire right now; the in-app row is still visible.
    if used["push"] and not in_quiet_hours(prefs):
        _trigger_push(opex_user, nid, title, body, metadata, ntype)
        with _db_lock, db() as c:
            c.execute(
                "UPDATE notifications SET delivered_push_at=? WHERE id=?",
                (int(time.time()), nid),
            )

    # Email: queue if user has a verified address (or any address in stub mode).
    if used["email"]:
        recipient = (prefs["email_address"] or "").strip()
        verified = bool(prefs["email_verified"])
        # In stub mode (development) we still queue even if not verified — it
        # writes locally and is useful for QA. In SMTP/SES we require verified.
        ok_to_queue = bool(recipient) and (verified or EMAIL_PROVIDER == "stub")
        if ok_to_queue:
            subject, text, html = render_email(ntype, title, body, metadata)
            digest = (prefs["digest_frequency"] or "realtime").lower()
            if digest == "off":
                pass
            else:
                # 'hourly' / 'daily' just delays scheduling; the worker handles it.
                if digest == "hourly":
                    scheduled = int(time.time()) + 3600
                elif digest == "daily":
                    scheduled = int(time.time()) + 86400
                else:
                    scheduled = int(time.time())
                queue_email(nid, recipient, subject, text, html, scheduled)

    log(f"notif sent id={nid} user={opex_user} type={ntype} channels={channels_used}")
    return {"notification_id": nid, "channels_used": channels_used}


# --------------------------------------------------------------------------
# HTTP handlers.
# --------------------------------------------------------------------------


_ID_RX = re.compile(r"^/notifications/(\d+)/(read|archive)$")
_PRICE_DEL_RX = re.compile(r"^/notifications/price-alerts/(\d+)$")


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "zkcex-notif/1.0"

    def log_message(self, fmt, *args):
        log(self.client_address[0], fmt % args)

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/notifications/inbox":
            return self._inbox()
        if path == "/notifications/unread-count":
            return self._unread_count()
        if path == "/notifications/prefs":
            return self._get_prefs()
        if path == "/notifications/price-alerts":
            return self._list_price_alerts()
        if path == "/notifications/health":
            return _json(self, 200, {"ok": True, "provider": EMAIL_PROVIDER})
        return _json(self, 404, {"error": "not_found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/notifications/send":
            return self._send_loopback()
        if path == "/notifications/mark-all-read":
            return self._mark_all_read()
        if path == "/notifications/email/send-verification":
            return self._send_verification()
        if path == "/notifications/email/verify":
            return self._verify_email()
        if path == "/notifications/price-alerts":
            return self._create_price_alert()
        if path == "/notifications/test":
            return self._self_test()
        m = _ID_RX.match(path)
        if m:
            nid = int(m.group(1))
            verb = m.group(2)
            return self._mark(nid, verb)
        return _json(self, 404, {"error": "not_found"})

    def do_PATCH(self):  # noqa: N802
        if self.path.split("?", 1)[0] == "/notifications/prefs":
            return self._patch_prefs()
        return _json(self, 404, {"error": "not_found"})

    def do_DELETE(self):  # noqa: N802
        m = _PRICE_DEL_RX.match(self.path.split("?", 1)[0])
        if m:
            return self._cancel_price_alert(int(m.group(1)))
        return _json(self, 404, {"error": "not_found"})

    # ---- /notifications/send (loopback) -------------------------------
    def _send_loopback(self):
        if not _client_is_loopback(self):
            return _json(self, 403, {"error": "loopback_only"})
        body = _read_body(self)
        opex_user = (body.get("opex_user") or "").strip()
        ntype = (body.get("type") or "").strip()
        category = (body.get("category") or "system").strip()
        title = (body.get("title") or "").strip()[:256]
        bdy = (body.get("body") or "").strip()[:2048]
        metadata = body.get("metadata") or {}
        channels = body.get("channels") or ["inapp", "push", "email"]
        if not opex_user or not ntype or not title:
            return _json(self, 400, {"error": "missing_fields"})
        try:
            out = _send_internal(opex_user, ntype, category, title, bdy, metadata, channels)
        except ValueError as e:
            return _json(self, 400, {"error": str(e)})
        return _json(self, 200, out)

    # ---- /notifications/inbox ----------------------------------------
    def _inbox(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        try:
            limit = max(1, min(200, int(qs.get("limit", ["50"])[0])))
        except ValueError:
            limit = 50
        unread_only = qs.get("unread_only", ["false"])[0].lower() in ("1", "true", "yes")
        with _db_lock, db() as c:
            sql = (
                "SELECT id, type, category, title, body, metadata_json,"
                " channel_inapp, channel_push, channel_email,"
                " read_at, archived_at, created_at,"
                " delivered_push_at, delivered_email_at"
                " FROM notifications"
                " WHERE opex_user=? AND archived_at IS NULL"
            )
            if unread_only:
                sql += " AND read_at IS NULL"
            sql += " ORDER BY id DESC LIMIT ?"
            rows = c.execute(sql, (user["opex_user"], limit)).fetchall()
        out = []
        for r in rows:
            try:
                meta = json.loads(r["metadata_json"] or "{}")
            except Exception:
                meta = {}
            glyph, jump = TYPE_HINT.get(r["type"], ("?", "/app/inbox.html"))
            out.append(
                {
                    "id": r["id"],
                    "type": r["type"],
                    "category": r["category"],
                    "title": r["title"],
                    "body": r["body"],
                    "metadata": meta,
                    "read": r["read_at"] is not None,
                    "archived": r["archived_at"] is not None,
                    "channels": {
                        "inapp": bool(r["channel_inapp"]),
                        "push": bool(r["channel_push"]),
                        "email": bool(r["channel_email"]),
                    },
                    "delivered_push_at": r["delivered_push_at"],
                    "delivered_email_at": r["delivered_email_at"],
                    "created_at": r["created_at"],
                    "glyph": glyph,
                    "jump_url": jump,
                }
            )
        return _json(self, 200, {"notifications": out})

    # ---- /notifications/unread-count ---------------------------------
    def _unread_count(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        with _db_lock, db() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM notifications"
                " WHERE opex_user=? AND read_at IS NULL AND archived_at IS NULL"
                "   AND channel_inapp=1",
                (user["opex_user"],),
            ).fetchone()
        return _json(self, 200, {"count": int(row["n"] if row else 0)})

    # ---- /notifications/<id>/{read,archive} --------------------------
    def _mark(self, nid: int, verb: str):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        mark_columns = {"read": "read_at", "archive": "archived_at"}
        col = mark_columns.get(verb)
        if col is None:
            return _json(self, 400, {"error": "invalid_mark"})
        with _db_lock, db() as c:
            cur = c.execute(
                f"UPDATE notifications SET {col}=? WHERE id=? AND opex_user=?",  # noqa: S608
                (int(time.time()), nid, user["opex_user"]),
            )
            if cur.rowcount == 0:
                return _json(self, 404, {"error": "not_found"})
        return _json(self, 200, {"ok": True})

    # ---- /notifications/mark-all-read --------------------------------
    def _mark_all_read(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        with _db_lock, db() as c:
            cur = c.execute(
                "UPDATE notifications SET read_at=? WHERE opex_user=? AND read_at IS NULL",
                (int(time.time()), user["opex_user"]),
            )
        return _json(self, 200, {"ok": True, "updated": cur.rowcount})

    # ---- /notifications/prefs ----------------------------------------
    def _get_prefs(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        row = ensure_prefs(user["opex_user"])
        # If email_address is empty, propose the user's auth email as a
        # convenience default (client can persist it on first save).
        prefs = prefs_to_dict(row)
        if not (prefs.get("email_address") or "").strip() and user.get("email"):
            prefs["email_address_suggested"] = user.get("email")
        return _json(self, 200, {"prefs": prefs})

    def _patch_prefs(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        body = _read_body(self)
        ensure_prefs(user["opex_user"])
        updates: list[tuple[str, Any]] = []
        for col in _PREF_BOOL_COLS:
            if col in body:
                updates.append((col, 1 if bool(body[col]) else 0))
        if "email_address" in body:
            addr = (body.get("email_address") or "").strip().lower()
            if addr and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr):
                return _json(self, 400, {"error": "invalid_email"})
            updates.append(("email_address", addr or None))
            # Changing the address invalidates verification.
            updates.append(("email_verified", 0))
        if "digest_frequency" in body:
            v = (body.get("digest_frequency") or "realtime").strip().lower()
            if v not in ("realtime", "hourly", "daily", "off"):
                return _json(self, 400, {"error": "invalid_digest_frequency"})
            updates.append(("digest_frequency", v))
        if "quiet_hours_start" in body:
            qhs = body.get("quiet_hours_start")
            if qhs is None:
                updates.append(("quiet_hours_start", None))
            else:
                try:
                    h = int(qhs)
                    if not (0 <= h <= 23):
                        raise ValueError
                    updates.append(("quiet_hours_start", h))
                except (TypeError, ValueError):
                    return _json(self, 400, {"error": "invalid_quiet_hours"})
        if "quiet_hours_end" in body:
            qhe = body.get("quiet_hours_end")
            if qhe is None:
                updates.append(("quiet_hours_end", None))
            else:
                try:
                    h = int(qhe)
                    if not (0 <= h <= 23):
                        raise ValueError
                    updates.append(("quiet_hours_end", h))
                except (TypeError, ValueError):
                    return _json(self, 400, {"error": "invalid_quiet_hours"})
        if not updates:
            return _json(self, 400, {"error": "no_fields"})
        updates.append(("updated_at", int(time.time())))
        sql_set = ", ".join(f"{k}=?" for k, _ in updates)
        values = [v for _, v in updates] + [user["opex_user"]]
        with _db_lock, db() as c:
            c.execute(
                f"UPDATE notification_prefs SET {sql_set} WHERE opex_user=?",  # noqa: S608
                values,
            )
            row = c.execute(
                "SELECT * FROM notification_prefs WHERE opex_user=?",
                (user["opex_user"],),
            ).fetchone()
        return _json(self, 200, {"prefs": prefs_to_dict(row)})

    # ---- /notifications/email/send-verification ----------------------
    def _send_verification(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        row = ensure_prefs(user["opex_user"])
        addr = (row["email_address"] or "").strip()
        if not addr:
            return _json(self, 400, {"error": "no_email_set"})
        vtoken = secrets.token_urlsafe(24)
        expires = int(time.time()) + 24 * 3600
        with _db_lock, db() as c:
            c.execute(
                "UPDATE notification_prefs SET email_verify_token=?,"
                " email_verify_expires_at=?, updated_at=? WHERE opex_user=?",
                (vtoken, expires, int(time.time()), user["opex_user"]),
            )
        verify_url = f"{PUBLIC_BASE}/app/notification-settings.html?verify={vtoken}"
        subject = "Verify your zkCEX email address"
        text = (
            "Confirm your email so zkCEX can send you account notifications.\n\n"
            f"Click to verify: {verify_url}\n\nThis link expires in 24 hours.\n"
            "If you did not request this, you can ignore this email."
        )
        html = (
            "<html><body style='font-family:Inter,Helvetica,sans-serif;"
            "background:#0b0f1a;color:#e6e9f0;padding:24px;'>"
            "<table style='max-width:560px;margin:0 auto;background:#11151f;"
            "border-radius:12px;padding:24px'><tr><td>"
            "<h1 style='margin:0 0 12px;font-size:20px;color:#e6e9f0'>"
            "Verify your email address</h1>"
            "<p style='margin:0 0 16px;color:#9aa3b3;line-height:1.5'>"
            "Click the button below to confirm we can send you notifications "
            "for this zkCEX account.</p>"
            f"<p><a href='{verify_url}' style='display:inline-block;"
            "background:#2a55ff;color:#fff;padding:10px 20px;border-radius:8px;"
            "text-decoration:none;font-weight:600'>Verify email</a></p>"
            f"<p style='font-size:12px;color:#5c6477;margin-top:24px'>Or paste this URL into your browser:<br>{verify_url}</p>"
            "</td></tr></table></body></html>"
        )
        # Queue as a "system" verification email — bypasses the type prefs
        # gating because the user clicked the button.
        # We pin notification_id to NULL via a sentinel — we still need a row
        # in notifications for the FK to make sense, so we create a minimal
        # placeholder of category=system.
        with _db_lock, db() as c:
            cur = c.execute(
                "INSERT INTO notifications(opex_user, type, category, title, body,"
                " metadata_json, channel_inapp, channel_push, channel_email, created_at)"
                " VALUES (?, 'security', 'system', ?, ?, '{}', 0, 0, 1, ?)",
                (user["opex_user"], subject, "Verification email sent.", int(time.time())),
            )
            nid = cur.lastrowid
        queue_email(nid, addr, subject, text, html, int(time.time()))
        log(f"verification email queued user={user['opex_user']} to={addr}")
        return _json(self, 200, {"ok": True})

    def _verify_email(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        body = _read_body(self)
        v = (body.get("token") or "").strip()
        if not v:
            return _json(self, 400, {"error": "missing_token"})
        with _db_lock, db() as c:
            row = c.execute(
                "SELECT email_verify_token, email_verify_expires_at"
                " FROM notification_prefs WHERE opex_user=?",
                (user["opex_user"],),
            ).fetchone()
            if not row or not row["email_verify_token"]:
                return _json(self, 400, {"error": "no_pending_verification"})
            if not hmac.compare_digest(v, row["email_verify_token"]):
                return _json(self, 400, {"error": "invalid_token"})
            if int(row["email_verify_expires_at"] or 0) < int(time.time()):
                return _json(self, 400, {"error": "expired_token"})
            c.execute(
                "UPDATE notification_prefs SET email_verified=1,"
                " email_verify_token=NULL, email_verify_expires_at=NULL,"
                " updated_at=? WHERE opex_user=?",
                (int(time.time()), user["opex_user"]),
            )
        return _json(self, 200, {"ok": True})

    # ---- price alerts ------------------------------------------------
    def _create_price_alert(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        body = _read_body(self)
        symbol = (body.get("symbol") or "").strip().upper()
        condition = (body.get("condition") or "").strip().lower()
        threshold = (body.get("threshold_price") or "").strip()
        if not symbol or condition not in ("above", "below") or not threshold:
            return _json(self, 400, {"error": "missing_or_invalid_fields"})
        try:
            t = float(threshold)
            if t <= 0:
                raise ValueError
        except ValueError:
            return _json(self, 400, {"error": "invalid_threshold"})
        with _db_lock, db() as c:
            row = c.execute(
                "SELECT COUNT(*) AS n FROM price_alerts" " WHERE opex_user=? AND status='active'",
                (user["opex_user"],),
            ).fetchone()
            if int(row["n"]) >= MAX_ACTIVE_PRICE_ALERTS:
                return _json(
                    self, 429, {"error": "too_many_active_alerts", "max": MAX_ACTIVE_PRICE_ALERTS}
                )
            cur = c.execute(
                "INSERT INTO price_alerts(opex_user, symbol, condition,"
                " threshold_price, status, created_at) VALUES (?, ?, ?, ?, 'active', ?)",
                (user["opex_user"], symbol, condition, threshold, int(time.time())),
            )
            new_id = cur.lastrowid
        return _json(
            self,
            200,
            {
                "id": new_id,
                "symbol": symbol,
                "condition": condition,
                "threshold_price": threshold,
                "status": "active",
            },
        )

    def _list_price_alerts(self):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        with _db_lock, db() as c:
            rows = c.execute(
                "SELECT id, symbol, condition, threshold_price, status,"
                " triggered_at, created_at FROM price_alerts"
                " WHERE opex_user=? ORDER BY id DESC LIMIT 200",
                (user["opex_user"],),
            ).fetchall()
        return _json(self, 200, {"alerts": [dict(r) for r in rows]})

    def _cancel_price_alert(self, aid: int):
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        with _db_lock, db() as c:
            cur = c.execute(
                "UPDATE price_alerts SET status='cancelled'"
                " WHERE id=? AND opex_user=? AND status='active'",
                (aid, user["opex_user"]),
            )
            if cur.rowcount == 0:
                return _json(self, 404, {"error": "not_found"})
        return _json(self, 200, {"ok": True})

    # ---- /notifications/test (Bearer) — self-send -------------------
    def _self_test(self):
        """Issue a test notification to the calling user via the requested
        channels. Used by the settings page's "Send test in-app / push /
        email" buttons. We bypass the per-type prefs filter for this single
        call so a user can verify channels are wired even if their prefs
        currently have promo+order off."""
        token = _bearer(self)
        user = resolve_user(token)
        if not user:
            return _json(self, 401, {"error": "unauthorized"})
        body = _read_body(self)
        channel = (body.get("channel") or "inapp").strip().lower()
        if channel not in ("inapp", "push", "email"):
            return _json(self, 400, {"error": "invalid_channel"})
        # Insert a stub notification row regardless, with only the chosen
        # channel set to 1 so we can audit what was sent.
        opex = user["opex_user"]
        now = int(time.time())
        title = f"Test notification ({channel})"
        bdy = "If you can read this, the channel is working end-to-end."
        with _db_lock, db() as c:
            cur = c.execute(
                "INSERT INTO notifications(opex_user, type, category, title, body,"
                " metadata_json, channel_inapp, channel_push, channel_email, created_at)"
                " VALUES (?, 'security', 'system', ?, ?, '{}', ?, ?, ?, ?)",
                (
                    opex,
                    title,
                    bdy,
                    1 if channel == "inapp" else 0,
                    1 if channel == "push" else 0,
                    1 if channel == "email" else 0,
                    now,
                ),
            )
            nid = cur.lastrowid
        if channel == "push":
            _trigger_push(opex, nid, title, bdy, {}, "security")
        elif channel == "email":
            prefs = ensure_prefs(opex)
            recipient = (prefs["email_address"] or "").strip()
            if not recipient:
                return _json(self, 400, {"error": "no_email_set"})
            subject, text, html = render_email("security", title, bdy, {})
            queue_email(nid, recipient, subject, text, html, now)
        return _json(self, 200, {"ok": True, "notification_id": nid, "channel": channel})


# --------------------------------------------------------------------------
# Server bootstrap.
# --------------------------------------------------------------------------


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    init_db()
    os.makedirs(EMAIL_OUTBOX, exist_ok=True)
    threading.Thread(target=email_worker_loop, daemon=True).start()
    threading.Thread(target=price_alert_loop, daemon=True).start()
    with ThreadingServer(("", port), Handler) as srv:
        log(f"notification center listening on :{port}")
        log(f"  db        -> {DB_PATH}")
        log(f"  outbox    -> {EMAIL_OUTBOX}")
        log(f"  templates -> {TEMPLATE_DIR}")
        log(f"  provider  -> {EMAIL_PROVIDER}")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
