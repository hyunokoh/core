/* zkCEX service worker
   - App-shell precache + stale-while-revalidate for the static UI
   - Bypasses all mutating / signed / live endpoints
   - Web Push receive + click handling
*/

const CACHE_VERSION = 'zkcex-v1';
const CACHE_NAME = `zkcex-shell-${CACHE_VERSION}`;

const APP_SHELL = [
  '/', '/en/',
  '/app/', '/app/index.html',
  '/app/trade.html', '/app/wallet.html', '/app/deposit.html', '/app/withdraw.html',
  '/app/verify.html', '/app/proof-of-reserves.html', '/app/api-keys.html', '/app/mcp.html',
  '/app/signin.html', '/app/kyc.html', '/app/reports.html', '/app/custody.html',
  '/app/notifications.html',
  '/app/app.css', '/app/app.js', '/app/ramp.css',
  '/app/ws-client.js', '/app/chains.js', '/app/sumsub.js',
  '/app/pwa.js', '/app/install-prompt.js',
  '/styles.css',
  '/manifest.json',
  '/icons/icon-192.png', '/icons/icon-512.png',
  '/icons/apple-touch-icon.png', '/icons/favicon.svg',
];

// Path prefixes whose GETs we never intercept (live / mutating / auth-bearing).
// Anything that matches one of these falls through to the network.
const BYPASS_RE = /^\/(auth|chain|kyc|v3|sapi|fapi|orders|mcp|pol|pol-snapshot|pol-feed|zkpol|bridge|api-keys|ws|export|deposit|withdraw|order|cancel|admin|internal|v1|push)\//;

self.addEventListener('install', e => {
  e.waitUntil((async () => {
    const cache = await caches.open(CACHE_NAME);
    // Use addAll's all-or-nothing semantics carefully — a single 404 would
    // abort install. Add each entry individually and swallow per-URL errors.
    await Promise.all(APP_SHELL.map(async url => {
      try { await cache.add(url); } catch (err) { /* skip missing */ }
    }));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', e => {
  e.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(
      keys.filter(k => k.startsWith('zkcex-') && k !== CACHE_NAME)
          .map(k => caches.delete(k))
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  // Cross-origin (fonts.googleapis.com, etc.) — let the browser cache it.
  if (url.origin !== self.location.origin) return;
  // Dynamic APIs — never cache, never intercept.
  if (BYPASS_RE.test(url.pathname)) return;
  if (url.pathname === '/v3/exchangeInfo' || url.pathname === '/v3/depth') return;

  // Stale-while-revalidate for everything else (HTML, CSS, JS, icons).
  e.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(req);
    const fetchPromise = fetch(req).then(resp => {
      if (resp && resp.status === 200 && resp.type !== 'opaque') {
        cache.put(req, resp.clone()).catch(() => {});
      }
      return resp;
    }).catch(() => cached);
    return cached || fetchPromise;
  })());
});

// --- Web Push --------------------------------------------------------------

self.addEventListener('push', e => {
  let payload = {};
  try { payload = e.data ? e.data.json() : {}; } catch (err) {
    try { payload = { body: e.data && e.data.text() }; } catch { payload = {}; }
  }
  const title = payload.title || 'zkCEX';
  const opts = {
    body: payload.body || '',
    icon: '/icons/icon-192.png',
    badge: '/icons/icon-192.png',
    tag: payload.tag || 'zkcex',
    data: payload.data || {},
    actions: payload.actions || [],
    renotify: !!payload.renotify,
    requireInteraction: !!payload.requireInteraction,
  };
  e.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/app/';
  e.waitUntil((async () => {
    const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const c of list) {
      try {
        if (c.url.endsWith(url) && 'focus' in c) return c.focus();
      } catch { /* ignore */ }
    }
    if (self.clients.openWindow) return self.clients.openWindow(url);
    return null;
  })());
});

// Allow the page to ask the SW to skip waiting (so installs take effect
// without a forced reload).
self.addEventListener('message', e => {
  if (e.data && e.data.type === 'SKIP_WAITING') self.skipWaiting();
});
