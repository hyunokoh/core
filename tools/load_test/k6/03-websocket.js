// 03-websocket.js
// 1000 concurrent WebSocket subscribers across the 60-second test window.
// Each VU opens a single connection, subscribes to depth + trade + ticker
// for a randomly chosen symbol, and stays connected for 20 s while counting
// frames received.  Measures the fan-out cost on ws_feed.py and the
// per-process file-descriptor / event-loop ceiling.
//
//   k6 run -e WS_URL=ws://localhost:5510 tools/load_test/k6/03-websocket.js
//
// Expected on a 2026 MacBook M-series:
//   1000 simultaneous sockets, ~50 msgs/sec/socket peak, p95 frame latency
//   under 250 ms.  Above ~2000 sockets one Python ws_feed worker runs out of
//   epoll headroom and starts dropping pongs -- horizontally scale ws_feed
//   behind nginx or HAProxy.

import ws from 'k6/ws';
import { check, sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const framesReceived = new Counter('ws_frames_received');
const connectLatency = new Trend('ws_connect_latency_ms', true);

export const options = {
  stages: [
    { duration: '20s', target: 200  },
    { duration: '20s', target: 1000 },
    { duration: '20s', target: 0    },
  ],
  thresholds: {
    'ws_connecting':        ['p(95)<1000'],
    'ws_connect_latency_ms': ['p(95)<500'],
  },
};

const SYMBOLS = ['ethusdt', 'btcusdt', 'solusdt', 'dogeusdt'];

export default function () {
  const wsUrl = __ENV.WS_URL || 'ws://localhost:5510';
  const sym   = SYMBOLS[Math.floor(Math.random() * SYMBOLS.length)];
  const streams = [`${sym}@depth20@100ms`, `${sym}@trade`, `${sym}@ticker`];
  const url = `${wsUrl}/stream?streams=${streams.join('/')}`;

  const t0 = Date.now();
  const res = ws.connect(url, {}, function (socket) {
    socket.on('open', () => connectLatency.add(Date.now() - t0));
    socket.on('message', () => framesReceived.add(1));
    socket.setTimeout(() => socket.close(), 20000);
  });

  check(res, { 'ws status 101': r => r && r.status === 101 });
  sleep(0.1);
}
