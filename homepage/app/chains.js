// chains.js — vanilla ES module for the multi-chain UI.
//
// The deposit / withdraw / wallet pages all need to:
//   - list registered chains and pick one (persisted in localStorage)
//   - render live RPC status (ok / degraded / red)
//   - hide write-style controls on read-only chains
//
// All endpoints are proxied through serve_homepage.py at /chain/*. The bearer
// token (when needed) is read from localStorage by app.js' authedFetch; here
// we only do plain fetch + lightweight bearer for /chain/wallet.

const STORAGE_KEY = "zkcex_chain";
const CHANGE_EVENT = "zkcex:chain-changed";

// Chains we know about even if /chain/info hasn't loaded yet, so the picker
// can render synchronously on first paint. Synced from chain_server.py's
// CHAINS list — keep in lockstep.
export const KNOWN_CHAINS = [
  { slug: "hardhat",          name: "Demo Hardhat",      shortKo: "데모",     shortEn: "Demo",      writable: true,  isDemo: true  },
  { slug: "sepolia",          name: "Sepolia",           shortKo: "세폴리아", shortEn: "Sepolia",   writable: false, isDemo: false },
  { slug: "polygon-amoy",     name: "Polygon Amoy",      shortKo: "Amoy",     shortEn: "Amoy",      writable: false, isDemo: false },
  { slug: "arbitrum-sepolia", name: "Arbitrum Sepolia",  shortKo: "Arb",      shortEn: "Arb Sep",   writable: false, isDemo: false },
];

function bearerHeader() {
  // app.js stores the session under "zkcex_session"; reach in directly so
  // chains.js stays standalone (no import cycle with app.js).
  try {
    const raw = localStorage.getItem("zkcex_session");
    if (!raw) return {};
    const s = JSON.parse(raw);
    return s?.token ? { Authorization: `Bearer ${s.token}` } : {};
  } catch {
    return {};
  }
}

async function jsonFetch(path, init = {}) {
  const headers = { Accept: "application/json", ...bearerHeader(), ...(init.headers || {}) };
  const resp = await fetch(path, { ...init, headers });
  let body = null;
  try { body = await resp.json(); } catch { /* non-JSON */ }
  if (!resp.ok) {
    const err = new Error(body?.error || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.body = body;
    throw err;
  }
  return body;
}

export async function listChains() {
  // Returns the full /chain/info payload: { default_chain, chains: [...] }.
  return jsonFetch("/chain/info");
}

export async function chainHealth() {
  return jsonFetch("/chain/health");
}

export async function walletFor(chainSlug) {
  const q = chainSlug ? `?chain=${encodeURIComponent(chainSlug)}` : "";
  return jsonFetch(`/chain/wallet${q}`);
}

export function getCurrentChainSlug() {
  try {
    return localStorage.getItem(STORAGE_KEY) || "hardhat";
  } catch {
    return "hardhat";
  }
}

export function setCurrentChainSlug(slug) {
  if (!slug) return;
  try { localStorage.setItem(STORAGE_KEY, slug); } catch {}
  // Fan-out to listeners on the same page.
  try {
    window.dispatchEvent(new CustomEvent(CHANGE_EVENT, { detail: { slug } }));
  } catch {}
}

export function isWritable(chainList, slug) {
  if (!Array.isArray(chainList)) return slug === "hardhat";
  const c = chainList.find(x => x.slug === slug);
  if (!c) return false;
  return Boolean(c.writable);
}

export function findChain(chainList, slug) {
  if (!Array.isArray(chainList)) return null;
  return chainList.find(c => c.slug === slug) || null;
}

// Compose the inline status pill shown above the chain selector. Returns an
// object { text, tone } so callers can insert it however they like.
export function chainStatusLine(chain) {
  if (!chain) return { text: "—", tone: "muted" };
  const status = chain.rpc_status || "unknown";
  const stale = Number(chain.stale_seconds || 0);
  let tone = "ok";
  if (status !== "ok") tone = "warn";
  if (stale > 600) tone = "err";
  const host = (() => {
    try { return new URL(chain.active_rpc || "").host || chain.active_rpc || "—"; }
    catch { return chain.active_rpc || "—"; }
  })();
  const blocks = chain.block_number != null
    ? formatBlockShort(chain.block_number)
    : "—";
  const latency = chain.latency_ms != null ? `${chain.latency_ms}ms` : "—";
  // Status dot is placed by the caller; we just give the textual fragment.
  const dot = tone === "ok" ? "🟢" : (tone === "warn" ? "🟡" : "🔴");
  return {
    tone,
    dot,
    text: `${host} · ${blocks} blocks · ${latency}`,
  };
}

function formatBlockShort(n) {
  const num = Number(n);
  if (!Number.isFinite(num)) return String(n);
  if (num >= 1_000_000) return (num / 1_000_000).toFixed(num >= 10_000_000 ? 0 : 1) + "M";
  if (num >= 1_000) return (num / 1_000).toFixed(num >= 100_000 ? 0 : 1) + "K";
  return String(num);
}

export const CHAIN_CHANGE_EVENT = CHANGE_EVENT;
