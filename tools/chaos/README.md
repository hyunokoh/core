# Chaos Engineering Toolkit

Tools for verifying the zkCEX demo stack survives partial failure.  All
scripts operate on the demo Python services in ports `5500-5640` only --
the zkCEX docker compose stack (ports `8091`, `8094`, ...) is OFF-LIMITS
and the registry refuses to touch it.

## Kill switch  (read this first)

If anything goes wrong, kill every chaos process with:

```bash
KILL_CHAOS=1 pkill -f tools/chaos
```

Setting `KILL_CHAOS=1` in the environment also makes new chaos scripts
refuse to start.

## Components

| script                   | purpose                                                      |
|--------------------------|--------------------------------------------------------------|
| `service_registry.py`    | single source of truth: port -> tier -> restart command      |
| `kill_loop.py`           | random service killer, with health probe + auto-restart      |
| `supervisor.py`          | background watchdog that restarts dead services              |
| `network_partition.py`   | print pfctl / iptables commands to drop traffic to a port    |
| `clock_skew.py`          | verify HMAC `recvWindow` rejection from the client side      |
| `data_corruption.py`     | evil proxy: corrupts a % of upstream responses               |

## Service tiers

| tier            | example services            | killable? |
|-----------------|-----------------------------|-----------|
| `CRITICAL`      | proxy, auth, chain          | NO        |
| `NORMAL`        | mm-bot, ws-feed, pol, ...   | yes       |
| `EXPERIMENTAL`  | custody signer + coord      | yes (3x)  |

## Recipes

### 1) Run chaos monkey for 10 minutes

```bash
# in terminal 1: keep things alive
python3 tools/chaos/supervisor.py --poll 5

# in terminal 2: throw chaos
python3 tools/chaos/kill_loop.py --interval 60 --duration 600
```

### 2) One-shot kill

```bash
python3 tools/chaos/kill_loop.py --kill 5600 --no-supervisor
# check it's actually down
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5500/mm/health
```

### 3) Network partition (manual)

```bash
python3 tools/chaos/network_partition.py --target 5503 --duration 30s
# prints the pfctl / iptables command to copy-paste
```

### 4) Clock skew rejection

```bash
python3 tools/chaos/clock_skew.py --skew-sec -120
# expects: skewed request returns 4xx, honest request returns 200
```

### 5) Corrupt upstream responses

```bash
# stand up an evil proxy in front of chain_server
python3 tools/chaos/data_corruption.py \
    --listen-port 5699 --upstream http://localhost:5502 \
    --corrupt-rate 0.10
# point a client at 5699 and verify it handles malformed responses
```

## Logs

`kill_loop.py` appends JSONL events to `tools/chaos/chaos.log`.  Each line:

```json
{"ts":"2026-05-11T05:00:00Z","event":"kill","port":5600,"name":"mm-bot",...}
{"ts":"2026-05-11T05:00:30Z","event":"post_kill","port":5600,
 "restarted":true,"restart_after_sec":2.1,"health_status":200}
```
