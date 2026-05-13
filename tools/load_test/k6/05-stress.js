// 05-stress.js
// Ramp from 100 to 5000 concurrent users on read-only endpoints until
// error_rate > 5 % or p95 > 2 s.  Identifies the breaking point of the
// public read path.
//
//   k6 run -e BASE_URL=http://localhost:5500 tools/load_test/k6/05-stress.js
//
// Expected on a 2026 MacBook M-series:
//   first failures around 2500-3000 VU.  The bottleneck is usually the
//   single Python proxy (serve_homepage.py) -- in production, replace it
//   with nginx or Envoy in front of the actual service mesh.
//
// Abort early signal: when error_rate breaks 5 % the abortOnFail threshold
// terminates the run automatically.

import http from 'k6/http';
import { check } from 'k6';

export const options = {
  stages: [
    { duration: '30s', target: 100  },
    { duration: '1m',  target: 500  },
    { duration: '1m',  target: 1500 },
    { duration: '1m',  target: 3000 },
    { duration: '1m',  target: 5000 },
    { duration: '30s', target: 0    },
  ],
  thresholds: {
    'http_req_failed':   [{ threshold: 'rate<0.05', abortOnFail: true, delayAbortEval: '20s' }],
    'http_req_duration': [{ threshold: 'p(95)<2000', abortOnFail: true, delayAbortEval: '20s' }],
  },
};

const SYMBOLS = ['ETHUSDT', 'BTCUSDT', 'SOLUSDT', 'DOGEUSDT'];

export default function () {
  const base = __ENV.BASE_URL || 'http://localhost:5500';
  const sym  = SYMBOLS[Math.floor(Math.random() * SYMBOLS.length)];
  const r = http.get(`${base}/v3/depth?symbol=${sym}&limit=5`);
  check(r, { 'status 200': x => x.status === 200 });
}
