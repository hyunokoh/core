#!/usr/bin/env bash
# 5-minute smoke test, safe to run on a dev box.
#
#   bash tools/load_test/quickstart.sh
#
# Uses k6 if available, falls back to the pure-Python runner otherwise.
set -euo pipefail
cd "$(dirname "$0")"

BASE_URL="${BASE_URL:-http://localhost:5500}"

if command -v k6 >/dev/null 2>&1; then
  echo "Using k6 ..."
  k6 run --duration 60s --vus 20 -e BASE_URL="$BASE_URL" k6/01-market-data.js
else
  echo "k6 not found, using Python fallback runner ..."
  python3 python_runner.py --scenario market-data --users 20 --duration 60s --base-url "$BASE_URL"
fi
