// Five-step spirit example for the JS SDK.
//
// Run:  node examples/place-order.mjs

import { ZkcexClient, ZkcexError } from "../zkcex.mjs";

const baseUrl = process.env.ZKCEX_URL || "http://localhost:5500";
const c = new ZkcexClient({ baseUrl });

const email = `sdk-js-${Date.now()}@example.com`;
const me = await c.signup(email, "pass1234", "SDK JS demo");
console.log("signup user:", me.user.opex_user);

const info = await c.exchangeInfo();
console.log("exchange has", info.symbols.length, "markets");

const depth = await c.depth("ETHUSDT", 5);
console.log("depth top ask:", depth.asks[0]);

const key = await c.createApiKey({ label: "sdk-js-demo",
                                   scopes: ["read", "trade"] });
c.apiKey = key.key_id;
c.apiSecret = key.secret;
console.log("api key:", key.key_id);

console.log("account:", await c.account());

try {
  const order = await c.placeOrder({
    symbol: "ETHUSDT", side: "BUY", type: "LIMIT",
    quantity: "0.01", price: "50", timeInForce: "GTC",
  });
  console.log("order:", order);
} catch (err) {
  if (err instanceof ZkcexError) {
    console.log("placeOrder rejected (often expected):", err.status, err.payload);
  } else {
    throw err;
  }
}
