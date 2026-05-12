#!/usr/bin/env bash
# Start the zkCEX Web Application Firewall on :5499 in front of the
# serve_homepage proxy at :5500.
#
# Usage:
#   tools/security/start_waf.sh                # foreground
#   tools/security/start_waf.sh --background   # daemonize, write PID file
#
# Environment overrides:
#   WAF_GEO_BLOCKLIST       comma-separated ISO2 list (default: US,IR,KP,CU,SY,RU)
#   WAF_ADMIN_TOKEN         set explicitly; otherwise generated on first start
#   WAF_UPSTREAM_HOST/PORT  default 127.0.0.1:5500

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$(dirname "$HERE")"
LOCAL_DIR="$TOOLS_DIR/.local"
PORT="${WAF_PORT:-5499}"
LOG="$LOCAL_DIR/waf.log"
PIDFILE="$LOCAL_DIR/waf.pid"

mkdir -p "$LOCAL_DIR"

cat <<EOF
=== zkCEX WAF startup ===
Listening:   :$PORT  (L7 origin shield)
Upstream:    ${WAF_UPSTREAM_HOST:-127.0.0.1}:${WAF_UPSTREAM_PORT:-5500}
Blocklist:   ${WAF_GEO_BLOCKLIST:-US,IR,KP,CU,SY,RU}

WAF is now in front. Public traffic should hit https://app.zkcex.io
which terminates at :$PORT.
Internal traffic (this dev box) continues to use :5500 directly.

EOF

if [ "${1:-}" = "--background" ]; then
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "Already running (pid=$(cat "$PIDFILE"))"
    exit 0
  fi
  nohup python3 "$TOOLS_DIR/waf.py" "$PORT" >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "WAF started (pid=$!). Logs: $LOG"
else
  exec python3 "$TOOLS_DIR/waf.py" "$PORT"
fi
