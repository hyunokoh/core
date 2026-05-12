#!/usr/bin/env bash
# Run every k6 scenario in sequence and dump a JSON summary per scenario.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v k6 >/dev/null 2>&1; then
  echo "k6 not installed.  Install with: brew install k6" >&2
  exit 1
fi

mkdir -p results

for scenario in k6/*.js; do
  name=$(basename "$scenario" .js)
  echo "=== Running $scenario ==="
  k6 run --summary-export="results/${name}.json" "$scenario" || {
    echo "scenario ${name} returned non-zero (likely threshold breach)" >&2
  }
done

echo
echo "All scenarios complete.  JSON summaries in results/"
ls -lh results/
