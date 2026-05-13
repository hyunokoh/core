# zkcex — JavaScript SDK

Minimal zero-dependency client for the zkCEX REST API. Works in Node 18+
and modern browsers (uses `fetch` + WebCrypto in the browser, `node:crypto`
in Node).

## Install

```bash
# from this repo (no registry publish step required):
cd sdks/javascript
npm pack
# or just copy zkcex.mjs into your app
```

## Five-line quickstart

```js
import { ZkcexClient } from "./zkcex.mjs";

const c = new ZkcexClient({
  baseUrl: "http://localhost:5500",
  apiKey:    "YOUR_KEY",
  apiSecret: "YOUR_SECRET",
});
console.log(await c.depth("ETHUSDT", 5));
console.log(await c.placeOrder({
  symbol: "ETHUSDT", side: "BUY", type: "LIMIT",
  quantity: "0.01", price: "50", timeInForce: "GTC",
}));
```

Issue a key at `/app/api-keys.html`.

## Auth surfaces

- HMAC (`X-MBX-APIKEY` header + signed query) for `/v3/*` and `/fapi/v1/*`.
  Pass `apiKey` + `apiSecret` to the constructor.
- Bearer session token for `/auth/me`, `/chain/*`, `/pol/*`,
  `/api-keys/*`, `/orders/conditional`. Call `signup()` or `login()` to
  obtain one, or pass `sessionToken` to the constructor.
- Public market endpoints need no auth.

## Examples

See `examples/place-order.mjs`.
