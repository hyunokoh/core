// 06-auth-flow.js
// Full signup -> login -> /auth/me cycle.  Per-iteration each VU creates a
// fresh email, signs up, logs in, then hits /auth/me with the returned
// token.  Measures the cost of bcrypt + JWT issuance + session lookup.
//
//   k6 run -e BASE_URL=http://localhost:5500 tools/load_test/k6/06-auth-flow.js
//
// Expected on a 2026 MacBook M-series:
//   ~30-60 full flows / sec at 20 VUs (bcrypt is intentionally slow).
//   p95 signup latency should stay under 800 ms.
// If signup p95 explodes when adding VUs, the bcrypt cost factor needs to
// be tuned down for tests OR signup needs to be queued (background hash).

import http from 'k6/http';
import { check, group, sleep } from 'k6';

export const options = {
  vus: 20,
  duration: '2m',
  thresholds: {
    'http_req_duration{step:signup}': ['p(95)<1000'],
    'http_req_duration{step:login}':  ['p(95)<1000'],
    'http_req_duration{step:me}':     ['p(95)<150'],
    'http_req_failed':                ['rate<0.02'],
  },
};

export default function () {
  const base = __ENV.BASE_URL || 'http://localhost:5500';
  const email = `lt+${__VU}-${__ITER}-${Date.now()}@example.test`;
  const pw    = 'CorrectHorse-Battery-7!';
  let token;

  group('signup', () => {
    const r = http.post(`${base}/auth/signup`,
      JSON.stringify({ email, password: pw }),
      { headers: { 'Content-Type': 'application/json' }, tags: { step: 'signup' } });
    check(r, { 'signup status 2xx/409': x => x.status < 500 });
  });

  group('login', () => {
    const r = http.post(`${base}/auth/login`,
      JSON.stringify({ email, password: pw }),
      { headers: { 'Content-Type': 'application/json' }, tags: { step: 'login' } });
    if (check(r, { 'login 200': x => x.status === 200 })) {
      try { token = r.json('token'); } catch (_) {}
    }
  });

  if (token) {
    group('me', () => {
      const r = http.get(`${base}/auth/me`,
        { headers: { Authorization: `Bearer ${token}` }, tags: { step: 'me' } });
      check(r, { '/auth/me 200': x => x.status === 200 });
    });
  }
  sleep(0.5);
}
