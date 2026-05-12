#!/usr/bin/env bash
# tools/dr/ci_run.sh
#
# CI harness for the game-day orchestrator. Runs a stripped subset: simulator
# only, no real disasters. Used to verify the orchestrator code parses, the
# scenario list is up to date, and every scenario's state machine wires up.
#
# Exit codes:
#   0   all scenarios walked successfully in dry-run mode
#   non-zero  at least one scenario crashed during dry-run (CI fails)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GAME_DAY="${HERE}/game_day.py"

if [[ ! -f "${GAME_DAY}" ]]; then
  echo "ci_run.sh: ${GAME_DAY} not found" >&2
  exit 2
fi

echo "==> listing scenarios"
python3 "${GAME_DAY}" --dry-run --list-scenarios

OUT="${1:-ci-result.json}"
echo "==> dry-running all scenarios -> ${OUT}"
python3 "${GAME_DAY}" --dry-run --run-all --output "${OUT}"

# Verify no scenario crashed (status != fail) in dry-run.
python3 - <<PY
import json, sys
with open("${OUT}") as fh:
    rep = json.load(fh)
fails = [r for r in rep.get("results", [])
         if r.get("result", {}).get("status") == "fail"]
if fails:
    print("FAIL: scenarios crashed in dry-run:")
    for r in fails:
        print(f"  - {r['scenario']}: {r['result'].get('error')}")
    sys.exit(1)
print("OK: dry-run for all scenarios walked the state machine cleanly")
print(f"   pass={rep['summary']['n_pass']} "
      f"fail={rep['summary']['n_fail']} "
      f"skipped={rep['summary']['n_skipped']} "
      f"timeout={rep['summary']['n_timeout']}")
PY
