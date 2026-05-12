// 01-market-data.js
// Public REST market-data endpoints: depth + 24h ticker + recent trades.
//
//   k6 run -e BASE_URL=http://localhost:5500 tools/load_test/k6/01-market-data.js
//
// Expected on a 2026 MacBook M-series:
//   sustained ~600-900 rps with p95 <  150 ms and error rate <  0.5 %.
// If p95 climbs past 200 ms during the 200-VU step, the matching gateway is
// probably saturated on a single Python event loop -- shard ws_feed or run
// multiple matching engine workers behind the proxy.

import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '30s', target: 50 },
    { duration: '1m',  target: 200 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    'http_req_duration': ['p(95)<200'],
    'http_req_failed':   ['rate<0.01'],
  },
};

const SYMBOLS = ['ETHUSDT', 'BTCUSDT', 'SOLUSDT', 'DOGEUSDT'];

export default function () {
  const base = __ENV.BASE_URL || 'http://localhost:5500';
  const s = SYMBOLS[Math.floor(Math.random() * SYMBOLS.length)];

  const responses = http.batch([
    ['GET', `${base}/v3/depth?symbol=${s}&limit=20`,        null, { tags: { endpoint: 'depth' } }],
    ['GET', `${base}/v3/ticker/24h?symbol=${s}`,            null, { tags: { endpoint: 'ticker' } }],
    ['GET', `${base}/v3/trades?symbol=${s}&limit=50`,       null, { tags: { endpoint: 'trades' } }],
  ]);

  responses.forEach((r, i) => {
    check(r, { [`status 200 (${i})`]: x => x.status === 200 });
  });

  sleep(0.1);
}
