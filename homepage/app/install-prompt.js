/* zkCEX install prompt banner.
   - Android/Chromium: listens for `beforeinstallprompt` and renders a small
     bottom-of-screen banner. Tapping the install button calls .prompt().
   - iOS Safari: doesn't fire that event, so we sniff the UA and show a
     short hint pointing users at Share -> Add to Home Screen.
   - Already installed (display-mode: standalone or navigator.standalone)?
     We render nothing.
*/

import { isStandalone } from "./pwa.js";

const DISMISS_KEY = "zkcex_install_dismissed_at";
const DISMISS_TTL_MS = 1000 * 60 * 60 * 24 * 14;  // 14 days

let deferredPrompt = null;

function dismissedRecently() {
  try {
    const t = parseInt(localStorage.getItem(DISMISS_KEY) || "0", 10);
    if (!t) return false;
    return Date.now() - t < DISMISS_TTL_MS;
  } catch { return false; }
}

function rememberDismissed() {
  try { localStorage.setItem(DISMISS_KEY, String(Date.now())); } catch {}
}

function buildBanner({ ios }) {
  const wrap = document.createElement("div");
  wrap.className = "pwa-install-banner";
  wrap.setAttribute("role", "dialog");
  wrap.setAttribute("aria-label", "앱 설치");
  if (ios) {
    wrap.innerHTML = `
      <div class="pwa-install-row">
        <span class="pwa-install-icon" aria-hidden="true">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
            <path d="M12 3v12m0 0l-4-4m4 4l4-4M5 20h14" stroke="currentColor"
                  stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </span>
        <div class="pwa-install-text">
          <strong>홈 화면에 추가</strong>
          <span class="pwa-install-en">
            공유
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M12 3v12m0-12l-4 4m4-4l4 4M5 12v7a2 2 0 002 2h10a2 2 0 002-2v-7"
                    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
            → 홈 화면에 추가
            <em>/ Share → Add to Home Screen</em>
          </span>
        </div>
        <button type="button" class="pwa-install-close" aria-label="닫기">×</button>
      </div>`;
  } else {
    wrap.innerHTML = `
      <div class="pwa-install-row">
        <span class="pwa-install-icon" aria-hidden="true">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none">
            <path d="M12 3v12m0 0l-4-4m4 4l4-4M5 20h14" stroke="currentColor"
                  stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </span>
        <div class="pwa-install-text">
          <strong>앱으로 설치하기</strong>
          <span class="pwa-install-en">Install zkCEX on your home screen</span>
        </div>
        <button type="button" class="pwa-install-go">설치 / Install</button>
        <button type="button" class="pwa-install-close" aria-label="닫기">×</button>
      </div>`;
  }
  return wrap;
}

function show({ ios }) {
  if (document.querySelector(".pwa-install-banner")) return;
  if (dismissedRecently()) return;
  const banner = buildBanner({ ios });
  document.body.appendChild(banner);
  banner.querySelector(".pwa-install-close")?.addEventListener("click", () => {
    rememberDismissed();
    banner.remove();
  });
  banner.querySelector(".pwa-install-go")?.addEventListener("click", async () => {
    if (!deferredPrompt) return;
    try {
      deferredPrompt.prompt();
      const choice = await deferredPrompt.userChoice;
      // Either way we hide the banner — Chrome won't fire the event again.
      rememberDismissed();
    } catch (err) {
      console.warn("[pwa] install prompt error", err);
    } finally {
      deferredPrompt = null;
      banner.remove();
    }
  });
}

if (!isStandalone()) {
  window.addEventListener("beforeinstallprompt", e => {
    e.preventDefault();
    deferredPrompt = e;
    show({ ios: false });
  });

  // iOS: no event. Only show on iPhone/iPad Safari, not in standalone mode.
  const isIos = /iPad|iPhone|iPod/.test(navigator.userAgent || "");
  if (isIos) {
    // Defer slightly so the banner doesn't slam into first paint.
    setTimeout(() => show({ ios: true }), 1200);
  }

  window.addEventListener("appinstalled", () => {
    rememberDismissed();
    document.querySelector(".pwa-install-banner")?.remove();
    // Light, silent confirmation. Avoid coupling to app.js's toast() since
    // this module is imported from index.html in isolation.
    console.info("[pwa] installed to home screen");
  });
}
