/* =========================================================================
   zkCEX — futures (USDT-margined perpetual swaps) UI module
   - Wraps the /fapi/v1/* endpoints exposed by tools/perp_engine.py
   - Uses the same authedFetch helper as the spot pages (X-Opex-User on the
     proxy, signed-or-bearer auth lower down)
   - Demo note: limit orders accepted but settled at mark; no resting book yet
   ========================================================================= */
import { authedFetch, getSession, fmtPrice, fmtAmount, toast } from "./app.js";

// ---------- Public reads (no auth) ----------
export async function getExchangeInfo() {
  return await fetch("/fapi/v1/exchangeInfo").then(r => r.json());
}

export async function getPremiumIndex(symbol) {
  const u = symbol
    ? `/fapi/v1/premiumIndex?symbol=${encodeURIComponent(symbol)}`
    : `/fapi/v1/premiumIndex`;
  return await fetch(u).then(r => r.json());
}

export async function getFundingHistory(symbol, limit = 100) {
  const u = `/fapi/v1/fundingRate?symbol=${encodeURIComponent(symbol)}&limit=${limit}`;
  return await fetch(u).then(r => r.json());
}

// ---------- Authed reads ----------
export async function getPositions(symbol) {
  const q = symbol ? `?symbol=${encodeURIComponent(symbol)}` : "";
  return await authedFetch(`/fapi/v1/positionRisk${q}`);
}

export async function getAccount() {
  return await authedFetch("/fapi/v1/account");
}

export async function getOpenOrders(symbol) {
  const q = symbol ? `?symbol=${encodeURIComponent(symbol)}` : "";
  return await authedFetch(`/fapi/v1/openOrders${q}`);
}

export async function getUserTrades(symbol, limit = 100) {
  return await authedFetch(
    `/fapi/v1/userTrades?symbol=${encodeURIComponent(symbol)}&limit=${limit}`
  );
}

export async function getIncome({ symbol, incomeType, limit = 100 } = {}) {
  const params = [];
  if (symbol) params.push(`symbol=${encodeURIComponent(symbol)}`);
  if (incomeType) params.push(`incomeType=${encodeURIComponent(incomeType)}`);
  params.push(`limit=${limit}`);
  return await authedFetch(`/fapi/v1/income?${params.join("&")}`);
}

// ---------- Authed mutations ----------
export async function placeOrder({ symbol, side, type = "MARKET", quantity,
                                   price, reduceOnly = false, postOnly = false,
                                   timeInForce, clientOrderId }) {
  const body = new URLSearchParams();
  body.set("symbol", symbol);
  body.set("side", side);
  body.set("type", type);
  body.set("quantity", String(quantity));
  if (price) body.set("price", String(price));
  if (reduceOnly) body.set("reduceOnly", "true");
  if (postOnly) body.set("postOnly", "true");
  if (timeInForce) body.set("timeInForce", timeInForce);
  if (clientOrderId) body.set("newClientOrderId", clientOrderId);
  return await authedFetch("/fapi/v1/order", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
}

export async function cancelOrder({ symbol, orderId, clientOrderId }) {
  const params = new URLSearchParams();
  params.set("symbol", symbol);
  if (orderId) params.set("orderId", orderId);
  else if (clientOrderId) params.set("origClientOrderId", clientOrderId);
  return await authedFetch(`/fapi/v1/order?${params.toString()}`, {
    method: "DELETE",
  });
}

export async function closePosition(symbol) {
  const body = new URLSearchParams();
  body.set("symbol", symbol);
  return await authedFetch("/fapi/v1/closePosition", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
}

export async function setLeverage(symbol, leverage) {
  const body = new URLSearchParams();
  body.set("symbol", symbol);
  body.set("leverage", String(leverage));
  return await authedFetch("/fapi/v1/leverage", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
}

export async function setMarginType(symbol, marginType = "ISOLATED") {
  const body = new URLSearchParams();
  body.set("symbol", symbol);
  body.set("marginType", marginType);
  return await authedFetch("/fapi/v1/marginType", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
}

export async function transferUSDT(direction, amount) {
  // direction: "SPOT_TO_FUTURES" | "FUTURES_TO_SPOT"
  const body = new URLSearchParams();
  body.set("asset", "USDT");
  body.set("amount", String(amount));
  body.set("type", direction);
  return await authedFetch("/fapi/v1/transfer", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: body.toString(),
  });
}

// ---------- Live polling ----------
//
// Poll /fapi/v1/premiumIndex once a second; cheap, no WS in v0. The callback
// receives the whole shape so the caller can decide which fields to repaint.
// Returns a stop() function.
export function startLiveUpdates(symbol, onTick, intervalMs = 1000) {
  let stopped = false;
  let inflight = false;
  async function tick() {
    if (stopped || inflight) return;
    inflight = true;
    try {
      const data = await getPremiumIndex(symbol);
      if (!stopped) onTick(data);
    } catch (e) {
      // Soft-fail; the next tick will retry.
      console.warn("perp tick error", e);
    } finally {
      inflight = false;
    }
  }
  const t = setInterval(tick, intervalMs);
  tick();
  function stop() { stopped = true; clearInterval(t); }
  window.addEventListener("beforeunload", stop, { once: true });
  return stop;
}

// ---------- Formatting helpers tailored to perps ----------
export function fmtPnl(n, quote = "USDT") {
  const v = Number(n);
  if (!Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : "";
  // Use Intl so locale separators stay consistent with the rest of /app.
  const s = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(v);
  return `${sign}${s} ${quote}`;
}

export function fmtFundingPct(rate) {
  const v = Number(rate) * 100;
  if (!Number.isFinite(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return `${sign}${v.toFixed(4)}%`;
}

// Estimate liquidation price client-side — just for the order-form preview.
// We don't trust this for actual margin checks; the server recomputes it
// authoritatively when the position opens.
export function estimateLiquidationPrice({ side, entry, qty, leverage, mmr = 0.005 }) {
  const e = Number(entry), q = Number(qty), L = Number(leverage);
  if (!Number.isFinite(e) || !Number.isFinite(q) || !Number.isFinite(L) ||
      e <= 0 || q <= 0 || L <= 0) return null;
  const notional = e * q;
  const margin = notional / L;
  const maint = notional * mmr;
  const cushion = (margin - maint) / q;
  if (side === "LONG")  return Math.max(0, e - cushion);
  if (side === "SHORT") return e + cushion;
  return null;
}
