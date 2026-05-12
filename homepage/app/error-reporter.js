/* zkCEX browser error reporter.
 *
 * Auto-installs window.onerror + onunhandledrejection listeners and forwards
 * the resulting payload to the central error collector at /errors/report.
 *
 * Loaded as a non-blocking ES module from every /app/*.html page (and from
 * /app/app.js as a side-effect import for older bundles). Silently no-ops
 * when the collector is unreachable; the network call is rate-limited at
 * 5s minimum spacing per page so a runaway error loop can't DoS the user
 * agent nor the collector.
 *
 * No third-party dependency, no globals beyond an explicit feature flag
 * (window.__ZKCEX_DISABLE_ERROR_REPORTER) to switch the listener off.
 */

const ERROR_ENDPOINT = "/errors/report";
const RATE_LIMIT_MS = 5000;
const SESSION_LS_KEY = "zkcex_session";

let lastReportAt = 0;
let installed = false;

function tryReadSessionUser() {
  try {
    const raw = localStorage.getItem(SESSION_LS_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw);
    return s?.user?.opex_user || s?.opex_user || null;
  } catch {
    return null;
  }
}

function envFromHost() {
  // Heuristic: localhost / 127.0.0.1 / private nets -> dev.
  // Anything ending in .zkcex.io -> production unless the explicit override.
  const h = (typeof location !== "undefined" && location.hostname) || "";
  if (!h) return "dev";
  if (h === "localhost" || h === "127.0.0.1" || h.endsWith(".local")) return "dev";
  if (h.includes("staging") || h.includes("preview")) return "staging";
  return "production";
}

function reportError(payload) {
  if (typeof fetch === "undefined") return;
  if (Date.now() - lastReportAt < RATE_LIMIT_MS) return;
  lastReportAt = Date.now();
  const body = {
    source: "browser-js",
    level: "error",
    ...payload,
    url: (typeof location !== "undefined") ? location.href : null,
    user_agent: (typeof navigator !== "undefined") ? navigator.userAgent : null,
    user_id: tryReadSessionUser(),
    release_version: (typeof window !== "undefined" && window.__ZKCEX_VERSION) || "dev",
    environment: (typeof window !== "undefined" && window.__ZKCEX_ENV) || envFromHost(),
  };
  try {
    fetch(ERROR_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* deliberately swallow — reporter must never throw */
  }
}

function install() {
  if (installed) return;
  if (typeof window === "undefined") return;
  if (window.__ZKCEX_DISABLE_ERROR_REPORTER) return;
  installed = true;

  window.addEventListener("error", (e) => {
    // Filter null events that some browsers emit when the page unloads.
    if (!e) return;
    reportError({
      message: e.message || (e.error && e.error.message) || "uncaught error",
      exception_type: (e.error && e.error.name) || "Error",
      stack_trace: e.error && e.error.stack ? e.error.stack : null,
      metadata: {
        filename: e.filename || null,
        lineno: e.lineno || null,
        colno: e.colno || null,
      },
    });
  });

  window.addEventListener("unhandledrejection", (e) => {
    const reason = e && e.reason;
    let message = "UnhandledRejection";
    let ex = "UnhandledRejection";
    let stack = null;
    if (reason instanceof Error) {
      message = reason.message || reason.name || "UnhandledRejection";
      ex = reason.name || "UnhandledRejection";
      stack = reason.stack || null;
    } else if (typeof reason === "string") {
      message = reason.slice(0, 4000);
    } else if (reason != null) {
      try { message = JSON.stringify(reason).slice(0, 4000); } catch { message = String(reason); }
    }
    reportError({
      message,
      exception_type: ex,
      stack_trace: stack,
    });
  });
}

install();

// Re-export so callers can manually report (e.g. inside try/catch).
export function manualReport(payload) {
  reportError({ ...payload, level: payload?.level || "error" });
}

export const __zkcex_error_reporter = { install, reportError };
