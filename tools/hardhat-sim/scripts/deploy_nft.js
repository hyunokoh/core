// Deploys ZkNFT (ERC721) + ZkNFT1155 to the local hardhat chain and mints
// 100 sample tokens to the custodial address. Writes the resulting addresses
// into .local/deployment.json alongside the existing ZETH/ZUSDT block.
//
// Idempotent: if both NFT contracts are already live at the addresses recorded
// in deployment.json the script no-ops. Re-running after `hardhat node`
// restart re-deploys from scratch.
const fs = require("fs");
const path = require("path");
const { ethers } = require("hardhat");

const OUT_PATH = path.resolve(__dirname, "../.local/deployment.json");
const META_BASE = "/nft-meta";  // served by the homepage static files
const SAMPLE_MINT_COUNT = 100;
const SAMPLE_1155_COUNT = 5;     // distinct 1155 ids
const SAMPLE_1155_QTY   = 50;    // copies per id

function loadDeployment() {
  if (!fs.existsSync(OUT_PATH)) {
    throw new Error(
      `deployment.json not found at ${OUT_PATH}. Run scripts/deploy.js first.`
    );
  }
  return JSON.parse(fs.readFileSync(OUT_PATH, "utf8"));
}

function saveDeployment(d) {
  fs.writeFileSync(OUT_PATH, JSON.stringify(d, null, 2));
}

async function alreadyDeployed(addr) {
  if (!addr) return false;
  const code = await ethers.provider.getCode(addr);
  return code && code !== "0x";
}

async function main() {
  const [deployer] = await ethers.getSigners();
  const network = await ethers.provider.getNetwork();
  const deployment = loadDeployment();
  const custodial = deployment.custodial;
  if (!custodial) throw new Error("deployment.custodial missing");

  console.log(`[deploy_nft] chainId=${network.chainId} deployer=${deployer.address}`);

  let zkNftAddr = deployment.nft?.ZkNFT;
  let zkNft1155Addr = deployment.nft?.ZkNFT1155;
  const zkNftLive = await alreadyDeployed(zkNftAddr);
  const zk1155Live = await alreadyDeployed(zkNft1155Addr);

  if (zkNftLive && zk1155Live) {
    console.log("[deploy_nft] existing NFT deployment is live; skipping.");
    console.log(JSON.stringify(deployment.nft, null, 2));
    return;
  }

  // Deploy ZkNFT (ERC721)
  const ZkNFT = await ethers.getContractFactory("ZkNFT");
  const zkNft = await ZkNFT.deploy("zkCEX Genesis", "ZKGEN", deployer.address);
  await zkNft.waitForDeployment();
  zkNftAddr = await zkNft.getAddress();
  console.log(`[deploy_nft] ZkNFT  @ ${zkNftAddr}`);

  // Deploy ZkNFT1155
  const ZkNFT1155 = await ethers.getContractFactory("ZkNFT1155");
  const zk1155 = await ZkNFT1155.deploy(
    "zkCEX Editions",
    "ZKEDT",
    deployer.address,
  );
  await zk1155.waitForDeployment();
  zkNft1155Addr = await zk1155.getAddress();
  console.log(`[deploy_nft] ZkNFT1155 @ ${zkNft1155Addr}`);

  // Mint 100 ERC721 tokens to the custodial address.
  console.log(`[deploy_nft] minting ${SAMPLE_MINT_COUNT} ERC721 sample tokens…`);
  // Use sendTransaction to batch a bit; hardhat is fast enough sequentially.
  for (let i = 1; i <= SAMPLE_MINT_COUNT; i++) {
    const uri = `${META_BASE}/${i}.json`;
    const tx = await zkNft.mint(custodial, uri);
    await tx.wait();
    if (i % 20 === 0) console.log(`  …minted ${i}`);
  }

  // Mint a handful of ERC1155 editions.
  console.log(`[deploy_nft] minting ${SAMPLE_1155_COUNT} ERC1155 editions x ${SAMPLE_1155_QTY}…`);
  for (let i = 1; i <= SAMPLE_1155_COUNT; i++) {
    // Index 1155 metadata in a separate range starting at 1001 so the
    // homepage SVG generator produces clearly different art.
    const metaId = 1000 + i;
    const uri = `${META_BASE}/${metaId}.json`;
    const tx = await zk1155.mint(custodial, SAMPLE_1155_QTY, uri);
    await tx.wait();
  }

  // Minimal ABIs the Python side needs to encode calls / decode events.
  const erc721Abi = [
    "function balanceOf(address) view returns (uint256)",
    "function ownerOf(uint256) view returns (address)",
    "function safeTransferFrom(address,address,uint256)",
    "function transferFrom(address,address,uint256)",
    "function approve(address,uint256)",
    "function setApprovalForAll(address,bool)",
    "function isApprovedForAll(address,address) view returns (bool)",
    "function tokenURI(uint256) view returns (string)",
    "function name() view returns (string)",
    "function symbol() view returns (string)",
    "function totalSupply() view returns (uint256)",
    "function nextTokenId() view returns (uint256)",
    "function mint(address,string) returns (uint256)",
    "event Transfer(address indexed from, address indexed to, uint256 indexed tokenId)",
    "event Minted(address indexed to, uint256 indexed tokenId, string uri)",
  ];
  const erc1155Abi = [
    "function balanceOf(address,uint256) view returns (uint256)",
    "function balanceOfBatch(address[],uint256[]) view returns (uint256[])",
    "function setApprovalForAll(address,bool)",
    "function isApprovedForAll(address,address) view returns (bool)",
    "function safeTransferFrom(address,address,uint256,uint256,bytes)",
    "function uri(uint256) view returns (string)",
    "function name() view returns (string)",
    "function symbol() view returns (string)",
    "function mint(address,uint256,string) returns (uint256)",
    "function mintMore(address,uint256,uint256)",
    "event TransferSingle(address indexed operator, address indexed from, address indexed to, uint256 id, uint256 value)",
    "event Minted(address indexed to, uint256 indexed tokenId, uint256 amount, string uri)",
  ];

  deployment.nft = {
    ZkNFT: zkNftAddr,
    ZkNFT1155: zkNft1155Addr,
    erc721Abi,
    erc1155Abi,
    collections: [
      {
        address: zkNftAddr,
        standard: "erc721",
        name: "zkCEX Genesis",
        symbol: "ZKGEN",
        description:
          "A 100-piece genesis collection minted on the zkCEX hardhat demo chain. " +
          "Pure SVG, deterministically derived from token id.",
        total_supply: SAMPLE_MINT_COUNT,
      },
      {
        address: zkNft1155Addr,
        standard: "erc1155",
        name: "zkCEX Editions",
        symbol: "ZKEDT",
        description:
          `${SAMPLE_1155_COUNT} ERC1155 editions, each with ${SAMPLE_1155_QTY} copies — ` +
          "demonstrates fractional editions next to the strictly non-fungible Genesis set.",
        total_supply: SAMPLE_1155_COUNT * SAMPLE_1155_QTY,
        edition_ids: Array.from({ length: SAMPLE_1155_COUNT }, (_, i) => i + 1),
        copies_per_id: SAMPLE_1155_QTY,
      },
    ],
    meta_base: META_BASE,
    sample_count_721: SAMPLE_MINT_COUNT,
    sample_count_1155: SAMPLE_1155_COUNT,
    copies_per_1155: SAMPLE_1155_QTY,
    deployedAt: Math.floor(Date.now() / 1000),
  };

  saveDeployment(deployment);
  console.log(`[deploy_nft] wrote ${OUT_PATH}`);
  console.log(JSON.stringify(deployment.nft, null, 2));
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
