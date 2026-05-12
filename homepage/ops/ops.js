/* zkCEX operator console — shared helpers.
   Talks to the ops_server JSON API mounted at /ops-api/. The static HTML
   pages live at /ops/*.html and are served by the same Python process
   (or by serve_homepage as a static fallback). */

const OPS_API = "/ops-api";
const SESSION_KEY = "zkcex.ops.session";

export function loadSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    if (!s.token || !s.expires_at) return null;
    if (s.expires_at * 1000 < Date.now()) {
      localStorage.removeItem(SESSION_KEY);
      return null;
    }
    return s;
  } catch { return null; }
}

export function saveSession(s) {
  localStorage.setItem(SESSION_KEY, JSON.stringify(s));
}

export function clearSession() {
  localStorage.removeItem(SESSION_KEY);
}

export function requireOpsSession() {
  const s = loadSession();
  if (!s) {
    window.location.href = "/ops/login.html?next=" + encodeURIComponent(window.location.pathname);
    return null;
  }
  return s;
}

export async function opsFetch(path, opts = {}) {
  const s = loadSession();
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (s && s.token) headers["Authorization"] = "Bearer " + s.token;
  const res = await fetch(OPS_API + path, { ...opts, headers });
  if (res.status === 401) {
    clearSession();
    window.location.href = "/ops/login.html?next=" + encodeURIComponent(window.location.pathname);
    return null;
  }
  let body = null;
  try { body = await res.json(); } catch {}
  return { ok: res.ok, status: res.status, body };
}

export function renderActionToast(msg, kind = "info", ttl = 3500) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "300ms"; }, ttl - 300);
  setTimeout(() => { el.remove(); }, ttl);
}

export function fmtTs(epoch_s) {
  if (!epoch_s) return "—";
  const d = new Date(epoch_s * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fmtRelative(epoch_s) {
  if (!epoch_s) return "—";
  const dt = Date.now() / 1000 - epoch_s;
  if (dt < 60) return `${Math.round(dt)}s ago`;
  if (dt < 3600) return `${Math.round(dt/60)}m ago`;
  if (dt < 86400) return `${Math.round(dt/3600)}h ago`;
  return `${Math.round(dt/86400)}d ago`;
}

export function truncate(s, n = 14) {
  if (!s) return "";
  if (s.length <= n) return s;
  return s.slice(0, n - 4) + "..." + s.slice(-3);
}

export function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
}

export function badge(text, kind = "muted") {
  return `<span class="badge ${kind}">${escapeHtml(text)}</span>`;
}

export function statusBadge(status) {
  const map = {
    "pending":            ["warn",  "pending"],
    "pending_review":     ["warn",  "review"],
    "approved":           ["ok",    "approved"],
    "verified":           ["ok",    "verified"],
    "rejected":           ["err",   "rejected"],
    "needs_more_info":    ["warn",  "needs info"],
    "open":               ["err",   "open"],
    "cleared":            ["ok",    "cleared"],
    "blocked_permanent":  ["err",   "blocked"],
    "investigating":      ["warn",  "investigating"],
    "mitigated":          ["info",  "mitigated"],
    "resolved":           ["ok",    "resolved"],
    "none":               ["muted", "none"],
  };
  const [kind, text] = map[status] || ["muted", status || "—"];
  return badge(text, kind);
}

export async function renderChrome(active) {
  const s = loadSession();
  if (!s) return;
  const links = [
    ["/ops/", "Overview", "overview"],
    ["/ops/kyc.html", "KYC", "kyc"],
    ["/ops/withdraws.html", "Withdraws", "withdraws"],
    ["/ops/aml.html", "AML", "aml"],
    ["/ops/incidents.html", "Incidents", "incidents"],
    ["/ops/waf.html", "WAF", "waf"],
    ["/ops/errors.html", "Errors", "errors"],
    ["/ops/audit.html", "Audit", "audit"],
    ["/ops/me.html", "Me", "me"],
  ];
  const nav = links.map(([href, label, key]) =>
    `<a class="${key === active ? "active" : ""}" href="${href}">${label}</a>`).join("");
  const op = s.operator || {};
  const html = `<header class="topbar">
    <span class="brand">zkCEX <small>OPERATOR CONSOLE</small></span>
    <nav>${nav}</nav>
    <div class="right">
      <div class="who">
        <span class="name">${escapeHtml(op.display_name || op.email || "")}</span>
        <span class="role">${escapeHtml(op.role || "")}</span>
      </div>
      <button class="btn" id="logoutBtn">Sign out</button>
    </div>
  </header>`;
  const mount = document.getElementById("chrome");
  if (mount) mount.outerHTML = html;
  document.getElementById("logoutBtn")?.addEventListener("click", async () => {
    await opsFetch("/auth/logout", { method: "POST" });
    clearSession();
    window.location.href = "/ops/login.html";
  });
}
