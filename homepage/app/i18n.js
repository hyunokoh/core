// Browser-side i18n helper.
//
// The proxy injects ``window.__ZKCEX_I18N`` (a flat ``{msgid: msgstr}`` map
// for the negotiated locale) and ``window.__ZKCEX_LANG`` (the locale code)
// into the <head> of every HTML response. This module exposes a tiny
// ``t(key, ...args)`` helper that the rest of the app can use to look up
// translated UI labels for strings that get rendered after the initial
// HTML payload — most notably the app header / footer that ``app.js``
// builds via template literals on page load.
//
// Usage examples:
//
//     import { t, LOCALE } from "./i18n.js";
//     button.textContent = t("로그아웃");
//     const greeting = t("{0}님, 환영합니다", user.name);
//
// Conventions:
// - Missing translations fall through to the source ``key`` so the UI
//   never goes blank — the worst case is "show me the Korean source".
// - Positional placeholders use ``{0}``, ``{1}``, ... and are filled by
//   the trailing varargs in order.
// - No HTML escaping is performed here — callers are responsible for
//   escaping when embedding into the DOM.

export const LOCALE = (
  (typeof document !== "undefined" && document.documentElement && document.documentElement.lang) ||
  (typeof window !== "undefined" && window.__ZKCEX_LANG) ||
  "ko"
).toLowerCase().split("-")[0];

export function t(key, ...args) {
  const dict = (typeof window !== "undefined" && window.__ZKCEX_I18N) || {};
  let tpl = (key in dict) ? dict[key] : key;
  if (args.length) {
    tpl = tpl.replace(/\{(\d+)\}/g, (whole, idx) => {
      const i = Number(idx);
      return Number.isInteger(i) && i < args.length ? String(args[i]) : whole;
    });
  }
  return tpl;
}

// Convenience: also expose on window so non-module legacy code can call
// ``window.t("...")`` without importing.
if (typeof window !== "undefined") {
  if (typeof window.t !== "function") {
    window.t = t;
  }
  window.__ZKCEX_LOCALE = LOCALE;
}
