// CLI helper: mint demo tokens to a given address.
// Usage:
//   npx hardhat run scripts/airdrop.js --network localhost \
//       --no-compile -- <recipient> [asset] [amount]
//
// `asset` is ZETH or ZUSDT; `amount` is the human (decimal) amount.
// Example: ... airdrop.js -- 0xabc... ZETH 100
//
// We avoid the chain bridge talking to hardhat over IPC; instead it just
// shells out to this script. Slightly slower but very simple.
const fs = require("fs");
const path = require("path");
const { ethers } = require("hardhat");

function parseArgs() {
  // Hardhat passes its own args before `--`; user args come after.
  const argv = process.argv.slice(2);
  const sep = argv.indexOf("--");
  const userArgs = sep >= 0 ? argv.slice(sep + 1) : argv;
  const [recipient, asset = "ALL", amount] = userArgs;
  return { recipient, asset: asset.toUpperCase(), amount };
}

async function mintOne(token, amountHuman, decimals, to) {
  const value = ethers.parseUnits(String(amountHuman), decimals);
  const tx = await token.mint(to, value);
  const receipt = await tx.wait();
  return { txHash: receipt.hash, value: value.toString() };
}

async function main() {
  const { recipient, asset, amount } = parseArgs();
  if (!recipient || !/^0x[0-9a-fA-F]{40}$/.test(recipient)) {
    throw new Error("airdrop.js: missing or invalid recipient address");
  }

  const deployment = JSON.parse(
    fs.readFileSync(path.resolve(__dirname, "../.local/deployment.json"), "utf8")
  );
  const Token = await ethers.getContractFactory("ZkERC20");
  const zeth = Token.attach(deployment.tokens.ZETH);
  const zusdt = Token.attach(deployment.tokens.ZUSDT);

  const out = {};
  if (asset === "ZETH" || asset === "ALL") {
    const a = amount || (asset === "ALL" ? "100" : "100");
    out.ZETH = await mintOne(zeth, a, 18, recipient);
  }
  if (asset === "ZUSDT" || asset === "ALL") {
    const a = amount || (asset === "ALL" ? "1000" : "1000");
    out.ZUSDT = await mintOne(zusdt, a, 6, recipient);
  }
  console.log(JSON.stringify(out));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
