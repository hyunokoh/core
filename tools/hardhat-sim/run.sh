#!/usr/bin/env bash
# Boots a local hardhat node, deploys ZETH + ZUSDT, then keeps the node in the
# foreground. The chain bridge (chain_server.py) reads .local/deployment.json
# and talks to the node via JSON-RPC.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d node_modules ]; then
  echo "[run.sh] installing npm deps (one-time)…"
  npm install --no-audit --no-fund --loglevel=error
fi

mkdir -p .local

# 1) hardhat node in the background
npx hardhat node --hostname 127.0.0.1 --port 8545 > .local/node.log 2>&1 &
NODE_PID=$!
echo "[run.sh] hardhat node pid=$NODE_PID, logs at .local/node.log"

# Best-effort cleanup on Ctrl+C / shell exit.
cleanup() { kill "$NODE_PID" 2>/dev/null || true; }
trap cleanup INT TERM EXIT

# 2) wait for RPC to come up.
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST -H 'Content-Type: application/json' \
    -d '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}' \
    http://127.0.0.1:8545 || true)
  if [ "$code" = "200" ]; then
    break
  fi
  sleep 0.5
done

# 3) deploy ERC20s
npx hardhat run scripts/deploy.js --network localhost > .local/deploy.log 2>&1 || {
  echo "[run.sh] deploy failed; see .local/deploy.log"; tail -50 .local/deploy.log; exit 1;
}
echo "[run.sh] erc20 deployment ready"

# 4) deploy NFT collections (ZkNFT + ZkNFT1155 plus 100 sample mints)
npx hardhat run scripts/deploy_nft.js --network localhost > .local/deploy_nft.log 2>&1 || {
  echo "[run.sh] nft deploy failed; see .local/deploy_nft.log"; tail -50 .local/deploy_nft.log; exit 1;
}
echo "[run.sh] nft deployment ready: $(cat .local/deployment.json | tr -d '\n' | head -c 200)…"

# 4) tail node logs in foreground until killed.
wait "$NODE_PID"
