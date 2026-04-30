#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_PYTHON="${SCRIPT_DIR}/.venv/bin/python"

if [[ ! -x "${VENV_PYTHON}" ]]; then
  echo "Bridge virtualenv is missing: ${VENV_PYTHON}" >&2
  exit 1
fi

if [[ -f "${SCRIPT_DIR}/env.local" ]]; then
  set -a
  . "${SCRIPT_DIR}/env.local"
  set +a
fi

export OPEX_WALLET_POSTGRES_DSN="${OPEX_WALLET_POSTGRES_DSN:-postgresql://opex:hiopex@localhost:5435/opex}"
export ZKPOL_MARIADB_DSN="${ZKPOL_MARIADB_DSN:-mysql://app:app-password@localhost:21002/zk_pol}"
export ZKPOL_BRIDGE_NAME="${ZKPOL_BRIDGE_NAME:-default}"
export ZKPOL_BRIDGE_BATCH_SIZE="${ZKPOL_BRIDGE_BATCH_SIZE:-500}"
export ZKPOL_BRIDGE_POLL_INTERVAL_SECONDS="${ZKPOL_BRIDGE_POLL_INTERVAL_SECONDS:-5}"
export ZKPOL_LEDGER_EVENT_ID_OFFSET="${ZKPOL_LEDGER_EVENT_ID_OFFSET:-1000000000000}"
export PYTHONPATH="${SCRIPT_DIR}/..${PYTHONPATH:+:${PYTHONPATH}}"

exec "${VENV_PYTHON}" "${SCRIPT_DIR}/zkpol_bridge.py" "${@:-status}"
