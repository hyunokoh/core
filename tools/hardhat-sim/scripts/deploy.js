// Deploys ZETH + ZUSDT, mints initial supply to the custodial address,
// and dumps everything (addresses, abi, custodial pk) to .local/deployment.json.
// Re-running is idempotent: if deployment.json already references contracts
// that still answer eth_getCode, the script no-ops.
const fs = require("fs");
const path = require("path");
const { ethers } = require("hardhat");

async function main() {
  const [deployer] = await ethers.getSigners();
  const network = await ethers.provider.getNetwork();

  // Hardhat ships with deterministic dev accounts. Account[0] is the deployer
  // and we also use it as the exchange custodial wallet to keep things simple.
  // The seed phrase + privkey is the well-known hardhat default; storing it in
  // deployment.json makes the chain bridge self-contained without hardcoding
  // anything in user-visible code.
  const HARDHAT_DEV_ACCOUNT_0_PK =
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";

  const outDir = path.resolve(__dirname, "../.local");
  fs.mkdirSync(outDir, { recursive: true });
  const outPath = path.join(outDir, "deployment.json");

  // If we already have a deployment that still has code, keep it.
  let existing = null;
  try {
    existing = JSON.parse(fs.readFileSync(outPath, "utf8"));
  } catch (_) {}
  if (existing && existing.tokens?.ZETH && existing.tokens?.ZUSDT) {
    const codeA = await ethers.provider.getCode(existing.tokens.ZETH);
    const codeB = await ethers.provider.getCode(existing.tokens.ZUSDT);
    if (codeA && codeA !== "0x" && codeB && codeB !== "0x") {
      console.log("[deploy] Existing deployment is still live; skipping.");
      console.log(JSON.stringify(existing, null, 2));
      return;
    }
  }

  console.log(`[deploy] chainId=${network.chainId} deployer=${deployer.address}`);
  const Token = await ethers.getContractFactory("ZkERC20");

  const zeth = await Token.deploy("zkETH", "ZETH", 18);
  await zeth.waitForDeployment();
  const zethAddr = await zeth.getAddress();
  console.log(`[deploy] ZETH @ ${zethAddr}`);

  const zusdt = await Token.deploy("zkUSDT", "ZUSDT", 6);
  await zusdt.waitForDeployment();
  const zusdtAddr = await zusdt.getAddress();
  console.log(`[deploy] ZUSDT @ ${zusdtAddr}`);

  const custodial = deployer.address;
  // Seed custodial with a healthy reserve so withdrawals can succeed.
  const seedZeth = ethers.parseUnits("10000000", 18);
  const seedZusdt = ethers.parseUnits("100000000", 6);
  await (await zeth.mint(custodial, seedZeth)).wait();
  await (await zusdt.mint(custodial, seedZusdt)).wait();

  // Standard ERC20 ABI subset we need on the Python side.
  const erc20Abi = [
    "function balanceOf(address) view returns (uint256)",
    "function transfer(address,uint256) returns (bool)",
    "function decimals() view returns (uint8)",
    "function symbol() view returns (string)",
    "function mint(address,uint256)",
    "event Transfer(address indexed from, address indexed to, uint256 value)",
  ];

  const out = {
    chainId: Number(network.chainId),
    rpc: "http://127.0.0.1:8545",
    deployer: deployer.address,
    deployerPrivKey: HARDHAT_DEV_ACCOUNT_0_PK,
    custodial,
    tokens: {
      ZETH: zethAddr,
      ZUSDT: zusdtAddr,
    },
    decimals: { ZETH: 18, ZUSDT: 6 },
    erc20Abi,
    deployedAt: Math.floor(Date.now() / 1000),
  };
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2));
  console.log(`[deploy] wrote ${outPath}`);
  console.log(JSON.stringify(out, null, 2));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
