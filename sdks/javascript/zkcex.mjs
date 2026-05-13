// zkCEX — official minimal JavaScript SDK (ES module).
//
// Runs in modern Node (>= 18, native fetch + node:crypto) and in modern
// browsers (uses crypto.subtle when node:crypto isn't importable).
//
// Five-line example:
//
//   import { ZkcexClient } from "./zkcex.mjs";
//   const c = new ZkcexClient({ baseUrl: "http://localhost:5500",
//                               apiKey: "...", apiSecret: "..." });
//   console.log(await c.depth("ETHUSDT", 5));
//   console.log(await c.placeOrder({ symbol: "ETHUSDT", side: "BUY",
//                                     type: "LIMIT", quantity: "0.01",
//                                     price: "50", timeInForce: "GTC" }));

const DEFAULT_BASE_URL = "http://localhost:5500";
const DEFAULT_RECV_WINDOW = 5000;

export class ZkcexError extends Error {
  constructor(status, payload, url) {
    super(`HTTP ${status} from ${url}: ${JSON.stringify(payload)}`);
    this.status = status;
    this.payload = payload;
    this.url = url;
  }
}

// ------------------------------------------------------------------
// HMAC adapter — picks node:crypto if available, falls back to WebCrypto.
// ------------------------------------------------------------------

let _nodeCrypto = null;
async function _loadNodeCrypto() {
  if (_nodeCrypto !== null) return _nodeCrypto;
  try {
    _nodeCrypto = await import("node:crypto");
  } catch (_e) {
    _nodeCrypto = false;
  }
  return _nodeCrypto;
}

async function hmacSha256Hex(secret, message) {
  const nc = await _loadNodeCrypto();
  if (nc && nc.createHmac) {
    return nc.createHmac("sha256", secret).update(message).digest("hex");
  }
  // Browser / Deno fallback: WebCrypto.
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  const bytes = new Uint8Array(sig);
  let hex = "";
  for (let i = 0; i < bytes.length; i++) {
    const b = bytes[i].toString(16);
    hex += b.length === 1 ? "0" + b : b;
  }
  return hex;
}

// ------------------------------------------------------------------
// Helpers
// ------------------------------------------------------------------

function buildQuery(params) {
  if (!params) return "";
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    usp.append(k, String(v));
  }
  return usp.toString();
}

async function signedQuery(secret, params, recvWindow) {
  const usp = new URLSearchParams();
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null) continue;
      usp.append(k, String(v));
    }
  }
  usp.append("timestamp", String(Date.now()));
  usp.append("recvWindow", String(recvWindow));
  const qs = usp.toString();
  const sig = await hmacSha256Hex(secret, qs);
  return `${qs}&signature=${sig}`;
}

// ------------------------------------------------------------------
// Client
// ------------------------------------------------------------------

export class ZkcexClient {
  constructor({
    baseUrl = DEFAULT_BASE_URL,
    apiKey = null,
    apiSecret = null,
    sessionToken = null,
    recvWindow = DEFAULT_RECV_WINDOW,
    userAgent = "zkcex-js/1.0",
  } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.apiKey = apiKey;
    this.apiSecret = apiSecret;
    this.sessionToken = sessionToken;
    this.recvWindow = recvWindow;
    this.userAgent = userAgent;
  }

  async _request(method, path, {
    params = null,
    body = null,
    signed = false,
    authed = false,
  } = {}) {
    let url = this.baseUrl + path;
    let qs = "";
    if (signed) {
      if (!this.apiKey || !this.apiSecret) {
        throw new ZkcexError(401,
          { error: "missing_api_credentials" }, url);
      }
      qs = await signedQuery(this.apiSecret, params, this.recvWindow);
    } else if (params) {
      qs = buildQuery(params);
    }
    if (qs) url = `${url}?${qs}`;

    const headers = { "User-Agent": this.userAgent };
    if (signed && this.apiKey) headers["X-MBX-APIKEY"] = this.apiKey;
    if (authed) {
      if (!this.sessionToken) {
        throw new ZkcexError(401,
          { error: "missing_session_token" }, url);
      }
      headers["Authorization"] = `Bearer ${this.sessionToken}`;
    }

    const init = { method, headers };
    if (body !== null && body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }

    const resp = await fetch(url, init);
    const text = await resp.text();
    let payload = text;
    const ct = resp.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      try { payload = JSON.parse(text); } catch (_e) { /* keep text */ }
    } else if (text === "") {
      payload = null;
    }
    if (!resp.ok) throw new ZkcexError(resp.status, payload, url);
    return payload;
  }

  // ---- Auth -------------------------------------------------------
  async signup(email, password, name) {
    const r = await this._request("POST", "/auth/signup",
      { body: { email, password, name } });
    if (r && r.token) this.sessionToken = r.token;
    return r;
  }
  async login(email, password) {
    const r = await this._request("POST", "/auth/login",
      { body: { email, password } });
    if (r && r.token) this.sessionToken = r.token;
    return r;
  }
  me() { return this._request("GET", "/auth/me", { authed: true }); }
  async logout() {
    await this._request("POST", "/auth/logout", { authed: true });
    this.sessionToken = null;
  }
  authHealth() { return this._request("GET", "/auth/health"); }

  // ---- Spot market ------------------------------------------------
  exchangeInfo() { return this._request("GET", "/v3/exchangeInfo"); }
  depth(symbol, limit = 20) {
    return this._request("GET", "/v3/depth",
      { params: { symbol, limit } });
  }
  klines(symbol, interval, limit = 200) {
    return this._request("GET", "/v3/klines",
      { params: { symbol, interval, limit } });
  }
  recentTrades(symbol, limit = 50) {
    return this._request("GET", "/v3/trades",
      { params: { symbol, limit } });
  }
  ticker24h(symbol = null) {
    return this._request("GET", "/v3/ticker/24hr",
      { params: symbol ? { symbol } : null });
  }

  // ---- Spot trade (signed) ----------------------------------------
  placeOrder({ symbol, side, type, quantity = null, price = null,
               timeInForce = "GTC", quoteOrderQty = null,
               newClientOrderId = null, ...rest } = {}) {
    const params = {
      symbol, side, type,
      quantity, price, quoteOrderQty, newClientOrderId, ...rest,
    };
    if ((type || "").toUpperCase() === "LIMIT") {
      params.timeInForce = timeInForce;
    }
    return this._request("POST", "/v3/order",
      { params, signed: true });
  }
  cancelOrder({ symbol, orderId = null, origClientOrderId = null } = {}) {
    return this._request("DELETE", "/v3/order",
      { params: { symbol, orderId, origClientOrderId }, signed: true });
  }
  openOrders(symbol = null) {
    return this._request("GET", "/v3/openOrders",
      { params: symbol ? { symbol } : null, signed: true });
  }
  myTrades(symbol, limit = 50) {
    return this._request("GET", "/v3/myTrades",
      { params: { symbol, limit }, signed: true });
  }
  account() {
    return this._request("GET", "/v3/account",
      { params: null, signed: true });
  }
  withdraw({ asset, amount, address, network = null } = {}) {
    return this._request("POST", "/v3/withdraw",
      { params: { asset, amount, address, network }, signed: true });
  }

  // ---- Futures ----------------------------------------------------
  futuresExchangeInfo() { return this._request("GET", "/fapi/v1/exchangeInfo"); }
  premiumIndex(symbol = null) {
    return this._request("GET", "/fapi/v1/premiumIndex",
      { params: symbol ? { symbol } : null });
  }
  fundingRate(symbol = null, limit = 100) {
    return this._request("GET", "/fapi/v1/fundingRate",
      { params: { symbol, limit } });
  }
  futuresDepth(symbol, limit = 20) {
    return this._request("GET", "/fapi/v1/depth",
      { params: { symbol, limit } });
  }
  futuresAccount() {
    return this._request("GET", "/fapi/v1/account",
      { params: null, signed: true });
  }
  positionRisk(symbol = null) {
    return this._request("GET", "/fapi/v1/positionRisk",
      { params: symbol ? { symbol } : null, signed: true });
  }
  futuresPlaceOrder({ symbol, side, type, quantity, price = null,
                      timeInForce = null, reduceOnly = null,
                      positionSide = null, newClientOrderId = null,
                      ...rest } = {}) {
    return this._request("POST", "/fapi/v1/order", {
      params: {
        symbol, side, type, quantity, price,
        timeInForce,
        reduceOnly: reduceOnly === null ? null : (reduceOnly ? "true" : "false"),
        positionSide, newClientOrderId, ...rest,
      },
      signed: true,
    });
  }
  futuresClosePosition(symbol) {
    return this._request("POST", "/fapi/v1/closePosition",
      { params: { symbol }, signed: true });
  }
  futuresSetLeverage(symbol, leverage) {
    return this._request("POST", "/fapi/v1/leverage",
      { params: { symbol, leverage }, signed: true });
  }
  futuresSetMarginType(symbol, marginType) {
    return this._request("POST", "/fapi/v1/marginType",
      { params: { symbol, marginType }, signed: true });
  }
  futuresTransfer({ asset, amount, type } = {}) {
    return this._request("POST", "/fapi/v1/transfer",
      { params: { asset, amount, type }, signed: true });
  }
  futuresIncome(symbol = null) {
    return this._request("GET", "/fapi/v1/income",
      { params: symbol ? { symbol } : null, signed: true });
  }

  // ---- Chain ------------------------------------------------------
  chainInfo() { return this._request("GET", "/chain/info"); }
  chainWallet(chain = null) {
    return this._request("GET", "/chain/wallet",
      { params: chain ? { chain } : null, authed: true });
  }
  chainDeposits() { return this._request("GET", "/chain/deposits", { authed: true }); }
  chainWithdraws() { return this._request("GET", "/chain/withdraws", { authed: true }); }
  chainAirdrop(body = {}) {
    return this._request("POST", "/chain/airdrop", { body, authed: true });
  }
  chainWithdraw(body) {
    return this._request("POST", "/chain/withdraw", { body, authed: true });
  }

  // ---- PoL --------------------------------------------------------
  polServerInfo() { return this._request("GET", "/pol/server-info"); }
  polLatestEpoch() { return this._request("GET", "/pol/latest-epoch"); }
  polMyProof() { return this._request("GET", "/pol/my-proof", { authed: true }); }
  polRefresh() { return this._request("POST", "/pol/refresh", { authed: true }); }
  reservesVsLiabilities() {
    return this._request("GET", "/pol/reserves-vs-liabilities");
  }
  polSnapshotLatest() {
    return this._request("GET", "/pol-snapshot/api/v1/certificate/latest");
  }

  // ---- API keys ---------------------------------------------------
  createApiKey({ label, scopes = ["read"], expires_in_days = 90,
                 ip_allowlist = null, confirm_phrase = null } = {}) {
    return this._request("POST", "/api-keys/create", {
      body: { label, scopes, expires_in_days, ip_allowlist, confirm_phrase },
      authed: true,
    });
  }
  listApiKeys() { return this._request("GET", "/api-keys/list", { authed: true }); }
  revokeApiKey(keyId) {
    return this._request("DELETE", `/api-keys/${encodeURIComponent(keyId)}`,
      { authed: true });
  }

  // ---- Conditional orders -----------------------------------------
  conditionalOrder(body) {
    return this._request("POST", "/orders/conditional",
      { body, authed: true });
  }
  listConditionals() {
    return this._request("GET", "/orders/conditional", { authed: true });
  }
  cancelConditional(clientOrderId) {
    return this._request("DELETE",
      `/orders/conditional/${encodeURIComponent(clientOrderId)}`,
      { authed: true });
  }
}

export default ZkcexClient;
