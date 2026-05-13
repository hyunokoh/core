// Deterministic per-user wallet derivation.
//
// Usage:
//   node scripts/derive.js <opex_user>
//
// Returns JSON: { address, privateKey } that the chain bridge caches in
// chain.db. No hardhat dependency — we only use ethers + @noble/hashes which
// are pulled in transitively by hardhat-toolbox. Running directly with `node`
// is much faster than `npx hardhat run` because we skip Hardhat startup.
const crypto = require("crypto");
const path = require("path");

// Resolve ethers from this package's node_modules so this script works when
// invoked from anywhere (the chain bridge cd's away).
let ethers;
try {
  ethers = require(path.resolve(__dirname, "../node_modules/ethers"));
} catch (_) {
  ethers = require("ethers");
}

const SALT = "zkcex-deposit-salt-v1";
const DOMAIN = "zkcex/eth-derive/v1";

function deriveFromOpexUser(opexUser) {
  const h = crypto.createHash("sha256");
  h.update(SALT);
  h.update(":");
  h.update(opexUser);
  h.update(":");
  h.update(DOMAIN);
  const seed = h.digest(); // 32 bytes — exactly the size of an Ethereum priv key.
  const pk = "0x" + seed.toString("hex");
  const wallet = new ethers.Wallet(pk);
  return { address: wallet.address, privateKey: pk };
}

function main() {
  const opexUser = process.argv[2];
  if (!opexUser) {
    console.error("derive.js: missing opex_user");
    process.exit(2);
  }
  const out = deriveFromOpexUser(opexUser);
  process.stdout.write(JSON.stringify(out));
}

if (require.main === module) main();
module.exports = { deriveFromOpexUser };
