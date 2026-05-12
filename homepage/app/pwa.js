/* zkCEX PWA bootstrap.
   Registers the service worker on every /app/* page. Imported as a side-effect
   module from app.js (`import "./pwa.js"`) so we don't pollute the global
   namespace and the SW gets exactly one registration per page load.
*/

if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  // Don't block initial render — fire-and-forget after the first paint.
  const reg = () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' })
      .catch(err => console.warn('[pwa] SW registration failed', err));
  };
  if (document.readyState === 'complete') reg();
  else window.addEventListener('load', reg, { once: true });
}

// Helper consumed by notifications.html — exposes a tiny wrapper around the
// PushManager so the UI doesn't have to repeat the boilerplate.
export async function ensurePushSubscription(vapidPublicKeyB64Url) {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    throw new Error('push_unsupported');
  }
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (sub) return sub;
  const applicationServerKey = b64urlToUint8(vapidPublicKeyB64Url);
  sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey,
  });
  return sub;
}

export async function dropPushSubscription() {
  if (!('serviceWorker' in navigator)) return false;
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (!sub) return false;
  return sub.unsubscribe();
}

export function isStandalone() {
  return (
    window.matchMedia && window.matchMedia('(display-mode: standalone)').matches
  ) || !!window.navigator.standalone;
}

function b64urlToUint8(b64url) {
  const pad = '='.repeat((4 - (b64url.length % 4)) % 4);
  const base64 = (b64url + pad).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}
