/* =========================================================================
   sumsub.js — Sumsub WebSDK glue for /app/kyc.html
   - Loads the canonical CDN bundle (https://static.sumsub.com/.../sns-websdk-builder.js)
   - Exchanges a session bearer for a one-shot Sumsub access token
   - Mounts the WebSDK iframe into #sumsub-websdk-container
   - Polls /kyc/sumsub/status every 5s while active and renders progress
   ========================================================================= */
import { authedFetch, escapeHtml, patchSessionUser, toast } from "./app.js";

const SDK_URL =
  "https://static.sumsub.com/idensic/static/sns-websdk-builder.js";

let sdkLoadPromise = null;
function loadSdk() {
  if (window.snsWebSdk) return Promise.resolve(window.snsWebSdk);
  if (sdkLoadPromise) return sdkLoadPromise;
  sdkLoadPromise = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = SDK_URL;
    s.async = true;
    s.onload = () => {
      if (window.snsWebSdk) resolve(window.snsWebSdk);
      else reject(new Error("snsWebSdk missing after script load"));
    };
    s.onerror = () => reject(new Error("Failed to load Sumsub WebSDK from " + SDK_URL));
    document.head.appendChild(s);
  });
  return sdkLoadPromise;
}

/**
 * Probe whether the server has Sumsub credentials configured.
 * Returns { configured: bool, level_name: string }.
 * Tolerant of failures: on network error, returns { configured: false }.
 */
export async function probeSumsubConfigured() {
  try {
    const res = await fetch("/kyc/sumsub/config", {
      headers: { Accept: "application/json" },
    });
    if (!res.ok) return { configured: false };
    const body = await res.json().catch(() => ({}));
    return {
      configured: !!body.configured,
      level_name: body.level_name || "basic-kyc-level",
    };
  } catch {
    return { configured: false };
  }
}

/**
 * Start a Sumsub session for the current user.
 * Resolves with { access_token, applicant_id, level_name, external_user_id }.
 * Rejects with err.code === "sumsub_not_configured" on 503.
 */
async function startSession() {
  try {
    return await authedFetch("/kyc/sumsub/start", { method: "POST" });
  } catch (e) {
    // authedFetch throws { status, body } on non-2xx
    if (e.status === 503) {
      const wrapped = new Error("sumsub_not_configured");
      wrapped.code = "sumsub_not_configured";
      throw wrapped;
    }
    throw e;
  }
}

async function refreshSession() {
  const r = await authedFetch("/kyc/sumsub/refresh-token", { method: "POST" });
  return r.access_token;
}

async function fetchStatus() {
  return authedFetch("/kyc/sumsub/status");
}

/**
 * Mount the WebSDK into #sumsub-websdk-container and start polling status into
 * #sumsub-status. Returns a stop() handle.
 */
export async function mountSumsub({ container, statusEl, onVerified } = {}) {
  if (!container) throw new Error("mountSumsub: missing container");
  if (!statusEl) throw new Error("mountSumsub: missing statusEl");

  renderStatus(statusEl, { review_status: "init", review_answer: null });

  let session;
  try {
    session = await startSession();
  } catch (e) {
    if (e.code === "sumsub_not_configured") {
      renderStatus(statusEl, {
        kind: "error",
        ko: "Sumsub가 구성되지 않았습니다",
        en: "Sumsub is not configured on this server",
      });
      throw e;
    }
    renderStatus(statusEl, {
      kind: "error",
      ko: "세션 생성 실패",
      en: e.message || "Failed to start Sumsub session",
    });
    throw e;
  }

  const snsWebSdk = await loadSdk();
  const lang = (navigator.language || "en").toLowerCase().startsWith("ko")
    ? "ko" : "en";

  const sdk = snsWebSdk
    .init(session.access_token, () => refreshSession())
    .withConf({ lang })
    .withOptions({ addViewportTag: false, adaptIframeHeight: true })
    .on("idCheck.onApplicantSubmitted", () => {
      // Kick a status fetch right after submission.
      pollOnce().catch(() => {});
    })
    .on("idCheck.onApplicantStatusChanged", (e) => {
      // Synthesise a status event for snappier UX.
      const ev = e?.payload || e || {};
      renderStatus(statusEl, {
        review_status: (ev.reviewStatus || "pending").toLowerCase(),
        review_answer: ev.reviewResult?.reviewAnswer || null,
      });
      pollOnce().catch(() => {});
    })
    .build();

  sdk.launch(container.id ? `#${container.id}` : container);

  // Polling loop.
  let stopped = false;
  let timer = null;
  let lastVerifiedFire = false;

  async function pollOnce() {
    if (stopped) return;
    let st;
    try {
      st = await fetchStatus();
    } catch (e) {
      // 503 means env was unset between probe and poll — surface and stop.
      if (e.status === 503) {
        renderStatus(statusEl, {
          kind: "error",
          ko: "Sumsub가 비활성화되었습니다",
          en: "Sumsub became unavailable",
        });
        stop();
        return;
      }
      return;
    }
    renderStatus(statusEl, st);
    if (st.review_status === "completed"
        && (st.review_answer || "").toUpperCase() === "GREEN"
        && !lastVerifiedFire) {
      lastVerifiedFire = true;
      patchSessionUser({ kyc_status: "verified" });
      toast("KYC 인증 완료", "KYC verified");
      stop();
      if (typeof onVerified === "function") onVerified(st);
    }
  }

  timer = setInterval(() => { pollOnce().catch(() => {}); }, 5000);
  pollOnce().catch(() => {});

  function stop() {
    if (stopped) return;
    stopped = true;
    if (timer) clearInterval(timer);
    timer = null;
  }

  window.addEventListener("beforeunload", stop, { once: true });
  return { stop, refresh: pollOnce };
}

function renderStatus(el, st) {
  if (st && st.kind === "error") {
    el.innerHTML = `
      <div class="sumsub-status sumsub-status-error">
        <span class="sumsub-status-dot"></span>
        <div>
          <div class="sumsub-status-ko">${escapeHtml(st.ko)}</div>
          <div class="sumsub-status-en">${escapeHtml(st.en || "")}</div>
        </div>
      </div>`;
    return;
  }
  const status = (st?.review_status || "init").toLowerCase();
  const answer = (st?.review_answer || "").toUpperCase();
  let kind = "init";
  let ko = "신원 확인 단계를 시작하세요";
  let en = "Begin verification";

  if (status === "init") {
    kind = "init";
    ko = "신원 확인 단계를 시작하세요";
    en = "Begin verification";
  } else if (status === "pending"
             || status === "queued"
             || status === "onhold") {
    kind = "pending";
    ko = "검토 중… (보통 1-3분)";
    en = "Under review (usually 1-3 minutes)";
  } else if (status === "completed") {
    if (answer === "GREEN") {
      kind = "ok";
      ko = "KYC 인증 완료";
      en = "KYC verified";
    } else if (answer === "RED") {
      kind = "rejected";
      ko = "거절됨";
      en = "Rejected. Please resubmit if you believe this is wrong.";
    } else if (answer === "YELLOW") {
      kind = "yellow";
      ko = "재제출 필요";
      en = "Resubmission required";
    } else {
      kind = "pending";
      ko = "검토 결과 대기 중";
      en = "Awaiting review result";
    }
  }
  el.innerHTML = `
    <div class="sumsub-status sumsub-status-${kind}">
      <span class="sumsub-status-dot"></span>
      <div>
        <div class="sumsub-status-ko">${escapeHtml(ko)}</div>
        <div class="sumsub-status-en">${escapeHtml(en)}</div>
      </div>
    </div>`;
}
