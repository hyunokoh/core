# zkPoL Bridge

`zkpol_bridge.py` mirrors OPEX wallet liability outbox rows into `zkPoL`'s MariaDB
`ledger_change_event` table.

## What it does

- reads `zkpol_liability_outbox` from the OPEX wallet Postgres database
- scales decimal balances/deltas into integer units using `currency.precision`
- writes deterministic event ids into `zkPoL.ledger_change_event`
- stores progress in `zkpol_bridge_state`

## Install

```bash
python3 -m pip install -r /Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/requirements.txt
```

## Required env

- `OPEX_WALLET_POSTGRES_DSN`
  - example: `postgresql://postgres:postgres@localhost:5432/opex`
- `ZKPOL_MARIADB_DSN`
  - example: `mysql://app:app-password@localhost:3306/zk_pol`

## Optional env

- `ZKPOL_BRIDGE_NAME`
  - default: `default`
- `ZKPOL_BRIDGE_BATCH_SIZE`
  - default: `500`
- `ZKPOL_BRIDGE_POLL_INTERVAL_SECONDS`
  - default: `5`
- `ZKPOL_LEDGER_EVENT_ID_OFFSET`
  - default: `1000000000000`
- `ZKPOL_TOKEN_ALLOWLIST`
  - comma-separated token ids, e.g. `USDT,BTC`
- `ZKPOL_EVENT_TYPE_MAP`
  - comma-separated mappings, e.g. `withdraw:withdrawal,fee:fee`

## Usage

Prepare local defaults by copying:

```bash
cp /Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/env.local.example /Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/env.local
```

Run one batch:

```bash
python3 /Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/zkpol_bridge.py run-once
```

Run continuously:

```bash
python3 /Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/zkpol_bridge.py daemon
```

Show current cursor:

```bash
python3 /Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/zkpol_bridge.py status
```

Use the local wrapper script:

```bash
/Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/run_local_bridge.sh status
/Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/run_local_bridge.sh daemon
```

Run a local end-to-end demo:

```bash
PYTHONPATH=/Users/hoh/Documents/Projects/zkCEX/core/tools \
OPEX_WALLET_POSTGRES_DSN=postgresql://opex:hiopex@localhost:5435/opex \
ZKPOL_MARIADB_DSN=mysql://app:app-password@localhost:21002/zk_pol \
/Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/.venv/bin/python \
/Users/hoh/Documents/Projects/zkCEX/core/tools/zkpol_bridge/demo_local_bridge.py
```
