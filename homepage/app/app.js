/* =========================================================================
   zkCEX trading-app — shared module
   - Fetch helpers (relative /v3 paths, JSON, error wrapping)
   - Number / time formatters
   - Polling loop with cleanup on beforeunload
   - Toast notifier
   - Shared header injection
   - PWA bootstrap (service worker registration as a side effect)
   ========================================================================= */

// Side-effect import: registers /sw.js on first page load. Re-exports a
// couple of small helpers so callers don't have to know about pwa.js.
import "./pwa.js";
// Side-effect import: installs window.error / unhandledrejection listeners
// that forward to the central error collector (/errors/report). Silent and
// rate-limited; disable per-page with window.__ZKCEX_DISABLE_ERROR_REPORTER.
import "./error-reporter.js";
export { ensurePushSubscription, dropPushSubscription, isStandalone } from "./pwa.js";

// --- Live tickers we display by default. The real list comes from /v3/exchangeInfo.
export const DEFAULT_QUOTES = ["USDT", "BUSD", "IRT"];

// --- Asset display names (when they exist; falls back to symbol).
const ASSET_NAMES = {
  BTC:  { ko: "비트코인",   en: "Bitcoin" },
  ETH:  { ko: "이더리움",   en: "Ethereum" },
  USDT: { ko: "테더",       en: "Tether" },
  BUSD: { ko: "BUSD",       en: "BUSD" },
  BNB:  { ko: "BNB",        en: "BNB" },
  SOL:  { ko: "솔라나",     en: "Solana" },
  DOGE: { ko: "도지코인",   en: "Dogecoin" },
  IRT:  { ko: "이란 리알",  en: "Iranian Toman" },
};

export function assetMeta(asset) {
  const m = ASSET_NAMES[asset];
  return {
    ko: m?.ko ?? asset,
    en: m?.en ?? asset,
    iconClass: asset.toLowerCase(),
  };
}

// --- Fetch wrapper. Always relative paths so the proxy handles routing.
export async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { Accept: "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    const err = new Error(`HTTP ${res.status} on ${path}: ${txt.slice(0, 240)}`);
    err.status = res.status;
    err.body = txt;
    throw err;
  }
  // Some endpoints return empty 200 bodies (e.g. POST /order success). Be tolerant.
  const text = await res.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

// =====================================================================
//  Session — real auth via /auth/* + bearer token, plus X-Opex-User
//  shim for the legacy wallet/order endpoints that still expect it.
//  Stored shape: { token, user: { id, email, name, opex_user, kyc_status } }
// =====================================================================
const SESSION_KEY = "zkcex_session";
const SEEDED_PREFIX = "zkcex_seeded.";

export function getSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    if (!s || typeof s.token !== "string" || !s.token) return null;
    if (!s.user || typeof s.user !== "object") return null;
    if (typeof s.user.opex_user !== "string" || !s.user.opex_user) return null;
    return s;
  } catch {
    return null;
  }
}

// Convenience accessor for the underlying X-Opex-User string.
export function sessionOpex(s) {
  if (!s) s = getSession();
  return s?.user?.opex_user || null;
}

// Replace the entire session payload in localStorage.
export function setSession(payload) {
  if (!payload || typeof payload.token !== "string") {
    throw new Error("setSession: missing token");
  }
  localStorage.setItem(SESSION_KEY, JSON.stringify(payload));
  window.dispatchEvent(new CustomEvent("zkcex:session-changed", { detail: payload }));
  return payload;
}

// Update only the cached user object (e.g. after KYC completes).
export function patchSessionUser(patch) {
  const s = getSession();
  if (!s) return null;
  const merged = { ...s, user: { ...s.user, ...patch } };
  return setSession(merged);
}

export function clearSession() {
  localStorage.removeItem(SESSION_KEY);
  for (let i = localStorage.length - 1; i >= 0; i--) {
    const k = localStorage.key(i);
    if (k && k.startsWith(SEEDED_PREFIX)) localStorage.removeItem(k);
  }
  window.dispatchEvent(new CustomEvent("zkcex:session-changed", { detail: null }));
}

export function requireSession({ redirectTo = "/app/signin.html" } = {}) {
  const s = getSession();
  if (!s) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.href = `${redirectTo}?next=${next}`;
    return null;
  }
  return s;
}

// requireKyc: like requireSession but also forwards to /app/kyc.html when
// kyc_status is anything but 'verified'. Refreshes against /auth/me first
// so a stale localStorage cache doesn't bounce someone who already verified.
export async function requireKyc({ redirectTo = "/app/signin.html",
                                  kycRedirect = "/app/kyc.html" } = {}) {
  const s = requireSession({ redirectTo });
  if (!s) return null;
  const fresh = await refreshMe();
  if (!fresh) return null;  // refreshMe handles the redirect on 401
  if ((fresh.user?.kyc_status || "none") !== "verified") {
    const next = encodeURIComponent(location.pathname + location.search);
    location.href = `${kycRedirect}?next=${next}`;
    return null;
  }
  return fresh;
}

// Hit /auth/me, update local cache, return fresh session or null on 401.
export async function refreshMe() {
  const s = getSession();
  if (!s) return null;
  try {
    const res = await fetch("/auth/me", {
      headers: { Authorization: `Bearer ${s.token}` },
    });
    if (res.status === 401) {
      clearSession();
      const next = encodeURIComponent(location.pathname + location.search);
      location.href = `/app/signin.html?next=${next}`;
      return null;
    }
    if (!res.ok) return s;
    const body = await res.json();
    if (body && body.user) return setSession({ token: s.token, user: body.user });
    return s;
  } catch {
    return s;  // offline — keep cached session
  }
}

// authedFetch: wrapper around api() that injects Authorization for /auth/* and
// /kyc/*, and X-Opex-User for everything else (the wallet/order/deposit
// endpoints still consume that header).
export async function authedFetch(path, init = {}) {
  const s = getSession();
  const method = (init.method || "GET").toUpperCase();
  const mutating = method !== "GET" && method !== "HEAD";
  if (!s && mutating) {
    const err = new Error("not signed in");
    err.code = "NO_SESSION";
    throw err;
  }
  const headers = { ...(init.headers || {}) };
  // /auth, /kyc, and /chain all live on demo-only stdlib servers that consume
  // the bearer token directly. The legacy wallet/order endpoints still rely
  // on X-Opex-User from the proxy.
  const wantsBearer =
    path.startsWith("/auth/") ||
    path.startsWith("/kyc/") ||
    path.startsWith("/chain/") ||
    path.startsWith("/pol/") ||
    path.startsWith("/zk-trade/") ||
    path.startsWith("/fees/") ||
    path.startsWith("/nft/") ||
    path.startsWith("/lending/");
  if (s) {
    if (wantsBearer) {
      headers["Authorization"] = `Bearer ${s.token}`;
    } else {
      headers["X-Opex-User"] = s.user.opex_user;
    }
  }
  return api(path, { ...init, headers });
}

// =====================================================================
//  /chain — on-chain ramp (deposit/withdraw) helpers. Each just wraps
//  authedFetch so the caller doesn't have to remember headers.
// =====================================================================
export const chain = {
  info()      { return api("/chain/info"); },
  wallet()    { return authedFetch("/chain/wallet"); },
  airdrop(asset) {
    return authedFetch("/chain/airdrop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(asset ? { asset } : {}),
    });
  },
  airdropTo(address) {
    return authedFetch("/chain/airdrop-to", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ address }),
    });
  },
  send(asset, amount) {
    return authedFetch("/chain/send-from-user-wallet", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset, amount: String(amount) }),
    });
  },
  deposits()  { return authedFetch("/chain/deposits"); },
  detect()    { return authedFetch("/chain/deposit-detect", { method: "POST" }); },
  withdraw(asset, amount, destination) {
    return authedFetch("/chain/withdraw", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ asset, amount: String(amount), destination }),
    });
  },
  withdraws() { return authedFetch("/chain/withdraws"); },
};

export function isSeeded(user) {
  if (!user) return false;
  return localStorage.getItem(SEEDED_PREFIX + user) === "true";
}
export function markSeeded(user) {
  if (!user) return;
  localStorage.setItem(SEEDED_PREFIX + user, "true");
}

// Manual top-up — server signup already auto-seeds, but the wallet page
// keeps a "Get demo funds" button that calls this.
export async function seedDemoFunds(user) {
  if (!user || isSeeded(user)) return;
  const ts = Date.now();
  const calls = [
    fetch(`/deposit/10_test-ethereum_ETH/${encodeURIComponent(user)}_MAIN?description=demo-seed&transferRef=demo-eth-${ts}`,
      { method: "POST" }),
    fetch(`/deposit/10_test-ethereum_USDT/${encodeURIComponent(user)}_MAIN?description=demo-seed&transferRef=demo-usd-${ts}`,
      { method: "POST" }),
  ];
  try {
    await Promise.allSettled(calls);
  } catch { /* ignore */ }
  markSeeded(user);
}

// --- Polling loop with auto cleanup. Returns a stop() handle.
export function poll(fn, intervalMs, opts = {}) {
  const { onError, immediate = true } = opts;
  let timer = null;
  let stopped = false;
  let inflight = false;

  async function tick() {
    if (stopped || inflight) return;
    inflight = true;
    try {
      await fn();
    } catch (e) {
      if (onError) onError(e);
      else console.warn("poll error", e);
    } finally {
      inflight = false;
    }
  }

  if (immediate) tick();
  timer = setInterval(tick, intervalMs);

  function stop() {
    stopped = true;
    if (timer) clearInterval(timer);
    timer = null;
  }
  // auto cleanup
  window.addEventListener("beforeunload", stop, { once: true });
  return stop;
}

// --- Number formatters
const _nfCache = new Map();
function nfCached(opts) {
  const k = JSON.stringify(opts);
  if (!_nfCache.has(k)) {
    _nfCache.set(k, new Intl.NumberFormat("en-US", opts));
  }
  return _nfCache.get(k);
}

export function fmtPrice(v, asset = "USDT", quote = "USDT") {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return "—";
  // Quote-asset precision conventions
  const decimals = priceDecimals(asset, quote, n);
  return nfCached({ minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(n);
}

export function priceDecimals(base, quote, n) {
  // Quote-driven: USDT/BUSD prices use 2dp for big-cap, more for cheap assets.
  // IRT prices for big-cap are massive — use no decimals.
  if (quote === "IRT") return 0;
  if (n >= 1000) return 2;
  if (n >= 1)    return 4;
  if (n >= 0.01) return 6;
  return 8;
}

export function fmtAmount(v, asset = "BTC") {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const decimals = amountDecimals(asset);
  return nfCached({ minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(n);
}

export function amountDecimals(asset) {
  if (asset === "BTC")  return 6;
  if (asset === "ETH")  return 4;
  if (asset === "BNB")  return 3;
  if (asset === "SOL")  return 3;
  if (asset === "DOGE") return 2;
  if (asset === "USDT" || asset === "BUSD") return 2;
  if (asset === "IRT")  return 0;
  return 4;
}

export function fmtVolume(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return "0";
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(2) + "K";
  return nfCached({ maximumFractionDigits: 2 }).format(n);
}

export function fmtPct(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "" : "";
  return `${sign}${n.toFixed(2)}%`;
}

export function fmtTime(ms) {
  if (!ms) return "—";
  return new Date(Number(ms)).toLocaleTimeString();
}

export function fmtRelative(ms) {
  const diff = Math.max(0, Date.now() - Number(ms || 0)) / 1000;
  if (diff < 1)  return "just now";
  if (diff < 60) return `${Math.floor(diff)}s`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  return `${Math.floor(diff / 3600)}h`;
}

// --- Toast notifier (singleton)
let toastHost = null;
function ensureToastHost() {
  if (!toastHost) {
    toastHost = document.createElement("div");
    toastHost.className = "toast-host";
    document.body.appendChild(toastHost);
  }
  return toastHost;
}
export function toast(ko, en) {
  const host = ensureToastHost();
  const el = document.createElement("div");
  el.className = "toast";
  el.innerHTML = `${escapeHtml(ko)}${en ? `<span class="en">${escapeHtml(en)}</span>` : ""}`;
  host.appendChild(el);
  // animate
  requestAnimationFrame(() => el.classList.add("shown"));
  setTimeout(() => {
    el.classList.remove("shown");
    setTimeout(() => el.remove(), 220);
  }, 2400);
}

export function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// --- Header (shared across app/* pages)
//
// Renders synchronously with the current session state to avoid any
// post-hydration flash. If the session changes (sign-in / sign-out elsewhere)
// the header re-renders on the `zkcex:session-changed` event.
export function renderHeader(active) {
  const tabs = [
    { id: "dashboard",    href: "index.html",              ko: "대시보드",   en: "Dashboard" },
    { id: "markets",      href: "index.html",              ko: "시장",       en: "Markets",       alias: "dashboard" },
    { id: "trade",        href: "trade.html",              ko: "거래",       en: "Trade" },
    { id: "wallet",       href: "wallet.html",             ko: "지갑",       en: "Wallet" },
    { id: "verify",       href: "verify.html",             ko: "잔고 검증",  en: "Verify" },
    { id: "transparency", href: "proof-of-reserves.html",  ko: "투명성",     en: "Transparency" },
  ];
  const navItems = tabs.filter(t => t.id !== "dashboard");

  function buildHtml() {
    const session = getSession();
    const nav = navItems.map(t => {
      const cls = (t.id === active || t.alias === active) ? "active" : "";
      return `<a href="${t.href}" class="${cls}">
        <span>${t.ko}</span>
        <span class="nav-en">${t.en}</span>
      </a>`;
    }).join("");

    let cta;
    if (!session) {
      cta = `
        <a class="home-link" href="../">← 홈으로 / Home</a>
        <a class="btn-link-quiet" href="signin.html">로그인 / Sign in</a>
        <a class="btn btn-primary btn-sm" href="signin.html?next=${encodeURIComponent("/app/")}">
          시작하기 <span class="btn-en">/ Get started</span>
        </a>
      `;
    } else {
      const u = session.user;
      const verified = (u.kyc_status || "none") === "verified";
      const kycBadge = verified
        ? `<span class="kyc-badge kyc-ok" title="본인인증 완료">
             <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true">
               <path d="M5 12l4 4L19 7" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>
             </svg>
             KYC 완료
           </span>`
        : `<a class="kyc-badge kyc-pending" href="kyc.html" title="본인인증이 필요합니다">
             <span class="kyc-dot"></span> KYC 미완료
           </a>`;
      const display = u.name ? escapeHtml(u.name) : escapeHtml(u.email);
      // Notification bell + unread badge. Polled every 10s; click opens a
      // dropdown with the last 5 notifications and a "View all" link.
      const bell = `
        <div class="notif-bell-wrap" id="notif-bell-wrap">
          <button class="notif-bell" type="button" aria-label="알림함 / Notifications"
                  aria-haspopup="menu" aria-expanded="false">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M18 16v-5a6 6 0 1 0-12 0v5l-2 2v1h16v-1l-2-2z"/>
              <path d="M9 21a3 3 0 0 0 6 0"/>
            </svg>
            <span class="notif-bell-badge" id="notif-bell-badge" hidden>0</span>
          </button>
          <div class="notif-bell-pop" role="menu" id="notif-bell-pop" hidden>
            <div class="notif-bell-list" id="notif-bell-list">
              <div class="muted" style="padding:14px;text-align:center;font-size:13px">불러오는 중… / Loading…</div>
            </div>
            <a class="notif-bell-all" href="inbox.html">전체 보기 / View all</a>
          </div>
        </div>
      `;
      cta = `
        <a class="home-link" href="../">← 홈으로 / Home</a>
        <span class="header-balance mono" id="header-balance"></span>
        ${kycBadge}
        ${bell}
        <div class="user-menu">
          <button class="user-pill" type="button" aria-haspopup="menu" aria-expanded="false">
            <span class="user-pill-dot"></span>
            <span>${display}</span>
            <span class="caret">▾</span>
          </button>
          <div class="user-menu-pop" role="menu">
            <div class="user-menu-meta">
              <div class="muted">${escapeHtml(u.email || "")}</div>
              <div class="dim mono" style="font-size:11px;">${escapeHtml(u.opex_user)}</div>
            </div>
            <a role="menuitem" href="wallet.html">지갑 <span class="menu-en">/ Wallet</span></a>
            <a role="menuitem" href="staking.html">스테이킹 <span class="menu-en">/ Staking</span></a>
            <a role="menuitem" href="earn.html">적립 / 대출 <span class="menu-en">/ Earn &amp; Borrow</span></a>
            <a role="menuitem" href="futures.html">선물 <span class="menu-en">/ Futures</span></a>
            <a role="menuitem" href="zk-trade.html">ZK 거래 <span class="menu-en">/ ZK trade</span></a>
            <a role="menuitem" href="wallet.html#orders">주문 내역 <span class="menu-en">/ Order history</span></a>
            <a role="menuitem" href="reports.html">리포트 <span class="menu-en">/ Reports</span></a>
            <a role="menuitem" href="verify.html">잔고 검증 <span class="menu-en">/ Verify</span></a>
            <a role="menuitem" href="anchor.html">온체인 <span class="menu-en">/ On-chain</span></a>
            ${verified
              ? `<a role="menuitem" href="kyc.html" class="dim">본인인증 정보 <span class="menu-en">/ KYC info</span></a>`
              : `<a role="menuitem" href="kyc.html">본인인증 <span class="menu-en">/ Complete KYC</span></a>`}
            <a role="menuitem" href="fees.html">VIP 등급 / 수수료 <span class="menu-en">/ VIP fees</span></a>
            <a role="menuitem" href="referral.html">친구 초대 <span class="menu-en">/ Referral</span></a>
            <a role="menuitem" href="api-keys.html">API 키 <span class="menu-en">/ API keys</span></a>
            <a role="menuitem" href="security.html">보안 / 2FA <span class="menu-en">/ Security</span></a>
            <a role="menuitem" href="api-docs.html">API 문서 <span class="menu-en">/ API docs</span></a>
            <a role="menuitem" href="mcp.html">AI 에이전트 <span class="menu-en">/ MCP</span></a>
            <button role="menuitem" type="button" data-action="signout">로그아웃 <span class="menu-en">/ Sign out</span></button>
          </div>
        </div>
      `;
    }

    return `
      <header class="app-header" id="app-header">
        <div class="app-header-row">
          <a class="brand" href="index.html"><span class="brand-mark"></span>zkCEX</a>
          <nav class="app-nav">${nav}</nav>
          <div class="app-header-cta">${cta}</div>
        </div>
      </header>
    `;
  }

  function buildBottomNav() {
    // Five-item bottom nav. The 5th opens a bottom sheet with the rest of the
    // menu. We always render this — CSS hides it above 720px.
    const items = [
      { id: "markets", href: "index.html", ko: "시장",   icon: marketsIcon() },
      { id: "trade",   href: "trade.html", ko: "거래",   icon: tradeIcon() },
      { id: "wallet",  href: "wallet.html", ko: "지갑",  icon: walletIcon() },
      { id: "verify",  href: "verify.html", ko: "검증",  icon: verifyIcon() },
      { id: "more",    href: "#more",      ko: "더보기", icon: moreIcon(),  more: true },
    ];
    return `
      <nav class="bottom-nav" id="bottom-nav" aria-label="모바일 메뉴">
        ${items.map(t => {
          const cls = (t.id === active || t.alias === active) ? "active" : "";
          const attrs = t.more
            ? `href="#more" data-action="open-more" role="button"`
            : `href="${t.href}"`;
          return `<a ${attrs} class="${cls}" data-id="${t.id}">
            <span class="bn-icon" aria-hidden="true">${t.icon}</span>
            <span class="bn-label">${t.ko}</span>
          </a>`;
        }).join("")}
      </nav>
    `;
  }

  function buildMoreSheet() {
    const session = getSession();
    const u = session?.user;
    const userBlock = u ? `
      <div class="more-meta">
        <div class="muted">${escapeHtml(u.email || "")}</div>
        <div class="dim mono" style="font-size:11px;">${escapeHtml(u.opex_user)}</div>
      </div>` : `
      <div class="more-meta">
        <a class="btn btn-primary btn-sm" href="signin.html">로그인 / Sign in</a>
      </div>`;
    const verified = u && (u.kyc_status || "none") === "verified";
    const links = [
      ["futures.html",        "선물 / Futures"],
      ["staking.html",        "스테이킹 / Staking"],
      ["earn.html",           "적립 / 대출 — Earn & Borrow"],
      ["reports.html",        "리포트 / Reports"],
      ["proof-of-reserves.html", "투명성 / Transparency"],
      ["fees.html",           "VIP 등급 / VIP fees"],
      ["api-keys.html",       "API 키 / API keys"],
      ["security.html",       "보안 / Security"],
      ["mcp.html",            "AI 에이전트 / MCP"],
      ["custody.html",        "커스터디 / Custody"],
      ["notifications.html",  "푸시 알림 / Notifications"],
      ["kyc.html",            verified ? "본인인증 정보 / KYC info" : "본인인증 / Complete KYC"],
    ];
    const linksHtml = links.map(([h, l]) => `<a href="${h}">${l}</a>`).join("");
    const signOut = u
      ? `<button type="button" class="more-signout" data-action="signout">로그아웃 / Sign out</button>`
      : "";
    return `
      <div class="more-sheet-backdrop" id="more-sheet-backdrop" hidden></div>
      <div class="more-sheet" id="more-sheet" hidden role="dialog" aria-modal="true" aria-label="더보기">
        <div class="more-sheet-handle" aria-hidden="true"></div>
        ${userBlock}
        <nav class="more-links">${linksHtml}</nav>
        ${signOut}
      </div>
    `;
  }

  function mountAndWire() {
    const html = buildHtml();
    const existing = document.getElementById("app-header");
    if (existing) existing.outerHTML = html;
    else document.body.insertAdjacentHTML("afterbegin", html);

    // Wire sign-out button (event delegation would also work).
    const so = document.querySelector('#app-header [data-action="signout"]');
    if (so) {
      so.addEventListener("click", async () => { await doSignOut(); });
    }

    // Bottom nav: replace existing or append.
    const existingNav = document.getElementById("bottom-nav");
    const navHtml = buildBottomNav();
    if (existingNav) existingNav.outerHTML = navHtml;
    else document.body.insertAdjacentHTML("beforeend", navHtml);

    // More sheet: replace existing or append.
    const existingSheet = document.getElementById("more-sheet");
    const existingBackdrop = document.getElementById("more-sheet-backdrop");
    const sheetHtml = buildMoreSheet();
    if (existingSheet) existingSheet.remove();
    if (existingBackdrop) existingBackdrop.remove();
    document.body.insertAdjacentHTML("beforeend", sheetHtml);

    // Wire "more" opener + close + sign-out button in the sheet.
    const moreLink = document.querySelector('#bottom-nav [data-action="open-more"]');
    moreLink?.addEventListener("click", e => {
      e.preventDefault();
      const sheet = document.getElementById("more-sheet");
      const back = document.getElementById("more-sheet-backdrop");
      sheet.hidden = false;
      back.hidden = false;
      requestAnimationFrame(() => {
        sheet.classList.add("open");
        back.classList.add("open");
      });
    });
    document.getElementById("more-sheet-backdrop")?.addEventListener("click", closeMoreSheet);
    document.querySelector('#more-sheet [data-action="signout"]')?.addEventListener("click", async () => {
      await doSignOut();
    });

    wireNotifBell();
  }

  // Notification bell: poll /notifications/unread-count every 10s, render a
  // dropdown of the last 5 entries on click. All requests use authedFetch so
  // they 401 cleanly while signed out (in which case we just don't render).
  let _notifBellTimer = null;
  function wireNotifBell() {
    const bell = document.querySelector("#app-header .notif-bell");
    if (!bell) return;
    const pop = document.getElementById("notif-bell-pop");
    bell.addEventListener("click", (e) => {
      e.stopPropagation();
      const willOpen = pop.hidden;
      // close other menus when opening (best-effort)
      pop.hidden = !willOpen;
      bell.setAttribute("aria-expanded", String(willOpen));
      if (willOpen) loadNotifBellList();
    });
    document.addEventListener("click", (e) => {
      const wrap = document.getElementById("notif-bell-wrap");
      if (!wrap || wrap.contains(e.target)) return;
      pop.hidden = true;
      bell.setAttribute("aria-expanded", "false");
    });
    refreshNotifBellBadge();
    if (_notifBellTimer) clearInterval(_notifBellTimer);
    _notifBellTimer = setInterval(refreshNotifBellBadge, 10000);
  }

  async function refreshNotifBellBadge() {
    const badge = document.getElementById("notif-bell-badge");
    if (!badge) return;
    try {
      const r = await authedFetch("/notifications/unread-count");
      if (!r.ok) { badge.hidden = true; return; }
      const j = await r.json();
      const n = Number(j.count || 0);
      if (n > 0) {
        badge.textContent = n > 99 ? "99+" : String(n);
        badge.hidden = false;
      } else {
        badge.hidden = true;
      }
    } catch {
      badge.hidden = true;
    }
  }

  async function loadNotifBellList() {
    const host = document.getElementById("notif-bell-list");
    if (!host) return;
    try {
      const r = await authedFetch("/notifications/inbox?limit=5");
      if (!r.ok) throw new Error("HTTP " + r.status);
      const j = await r.json();
      const list = j.notifications || [];
      if (!list.length) {
        host.innerHTML = `<div class="muted" style="padding:14px;text-align:center;font-size:13px">알림이 없습니다 / No notifications</div>`;
        return;
      }
      host.innerHTML = list.map(n => `
        <a class="notif-bell-item ${n.read ? "" : "unread"}" href="inbox.html">
          <div class="notif-bell-item-title">${escapeHtml(n.title)}</div>
          <div class="notif-bell-item-body">${escapeHtml(n.body)}</div>
        </a>
      `).join("");
    } catch {
      host.innerHTML = `<div class="muted" style="padding:14px;text-align:center;font-size:13px">불러올 수 없습니다 / Could not load</div>`;
    }
  }

  function closeMoreSheet() {
    const sheet = document.getElementById("more-sheet");
    const back = document.getElementById("more-sheet-backdrop");
    if (!sheet || !back) return;
    sheet.classList.remove("open");
    back.classList.remove("open");
    setTimeout(() => {
      sheet.hidden = true;
      back.hidden = true;
    }, 200);
  }

  async function doSignOut() {
    const s = getSession();
    if (s) {
      try {
        await fetch("/auth/logout", {
          method: "POST",
          headers: { Authorization: `Bearer ${s.token}` },
        });
      } catch { /* ignore */ }
    }
    clearSession();
    location.href = "/";
  }

  // ---- inline SVG icons (kept tiny so they ship inside renderHeader)
  function marketsIcon() {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" width="20" height="20">
      <path d="M3 17l5-5 4 4 9-9"/><path d="M14 7h7v7"/></svg>`;
  }
  function tradeIcon() {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" width="20" height="20">
      <path d="M7 7h13"/><path d="M16 3l4 4-4 4"/>
      <path d="M17 17H4"/><path d="M8 13l-4 4 4 4"/></svg>`;
  }
  function walletIcon() {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" width="20" height="20">
      <rect x="3" y="6" width="18" height="14" rx="3"/>
      <path d="M3 10h18"/><circle cx="17" cy="15" r="1.2" fill="currentColor"/></svg>`;
  }
  function verifyIcon() {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" width="20" height="20">
      <path d="M12 3l8 4v5c0 4.5-3.4 8.4-8 9-4.6-.6-8-4.5-8-9V7l8-4z"/>
      <path d="M9 12l2 2 4-4"/></svg>`;
  }
  function moreIcon() {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
      stroke-linecap="round" stroke-linejoin="round" width="20" height="20">
      <circle cx="5" cy="12" r="1.5" fill="currentColor"/>
      <circle cx="12" cy="12" r="1.5" fill="currentColor"/>
      <circle cx="19" cy="12" r="1.5" fill="currentColor"/></svg>`;
  }

  mountAndWire();
  // Re-render on session change (sign in from another tab, etc.)
  window.addEventListener("zkcex:session-changed", () => mountAndWire());
}

export function renderFooter() {
  const html = `
    <footer class="app-footer">
      <div class="app-footer-row">
        <a class="brand" href="index.html"><span class="brand-mark"></span>zkCEX</a>
        <span>실시간 거래 데이터 · 5분 주기 PoL · 컴플라이언스 친화 거래소</span>
        <span class="links">
          <a href="index.html">시장 / Markets</a>
          <a href="trade.html?symbol=ETHUSDT">거래 / Trade</a>
          <a href="verify.html">잔고 검증 / Verify</a>
          <a href="../">홈 / Home</a>
        </span>
      </div>
    </footer>
  `;
  const slot = document.getElementById("app-footer");
  if (slot) slot.outerHTML = html;
  else document.body.insertAdjacentHTML("beforeend", html);
}

// --- Update-tick UI helper
export function makeUpdateTick(el) {
  let lastOk = Date.now();
  return {
    ok() {
      lastOk = Date.now();
      el.classList.remove("warn");
      el.textContent = `방금 갱신 · just now`;
    },
    fail() {
      el.classList.add("warn");
      el.textContent = `⚠ 데이터 갱신 실패, 재시도 중… / Failed to refresh, retrying…`;
    },
    refresh() {
      if (el.classList.contains("warn")) return;
      const s = Math.floor((Date.now() - lastOk) / 1000);
      el.textContent = s < 1
        ? `방금 갱신 · just now`
        : `${s}초 전 갱신 · ${s}s ago`;
    },
  };
}

// --- query-string helpers
export function getQuery(name, fallback) {
  const u = new URLSearchParams(window.location.search);
  return u.get(name) ?? fallback;
}

// =====================================================================
//  EIP-1193 wallet helpers (MetaMask et al). Raw `window.ethereum.request`
//  only — no ethers/web3 dependency. Used by deposit.html for the
//  "sign in your own wallet" path. Throws on user rejection / RPC errors so
//  the caller can show a toast.
// =====================================================================

// Local hardhat dev chain config for `wallet_addEthereumChain`. We don't
// configure a block explorer because there isn't one for a local node.
const HARDHAT_CHAIN = {
  chainId: "0x7a69",          // 31337
  chainName: "zkCEX Demo Chain",
  rpcUrls: ["http://127.0.0.1:8545"],
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
};

// Pad a hex string (no 0x) to 32 bytes (64 chars) by left-zero-fill.
function padWord(hexNo0x) {
  if (hexNo0x.length > 64) throw new Error("hex word too long: " + hexNo0x);
  return "0".repeat(64 - hexNo0x.length) + hexNo0x;
}

// Convert a decimal string `amount` to a uint256 wei integer (BigInt) for the
// given decimals. Throws on negatives or overlong fractions.
function decimalToWei(amountStr, decimals) {
  const s = String(amountStr ?? "").trim();
  if (!s) throw new Error("empty amount");
  if (!/^\d+(\.\d+)?$/.test(s)) throw new Error("bad amount: " + s);
  const [intPart, fracPart = ""] = s.split(".");
  if (fracPart.length > decimals) {
    throw new Error(`amount has more than ${decimals} decimals`);
  }
  const padded = (intPart + fracPart).padEnd(intPart.length + decimals, "0");
  // Trim leading zeros but keep at least one digit.
  const trimmed = padded.replace(/^0+/, "") || "0";
  return BigInt(trimmed);
}

export const eip1193 = {
  isAvailable() { return typeof window !== "undefined" && typeof window.ethereum !== "undefined"; },

  getProvider() {
    if (!this.isAvailable()) return null;
    return window.ethereum;
  },

  async _req(method, params) {
    const p = this.getProvider();
    if (!p) throw new Error("MetaMask not available");
    return p.request({ method, params: params ?? [] });
  },

  async chainId() { return this._req("eth_chainId"); },

  async accounts() { return this._req("eth_accounts"); },

  async connect() {
    const accounts = await this._req("eth_requestAccounts");
    if (!accounts || !accounts.length) throw new Error("no account");
    return accounts[0];
  },

  async ensureChain(chainIdHex = HARDHAT_CHAIN.chainId) {
    try {
      await this._req("wallet_switchEthereumChain", [{ chainId: chainIdHex }]);
      return true;
    } catch (e) {
      // 4902 = chain not added; fall through to add. Some wallets nest the
      // code under e.data — try both.
      const code = e?.code ?? e?.data?.originalError?.code;
      if (code === 4902 || /Unrecognized chain ID/i.test(e?.message || "")) {
        await this._req("wallet_addEthereumChain", [HARDHAT_CHAIN]);
        return true;
      }
      throw e;
    }
  },

  async getNativeBalance(addr) {
    const hex = await this._req("eth_getBalance", [addr, "latest"]);
    return BigInt(hex || "0x0");
  },

  // ERC-20 balanceOf via eth_call. Returns BigInt of base units.
  async getErc20Balance(addr, tokenAddr) {
    const SEL = "0x70a08231"; // balanceOf(address)
    const data = SEL + padWord(addr.toLowerCase().replace(/^0x/, ""));
    const hex = await this._req("eth_call", [{ to: tokenAddr, data }, "latest"]);
    return BigInt(hex && hex !== "0x" ? hex : "0x0");
  },

  // Build calldata + send eth_sendTransaction for transfer(to, amount).
  // Returns { txHash, data } so the caller can show the data for debugging.
  async sendErc20Transfer({ from, tokenAddr, to, amount, decimals }) {
    const SEL = "0xa9059cbb"; // transfer(address,uint256)
    const valueWei = decimalToWei(amount, decimals);
    const data =
      SEL +
      padWord(to.toLowerCase().replace(/^0x/, "")) +
      padWord(valueWei.toString(16));
    const txHash = await this._req("eth_sendTransaction", [{
      from,
      to: tokenAddr,
      data,
      // Don't set gas — MetaMask estimates. Hardhat is fine with that.
    }]);
    return { txHash, data };
  },

  async getReceipt(txHash) {
    return this._req("eth_getTransactionReceipt", [txHash]);
  },

  async waitForReceipt(txHash, { intervalMs = 1000, timeoutMs = 60000 } = {}) {
    const t0 = Date.now();
    // eslint-disable-next-line no-constant-condition
    while (true) {
      const r = await this.getReceipt(txHash);
      if (r) return r;
      if (Date.now() - t0 > timeoutMs) {
        throw new Error("timed out waiting for receipt");
      }
      await new Promise((res) => setTimeout(res, intervalMs));
    }
  },

  // --- helpers exposed for tests / callers ---
  _padWord: padWord,
  _decimalToWei: decimalToWei,
  HARDHAT_CHAIN,
};

// --- Tiny inline sparkline from klines.
// klines: [[openTime, o, h, l, c, v, …], …]
export function renderSparkline(svg, klines) {
  const ns = "http://www.w3.org/2000/svg";
  while (svg.firstChild) svg.removeChild(svg.firstChild);

  const closes = (klines || [])
    .map(k => Number(k[4]))
    .filter(n => Number.isFinite(n) && n > 0);

  const w = svg.clientWidth || 110;
  const h = svg.clientHeight || 36;

  if (closes.length < 2) {
    // Draw a flat line as placeholder
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", 0); line.setAttribute("x2", w);
    line.setAttribute("y1", h / 2); line.setAttribute("y2", h / 2);
    line.setAttribute("stroke", "rgba(255,255,255,0.10)");
    line.setAttribute("stroke-width", "1");
    line.setAttribute("stroke-dasharray", "3 3");
    svg.appendChild(line);
    return;
  }

  let lo = Infinity, hi = -Infinity;
  for (const c of closes) { if (c < lo) lo = c; if (c > hi) hi = c; }
  if (lo === hi) { lo -= 1; hi += 1; }

  const pad = 2;
  const xStep = (w) / (closes.length - 1);
  const points = closes.map((c, i) => {
    const x = i * xStep;
    const y = pad + (h - pad * 2) * (1 - (c - lo) / (hi - lo));
    return [x, y];
  });

  const up = closes[closes.length - 1] >= closes[0];
  const stroke = up ? "#00b87c" : "#ff4d63";
  const fill   = up ? "rgba(0, 184, 124, 0.14)" : "rgba(255, 77, 99, 0.14)";

  const d = points.map((p, i) => (i === 0 ? "M" : "L") + p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ");
  const area = d + ` L${w.toFixed(1)},${h} L0,${h} Z`;

  const fillPath = document.createElementNS(ns, "path");
  fillPath.setAttribute("d", area);
  fillPath.setAttribute("fill", fill);
  svg.appendChild(fillPath);

  const linePath = document.createElementNS(ns, "path");
  linePath.setAttribute("d", d);
  linePath.setAttribute("fill", "none");
  linePath.setAttribute("stroke", stroke);
  linePath.setAttribute("stroke-width", "1.5");
  linePath.setAttribute("stroke-linejoin", "round");
  linePath.setAttribute("stroke-linecap", "round");
  svg.appendChild(linePath);
}
