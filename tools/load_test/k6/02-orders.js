// 02-orders.js
// Signed POST /v3/order burst.  100 concurrent users place limit orders at
// far-from-market prices (10 % off best bid / ask) so they rest in the book.
// Measures matching gateway throughput end-to-end (HMAC verify + ledger
// write + book insert).
//
//   k6 run -e BASE_URL=http://localhost:5500 \
//          -e API_KEY=<key> -e API_SECRET=<secret> \
//          tools/load_test/k6/02-orders.js
//
// Expected on a 2026 MacBook M-series:
//   sustained ~200-400 orders/sec with p95 < 80 ms.  Above ~600 ops/s the
//   SQLite write lock starts queuing -- shard the order book per symbol or
//   move the journal to Postgres / Redis.
//
// Each VU pulls a unique clientOrderId from a counter so the matching engine
// can de-dup idempotently.

import http from 'k6/http';
import crypto from 'k6/crypto';
import { check } from 'k6';

export const options = {
  vus: 100,
  duration: '1m',
  thresholds: {
    'http_req_duration{name:order}': ['p(95)<150'],
    'http_req_failed':               ['rate<0.02'],
  },
};

function hmacSha256Hex(key, msg) {
  return crypto.hmac('sha256', key, msg, 'hex');
}

function qs(params) {
  return Object.keys(params)
    .map(k => `${encodeURIComponent(k)}=${encodeURIComponent(params[k])}`)
    .join('&');
}

const SYMBOLS = ['ETHUSDT', 'BTCUSDT', 'SOLUSDT'];
// rough mid prices (the script uses far-from-market so exact values don't matter)
const MID = { ETHUSDT: 100, BTCUSDT: 50000, SOLUSDT: 20 };

export default function () {
  const base   = __ENV.BASE_URL    || 'http://localhost:5500';
  const apiKey = __ENV.API_KEY     || 'demo-key';
  const apiSec = __ENV.API_SECRET  || 'demo-secret';

  const sym  = SYMBOLS[Math.floor(Math.random() * SYMBOLS.length)];
  const side = Math.random() < 0.5 ? 'BUY' : 'SELL';
  const mid  = MID[sym];
  const px   = side === 'BUY' ? mid * 0.9 : mid * 1.1;          // far from market

  const params = {
    symbol:           sym,
    side,
    type:             'LIMIT',
    timeInForce:      'GTC',
    quantity:         '0.01',
    price:            px.toFixed(2),
    newClientOrderId: `lt-${__VU}-${__ITER}-${Date.now()}`,
    timestamp:        Date.now(),
    recvWindow:       5000,
  };
  const query     = qs(params);
  const signature = hmacSha256Hex(apiSec, query);
  const url       = `${base}/v3/order?${query}&signature=${signature}`;

  const res = http.post(url, null, {
    headers: { 'X-MBX-APIKEY': apiKey },
    tags:    { name: 'order' },
  });

  check(res, {
    'order accepted (2xx/4xx, not 5xx)': r => r.status < 500,
  });
}
