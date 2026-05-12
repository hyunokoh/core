# k6 Load Test Scenarios

Standalone [k6](https://k6.io/docs/) scripts for the zkCEX demo stack.  They
use only k6's built-in modules -- no `xk6` extensions -- so a stock `k6`
binary is enough.

## Install

```bash
# macOS
brew install k6

# Linux (Debian / Ubuntu)
sudo apt-get install k6
```

## Run one scenario

```bash
k6 run -e BASE_URL=http://localhost:5500 \
       tools/load_test/k6/01-market-data.js
```

## Run them all

```bash
tools/load_test/run.sh
# results land in tools/load_test/results/<scenario>.json
```

## Environment variables

| var          | default                  | used by                                  |
|--------------|--------------------------|------------------------------------------|
| `BASE_URL`   | `http://localhost:5500`  | every scenario except 03                 |
| `WS_URL`     | `ws://localhost:5510`    | 03-websocket.js                          |
| `API_KEY`    | `demo-key`               | 02-orders.js, 04-mixed-realistic.js      |
| `API_SECRET` | `demo-secret`            | 02-orders.js, 04-mixed-realistic.js      |

## Scenarios

| file                     | what it measures                              |
|--------------------------|-----------------------------------------------|
| `01-market-data.js`      | public REST depth / ticker / trades           |
| `02-orders.js`           | signed order place throughput                 |
| `03-websocket.js`        | 1 000 concurrent ws subscribers               |
| `04-mixed-realistic.js`  | realistic 70/20/8/2 read/balance/place/cancel |
| `05-stress.js`           | ramp to failure (find the breaking point)     |
| `06-auth-flow.js`        | signup -> login -> /auth/me cycle             |
| `07-chain.js`            | wallet + chain-info polling                   |

Each script declares `options.thresholds`.  k6 exits with code `99` when a
threshold fails -- perfect for CI gating.

## Interpreting output

Look for these three numbers per run:

- `http_req_duration ... p(95)=...` -- 95th percentile latency.
- `http_req_failed ... rate=...`    -- error rate (0 = perfect).
- `iterations ... rate=...`         -- ops/sec.

Per-scenario expected numbers on a 2026 MacBook M-series live in the comment
at the top of each `.js` file -- compare your numbers against those to spot
regressions.

## Kill switch

`Ctrl-C` aborts the test gracefully.  No data is touched on the demo
services; the only side effect is rows in the order book (which `mm_bot`
flushes anyway).
