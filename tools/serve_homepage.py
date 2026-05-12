#!/usr/bin/env python3
"""Static homepage server with reverse-proxy for the live Binance-compatible API.

Serves /Users/hoh/Documents/Projects/zkCEX/core/homepage on the chosen port
and forwards /v3/*, /sapi/*, /api/* to http://127.0.0.1:8094 so the trading UI
in /app can call the live exchange backend without CORS pain.
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
ROOT = os.path.dirname(_HERE) + "/homepage"

# OpenTelemetry: this proxy is the entry point for browser traffic, so every
# trace tree originates here. Set service name BEFORE importing otel.
os.environ.setdefault("OTEL_SERVICE_NAME", "zkcex-proxy")
try:
    from otel.shim import install as _otel_install  # noqa: E402
    from otel.shim import server_span as _otel_server_span
except Exception:  # noqa: BLE001

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


# i18n: HTML responses get rewritten in-flight to the negotiated locale.
# The layer is lazy-imported so the proxy still boots in a degraded state
# (KO/EN bilingual pages, the pre-i18n behaviour) if the package is missing.
try:
    from i18n.locale import (
        SUPPORTED as I18N_SUPPORTED,
    )
    from i18n.locale import (
        negotiate as i18n_negotiate,
    )
    from i18n.locale import (
        parse_cookie_header as i18n_cookie,
    )
    from i18n.locale import (
        parse_query_lang as i18n_query_lang,
    )
    from i18n.postprocess import localize_html as i18n_localize

    I18N_ENABLED = True
except Exception as _i18n_exc:  # noqa: BLE001
    sys.stderr.write(f"[serve_homepage] i18n disabled: {_i18n_exc}\n")
    I18N_SUPPORTED = ["ko"]  # type: ignore[assignment]
    I18N_ENABLED = False

    def i18n_negotiate(*_a, **_kw):
        return "ko"  # type: ignore[misc]

    def i18n_cookie(*_a, **_kw):
        return None  # type: ignore[misc]

    def i18n_query_lang(*_a, **_kw):
        return None  # type: ignore[misc]

    def i18n_localize(body, _lang):
        return body  # type: ignore[misc]


# Path prefix -> upstream base. Longer prefixes win (sorted by length desc at lookup time).
# Backend ports come from docker-compose: api=8094, wallet=8091, matching-gateway=8093,
# accountant=8089, market=8096.
ROUTES = {
    "/v3/": "http://127.0.0.1:8094",  # Binance-compatible REST (signed)
    "/sapi/": "http://127.0.0.1:8094",
    "/api/": "http://127.0.0.1:8094",
    "/fapi/": "http://127.0.0.1:5590",  # perp_engine.py: USDT-margined perpetual futures
    "/v1/owner/": "http://127.0.0.1:8091",  # wallet ownership (balances)
    "/v1/deposit/": "http://127.0.0.1:8091",
    "/withdraw/": "http://127.0.0.1:8091",
    "/admin/withdraw/": "http://127.0.0.1:8091",
    "/deposit/": "http://127.0.0.1:8091",  # admin/test deposit (demo seed)
    "/order/": "http://127.0.0.1:8093",
    "/order": "http://127.0.0.1:8093",  # exact match for POST /order
    "/cancel/": "http://127.0.0.1:8093",
    "/internal/": "http://127.0.0.1:8091",
    # Demo-only auxiliary services run by other tools/ scripts.
    "/auth/": "http://127.0.0.1:5501",  # auth_server.py: signup/login/sessions
    "/kyc/": "http://127.0.0.1:5501",  # auth_server.py: Korean PASS-style KYC simulation
    "/chain/": "http://127.0.0.1:5502",  # chain_server.py: hardhat ramp on/off bridge
    "/pol/": "http://127.0.0.1:5503",  # pol_server.py: Proof-of-Liabilities snapshots
    "/zkpol/": "http://127.0.0.1:21011",  # upstream Rust zkPoL service (token-scoped reads)
    "/bridge/": "http://127.0.0.1:5504",  # zkpol_bridge.py (wallet -> ledger_change_event)
    "/anchor/": "http://127.0.0.1:5707",  # anchor_indexer.py (BulletinBoard.sol on-chain trace)
    "/pol-snapshot/": "http://127.0.0.1:21100",  # upstream Rust pol-snapshot-server (5-min Merkle-sum cert)
    "/pol-feed/": "http://127.0.0.1:5505",  # pol_snapshot_feed.py (ExchangeSnapshot pull source)
    "/ws/": "http://127.0.0.1:5510",  # ws_feed.py: market-data WebSocket (also serves /health)
    "/orders/": "http://127.0.0.1:5570",  # order_engine.py: stop-limit / OCO (5520 taken by custody-signer-0)
    "/export/": "http://127.0.0.1:5540",  # export_server.py: CSV report exports (no path rewrite)
    "/api-keys/": "http://127.0.0.1:5550",  # api_key_server.py: external HMAC API key issuance + verify
    "/mcp/": "http://127.0.0.1:5560",  # mcp_server.py: Model Context Protocol (JSON-RPC over HTTP+SSE)
    "/push/": "http://127.0.0.1:5580",  # push_server.py: PWA Web Push subscriptions
    "/mm/": "http://127.0.0.1:5600",  # mm_bot.py: demo market-maker (keeps order book non-empty)
    "/safu/": "http://127.0.0.1:5601",  # safu_server.py: customer protection fund ledger + attestation
    "/travel-rule/": "http://127.0.0.1:5630",  # travel_rule_server.py: FATF IVMS 101 messaging
    "/ops-api/": "http://127.0.0.1:5620",  # ops_server.py: compliance/ops console JSON API (operator-only)
    "/zk-trade/": "http://127.0.0.1:5660",  # zk_orderbook.py: commit-reveal FBA privacy trading
    "/errors/": "http://127.0.0.1:5690",  # error_collector.py: Sentry-style JS+Python error aggregator
    "/fees/": "http://127.0.0.1:5710",  # fee_engine.py: VIP tier fee discount ladder
    "/lending/": "http://127.0.0.1:5693",  # lending.py: earn / loan pool product (supply, borrow, liquidate)
    "/staking/": "http://127.0.0.1:5692",  # staking.py: Earn-style token staking (lock + accrue yield)
    "/referral/": "http://127.0.0.1:5695",  # referral.py: per-user codes + lifetime fee-share rewards
    "/notifications/": "http://127.0.0.1:5691",  # notification_center.py: in-app inbox + email queue + push fan-out
    "/nft/": "http://127.0.0.1:5694",  # nft_marketplace.py: ERC721 + ERC1155 listing / browse / buy / sell
}


def _log(msg: str) -> None:
    sys.stderr.write(f"[serve_homepage] {msg}\n")


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


# api_key_server.py base URL (used for the server-internal HMAC verify call).
API_KEY_VERIFY_URL = _validated_http_url("http://127.0.0.1:5550/api-keys/verify")
LISTEN_HOST = os.environ.get("HOMEPAGE_HOST", "127.0.0.1")

# Paths whose signed-request scope requirement differs by HTTP method.
# Read endpoints (GET) need 'read'; trade endpoints (POST/DELETE) need 'trade';
# withdraw endpoints (anywhere /withdraw appears under /v3 or /sapi) need
# 'withdraw'. Anything not explicitly mapped defaults to 'read'.
_TRADE_PATHS = ("/v3/order", "/v3/openOrders", "/v3/orderList", "/v3/order/test")


def _required_scope(method: str, path: str) -> str:
    """Return the scope an HMAC-signed request to ``path`` needs."""
    p = path.split("?", 1)[0]
    # Withdraw is the most restrictive. Match anywhere /withdraw appears under
    # /sapi/... (Binance's withdraw lives at /sapi/v1/capital/withdraw/apply).
    if p == "/v3/withdraw" or p.startswith("/v3/withdraw/"):
        return "withdraw"
    if p.startswith("/sapi/") and "/withdraw" in p:
        return "withdraw"
    if method.upper() in ("POST", "DELETE", "PUT"):
        for tp in _TRADE_PATHS:
            if p == tp or p.startswith(tp + "/"):
                return "trade"
        # /sapi/* mutating calls default to trade (e.g. transfer, sub-account).
        if p.startswith("/sapi/"):
            return "trade"
        # /fapi/* mutating calls (place/cancel order, set leverage, transfer
        # between spot and futures wallets) all need 'trade' scope.
        if p.startswith("/fapi/"):
            return "trade"
    # Anything that's a GET on /v3, /sapi, or /fapi (account, depth, klines,
    # myTrades, premiumIndex, positionRisk, …) -> read.
    return "read"


# Subpaths under /zkpol/ that the proxy will NOT forward, even if the upstream
# service implements them. We only expose the read endpoints we actually use.
# The blocklist is matched after stripping the /zkpol prefix; trailing-slash
# variants are handled in _zkpol_blocked() below.
_ZKPOL_ALLOW_PREFIXES = (
    "/health/",  # /zkpol/health/live, /zkpol/health/ready
    "/version",  # /zkpol/version
    "/tokens/",  # token-scoped endpoints (further-filtered below)
)
# Per-token endpoints we explicitly expose. Requests under /zkpol/tokens/<id>/...
# must match one of these tails (after the {token_id} segment).
_ZKPOL_TOKEN_ALLOW_TAILS = (
    "/summary",
    "/pipeline",
    "/batches",  # also matches /batches/<seq>... and /batches?...
    "/events/history",
    # /accounts/<address> and /accounts/<address>/verify — see _zkpol_blocked
)
# Route prefixes whose request path should be rewritten before forwarding to
# the upstream. e.g. /zkpol/health/live -> http://127.0.0.1:21011/health/live
# (the upstream Rust service has no /zkpol prefix). Existing routes such as
# /auth/, /pol/ keep their prefix because those upstreams are written to
# expect it.
_STRIP_PREFIX_ON_FORWARD = {
    "/zkpol/": "/zkpol",
    "/bridge/": "/bridge",
    "/anchor/": "/anchor",
    "/pol-snapshot/": "/pol-snapshot",  # /pol-snapshot/api/v1/... -> /api/v1/...
    "/pol-feed/": "/pol-feed",
    "/ws/": "/ws",  # /ws/stream -> /stream upstream (ws_feed.py speaks raw WS on /)
}
PASSTHROUGH_REQ_HEADERS = (
    "Content-Type",
    "X-MBX-APIKEY",
    "X-Opex-User",
    "Authorization",
    # Forward client IP so the upstream geo-blocking middleware (auth_server,
    # chain_server) can see the real caller. The proxy listens on 127.0.0.1
    # which sits on the trusted-proxy list, so XFF is honoured.
    "X-Forwarded-For",
    "X-Real-IP",
)
DROP_RESP_HEADERS = {
    "transfer-encoding",
    "connection",
    "access-control-allow-origin",
    "access-control-allow-methods",
    "access-control-allow-headers",
}

# Internal-only paths that exist on upstream services but must never be
# reachable from the public proxy. Same idea as the zkPoL allow-list, just
# expressed as an outright deny-list because there's no allow-listed sibling
# under the same prefix. Match by exact path or "<entry>/" prefix so a stray
# trailing slash doesn't sneak through.
INTERNAL_ONLY_PATHS = (
    "/auth/users-for-snapshot",  # used by pol_server.py to enumerate users
    # when the auth backend is Postgres
    "/auth/2fa/verify-withdraw",  # loopback-only: chain_server -> auth_server
)


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def _route(self) -> str | None:
        """Return upstream base URL if path matches a proxy route, else None.
        Longer prefix wins so /v1/owner/ matches before /v1/ would (we don't have /v1/ but the
        principle stops accidental shadowing if more routes are added later)."""
        for prefix in sorted(ROUTES, key=len, reverse=True):
            if self.path == prefix.rstrip("/") or self.path.startswith(prefix):
                return ROUTES[prefix]
        return None

    def _is_proxied(self) -> bool:
        return self._route() is not None

    # --- i18n helpers ----------------------------------------------------

    def _is_html_path(self) -> bool:
        """Return True if the request will resolve to an HTML file under ROOT.

        We map directory paths to ``index.html`` the same way SimpleHTTPRequestHandler
        does so the localisation kicks in for both ``/`` and ``/app/`` requests.
        """
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        # Reject path traversal up front — SimpleHTTPRequestHandler also
        # guards against this, but we'd rather refuse early.
        if ".." in path.split("/"):
            return False
        fs_path = os.path.join(ROOT, path.lstrip("/"))
        if os.path.isdir(fs_path):
            fs_path = os.path.join(fs_path, "index.html")
        return os.path.isfile(fs_path) and fs_path.endswith(".html")

    def _negotiate_lang(self) -> tuple[str, bool]:
        """Pick the locale for this request. Returns ``(lang, set_cookie)``.

        ``set_cookie`` is True only when the user explicitly chose a language
        via the ``?lang=`` URL param — that's when we want to persist their
        choice across subsequent requests. Cookie / Accept-Language defaults
        don't trigger a Set-Cookie (would be a wasted response header).
        """
        path_qs = self.path.split("?", 1)
        qs = path_qs[1] if len(path_qs) > 1 else ""
        query_lang = i18n_query_lang(qs)
        cookie_lang = i18n_cookie(self.headers.get("Cookie"))
        accept_lang = self.headers.get("Accept-Language")
        lang = i18n_negotiate(accept_lang, cookie_lang, query_lang)
        set_cookie = bool(query_lang and query_lang in I18N_SUPPORTED)
        return lang, set_cookie

    def _serve_localised_html(self) -> None:
        """Read the matching HTML file off disk, localise, and send.

        We deliberately do not stream — HTML pages here are <100 KB so a full
        in-memory rewrite is fine and lets us set Content-Length accurately.
        """
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        fs_path = os.path.join(ROOT, path.lstrip("/"))
        if os.path.isdir(fs_path):
            fs_path = os.path.join(fs_path, "index.html")
        try:
            with open(fs_path, encoding="utf-8") as fh:
                body = fh.read()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        lang, set_cookie = self._negotiate_lang()
        try:
            localised = i18n_localize(body, lang)
        except Exception as exc:  # noqa: BLE001 — defensive, never want a 500 here
            sys.stderr.write(f"[serve_homepage] i18n rewrite failed: {exc!r}\n")
            localised = body
        encoded = localised.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        # Don't cache: the body depends on Accept-Language / cookies so a
        # public cache would serve the wrong locale.
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Vary", "Accept-Language, Cookie")
        self.send_header("Content-Language", lang)
        if set_cookie:
            # 1 year, lax SameSite, path=/ — the user's choice is meant to be
            # remembered across the whole site.
            self.send_header(
                "Set-Cookie",
                f"zkcex_lang={lang}; Path=/; Max-Age=31536000; SameSite=Lax",
            )
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _forward_path(self) -> str:
        """Return the upstream path to use for the current request.

        Most routes are forwarded as-is (the upstream service expects to see
        the prefix). For routes listed in _STRIP_PREFIX_ON_FORWARD the prefix
        is removed before forwarding.

        zkPoL has a slightly different mounting layout: ``/health/*`` and
        ``/version`` live at the root, while token-scoped reads sit under
        ``/api`` (e.g. ``GET /api/tokens/ETH/summary``). We rewrite the
        public ``/zkpol/...`` URLs accordingly so callers don't have to know.
        """
        for prefix, strip in _STRIP_PREFIX_ON_FORWARD.items():
            if self.path == prefix.rstrip("/") or self.path.startswith(prefix):
                rest = self.path[len(strip) :]  # keeps the leading "/"
                if not rest.startswith("/"):
                    rest = "/" + rest
                if prefix == "/zkpol/":
                    # /zkpol/health/...   -> /health/...
                    # /zkpol/version      -> /version
                    # /zkpol/tokens/...   -> /api/tokens/...
                    sub_path = rest.split("?", 1)[0]
                    if (
                        sub_path == "/health"
                        or sub_path.startswith("/health/")
                        or sub_path == "/version"
                        or sub_path == "/version/"
                    ):
                        return rest
                    if sub_path.startswith("/tokens"):
                        return "/api" + rest
                    # anything else stays as-is (it'll likely 404 upstream).
                    return rest
                return rest
        return self.path

    def _zkpol_blocked(self) -> tuple[int, str] | None:
        """Return (status, reason) if the request should be blocked at the proxy.

        We expose the upstream zkPoL Rust service to the public web only on a
        narrow allow-list of read endpoints. In particular, the directory
        listing ``GET /tokens/{id}/accounts`` (no address) is blocked because
        it would let anonymous callers enumerate every customer address that
        happens to be registered in zkPoL — even though the address is the
        opaque opex_user, leaking the population is unwanted.
        """
        full = self.path
        if not full.startswith("/zkpol/") and full != "/zkpol":
            return None
        # Strip the /zkpol prefix; what remains starts with "/" (or is empty).
        sub = full[len("/zkpol") :] or "/"
        sub_path = sub.split("?", 1)[0]
        # health & version
        if sub_path in ("/health", "/health/", "/version", "/version/"):
            return None
        if sub_path.startswith("/health/"):
            return None
        # token-scoped reads
        if sub_path.startswith("/tokens/"):
            parts = sub_path.split("/", 3)  # ['', 'tokens', '<id>', '<rest...>']
            if len(parts) < 4 or not parts[2]:
                return (404, "no_token_id")
            tail = "/" + parts[3]  # always begins with "/"
            # Account endpoints: only allow accounts/<address>(/verify)?
            if tail.startswith("/accounts"):
                # /accounts (listing) -> blocked
                if tail == "/accounts" or tail == "/accounts/":
                    return (403, "listing_disabled")
                # /accounts/<address> or /accounts/<address>/verify -> allowed
                acc_parts = tail.split("/", 3)  # ['', 'accounts', '<addr>', '<rest>']
                if len(acc_parts) < 3 or not acc_parts[2]:
                    return (403, "listing_disabled")
                if len(acc_parts) == 3:
                    return None
                # Only /verify is exposed past the address segment.
                if acc_parts[3] in ("verify", "verify/"):
                    return None
                return (403, "endpoint_disabled")
            # Other allow-listed tails: /summary, /pipeline, /batches, /events/history
            for ok in _ZKPOL_TOKEN_ALLOW_TAILS:
                if tail == ok or tail.startswith(ok + "/") or tail.startswith(ok + "?"):
                    return None
            return (403, "endpoint_disabled")
        return (403, "endpoint_disabled")

    def do_GET(self):
        with _otel_server_span(self):
            if self._is_proxied():
                # WebSocket upgrade: /ws/* with `Upgrade: websocket` -> bidirectional
                # raw-socket pump to the upstream. Detection has to happen here
                # because http.server has already parsed the headers for us.
                upgrade = (self.headers.get("Upgrade") or "").strip().lower()
                if upgrade == "websocket" and (self.path == "/ws" or self.path.startswith("/ws/")):
                    return self._proxy_ws()
                self._proxy()
            else:
                # Static asset. For HTML, run the response body through the
                # i18n post-processor so the negotiated locale is applied
                # before bytes hit the wire. For everything else (CSS / JS /
                # images / JSON), fall through to the stock SimpleHTTPRequestHandler.
                if I18N_ENABLED and self._is_html_path():
                    return self._serve_localised_html()
                super().do_GET()

    def do_POST(self):
        with _otel_server_span(self):
            self._proxy()

    def do_DELETE(self):
        with _otel_server_span(self):
            self._proxy()

    def do_PUT(self):
        with _otel_server_span(self):
            self._proxy()

    def do_PATCH(self):
        with _otel_server_span(self):
            self._proxy()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def _proxy(self):
        upstream = self._route()
        if upstream is None:
            self.send_response(404)
            self.end_headers()
            return
        # Block external callers from poking the server-internal verify
        # endpoint. The api_key_server itself also enforces loopback-only,
        # but we belt-and-brace at the proxy too — to outsiders this route
        # simply doesn't exist.
        if self.path.startswith("/api-keys/verify"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        # /push/send is loopback-only — used by chain_server, auth_server,
        # order_engine to fan out notifications. Hide it from the public proxy
        # the same way as /api-keys/verify.
        if self.path.startswith("/push/send"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        # /notifications/send is loopback-only — sibling services
        # (chain_server, auth_server, order_engine, ops_server, safu_server)
        # fan out notifications through it. Same hide-from-public treatment.
        if self.path.startswith("/notifications/send"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        # /travel-rule/screen is the loopback-only entry point that
        # chain_server uses before broadcasting a withdraw. Hide it from
        # the public proxy. /travel-rule/admin/* (operator approve/reject
        # endpoints) is admin-bearer guarded server-side, but we also
        # belt-and-brace by hiding it from public traffic. /travel-rule/
        # inbound IS public (counterparty VASPs need to reach it), so we
        # only block screen + admin here.
        if (
            self.path.startswith("/travel-rule/screen")
            or self.path.startswith("/travel-rule/admin/")
            or self.path.startswith("/travel-rule/_admin/")
        ):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        # /anchor/internal/* is loopback-only on anchor_indexer (zkpol_bridge
        # POSTs pending-change notifications, ops drops the cursor). Hide it
        # from the public proxy. /anchor/health, /anchor/batches, etc. pass
        # through normally.
        if self.path.startswith("/anchor/internal/") or self.path == "/anchor/internal":
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        # /errors/internal/* is loopback-only on the error_collector itself,
        # but we also block it at the proxy so external traffic cannot even
        # discover the route. The public /errors/report POST and the admin
        # /errors/* GETs continue to pass through normally.
        if self.path.startswith("/errors/internal/") or self.path == "/errors/internal":
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        # /fees/internal/* is the loopback-only entrypoint that matching
        # engines call to record a fill / force-recompute a tier. Hide it
        # from the public proxy. /fees/admin/* is admin-bearer guarded
        # server-side, but we also block public traffic from reaching it.
        if (
            self.path.startswith("/fees/internal/")
            or self.path == "/fees/internal"
            or self.path.startswith("/fees/admin/")
            or self.path.startswith("/referral/internal/")
            or self.path == "/referral/internal"
            or self.path == "/referral/apply"
            or self.path.startswith("/lending/internal/")
            or self.path == "/lending/internal"
            or self.path.startswith("/lending/admin/")
        ):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"error":"not_found"}')
            return
        # Internal-only auth/pol endpoints. Same belt-and-braces approach.
        path_only = self.path.split("?", 1)[0]
        for blk in INTERNAL_ONLY_PATHS:
            if path_only == blk or path_only.startswith(blk + "/"):
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error":"not_found"}')
                return
        blocked = self._zkpol_blocked()
        if blocked is not None:
            code, reason = blocked
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(
                (
                    f'{{"error":"{reason}","message":"this zkPoL endpoint is not exposed publicly"}}'
                ).encode()
            )
            return
        # SSE / streaming endpoints need a write-as-you-go pipe, not the
        # default urllib behaviour which buffers/blocks. Detect via the path
        # (well-known SSE endpoints) and use a raw http.client connection to
        # forward bytes as they arrive.
        is_stream_path = self.path.endswith("/live/stream") or "/live/stream?" in self.path
        forward_path = self._forward_path()
        if is_stream_path:
            return self._proxy_stream(upstream, forward_path)
        url = upstream + forward_path
        body = b""
        if self.headers.get("Content-Length"):
            body = self.rfile.read(int(self.headers["Content-Length"]))

        # ---- Binance-compat HMAC verification --------------------------
        # If the caller presented an X-MBX-APIKEY, treat the request as a
        # signed external API call: validate the signature against the
        # api_key_server, strip the signing fields, and inject X-Opex-User
        # for the upstream services that key off it.
        injected_opex_user: str | None = None
        api_key_id = self.headers.get("X-MBX-APIKEY")
        if api_key_id and self.path.split("?", 1)[0].startswith(("/v3/", "/sapi/", "/fapi/")):
            verify_result = self._verify_signed_request(api_key_id, body)
            if verify_result is None:
                # _verify_signed_request already wrote the error response.
                return
            injected_opex_user = verify_result["opex_user"]
            # Rebuild URL / body without the signature param so upstream
            # never sees it. The underlying matching engine doesn't accept
            # extra params; cleaning them avoids surprises.
            url, body = verify_result["clean_url"], verify_result["clean_body"]
            forward_path = url[len(upstream) :] if url.startswith(upstream) else forward_path

        req = _http_request(url, data=body if body else None, method=self.command)
        # Track whether the inbound request already carries an XFF -- if not
        # we synthesize one from the socket peer so upstream geo-blocking
        # sees the real client IP rather than the proxy's loopback IP.
        inbound_xff = self.headers.get("X-Forwarded-For")
        for h in PASSTHROUGH_REQ_HEADERS:
            v = self.headers.get(h)
            if v is not None:
                # When we authenticated via HMAC, drop the inbound
                # X-MBX-APIKEY / X-Opex-User and inject the canonical one
                # so upstream sees only the verified identity.
                if injected_opex_user and h in ("X-MBX-APIKEY", "X-Opex-User"):
                    continue
                req.add_header(h, v)
        if injected_opex_user:
            req.add_header("X-Opex-User", injected_opex_user)
        if not inbound_xff:
            peer_ip = ""
            try:
                peer_ip = self.client_address[0] if self.client_address else ""
            except Exception as e:  # noqa: BLE001
                _log(f"client peer lookup failed: {e!r}")
                peer_ip = ""
            if peer_ip:
                req.add_header("X-Forwarded-For", peer_ip)
        try:
            with _http_urlopen(req, timeout=20) as resp:
                self.send_response(resp.status)
                for k, v in resp.headers.items():
                    if k.lower() in DROP_RESP_HEADERS:
                        continue
                    self.send_header(k, v)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(resp.read())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in (e.headers or {}).items():
                if k.lower() in DROP_RESP_HEADERS:
                    continue
                self.send_header(k, v)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as exc:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(b'{"error":"upstream","message":"%s"}' % str(exc).encode())

    def _verify_signed_request(self, api_key_id: str, body: bytes):
        """Validate a Binance-style HMAC signed request against api_key_server.

        Returns ``{"opex_user", "clean_url", "clean_body"}`` on allow, or
        ``None`` after writing a Binance-shaped error response on deny.

        The canonical signing string is whichever side carries the
        ``signature=`` param: query-string for GET/DELETE, form body for POST.
        Per Binance spec, the SAME bytes (minus ``signature=``) must be the
        HMAC input. We extract the signature and pass everything else to the
        verify endpoint; upstream services never see ``signature``.
        """
        full_path = self.path
        path_only, _, qs = full_path.partition("?")
        # Decide where the signature lives.
        body_text = ""
        try:
            body_text = body.decode("utf-8") if body else ""
        except UnicodeDecodeError:
            body_text = ""
        sig_in_query = "signature=" in qs
        sig_in_body = (not sig_in_query) and "signature=" in body_text
        if not sig_in_query and not sig_in_body:
            return self._write_binance_error(
                -1102, "Mandatory parameter 'signature' was not sent.", 400
            )

        def _split_off_signature(s: str) -> tuple[str, str]:
            """Return ``(canonical_query, signature_hex)``."""
            pairs = s.split("&") if s else []
            keep: list[str] = []
            sig_val = ""
            for pair in pairs:
                k, _, v = pair.partition("=")
                if k == "signature":
                    sig_val = v
                else:
                    keep.append(pair)
            return "&".join(keep), sig_val

        if sig_in_query:
            canonical_qs, signature = _split_off_signature(qs)
            clean_body = body
        else:
            canonical_qs, signature = _split_off_signature(body_text)
            clean_body = canonical_qs.encode("utf-8")

        scope = _required_scope(self.command, path_only)
        peer_ip = self.client_address[0] if self.client_address else ""
        verify_payload = {
            "key_id": api_key_id,
            "signature": signature,
            "query_string": canonical_qs,
            "client_ip": peer_ip,
            "endpoint_path": path_only,
            "method": self.command,
            "required_scope": scope,
        }
        try:
            req = _http_request(
                API_KEY_VERIFY_URL,
                data=json.dumps(verify_payload).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with _http_urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            err_body = b""
            try:
                err_body = e.read()
            except Exception as read_error:  # noqa: BLE001
                _log(f"api-key verify error body read failed: {read_error!r}")
            try:
                payload = json.loads(err_body.decode("utf-8") or "{}")
                code = int(payload.get("code") or -2014)
                msg = str(payload.get("msg") or "API-key format invalid.")
            except Exception:
                code, msg = -2014, "API-key format invalid."
            return self._write_binance_error(code, msg, e.code)
        except Exception as exc:
            sys.stderr.write(f"[proxy] api-key verify failed: {exc!r}\n")
            return self._write_binance_error(
                -1001, "Internal error; unable to verify API key.", 503
            )

        opex_user = data.get("opex_user")
        if not opex_user:
            return self._write_binance_error(-2014, "API-key format invalid.", 401)

        # Reconstruct the upstream URL using the original path, with the
        # signature stripped. We keep timestamp/recvWindow on the wire because
        # downstream Binance-compat endpoints expect/echo them.
        upstream = self._route() or ""
        forward_path = self._forward_path()
        if "?" in forward_path:
            forward_path = forward_path.split("?", 1)[0]
        if sig_in_query and canonical_qs:
            url = upstream + forward_path + "?" + canonical_qs
        else:
            url = upstream + forward_path
        return {"opex_user": opex_user, "clean_url": url, "clean_body": clean_body}

    def _write_binance_error(self, code: int, msg: str, http_status: int = 401):
        """Send a Binance-shaped ``{"code":-2014,"msg":"..."}`` JSON error."""
        body = json.dumps({"code": code, "msg": msg}).encode("utf-8")
        self.send_response(http_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return None

    def _proxy_stream(self, upstream: str, forward_path: str | None = None):
        """Forward an SSE stream byte-for-byte. Opens a raw TCP socket to the
        upstream, forwards the HTTP request, then pipes the response straight
        back to the client. Unbuffered and bidirectional-EOF-aware.
        urllib.request and http.client buffer responses, so this is the only
        way to get sub-second SSE latency.
        """
        import socket

        parsed = urllib.parse.urlsplit(upstream)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=10)
            sock.settimeout(None)

            # Build & send the upstream HTTP request line + headers.
            up_path = forward_path if forward_path is not None else self.path
            req_lines = [f"{self.command} {up_path} HTTP/1.1"]
            req_lines.append(f"Host: {host}:{port}")
            req_lines.append("Accept: text/event-stream")
            req_lines.append("Cache-Control: no-cache")
            req_lines.append("Connection: close")
            for h in PASSTHROUGH_REQ_HEADERS:
                v = self.headers.get(h)
                if v is not None:
                    req_lines.append(f"{h}: {v}")
            sock.sendall(("\r\n".join(req_lines) + "\r\n\r\n").encode("ascii"))

            # Read upstream response headers byte-by-byte until \r\n\r\n.
            buf = b""
            while b"\r\n\r\n" not in buf:
                more = sock.recv(1024)
                if not more:
                    break
                buf += more
                if len(buf) > 65536:
                    raise RuntimeError("upstream headers oversized")
            head, _, rest = buf.partition(b"\r\n\r\n")
            head_lines = head.split(b"\r\n")
            if not head_lines:
                raise RuntimeError("empty upstream response")
            try:
                _proto, status, _msg = head_lines[0].decode("iso-8859-1").split(" ", 2)
                status_code = int(status)
            except Exception:
                status_code = 502

            # Forward status + (filtered) headers to the client.
            self.send_response(status_code)
            for line in head_lines[1:]:
                if not line:
                    continue
                if b":" not in line:
                    continue
                name, _, value = line.decode("iso-8859-1").partition(":")
                lname = name.strip().lower()
                if lname in DROP_RESP_HEADERS:
                    continue
                if lname == "content-length":
                    continue
                self.send_header(name.strip(), value.strip())
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # Flush any post-header bytes that came along with the headers.
            if rest:
                try:
                    self.wfile.write(rest)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            # Now stream-forward each chunk as it arrives.
            while True:
                try:
                    chunk = sock.recv(4096)
                except (TimeoutError, OSError):
                    break
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    break
        except Exception as exc:
            try:
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b'{"error":"upstream_stream","message":"%s"}' % str(exc).encode())
            except Exception as write_error:  # noqa: BLE001
                _log(f"stream error response write failed: {write_error!r}")
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception as close_error:  # noqa: BLE001
                _log(f"stream socket close failed: {close_error!r}")

    def _proxy_ws(self):
        """Proxy a WebSocket upgrade. Forward the handshake to the upstream,
        then pipe bytes both directions until either side closes.

        We can't use urllib here at all — once the upstream returns
        ``101 Switching Protocols`` the connection becomes a raw bidirectional
        byte stream. We open a fresh socket, replay the request, send the
        upstream's 101 verbatim back to the browser, and run two pump threads
        until EOF.
        """
        import socket
        import threading

        upstream = self._route()
        if upstream is None:
            self.send_response(404)
            self.end_headers()
            return
        forward_path = self._forward_path()
        parsed = urllib.parse.urlsplit(upstream)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        up_sock = None
        try:
            up_sock = socket.create_connection((host, port), timeout=10)
            up_sock.settimeout(None)
            # Re-issue the request with the original WS headers preserved.
            req_lines = [f"GET {forward_path} HTTP/1.1", f"Host: {host}:{port}"]
            for h_name in (
                "Upgrade",
                "Connection",
                "Sec-WebSocket-Key",
                "Sec-WebSocket-Version",
                "Sec-WebSocket-Protocol",
                "Sec-WebSocket-Extensions",
                "Origin",
                "User-Agent",
            ):
                v = self.headers.get(h_name)
                if v is not None:
                    req_lines.append(f"{h_name}: {v}")
            up_sock.sendall(("\r\n".join(req_lines) + "\r\n\r\n").encode("ascii"))

            # Read upstream response headers until \r\n\r\n.
            buf = b""
            while b"\r\n\r\n" not in buf:
                more = up_sock.recv(2048)
                if not more:
                    raise RuntimeError("upstream closed during ws handshake")
                buf += more
                if len(buf) > 16384:
                    raise RuntimeError("upstream ws headers too large")
            head, _, after = buf.partition(b"\r\n\r\n")
            # Forward the upstream's 101 (or whatever it returned) verbatim to
            # the browser — including all headers — so the browser sees a real
            # Sec-WebSocket-Accept derived from its own key.
            client_sock = self.connection
            client_sock.sendall(head + b"\r\n\r\n")
            if after:
                client_sock.sendall(after)

            # Bidirectional pump. Run upstream->client in this thread and
            # client->upstream in a background thread; first to EOF tears
            # everything down.
            def pump(src, dst):
                try:
                    while True:
                        chunk = src.recv(4096)
                        if not chunk:
                            break
                        dst.sendall(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass

            t_in = threading.Thread(target=pump, args=(client_sock, up_sock), daemon=True)
            t_in.start()
            pump(up_sock, client_sock)
            t_in.join(timeout=2.0)
        except Exception as exc:
            try:
                self.connection.sendall(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                    b"Connection: close\r\n\r\n"
                )
            except OSError:
                pass
            sys.stderr.write(f"[serve_homepage] ws proxy error: {exc}\n")
        finally:
            try:
                if up_sock is not None:
                    up_sock.close()
            except OSError:
                pass
            # Mark close_connection so http.server doesn't try to keep this
            # alive — the connection has been hijacked.
            try:
                self.close_connection = True
            except Exception as e:  # noqa: BLE001
                _log(f"connection close flag failed: {e!r}")

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.address_string()} - {fmt % args}\n")


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5500
    try:
        _otel_install()
    except Exception as e:  # noqa: BLE001
        _log(f"otel install skipped: {e!r}")
    with ThreadingServer((LISTEN_HOST, port), Handler) as srv:
        sys.stderr.write(f"serving {ROOT} on {LISTEN_HOST}:{port}\n")
        for prefix, target in sorted(ROUTES.items(), key=lambda kv: -len(kv[0])):
            sys.stderr.write(f"  proxy {prefix:18} -> {target}\n")
        srv.serve_forever()


if __name__ == "__main__":
    main()
