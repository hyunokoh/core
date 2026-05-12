// 04-mixed-realistic.js
// Realistic retail trading day mix:
//   70 % market-data reads   (depth / ticker)
//   20 % balance lookups     (signed GET /v3/account)
//    8 % order place         (signed POST /v3/order, far from market)
//    2 % order cancel        (signed DELETE /v3/order)
//
//   k6 run -e BASE_URL=... -e API_KEY=... -e API_SECRET=... \
//          tools/load_test/k6/04-mixed-realistic.js
//
// Expected on a 2026 MacBook M-series:
//   ~500-800 ops/sec sustained for 5 min, p95 < 250 ms, error rate < 1 %.
// If errors cluster on cancels, the SQLite write lock is the bottleneck (the
// open-orders table gets contention from both the matcher and cancels).

import http from 'k6/http';
import crypto from 'k6/crypto';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '1m', target: 100 },
    { duration: '3m', target: 250 },
    { duration: '1m', target: 0   },
  ],
  thresholds: {
    'http_req_duration': ['p(95)<500'],
    'http_req_failed':   ['rate<0.02'],
  },
};

const SYMBOLS = ['ETHUSDT', 'BTCUSDT', 'SOLUSDT', 'DOGEUSDT'];
const MID = { ETHUSDT: 100, BTCUSDT: 50000, SOLUSDT: 20, DOGEUSDT: 0.1 };

function hmacHex(secret, msg) {
  return crypto.hmac('sha256', secret, msg, 'hex');
}
function qs(o) {
  return Object.keys(o).map(k => `${k}=${encodeURIComponent(o[k])}`).join('&');
}
function pickSymbol() { return SYMBOLS[Math.floor(Math.random() * SYMBOLS.length)]; }

export default function () {
  const base   = __ENV.BASE_URL   || 'http://localhost:5500';
  const apiKey = __ENV.API_KEY    || 'demo-key';
  const apiSec = __ENV.API_SECRET || 'demo-secret';

  const dice = Math.random();
  const sym  = pickSymbol();

  if (dice < 0.70) {                                            // read
    const which = Math.random() < 0.5 ? 'depth?limit=20' : 'ticker/24h';
    const r = http.get(`${base}/v3/${which}&symbol=${sym}`.replace('?limit=20&', '?symbol=' + sym + '&limit=20').replace('ticker/24h&', 'ticker/24h?'));
    check(r, { 'read 200': x => x.status === 200 });

  } else if (dice < 0.90) {                                     // balance
    const params = { timestamp: Date.now(), recvWindow: 5000 };
    const q = qs(params);
    const sig = hmacHex(apiSec, q);
    const r = http.get(`${base}/v3/account?${q}&signature=${sig}`,
      { headers: { 'X-MBX-APIKEY': apiKey }, tags: { name: 'account' } });
    check(r, { 'account < 500': x => x.status < 500 });

  } else if (dice < 0.98) {                                     // place
    const side = Math.random() < 0.5 ? 'BUY' : 'SELL';
    const px   = side === 'BUY' ? MID[sym] * 0.9 : MID[sym] * 1.1;
    const p = {
      symbol: sym, side, type: 'LIMIT', timeInForce: 'GTC',
      quantity: '0.01', price: px.toFixed(2),
      newClientOrderId: `mix-${__VU}-${__ITER}`,
      timestamp: Date.now(), recvWindow: 5000,
    };
    const q = qs(p);
    const sig = hmacHex(apiSec, q);
    const r = http.post(`${base}/v3/order?${q}&signature=${sig}`, null,
      { headers: { 'X-MBX-APIKEY': apiKey }, tags: { name: 'place' } });
    check(r, { 'place < 500': x => x.status < 500 });

  } else {                                                      // cancel
    const p = { symbol: sym, origClientOrderId: `mix-${__VU}-${__ITER - 1}`,
                timestamp: Date.now(), recvWindow: 5000 };
    const q = qs(p);
    const sig = hmacHex(apiSec, q);
    const r = http.del(`${base}/v3/order?${q}&signature=${sig}`, null,
      { headers: { 'X-MBX-APIKEY': apiKey }, tags: { name: 'cancel' } });
    check(r, { 'cancel < 500': x => x.status < 500 });
  }
  sleep(0.05);
}
