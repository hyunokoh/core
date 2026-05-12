// 07-chain.js
// /chain/info + /chain/wallet polling.  Simulates the homepage Wallet page
// polling the on/off ramp bridge once per second for many concurrent users.
//
//   k6 run -e BASE_URL=http://localhost:5500 tools/load_test/k6/07-chain.js
//
// Expected on a 2026 MacBook M-series:
//   200 concurrent wallet pollers, ~200 ops/sec, p95 < 120 ms.
// chain_server.py talks to a local hardhat node -- if hardhat is the
// bottleneck the read path will plateau around 250 ops/sec regardless of VU.

import http from 'k6/http';
import { check, sleep } from 'k6';

export const options = {
  stages: [
    { duration: '30s', target: 50  },
    { duration: '2m',  target: 200 },
    { duration: '30s', target: 0   },
  ],
  thresholds: {
    'http_req_duration': ['p(95)<400'],
    'http_req_failed':   ['rate<0.02'],
  },
};

export default function () {
  const base = __ENV.BASE_URL || 'http://localhost:5500';
  const responses = http.batch([
    ['GET', `${base}/chain/info`,                          null, { tags: { name: 'info'   } }],
    ['GET', `${base}/chain/wallet?address=0x0000000000000000000000000000000000000001`,
                                                          null, { tags: { name: 'wallet' } }],
  ]);
  responses.forEach((r, i) =>
    check(r, { [`status (${i})`]: x => x.status === 200 || x.status === 404 }));
  sleep(1);
}
