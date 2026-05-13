// zkCEX SDK — CommonJS wrapper around ./zkcex.mjs.
//
// Node 12.20+ supports dynamic import() inside CommonJS, so we expose
// the same surface via a lazy async loader. Consumers typically do:
//
//   const { ZkcexClient } = require("zkcex");
//   const c = await ZkcexClient.create({ baseUrl: "..." });
//   await c.depth("ETHUSDT", 5);
//
// Or, since most projects on Node 18+ already support ESM, prefer
//   import { ZkcexClient } from "zkcex";

let _modPromise = null;
function _load() {
  if (!_modPromise) _modPromise = import("./zkcex.mjs");
  return _modPromise;
}

class ZkcexClient {
  constructor(opts) {
    this._opts = opts || {};
    this._inner = null;
  }
  static async create(opts) {
    const c = new ZkcexClient(opts);
    await c._ready();
    return c;
  }
  async _ready() {
    if (this._inner) return this._inner;
    const mod = await _load();
    this._inner = new mod.ZkcexClient(this._opts);
    return this._inner;
  }
}

// Define proxy methods that forward to the underlying ESM instance.
const PROXY_METHODS = [
  "signup", "login", "me", "logout", "authHealth",
  "exchangeInfo", "depth", "klines", "recentTrades", "ticker24h",
  "placeOrder", "cancelOrder", "openOrders", "myTrades", "account",
  "withdraw",
  "futuresExchangeInfo", "premiumIndex", "fundingRate", "futuresDepth",
  "futuresAccount", "positionRisk", "futuresPlaceOrder",
  "futuresClosePosition", "futuresSetLeverage", "futuresSetMarginType",
  "futuresTransfer", "futuresIncome",
  "chainInfo", "chainWallet", "chainDeposits", "chainWithdraws",
  "chainAirdrop", "chainWithdraw",
  "polServerInfo", "polLatestEpoch", "polMyProof", "polRefresh",
  "reservesVsLiabilities", "polSnapshotLatest",
  "createApiKey", "listApiKeys", "revokeApiKey",
  "conditionalOrder", "listConditionals", "cancelConditional",
];

for (const m of PROXY_METHODS) {
  ZkcexClient.prototype[m] = async function (...args) {
    const inner = await this._ready();
    return inner[m](...args);
  };
}

async function ZkcexError() {
  const mod = await _load();
  return mod.ZkcexError;
}

module.exports = { ZkcexClient, ZkcexError };
