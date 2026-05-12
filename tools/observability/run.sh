#!/usr/bin/env bash
# zkCEX observability launcher.
#
# Starts the metrics aggregator + the PagerDuty webhook on the host, then
# brings up Grafana + Prometheus + Loki + Promtail in docker compose.
#
# Idempotent: kills any prior instance of the host services before relaunch.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TOOLS_DIR="$(cd .. && pwd)"
AGG_PORT="${AGG_PORT:-5640}"
PD_PORT="${PD_PORT:-5641}"

log() { echo "[run.sh] $*"; }

# ----- 0) Build the dashboards (deterministic, version-controllable JSON) ---
log "regenerating Grafana dashboards"
python3 "$SCRIPT_DIR/build_dashboards.py" >/dev/null

# ----- 1) Stop any prior host-side services on these ports ------------------
for port in "$AGG_PORT" "$PD_PORT"; do
    if lsof -tiTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        log "killing prior listener on :$port"
        kill -TERM "$(lsof -tiTCP:"$port" -sTCP:LISTEN)" 2>/dev/null || true
        sleep 0.5
    fi
done

# ----- 2) Start the aggregator ---------------------------------------------
log "starting metrics_aggregator on :$AGG_PORT"
nohup python3 "$TOOLS_DIR/metrics_aggregator.py" "$AGG_PORT" \
    > /tmp/metrics_aggregator.log 2>&1 &
echo $! > /tmp/metrics_aggregator.pid

# ----- 3) Start the PagerDuty webhook --------------------------------------
log "starting pagerduty_webhook on :$PD_PORT (PAGERDUTY_INTEGRATION_KEY=${PAGERDUTY_INTEGRATION_KEY:-<unset, log-only>})"
nohup python3 "$SCRIPT_DIR/pagerduty_webhook.py" "$PD_PORT" \
    > /tmp/pagerduty_webhook.log 2>&1 &
echo $! > /tmp/pagerduty_webhook.pid

# ----- 4) docker compose: Prometheus + Loki + Promtail + Grafana + AM ------
if ! command -v docker >/dev/null 2>&1; then
    log "docker not found — host services are up but Grafana/Prom/Loki are not"
    exit 0
fi

log "docker compose up -d"
docker compose up -d

# Tempo lives in a sibling compose file so the base stack stays small for
# users who only want metrics+logs. Bring it up if the file is present.
if [ -f "$SCRIPT_DIR/tempo-compose.yml" ]; then
    log "docker compose -f tempo-compose.yml up -d (distributed tracing)"
    docker compose -f tempo-compose.yml up -d || \
        log "tempo failed to start (continuing without traces)"
fi

# ----- 5) Wait for at least one /metrics scrape pass to land ---------------
log "waiting for first scrape to complete (~3s)"
sleep 4
agg_count=$(curl -s -m 2 "http://localhost:${AGG_PORT}/health" \
    | python3 -c "import json,sys;print(json.load(sys.stdin).get('scrape_count','?'))" 2>/dev/null || echo "?")
log "aggregator scrape_count=$agg_count"

# ----- 6) Friendly URLs ----------------------------------------------------
cat <<EOF

zkCEX observability stack is up.

  Grafana             http://localhost:3000   (admin / zkcex, anon viewer OK)
  Prometheus          http://localhost:9090
  Loki                http://localhost:3100   (read by Grafana)
  Tempo (traces)      http://localhost:3200   (OTLP HTTP: :4318, gRPC: :4317)
  Alertmanager        http://localhost:9093
  Metrics aggregator  http://localhost:${AGG_PORT}/metrics
  PagerDuty webhook   http://localhost:${PD_PORT}/health
  Status page         http://localhost:5500/app/status.html

Logs:
  /tmp/metrics_aggregator.log
  /tmp/pagerduty_webhook.log

EOF
