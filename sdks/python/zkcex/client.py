"""ZkcexClient — minimal stdlib-only client for zkCEX.

Auth surface mirrors Binance Spot + Futures:

* HMAC keys (``X-MBX-APIKEY`` header + ``signature=`` query param) for
  ``/v3/*`` and ``/fapi/v1/*`` signed routes.
* Bearer session tokens for ``/auth/me``, ``/chain/*``, ``/pol/*``,
  ``/api-keys/*``, ``/orders/conditional`` and friends.

All HTTP is driven by ``urllib.request`` — there are deliberately no
third-party dependencies. The HMAC scheme matches Binance exactly so
existing Binance SDKs can talk to zkCEX side-by-side.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping


DEFAULT_BASE_URL = "http://localhost:5500"
DEFAULT_RECV_WINDOW = 5000
DEFAULT_TIMEOUT = 30.0


class ZkcexError(Exception):
    """Raised when the server returns a non-2xx response."""

    def __init__(self, status: int, payload: Any, url: str):
        self.status = status
        self.payload = payload
        self.url = url
        msg = f"HTTP {status} from {url}: {payload!r}"
        super().__init__(msg)


def _signed_query(secret: str, params: Mapping[str, Any] | None,
                  recv_window: int = DEFAULT_RECV_WINDOW) -> str:
    """Build a Binance-compatible signed query string.

    1. Filter out ``None`` values.
    2. Append ``timestamp`` (ms) and ``recvWindow``.
    3. URL-encode in insertion order — Binance does NOT require lexical
       sort; the signature is over the *literal* query string the client
       sends, so we just preserve order.
    4. HMAC-SHA256 the encoded query with ``secret`` and append
       ``signature=<hex>``.
    """
    items: list[tuple[str, Any]] = []
    if params:
        for k, v in params.items():
            if v is None:
                continue
            items.append((k, v))
    items.append(("timestamp", int(time.time() * 1000)))
    items.append(("recvWindow", recv_window))
    qs = urllib.parse.urlencode(items, safe=",")
    sig = hmac.new(secret.encode("utf-8"), qs.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{qs}&signature={sig}"


def _strip_none(d: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class ZkcexClient:
    """zkCEX REST client.

    Parameters
    ----------
    base_url:
        Proxy origin. Defaults to ``http://localhost:5500``.
    api_key, api_secret:
        Issue these at ``/app/api-keys.html``. Required for any signed
        ``/v3/*`` or ``/fapi/v1/*`` route.
    session_token:
        Bearer token from ``signup()`` or ``login()``. Required for the
        ``/auth/me``, ``/chain/*``, ``/pol/*``, ``/api-keys/*``,
        ``/orders/conditional`` routes.
    timeout:
        Per-request timeout in seconds (default 30).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        api_secret: str | None = None,
        session_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        recv_window: int = DEFAULT_RECV_WINDOW,
        user_agent: str = "zkcex-python/1.0",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.session_token = session_token
        self.timeout = timeout
        self.recv_window = recv_window
        self.user_agent = user_agent

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        signed: bool = False,
        authed: bool = False,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        url = self.base_url + path
        if signed:
            if not self.api_key or not self.api_secret:
                raise ZkcexError(401,
                                 {"error": "missing_api_credentials"},
                                 url)
            qs = _signed_query(self.api_secret, params, self.recv_window)
            url = f"{url}?{qs}"
        elif params:
            qs = urllib.parse.urlencode(_strip_none(params), safe=",")
            if qs:
                url = f"{url}?{qs}"

        data: bytes | None = None
        h: dict[str, str] = {"User-Agent": self.user_agent}
        if headers:
            h.update(dict(headers))
        if signed and self.api_key:
            h["X-MBX-APIKEY"] = self.api_key
        if authed:
            if not self.session_token:
                raise ZkcexError(401, {"error": "missing_session_token"}, url)
            h["Authorization"] = f"Bearer {self.session_token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                if not raw:
                    return None
                ct = resp.headers.get("Content-Type", "")
                if "application/json" in ct:
                    return json.loads(raw.decode("utf-8"))
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = raw.decode("utf-8", errors="replace")
            raise ZkcexError(exc.code, payload, url) from None

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def signup(self, email: str, password: str, name: str) -> dict:
        """Create a new account. Stores the issued bearer token on this
        client so subsequent ``authed=True`` calls work immediately."""
        r = self._request("POST", "/auth/signup",
                          body={"email": email, "password": password,
                                "name": name})
        if isinstance(r, dict) and r.get("token"):
            self.session_token = r["token"]
        return r

    def login(self, email: str, password: str) -> dict:
        r = self._request("POST", "/auth/login",
                          body={"email": email, "password": password})
        if isinstance(r, dict) and r.get("token"):
            self.session_token = r["token"]
        return r

    def me(self) -> dict:
        return self._request("GET", "/auth/me", authed=True)

    def logout(self) -> None:
        self._request("POST", "/auth/logout", authed=True)
        self.session_token = None

    def auth_health(self) -> dict:
        return self._request("GET", "/auth/health")

    # ------------------------------------------------------------------
    # Spot market data — no auth
    # ------------------------------------------------------------------

    def exchange_info(self) -> dict:
        return self._request("GET", "/v3/exchangeInfo")

    def depth(self, symbol: str, limit: int = 20) -> dict:
        return self._request("GET", "/v3/depth",
                             params={"symbol": symbol, "limit": limit})

    def klines(self, symbol: str, interval: str, limit: int = 200) -> list:
        return self._request("GET", "/v3/klines",
                             params={"symbol": symbol,
                                     "interval": interval,
                                     "limit": limit})

    def recent_trades(self, symbol: str, limit: int = 50) -> list:
        return self._request("GET", "/v3/trades",
                             params={"symbol": symbol, "limit": limit})

    def ticker_24h(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/v3/ticker/24hr", params=params)

    # ------------------------------------------------------------------
    # Spot trading — HMAC signed
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        side: str,
        type: str,
        quantity: str | None = None,
        price: str | None = None,
        time_in_force: str = "GTC",
        quote_order_qty: str | None = None,
        new_client_order_id: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """POST /v3/order — Binance-compatible body shape, all params
        sent as query and HMAC-signed."""
        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": type,
            "quantity": quantity,
            "price": price,
            "quoteOrderQty": quote_order_qty,
            "newClientOrderId": new_client_order_id,
        }
        if type.upper() == "LIMIT":
            params["timeInForce"] = time_in_force
        params.update(kwargs)
        return self._request("POST", "/v3/order",
                             params=_strip_none(params), signed=True)

    def cancel_order(self, symbol: str, order_id: int | None = None,
                     client_order_id: str | None = None) -> dict:
        params = _strip_none({
            "symbol": symbol,
            "orderId": order_id,
            "origClientOrderId": client_order_id,
        })
        return self._request("DELETE", "/v3/order", params=params, signed=True)

    def open_orders(self, symbol: str | None = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/v3/openOrders", params=params, signed=True)

    def my_trades(self, symbol: str, limit: int = 50) -> list:
        return self._request("GET", "/v3/myTrades",
                             params={"symbol": symbol, "limit": limit},
                             signed=True)

    def account(self) -> dict:
        return self._request("GET", "/v3/account", params={}, signed=True)

    def withdraw(self, asset: str, amount: str, address: str,
                 network: str | None = None) -> dict:
        return self._request("POST", "/v3/withdraw",
                             params=_strip_none({
                                 "asset": asset,
                                 "amount": amount,
                                 "address": address,
                                 "network": network,
                             }), signed=True)

    # ------------------------------------------------------------------
    # Futures
    # ------------------------------------------------------------------

    def futures_exchange_info(self) -> dict:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def premium_index(self, symbol: str | None = None) -> Any:
        params = {"symbol": symbol} if symbol else None
        return self._request("GET", "/fapi/v1/premiumIndex", params=params)

    def funding_rate(self, symbol: str | None = None, limit: int = 100) -> list:
        params = _strip_none({"symbol": symbol, "limit": limit})
        return self._request("GET", "/fapi/v1/fundingRate", params=params)

    def futures_depth(self, symbol: str, limit: int = 20) -> dict:
        return self._request("GET", "/fapi/v1/depth",
                             params={"symbol": symbol, "limit": limit})

    def futures_account(self) -> dict:
        return self._request("GET", "/fapi/v1/account",
                             params={}, signed=True)

    def position_risk(self, symbol: str | None = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/positionRisk",
                             params=params, signed=True)

    def futures_place_order(
        self,
        symbol: str,
        side: str,
        type: str,
        quantity: str,
        price: str | None = None,
        time_in_force: str | None = None,
        reduce_only: bool | None = None,
        position_side: str | None = None,
        new_client_order_id: str | None = None,
        **kwargs: Any,
    ) -> dict:
        params: dict[str, Any] = {
            "symbol": symbol, "side": side, "type": type,
            "quantity": quantity, "price": price,
            "timeInForce": time_in_force,
            "reduceOnly": "true" if reduce_only else None,
            "positionSide": position_side,
            "newClientOrderId": new_client_order_id,
        }
        params.update(kwargs)
        return self._request("POST", "/fapi/v1/order",
                             params=_strip_none(params), signed=True)

    def futures_close_position(self, symbol: str) -> dict:
        return self._request("POST", "/fapi/v1/closePosition",
                             params={"symbol": symbol}, signed=True)

    def futures_set_leverage(self, symbol: str, leverage: int) -> dict:
        return self._request("POST", "/fapi/v1/leverage",
                             params={"symbol": symbol, "leverage": leverage},
                             signed=True)

    def futures_set_margin_type(self, symbol: str, margin_type: str) -> dict:
        return self._request("POST", "/fapi/v1/marginType",
                             params={"symbol": symbol, "marginType": margin_type},
                             signed=True)

    def futures_transfer(self, asset: str, amount: str, type: int) -> dict:
        """type: 1 = spot->futures, 2 = futures->spot."""
        return self._request("POST", "/fapi/v1/transfer",
                             params={"asset": asset, "amount": amount,
                                     "type": type},
                             signed=True)

    def futures_income(self, symbol: str | None = None) -> list:
        params = {"symbol": symbol} if symbol else {}
        return self._request("GET", "/fapi/v1/income",
                             params=params, signed=True)

    # ------------------------------------------------------------------
    # Chain
    # ------------------------------------------------------------------

    def chain_info(self) -> dict:
        return self._request("GET", "/chain/info")

    def chain_wallet(self, chain: str | None = None) -> dict:
        params = {"chain": chain} if chain else None
        return self._request("GET", "/chain/wallet", params=params, authed=True)

    def chain_deposits(self) -> list:
        return self._request("GET", "/chain/deposits", authed=True)

    def chain_withdraws(self) -> list:
        return self._request("GET", "/chain/withdraws", authed=True)

    def chain_airdrop(self, asset: str | None = None,
                      amount: str | None = None,
                      chain: str | None = None) -> dict:
        return self._request("POST", "/chain/airdrop",
                             body=_strip_none({"asset": asset,
                                               "amount": amount,
                                               "chain": chain}),
                             authed=True)

    def chain_withdraw(self, asset: str, amount: str, to_address: str,
                       chain: str | None = None) -> dict:
        return self._request("POST", "/chain/withdraw",
                             body=_strip_none({"asset": asset,
                                               "amount": amount,
                                               "to_address": to_address,
                                               "chain": chain}),
                             authed=True)

    # ------------------------------------------------------------------
    # PoL
    # ------------------------------------------------------------------

    def pol_server_info(self) -> dict:
        return self._request("GET", "/pol/server-info")

    def pol_latest_epoch(self) -> dict:
        return self._request("GET", "/pol/latest-epoch")

    def pol_my_proof(self) -> dict:
        return self._request("GET", "/pol/my-proof", authed=True)

    def pol_refresh(self) -> dict:
        return self._request("POST", "/pol/refresh", authed=True)

    def reserves_vs_liabilities(self) -> dict:
        return self._request("GET", "/pol/reserves-vs-liabilities")

    def porl_reserves_vs_liabilities(self) -> dict:
        """Alias kept for parity with the brief's surface naming."""
        return self.reserves_vs_liabilities()

    def pol_snapshot_latest(self) -> dict:
        return self._request("GET",
                             "/pol-snapshot/api/v1/certificate/latest")

    # ------------------------------------------------------------------
    # API keys
    # ------------------------------------------------------------------

    def create_api_key(self, label: str, scopes: list[str] | None = None,
                       expires_in_days: int = 90,
                       ip_allowlist: str | None = None,
                       confirm_phrase: str | None = None) -> dict:
        body = _strip_none({
            "label": label,
            "scopes": scopes or ["read"],
            "ip_allowlist": ip_allowlist,
            "expires_in_days": expires_in_days,
            "confirm_phrase": confirm_phrase,
        })
        return self._request("POST", "/api-keys/create",
                             body=body, authed=True)

    def list_api_keys(self) -> list:
        return self._request("GET", "/api-keys/list", authed=True)

    def revoke_api_key(self, key_id: str) -> None:
        self._request("DELETE", f"/api-keys/{key_id}", authed=True)

    # ------------------------------------------------------------------
    # Conditional orders
    # ------------------------------------------------------------------

    def conditional_order(self, **body: Any) -> dict:
        return self._request("POST", "/orders/conditional",
                             body=_strip_none(body), authed=True)

    def list_conditionals(self) -> list:
        return self._request("GET", "/orders/conditional", authed=True)

    def cancel_conditional(self, client_order_id: str) -> dict:
        return self._request("DELETE",
                             f"/orders/conditional/{client_order_id}",
                             authed=True)
