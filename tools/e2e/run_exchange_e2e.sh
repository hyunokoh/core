#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -z "${DOCKER_SOCK:-}" ]]; then
  if [[ -S "${HOME}/.docker/run/docker.sock" ]]; then
    DOCKER_SOCK="unix://${HOME}/.docker/run/docker.sock"
  else
    DOCKER_SOCK="unix:///var/run/docker.sock"
  fi
fi
COMPOSE=(docker -H "$DOCKER_SOCK" compose
  --env-file "$ROOT_DIR/.env.e2e"
  -f "$ROOT_DIR/docker-compose.yml"
  -f "$ROOT_DIR/docker-compose.override.yml"
  -f "$ROOT_DIR/docker-compose.build.yml"
  -f "$ROOT_DIR/docker-compose.local.yml"
  -f "$ROOT_DIR/tools/e2e/docker-compose.e2e.yml"
)
COMPOSE_FULL_STACK=(docker -H "$DOCKER_SOCK" compose
  --profile full-stack
  --env-file "$ROOT_DIR/.env.e2e"
  -f "$ROOT_DIR/docker-compose.yml"
  -f "$ROOT_DIR/docker-compose.override.yml"
  -f "$ROOT_DIR/docker-compose.build.yml"
  -f "$ROOT_DIR/docker-compose.local.yml"
  -f "$ROOT_DIR/tools/e2e/docker-compose.e2e.yml"
)

PACKAGE=0
BUILD=0
KEEP_RUNNING=0
RESET=0
EVENTUAL_TIMEOUT=240
CURL_CONNECT_TIMEOUT="${CURL_CONNECT_TIMEOUT:-5}"
CURL_MAX_TIME="${CURL_MAX_TIME:-20}"

curl() {
  command curl --connect-timeout "$CURL_CONNECT_TIMEOUT" --max-time "$CURL_MAX_TIME" "$@"
}

usage() {
  cat <<EOF
Usage: tools/e2e/run_exchange_e2e.sh [--package] [--build] [--reset] [--keep-running] [--help]

Runs a real Docker-backed exchange E2E flow:
  1. Wait for wallet, accountant, matching-engine, matching-gateway, and market.
  2. Deposit ETH/USDT into seller and buyer MAIN wallets.
  3. Submit matching ETH_USDT ask/bid orders through matching-gateway.
  4. Wait for the current seller/buyer trade to propagate through market.
  5. Verify final wallet settlement balances, including fees.
  6. Submit an unmatched order, verify reservation/open book state, cancel it, and verify release.
  7. Verify the user order query API returns that order and rejects missing/unauthorized lookups.
  8. Partially fill an order, verify the remaining book quantity, cancel the remainder, and verify release.
  9. Verify an IOC order with no liquidity is canceled immediately and releases reserved funds.
  10. Verify an IOC market ask consumes available bid liquidity and the maker remainder can be canceled.
  11. Verify an IOC market ask sweeps multiple bid price levels and leaves/cancels the maker remainder.
  12. Verify an IOC market bid sweeps multiple ask price levels and leaves/cancels the maker remainder.
  13. Verify price priority by matching against the better bid before a lower bid.
  14. Verify same-price time priority by filling the older bid before the newer bid.
  15. Verify already reserved base/quote balances cannot be over-reserved by second orders.
  16. Verify a different user cannot cancel someone else's open order.
  17. Verify a different user cannot edit someone else's open order.
  18. Verify a duplicate cancel of an already canceled order does not release funds twice.
  19. Verify malformed cancel requests are rejected before they reach market state.
  20. Verify malformed edit requests are rejected before they reach market state.
  21. Verify an unsupported FOK order is rejected at the gateway before it reaches market state.
  22. Verify same-account crossing orders are rejected by self-trade prevention and release reserved funds.
  22b. Verify self-trade prevention rejects before any partial external fill when own liquidity is behind the best price.
  22c. Verify same-account crossing order edits are rejected by self-trade prevention without mutating open orders or balances.
  23. Verify underfunded ask/bid orders are rejected before they reach market state.
  24. Verify invalid order parameters are rejected before they reach market state.
  25. Verify the public order book is empty after all E2E open-order scenarios are cleaned up.
  26. Verify the public recent-trades feed contains the expected trade count and price/quantity distribution.
  27. Verify wallet/accountant/market database invariants after settlement.
  27b. Verify public API active-user/order/trade counts match market projections.
  28. Verify normalized accountant order definitions match market order definitions.
  29. Verify accountant order reservation/release actions match eventlog order lifecycle events.
  30. Verify accountant trade settlement transfers match market trade executions.
  31. Verify accountant and wallet fee ledgers match market trade commissions.
  32. Verify every processed accountant financial action for E2E users has a matching wallet transaction.
  33. Verify wallet user-transaction ledger rows match wallet transaction movements.
  34. Verify eventlog trade audit rows match market trade projections before replay.
  35. Verify Binance-compatible public REST exchangeInfo/depth/trades reflect the same exchange state.
  35b. Verify Binance-compatible price ticker reflects the latest executed trade.
  35c. Verify Binance-compatible 24h ticker reflects the latest executed trade statistics.
  35d. Verify Binance-compatible klines reflect executed trade OHLC and volume statistics.
  35e. Verify Binance-compatible default and endTime-only klines return the latest market-projected candle.
  36. Verify Binance-compatible private order submission/cancel/account/order/trade APIs reflect settled balances and executions.
  36b. Verify Binance-compatible openOrders/allOrders without a symbol returns the user's orders across markets.
  36c. Verify Binance-compatible myTrades fromId returns trades by inclusive exchange trade id.
  36d. Verify Binance-compatible myTrades orderId returns trades for that user order.
  36e. Verify Binance-compatible myTrades orderId is not confused with exchange trade id.
  36f. Verify Binance-compatible myTrades orderId does not leak another user's trades.
  36f2. Verify Binance-compatible allOrders/myTrades startTime/endTime use epoch-millisecond ranges.
  36g. Verify Binance-compatible order lookup by orderId rejects another user's order.
  36h. Verify Binance-compatible origClientOrderId lookup/cancel cannot affect another user's order.
  36i. Verify Binance-compatible orderId cancel cannot affect another user's order.
  36j. Verify Binance-compatible duplicate orderId cancel keeps balances unchanged.
  36k. Verify Binance-compatible filled orderId cancel is rejected and keeps balances unchanged.
  36l. Verify Binance-compatible partial fill orderId cancel releases only the remaining locked balance.
  36m0. Verify Binance-compatible MARKET sell with no liquidity cancels immediately and releases funds.
  36m. Verify Binance-compatible MARKET sell maps to IOC and settles through the real exchange path.
  36n0. Verify Binance-compatible price-capped MARKET buy with no liquidity cancels immediately and releases funds.
  36n. Verify Binance-compatible price-capped MARKET buy maps to IOC and settles through the real exchange path.
  36o. Verify Binance-compatible MARKET buy without a price cap is rejected before balances or book state change.
  36p. Verify Binance-compatible newOrderRespType=ACK/RESULT/FULL responses submit, reserve, cancel, and project through the real exchange path.
  36p2. Verify Binance-compatible explicit and generated client order create responses include projected order status fields.
  36q. Verify Binance-compatible cancel newClientOrderId is returned without changing the real cancel path.
  36r. Verify Binance-compatible private API timestamp and recvWindow checks reject invalid signed requests.
  36s. Verify Binance-compatible LIMIT IOC with no liquidity cancels and releases reserved funds.
  36t. Verify Binance-compatible LIMIT IOC partial fill cancels the remainder and releases reserved funds.
  36u. Verify Binance-compatible LIMIT IOC sell partial fill cancels the remainder and releases reserved funds.
  36v. Verify Binance-compatible same-account crossing orders are rejected by self-trade prevention and release reserved funds.
  36w. Verify Binance-compatible underfunded LIMIT orders are rejected before balances or order projections change.
  37. Verify no negative balances, duplicate ledger refs, unprocessed accounting actions, or structurally invalid market trades remain.
  38. Restart Market and verify public market state is still available from persisted data.
  39. Verify BTC_USDT can trade independently from the ETH_USDT market.
  40. Verify SOL_USDT, DOGE_USDT, and TON_USDT can trade on the secondary matching-engine shard.
  41. Restart Matching Engine with an open order, verify Redis snapshot restore, and verify it can still be matched.
  42. Restart Wallet before a trade settlement and verify balances still settle correctly.
  43. Restart Accountant before a trade settlement and verify financial actions still settle correctly.
  44. Restart Matching Gateway and verify new order submission still works.
  45. Restart all core exchange services and verify a fresh trade still settles.
  46. Verify Matching Gateway rejects new orders while Kafka is down, then restart Kafka and verify a fresh trade settles.
  47. Restart Wallet/Accountant/Market Postgres datastores and verify a fresh trade still settles.
  48. Verify duplicate deposit transfer references are rejected without double-crediting the wallet, transaction history, or legacy transaction API.
  49. Verify wallet transfer success/rejection/idempotency through the real HTTP path, ledger, and transaction API.
  50. Verify withdraw request/cancel/process/accept/reject transitions, duplicate accept rejection, user history, transaction history, and legacy transaction API.
  50b. Verify Binance-compatible deposit/withdraw history APIs reflect bc-gateway deposits, wallet withdraw terminal states, and status filters.
  51. Replay real order create/cancel Kafka records and verify matching/accounting remain idempotent.
  52. Replay real richOrder/richTrade Kafka records and verify market projections remain idempotent.

Options:
  --package       Run Maven package for Docker-backed app jars before building.
  --build         Run docker compose build before starting services.
  --reset         Remove the E2E compose stack and volumes before starting.
  --keep-running  Leave app containers running after a successful flow.
  --help          Show this help text.
EOF
}

for arg in "$@"; do
  case "$arg" in
    --package) PACKAGE=1 ;;
    --build) BUILD=1 ;;
    --keep-running) KEEP_RUNNING=1 ;;
    --reset) RESET=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 2 ;;
  esac
done

write_defaults() {
  if [[ ! -f "$ROOT_DIR/.env.e2e" ]]; then
    cat > "$ROOT_DIR/.env.e2e" <<'ENV'
APP_NAME=Opex-e2e
APP_BASE_URL=localhost:8080
PANEL_PASS=admin
BACKEND_USER=admin
KEYCLOAK_ADMIN_USERNAME=opex
KEYCLOAK_ADMIN_PASSWORD=hiopex
SMTP_PASS=x
API_KEY_CLIENT_SECRET=x
KEYCLOAK_FRONTEND_URL=http://localhost:8083/auth
KEYCLOAK_ADMIN_URL=http://localhost:8083/auth
KEYCLOAK_VERIFY_REDIRECT_URL=http://localhost:8080/verify
KEYCLOAK_FORGOT_REDIRECT_URL=http://localhost:8080/forgot
PREFERENCES=/preferences.yml
WHITELIST_REGISTER_ENABLED=false
WHITELIST_LOGIN_ENABLED=false
WALLET_BACKUP_ENABLED=false
OPEX_ADMIN_KEYCLOAK_CLIENT_SECRET=x
TAG=e2e
ENV
  else
    if grep -q '^PREFERENCES=' "$ROOT_DIR/.env.e2e"; then
      sed -i.bak 's#^PREFERENCES=.*#PREFERENCES=/preferences.yml#' "$ROOT_DIR/.env.e2e"
      rm -f "$ROOT_DIR/.env.e2e.bak"
    else
      printf '\nPREFERENCES=/preferences.yml\n' >> "$ROOT_DIR/.env.e2e"
    fi
  fi

  if [[ ! -f "$ROOT_DIR/preferences.yml" ]]; then
    cp "$ROOT_DIR/preferences-dev.yml" "$ROOT_DIR/preferences.yml"
  fi
}

configure_java() {
  if [[ -n "${JAVA_HOME:-}" && -x "$JAVA_HOME/bin/java" ]]; then
    return 0
  fi

  local local_java_home="$ROOT_DIR/.local-tools/jdk-21.0.11.jdk/Contents/Home"
  if [[ -x "$local_java_home/bin/java" ]]; then
    export JAVA_HOME="$local_java_home"
    export PATH="$JAVA_HOME/bin:$PATH"
  fi
}

find_maven() {
  if command -v mvn >/dev/null 2>&1; then
    printf 'mvn\n'
    return 0
  fi

  local local_maven="$ROOT_DIR/.local-tools/apache-maven-3.9.9/bin/mvn"
  if [[ -x "$local_maven" ]]; then
    printf '%s\n' "$local_maven"
    return 0
  fi

  return 1
}

package_apps() {
  configure_java

  local mvn_bin
  if ! mvn_bin="$(find_maven)"; then
    echo "Maven not found. Install Maven or place it at .local-tools/apache-maven-3.9.9/bin/mvn." >&2
    exit 1
  fi

  "$mvn_bin" \
    -pl wallet/wallet-app,accountant/accountant-app,matching-engine/matching-engine-app,matching-gateway/matching-gateway-app,market/market-app,eventlog/eventlog-app,api/api-app \
    -am \
    package \
    -Dmaven.test.skip=true
}

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "$command_name is required for the E2E flow but was not found on PATH." >&2
    exit 1
  fi
}

curl_json() {
  local method="$1"
  local url="$2"
  local body="${3:-}"
  local header_user="${4:-}"
  local response_file
  response_file="$(mktemp)"

  local args=(-sS -X "$method" -H "Content-Type: application/json" -w "%{http_code}" -o "$response_file")
  if [[ -n "$header_user" ]]; then
    args+=(-H "X-Opex-User: $header_user")
  fi
  if [[ -n "$body" ]]; then
    args+=(-d "$body")
  fi
  args+=("$url")

  local status
  status="$(curl "${args[@]}")"
  printf '%s\n' "$status"
  cat "$response_file"
  rm -f "$response_file"
}

wait_http() {
  local name="$1"
  local url="$2"
  local deadline=$((SECONDS + 900))
  until curl -fsS "$url" >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $name at $url" >&2
      "${COMPOSE[@]}" ps >&2 || true
      "${COMPOSE[@]}" logs --tail=120 "$name" >&2 || true
      exit 1
    fi
    sleep 3
  done
  echo "ready: $name"
}

wait_log() {
  local service="$1"
  local label="$2"
  local pattern="$3"
  local logs
  logs="$("${COMPOSE[@]}" logs "$service" 2>/dev/null || true)"
  if grep -q "$pattern" <<<"$logs"; then
    echo "ready: $label"
  else
    wait_log_since "$service" "$label" "$pattern" "$(log_since_now)" 180
  fi
}

log_since_now() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

wait_log_since() {
  local service="$1"
  local label="$2"
  local pattern="$3"
  local since="$4"
  local timeout="${5:-180}"
  local deadline=$((SECONDS + timeout))
  local logs
  until logs="$("${COMPOSE[@]}" logs --since="$since" "$service" 2>/dev/null)" && grep -q "$pattern" <<<"$logs"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label in $service logs" >&2
      "${COMPOSE[@]}" logs --tail=200 "$service" >&2 || true
      exit 1
    fi
    sleep 3
  done
  echo "ready: $label"
}

wait_exchange_consumer_groups_stable_since() {
  local since="$1"
  local label="$2"
  wait_consumer_group_stable "engine" "$label engine consumer group"
  wait_consumer_group_stable "accountant" "$label accountant consumer group"
  wait_consumer_group_stable "market" "$label market consumer group"
  wait_consumer_group_stable "eventlog" "$label eventlog consumer group"
}

wait_consumer_group_stable() {
  local group="$1"
  local label="$2"
  local timeout="${3:-300}"
  local deadline=$((SECONDS + timeout))
  local description
  until description="$("${COMPOSE[@]}" exec -T kafka-1 env KAFKA_OPTS= kafka-consumer-groups \
      --bootstrap-server kafka-1:29092 \
      --describe \
      --group "$group" 2>/dev/null)" &&
    awk -v group="$group" '
      $1 == group {
        rows++
        if ($6 != "0") bad = 1
      }
      /has no active members/ { bad = 1 }
      END { exit !(rows > 0 && bad != 1) }
    ' <<<"$description"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label" >&2
      printf '%s\n' "$description" >&2
      exit 1
    fi
    sleep 3
  done
  echo "ready: $label"
}

remove_full_stack_residue() {
  # E2E is intentionally single-broker. Stale full-stack containers can rejoin
  # ZooKeeper and change topic leadership/metadata while the test is running.
  "${COMPOSE_FULL_STACK[@]}" rm -f -s -v \
    kafka-2 kafka-3 akhq matching-engine-duo auth api bc-gateway postgres-opex \
    >/dev/null 2>&1 || true
}

wait_zookeeper_broker_id_released() {
  local broker_id="$1"
  local deadline=$((SECONDS + 120))
  local ids
  until ids="$("${COMPOSE[@]}" exec -T zookeeper sh -lc "printf 'ls /brokers/ids\nquit\n' | zookeeper-shell zookeeper:2181" 2>/dev/null)" &&
    ! grep -Eq "\[[^]]*${broker_id}[^]]*\]" <<<"$ids"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for zookeeper broker id $broker_id to be released" >&2
      echo "$ids" >&2
      "${COMPOSE[@]}" logs --tail=200 zookeeper kafka-1 >&2 || true
      exit 1
    fi
    sleep 3
  done
  echo "ready: zookeeper broker id $broker_id released"
}

wait_kafka_topic_ready() {
  local topic="$1"
  local deadline=$((SECONDS + 180))
  local description
  until description="$("${COMPOSE[@]}" exec -T kafka-1 env KAFKA_OPTS= kafka-topics --bootstrap-server kafka-1:29092 --describe --topic "$topic" 2>/dev/null)" &&
    grep -q "Leader: 1001" <<<"$description"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for kafka topic $topic leader readiness" >&2
      echo "$description" >&2
      "${COMPOSE[@]}" logs --tail=200 kafka-1 zookeeper >&2 || true
      exit 1
    fi
    sleep 3
  done
  echo "ready: kafka topic $topic"
}

ensure_kafka_topic_ready() {
  local topic="$1"
  local deadline=$((SECONDS + 180))
  until "${COMPOSE[@]}" exec -T kafka-1 env KAFKA_OPTS= kafka-topics \
    --bootstrap-server kafka-1:29092 \
    --create \
    --if-not-exists \
    --partitions 1 \
    --replication-factor 1 \
    --topic "$topic" >/dev/null 2>&1; do
    if (( SECONDS > deadline )); then
      echo "Timed out creating kafka topic $topic" >&2
      "${COMPOSE[@]}" logs --tail=200 kafka-1 zookeeper >&2 || true
      exit 1
    fi
    sleep 3
  done
  wait_kafka_topic_ready "$topic"
}

ensure_exchange_topics_ready() {
  ensure_kafka_topic_ready "orders_ETH_USDT"
  ensure_kafka_topic_ready "events_ETH_USDT"
  ensure_kafka_topic_ready "trades_ETH_USDT"
  ensure_kafka_topic_ready "orders_BTC_USDT"
  ensure_kafka_topic_ready "events_BTC_USDT"
  ensure_kafka_topic_ready "trades_BTC_USDT"
  ensure_kafka_topic_ready "orders_SOL_USDT"
  ensure_kafka_topic_ready "events_SOL_USDT"
  ensure_kafka_topic_ready "trades_SOL_USDT"
  ensure_kafka_topic_ready "orders_DOGE_USDT"
  ensure_kafka_topic_ready "events_DOGE_USDT"
  ensure_kafka_topic_ready "trades_DOGE_USDT"
  ensure_kafka_topic_ready "orders_TON_USDT"
  ensure_kafka_topic_ready "events_TON_USDT"
  ensure_kafka_topic_ready "trades_TON_USDT"
  ensure_kafka_topic_ready "richOrder"
  ensure_kafka_topic_ready "richTrade"
}

wait_kafka_broker_ready() {
  local deadline=$((SECONDS + 180))
  until "${COMPOSE[@]}" exec -T kafka-1 env KAFKA_OPTS= kafka-topics --bootstrap-server kafka-1:29092 --list >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for kafka-1 broker readiness" >&2
      "${COMPOSE[@]}" logs --tail=200 kafka-1 zookeeper >&2 || true
      exit 1
    fi
    sleep 3
  done
  echo "ready: kafka-1"
}

restart_market_and_verify_public_state() {
  "${COMPOSE[@]}" restart market
  wait_http "market" "http://127.0.0.1:8096/actuator/health"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  wait_recent_trades_distribution "ETH_USDT" /tmp/opex-e2e-recent-trades-after-market-restart.json
}

restart_matching_engine_and_wait() {
  local since
  since="$(log_since_now)"
  "${COMPOSE[@]}" restart matching-engine
  wait_http "matching-engine" "http://127.0.0.1:8092/actuator/health"
  wait_log_since "matching-engine" "matching-engine ETH_USDT order consumer after restart" "orders_ETH_USDT-0" "$since" 180
  sleep 5
}

restart_wallet_and_wait() {
  "${COMPOSE[@]}" restart wallet
  wait_http "wallet" "http://127.0.0.1:8091/actuator/health"
  sleep 5
}

restart_accountant_and_wait() {
  local since
  since="$(log_since_now)"
  "${COMPOSE[@]}" restart accountant
  wait_http "accountant" "http://127.0.0.1:8089/actuator/health"
  wait_log_since "accountant" "accountant ETH_USDT order consumer after restart" "orders_ETH_USDT-0" "$since" 180
  sleep 5
}

restart_matching_gateway_and_wait() {
  "${COMPOSE[@]}" restart matching-gateway
  wait_http "matching-gateway" "http://127.0.0.1:8093/actuator/health"
  sleep 5
}

restart_core_services_and_wait() {
  local since
  since="$(log_since_now)"
  "${COMPOSE[@]}" restart matching-gateway matching-engine accountant wallet market eventlog
  wait_http "wallet" "http://127.0.0.1:8091/actuator/health"
  wait_http "accountant" "http://127.0.0.1:8089/actuator/health"
  wait_http "eventlog" "http://127.0.0.1:8090/actuator/health"
  wait_http "matching-engine" "http://127.0.0.1:8092/actuator/health"
  wait_http "matching-gateway" "http://127.0.0.1:8093/actuator/health"
  wait_http "market" "http://127.0.0.1:8096/actuator/health"
  wait_exchange_consumer_groups_stable_since "$since" "core restart"
  sleep 8
}

restart_kafka_and_wait() {
  local since
  since="$(log_since_now)"
  "${COMPOSE[@]}" stop kafka-1
  wait_zookeeper_broker_id_released "1001"
  "${COMPOSE[@]}" start kafka-1
  wait_kafka_broker_ready
  ensure_exchange_topics_ready
  wait_exchange_consumer_groups_stable_since "$since" "kafka restart"
  sleep 15
}

restart_kafka_with_gateway_rejection_check() {
  local order_body="$1"
  local owner="$2"
  local unchanged_asset="$3"
  local unchanged_balance="$4"
  local since
  since="$(log_since_now)"

  "${COMPOSE[@]}" stop kafka-1
  wait_zookeeper_broker_id_released "1001"
  wait_log_since "matching-gateway" "matching-gateway Kafka health down" "Kafka is not healthy" "$since" 60
  expect_http_status "order while Kafka unavailable" "503" "$(curl_json POST "http://127.0.0.1:8093/order" "$order_body" "$owner")" >/tmp/opex-e2e-kafka-down-order.json
  assert_wallet_balance "Kafka-down owner balance unchanged" "$owner" "$unchanged_asset" "$unchanged_balance"
  wait_no_user_open_orders "$owner" "ETH_USDT"
  assert_no_user_orders "$owner" "ETH_USDT"

  "${COMPOSE[@]}" start kafka-1
  wait_kafka_broker_ready
  ensure_exchange_topics_ready
  wait_exchange_consumer_groups_stable_since "$since" "kafka restart"
  sleep 15
}

wait_postgres() {
  local service="$1"
  local deadline=$((SECONDS + 120))
  until "${COMPOSE[@]}" exec -T "$service" pg_isready -U opex -d opex >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $service Postgres readiness" >&2
      "${COMPOSE[@]}" logs --tail=120 "$service" >&2 || true
      exit 1
    fi
    sleep 2
  done
  echo "ready: $service"
}

restart_postgres_datastores_and_wait() {
  "${COMPOSE[@]}" restart postgres-wallet postgres-accountant postgres-market
  wait_postgres "postgres-wallet"
  wait_postgres "postgres-accountant"
  wait_postgres "postgres-market"
  wait_http "wallet" "http://127.0.0.1:8091/actuator/health"
  wait_http "accountant" "http://127.0.0.1:8089/actuator/health"
  wait_http "market" "http://127.0.0.1:8096/actuator/health"
  sleep 15
}

expect_2xx() {
  local label="$1"
  local output="$2"
  local status body
  status="$(printf '%s\n' "$output" | head -n1)"
  body="$(printf '%s\n' "$output" | tail -n +2)"
  if [[ ! "$status" =~ ^2 ]]; then
    echo "$label failed with HTTP $status" >&2
    echo "$body" >&2
    exit 1
  fi
  printf '%s\n' "$body"
}

expect_2xx_retry() {
  local label="$1"
  local command="$2"
  local deadline=$((SECONDS + 60))
  local output status body
  while true; do
    output="$(eval "$command")"
    status="$(printf '%s\n' "$output" | head -n1)"
    body="$(printf '%s\n' "$output" | tail -n +2)"
    if [[ "$status" =~ ^2 ]]; then
      printf '%s\n' "$body"
      return 0
    fi
    if (( SECONDS > deadline )); then
      echo "$label failed with HTTP $status" >&2
      echo "$body" >&2
      return 1
    fi
    sleep 2
  done
}

expect_non_2xx() {
  local label="$1"
  local output="$2"
  local status body
  status="$(printf '%s\n' "$output" | head -n1)"
  body="$(printf '%s\n' "$output" | tail -n +2)"
  if [[ "$status" =~ ^2 ]]; then
    echo "$label unexpectedly succeeded with HTTP $status" >&2
    echo "$body" >&2
    exit 1
  fi
  printf '%s\n' "$body"
}

expect_http_status() {
  local label="$1"
  local expected_status="$2"
  local output="$3"
  local status body
  status="$(printf '%s\n' "$output" | head -n1)"
  body="$(printf '%s\n' "$output" | tail -n +2)"
  if [[ "$status" != "$expected_status" ]]; then
    echo "$label expected HTTP $expected_status but got HTTP $status" >&2
    echo "$body" >&2
    exit 1
  fi
  printf '%s\n' "$body"
}

assert_opex_error() {
  local label="$1"
  local expected_error="$2"
  local expected_code="$3"
  local body="$4"
  if ! printf '%s\n' "$body" | jq -e --arg error "$expected_error" --argjson code "$expected_code" \
    '.error == $error and .code == $code' >/dev/null; then
    echo "$label expected Opex error $expected_error/$expected_code" >&2
    echo "$body" >&2
    exit 1
  fi
}

json_number() {
  local field="$1"
  sed -E "s/.*\"${field}\":([^,}]+).*/\\1/"
}

assert_number_eq() {
  local label="$1"
  local actual="$2"
  local expected="$3"
  awk -v label="$label" -v actual="$actual" -v expected="$expected" '
    BEGIN {
      diff = actual - expected
      if (diff < 0) diff = -diff
      if (diff > 0.000001) {
        printf "%s expected %s but got %s\n", label, expected, actual > "/dev/stderr"
        exit 1
      }
    }
  '
}

assert_text_eq() {
  local label="$1"
  local actual="$2"
  local expected="$3"
  if [[ "$actual" != "$expected" ]]; then
    echo "$label expected:" >&2
    printf '%s\n' "$expected" >&2
    echo "$label actual:" >&2
    printf '%s\n' "$actual" >&2
    exit 1
  fi
}

psql_query() {
  local service="$1"
  local query="$2"
  "${COMPOSE[@]}" exec -T "$service" psql -U opex -d opex -At -F ',' -c "$query" | sed '/^$/d'
}

assert_query_eq() {
  local label="$1"
  local service="$2"
  local expected="$3"
  local query="$4"
  local actual
  actual="$(psql_query "$service" "$query")"
  assert_text_eq "$label" "$actual" "$expected"
}

kafka_producer_classpath() {
  local kafka_clients slf4j_api lz4_java snappy_java zstd_jni
  kafka_clients="$(find "$HOME/.m2/repository/org/apache/kafka/kafka-clients" -name 'kafka-clients-*.jar' ! -name '*-test.jar' | sort | tail -1)"
  slf4j_api="$(find "$HOME/.m2/repository/org/slf4j/slf4j-api" -name 'slf4j-api-*.jar' | sort | tail -1)"
  lz4_java="$(find "$HOME/.m2/repository/org/lz4/lz4-java" -name 'lz4-java-*.jar' | sort | tail -1)"
  snappy_java="$(find "$HOME/.m2/repository/org/xerial/snappy/snappy-java" -name 'snappy-java-*.jar' | sort | tail -1)"
  zstd_jni="$(find "$HOME/.m2/repository/com/github/luben/zstd-jni" -name 'zstd-jni-*.jar' | sort | tail -1)"

  if [[ -z "$kafka_clients" || -z "$slf4j_api" || -z "$lz4_java" || -z "$snappy_java" || -z "$zstd_jni" ]]; then
    echo "Kafka producer dependencies were not found in ~/.m2; run with --package first." >&2
    exit 1
  fi

  printf '%s:%s:%s:%s:%s\n' "$kafka_clients" "$slf4j_api" "$lz4_java" "$snappy_java" "$zstd_jni"
}

kafka_producer_container_classpath() {
  kafka_producer_classpath | sed "s#${HOME}/.m2/repository#/m2#g"
}

compile_kafka_replay_producer() {
  local src="/tmp/opex-e2e-kafka-replay/OpexKafkaReplay.java"
  local class_file="/tmp/opex-e2e-kafka-replay/OpexKafkaReplay.class"
  local classpath
  classpath="$(kafka_producer_classpath)"
  mkdir -p /tmp/opex-e2e-kafka-replay
  cat > "$src" <<'JAVA'
import org.apache.kafka.clients.producer.KafkaProducer;
import org.apache.kafka.clients.producer.ProducerConfig;
import org.apache.kafka.clients.producer.ProducerRecord;
import org.apache.kafka.common.serialization.StringSerializer;

import java.nio.charset.StandardCharsets;
import java.util.Properties;

public class OpexKafkaReplay {
    public static void main(String[] args) throws Exception {
        if (args.length != 4) {
            throw new IllegalArgumentException("usage: OpexKafkaReplay <bootstrap> <topic> <typeId> <jsonPayload>");
        }

        Properties props = new Properties();
        props.put(ProducerConfig.BOOTSTRAP_SERVERS_CONFIG, args[0]);
        props.put(ProducerConfig.KEY_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
        props.put(ProducerConfig.VALUE_SERIALIZER_CLASS_CONFIG, StringSerializer.class.getName());
        props.put(ProducerConfig.ACKS_CONFIG, "all");

        try (KafkaProducer<String, String> producer = new KafkaProducer<>(props)) {
            ProducerRecord<String, String> record = new ProducerRecord<>(args[1], null, args[3]);
            record.headers().add("__TypeId__", args[2].getBytes(StandardCharsets.UTF_8));
            producer.send(record).get();
        }
    }
}
JAVA
  javac --release 11 -cp "$classpath" "$src"
}

publish_kafka_json_with_type() {
  local topic="$1"
  local type_id="$2"
  local payload="$3"
  local container_classpath network_name
  compile_kafka_replay_producer
  container_classpath="/work:$(kafka_producer_container_classpath)"
  network_name="${COMPOSE_PROJECT_NAME:-$(basename "$ROOT_DIR")}_default"
  docker -H "$DOCKER_SOCK" run --rm \
    --network "$network_name" \
    -v /tmp/opex-e2e-kafka-replay:/work:ro \
    -v "$HOME/.m2/repository:/m2:ro" \
    eclipse-temurin:11-jre \
    java -cp "$container_classpath" OpexKafkaReplay "kafka-1:29092" "$topic" "$type_id" "$payload"
}

publish_kafka_poison_value() {
  local topic="$1"
  printf '%s\n' '{"not":"a spring kafka typed record"}' | "${COMPOSE[@]}" exec -T kafka-1 env KAFKA_OPTS= kafka-console-producer \
    --broker-list kafka-1:29092 \
    --topic "$topic"
}

replay_first_kafka_record() {
  local topic="$1"
  local line type_id payload
  line="$("${COMPOSE[@]}" exec -T kafka-1 env KAFKA_OPTS= kafka-console-consumer \
    --bootstrap-server kafka-1:29092 \
    --topic "$topic" \
    --from-beginning \
    --timeout-ms 8000 \
    --property print.headers=true \
    --property print.key=true \
    --max-messages 1 | sed -n '/^__TypeId__:/p' | head -n1)"

  if [[ -z "$line" ]]; then
    echo "Could not read a replayable Kafka record from topic $topic" >&2
    exit 1
  fi

  type_id="$(printf '%s\n' "$line" | cut -f1 | sed 's/^__TypeId__://')"
  payload="$(printf '%s\n' "$line" | cut -f3-)"
  if [[ -z "$type_id" || -z "$payload" || "$payload" == "$line" ]]; then
    echo "Could not parse Kafka record from topic $topic" >&2
    printf '%s\n' "$line" >&2
    exit 1
  fi

  publish_kafka_json_with_type "$topic" "$type_id" "$payload"
}

replay_first_kafka_record_by_type() {
  local topic="$1"
  local expected_type_id="$2"
  local line type_id payload
  line="$("${COMPOSE[@]}" exec -T kafka-1 env KAFKA_OPTS= kafka-console-consumer \
    --bootstrap-server kafka-1:29092 \
    --topic "$topic" \
    --from-beginning \
    --timeout-ms 8000 \
    --property print.headers=true \
    --property print.key=true \
    --max-messages 200 | sed -n "/^__TypeId__:${expected_type_id}/p" | head -n1)"

  if [[ -z "$line" ]]; then
    echo "Could not read a replayable Kafka record with type $expected_type_id from topic $topic" >&2
    exit 1
  fi

  type_id="$(printf '%s\n' "$line" | cut -f1 | sed 's/^__TypeId__://')"
  payload="$(printf '%s\n' "$line" | cut -f3-)"
  if [[ "$type_id" != "$expected_type_id" || -z "$payload" || "$payload" == "$line" ]]; then
    echo "Could not parse Kafka record from topic $topic with type $expected_type_id" >&2
    printf '%s\n' "$line" >&2
    exit 1
  fi

  publish_kafka_json_with_type "$topic" "$type_id" "$payload"
}

replay_market_projection_duplicates() {
  publish_kafka_poison_value "richOrder"
  publish_kafka_poison_value "richTrade"
  replay_first_kafka_record "richOrder"
  replay_first_kafka_record "richTrade"
  wait_log "market" "market richOrder poison record redirected to DLT" "richOrder.DLT"
  wait_log "market" "market richTrade poison record redirected to DLT" "richTrade.DLT"
  wait_log "market" "market duplicate richTrade replay after poison record" "Duplicate RichTrade"
  wait_query_eq "eventlog persisted market dead letters" "postgres-eventlog" $'richOrder,market,org.springframework.kafka.support.serializer.DeserializationException,1\nrichTrade,market,org.springframework.kafka.support.serializer.DeserializationException,1' "
    select origin_topic, consumer_group, exception_class_name, count(*)
    from dead_letter_events
    where origin_topic in ('richOrder', 'richTrade')
    group by origin_topic, consumer_group, exception_class_name
    order by origin_topic;
  "
  wait_query_eq "eventlog dead letter total" "postgres-eventlog" "2" "
    select count(*)
    from dead_letter_events;
  "
  sleep 5
}

replay_order_request_duplicate() {
  local since
  since="$(log_since_now)"
  replay_first_kafka_record "orders_ETH_USDT"
  wait_log_since "matching-engine" "matching-engine duplicate order replay ignored" "Duplicate order create command ignored" "$since" 120
  sleep 5
}

replay_cancel_request_duplicate() {
  local since
  since="$(log_since_now)"
  replay_first_kafka_record_by_type "orders_ETH_USDT" "order_request_cancel"
  wait_log_since "matching-engine" "matching-engine duplicate cancel replay ignored" "Duplicate order cancel command ignored" "$since" 120
  sleep 5
}

replay_accountant_event_duplicates() {
  local action_count_before action_count_after
  action_count_before="$(psql_query "postgres-accountant" "select count(*) from fi_actions;")"
  replay_first_kafka_record_by_type "events_ETH_USDT" "co.nilin.opex.matching.engine.core.eventh.events.CancelOrderEvent"
  sleep 5
  action_count_after="$(psql_query "postgres-accountant" "select count(*) from fi_actions;")"
  assert_text_eq "accountant duplicate cancel replay did not create financial actions" "$action_count_after" "$action_count_before"

  action_count_before="$(psql_query "postgres-accountant" "select count(*) from fi_actions;")"
  replay_first_kafka_record_by_type "trades_ETH_USDT" "co.nilin.opex.matching.engine.core.eventh.events.TradeEvent"
  sleep 5
  action_count_after="$(psql_query "postgres-accountant" "select count(*) from fi_actions;")"
  assert_text_eq "accountant duplicate trade replay did not create financial actions" "$action_count_after" "$action_count_before"
  sleep 5
}

wait_query_eq() {
  local label="$1"
  local service="$2"
  local expected="$3"
  local query="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local actual
  until actual="$(psql_query "$service" "$query")" && [[ "$actual" == "$expected" ]]; do
    if (( SECONDS > deadline )); then
      assert_text_eq "$label" "$actual" "$expected"
    fi
    sleep 2
  done
}

wait_active_users_api_matches_orders() {
  local deadline=$((SECONDS + 120))
  local expected body actual
  until expected="$(psql_query "postgres-market" "select count(distinct uuid) from orders where create_date >= now() - interval '24 hours';")" &&
    body="$(curl -fsS "http://127.0.0.1:8094/v1/landing/exchangeInfo?interval=24h")" &&
    actual="$(jq -r '.activeUsers' <<<"$body")" &&
    [[ "$actual" == "$expected" ]]; do
    if (( SECONDS > deadline )); then
      echo "active user API count expected $expected but got $actual" >&2
      printf '%s\n' "$body" >&2
      exit 1
    fi
    sleep 2
  done
}

wait_market_count_apis_match_database() {
  local label="$1"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local expected_active expected_orders expected_eth_orders expected_trades expected_eth_trades
  local active_body orders_body eth_orders_body trades_body eth_trades_body landing_body
  local actual_active actual_orders actual_eth_orders actual_trades actual_eth_trades
  local landing_active landing_orders landing_trades
  until expected_active="$(psql_query "postgres-market" "select count(distinct uuid) from orders where create_date >= now() - interval '24 hours';")" &&
    expected_orders="$(psql_query "postgres-market" "select count(*) from orders where create_date >= now() - interval '24 hours';")" &&
    expected_eth_orders="$(psql_query "postgres-market" "select count(*) from orders where symbol = 'ETH_USDT' and create_date >= now() - interval '24 hours';")" &&
    expected_trades="$(psql_query "postgres-market" "select count(*) from trades where create_date >= now() - interval '24 hours';")" &&
    expected_eth_trades="$(psql_query "postgres-market" "select count(*) from trades where symbol = 'ETH_USDT' and create_date >= now() - interval '24 hours';")" &&
    active_body="$(curl -fsS "http://127.0.0.1:8096/v1/market/active-users?interval=TwentyFourHours")" &&
    orders_body="$(curl -fsS "http://127.0.0.1:8096/v1/market/orders-count?interval=TwentyFourHours")" &&
    eth_orders_body="$(curl -fsS "http://127.0.0.1:8096/v1/market/orders-count?interval=TwentyFourHours&symbol=ETH_USDT")" &&
    trades_body="$(curl -fsS "http://127.0.0.1:8096/v1/market/trades-count?interval=TwentyFourHours")" &&
    eth_trades_body="$(curl -fsS "http://127.0.0.1:8096/v1/market/trades-count?interval=TwentyFourHours&symbol=ETH_USDT")" &&
    landing_body="$(curl -fsS "http://127.0.0.1:8094/v1/landing/exchangeInfo?interval=24h")" &&
    actual_active="$(jq -r '.value' <<<"$active_body")" &&
    actual_orders="$(jq -r '.value' <<<"$orders_body")" &&
    actual_eth_orders="$(jq -r '.value' <<<"$eth_orders_body")" &&
    actual_trades="$(jq -r '.value' <<<"$trades_body")" &&
    actual_eth_trades="$(jq -r '.value' <<<"$eth_trades_body")" &&
    landing_active="$(jq -r '.activeUsers' <<<"$landing_body")" &&
    landing_orders="$(jq -r '.totalOrders' <<<"$landing_body")" &&
    landing_trades="$(jq -r '.totalTrades' <<<"$landing_body")" &&
    [[ "$actual_active" == "$expected_active" ]] &&
    [[ "$actual_orders" == "$expected_orders" ]] &&
    [[ "$actual_eth_orders" == "$expected_eth_orders" ]] &&
    [[ "$actual_trades" == "$expected_trades" ]] &&
    [[ "$actual_eth_trades" == "$expected_eth_trades" ]] &&
    [[ "$landing_active" == "$expected_active" ]] &&
    [[ "$landing_orders" == "$expected_orders" ]] &&
    [[ "$landing_trades" == "$expected_trades" ]]; do
    if (( SECONDS > deadline )); then
      echo "$label count API mismatch" >&2
      printf 'expected active/orders/ethOrders/trades/ethTrades: %s/%s/%s/%s/%s\n' \
        "$expected_active" "$expected_orders" "$expected_eth_orders" "$expected_trades" "$expected_eth_trades" >&2
      printf 'actual direct active/orders/ethOrders/trades/ethTrades: %s/%s/%s/%s/%s\n' \
        "$actual_active" "$actual_orders" "$actual_eth_orders" "$actual_trades" "$actual_eth_trades" >&2
      printf 'actual landing active/orders/trades: %s/%s/%s\n' "$landing_active" "$landing_orders" "$landing_trades" >&2
      printf 'active body: %s\norders body: %s\neth orders body: %s\ntrades body: %s\neth trades body: %s\nlanding body: %s\n' \
        "$active_body" "$orders_body" "$eth_orders_body" "$trades_body" "$eth_trades_body" "$landing_body" >&2
      "${COMPOSE[@]}" logs --tail=200 market api >&2 || true
      exit 1
    fi
    sleep 2
  done

  jq -nc \
    --argjson active "$actual_active" \
    --argjson orders "$actual_orders" \
    --argjson ethOrders "$actual_eth_orders" \
    --argjson trades "$actual_trades" \
    --argjson ethTrades "$actual_eth_trades" \
    --argjson landingActive "$landing_active" \
    --argjson landingOrders "$landing_orders" \
    --argjson landingTrades "$landing_trades" \
    '{activeUsers:$active,totalOrders:$orders,ethOrders:$ethOrders,totalTrades:$trades,ethTrades:$ethTrades,landing:{activeUsers:$landingActive,totalOrders:$landingOrders,totalTrades:$landingTrades}}' \
    >/tmp/opex-e2e-market-counts.json
}

wait_wallet_type_balance() {
  local label="$1"
  local owner="$2"
  local wallet_type="$3"
  local currency="$4"
  local expected="$5"
  wait_query_eq "$label" "postgres-wallet" "$expected" "
    select to_char(coalesce(sum(w.balance), 0), 'FM9999999990.00000000')
    from wallet w
    join wallet_owner wo on wo.id = w.owner
    where wo.uuid = '$owner'
      and w.wallet_type = '$wallet_type'
      and w.currency = '$currency';
  "
}

try_wallet_balance() {
  local owner="$1"
  local symbol="$2"
  local expected_balance="$3"
  local output status body actual_balance
  output="$(curl_json GET "http://127.0.0.1:8091/v1/owner/${owner}/wallets/${symbol}")"
  status="$(printf '%s\n' "$output" | head -n1)"
  body="$(printf '%s\n' "$output" | tail -n +2)"
  if [[ ! "$status" =~ ^2 ]]; then
    return 1
  fi
  actual_balance="$(printf '%s\n' "$body" | json_number balance)"
  awk -v actual="$actual_balance" -v expected="$expected_balance" '
    BEGIN {
      diff = actual - expected
      if (diff < 0) diff = -diff
      exit(diff <= 0.000001 ? 0 : 1)
    }
  '
}

assert_wallet_balance() {
  local label="$1"
  local owner="$2"
  local symbol="$3"
  local expected_balance="$4"
  local body actual_balance
  body="$(expect_2xx "$label" "$(curl_json GET "http://127.0.0.1:8091/v1/owner/${owner}/wallets/${symbol}")")"
  actual_balance="$(printf '%s\n' "$body" | json_number balance)"
  assert_number_eq "$label balance" "$actual_balance" "$expected_balance"
}

wait_withdraw_status() {
  local label="$1"
  local withdraw_id="$2"
  local expected_status="$3"
  local output_file="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(expect_2xx "$label" "$(curl_json GET "http://127.0.0.1:8091/admin/withdraw/${withdraw_id}")")" &&
    printf '%s\n' "$body" | jq -e --arg status "$expected_status" '.status == $status' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for withdraw $withdraw_id status=$expected_status" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_user_open_order() {
  local owner="$1"
  local symbol="$2"
  local price="$3"
  local quantity="$4"
  local output_file="$5"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/user/${owner}/orders/${symbol}/open?limit=20")" &&
    printf '%s\n' "$body" | jq -e --argjson price "$price" --argjson quantity "$quantity" '
      [.[] | select(.price == $price and .quantity == $quantity and (.status == "NEW" or .status == "PARTIALLY_FILLED"))] | length == 1
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for open order owner=$owner symbol=$symbol price=$price quantity=$quantity" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_no_user_open_orders() {
  local owner="$1"
  local symbol="$2"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/user/${owner}/orders/${symbol}/open?limit=20")" &&
    printf '%s\n' "$body" | jq -e 'length == 0' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for no open orders owner=$owner symbol=$symbol" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
}

assert_no_user_orders() {
  local owner="$1"
  local symbol="$2"
  local body
  body="$(curl -fsS -X POST -H "Content-Type: application/json" -d "{\"symbol\":\"${symbol}\",\"startTime\":null,\"endTime\":null,\"limit\":20}" "http://127.0.0.1:8096/v1/user/${owner}/orders")"
  if ! printf '%s\n' "$body" | jq -e 'length == 0' >/dev/null; then
    echo "Expected no market orders for owner=$owner symbol=$symbol" >&2
    echo "$body" >&2
    exit 1
  fi
}

assert_no_user_order_by_price() {
  local owner="$1"
  local symbol="$2"
  local price="$3"
  local quantity="$4"
  local body
  body="$(curl -fsS -X POST -H "Content-Type: application/json" -d "{\"symbol\":\"${symbol}\",\"startTime\":null,\"endTime\":null,\"limit\":20}" "http://127.0.0.1:8096/v1/user/${owner}/orders")"
  if ! printf '%s\n' "$body" | jq -e --argjson price "$price" --argjson quantity "$quantity" '
    [.[] | select(.price == $price and .quantity == $quantity)] | length == 0
  ' >/dev/null; then
    echo "Expected no market order for owner=$owner symbol=$symbol price=$price quantity=$quantity" >&2
    echo "$body" >&2
    exit 1
  fi
}

wait_user_order_status_by_price() {
  local owner="$1"
  local symbol="$2"
  local price="$3"
  local quantity="$4"
  local expected_status="$5"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS -X POST -H "Content-Type: application/json" -d "{\"symbol\":\"${symbol}\",\"startTime\":null,\"endTime\":null,\"limit\":20}" "http://127.0.0.1:8096/v1/user/${owner}/orders")" &&
    printf '%s\n' "$body" | jq -e --argjson price "$price" --argjson quantity "$quantity" --arg expected_status "$expected_status" '
      [.[] | select(.price == $price and .quantity == $quantity and .status == $expected_status)] | length >= 1
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for owner=$owner symbol=$symbol price=$price quantity=$quantity status=$expected_status" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_user_order_projection_by_price() {
  local owner="$1"
  local symbol="$2"
  local price="$3"
  local quantity="$4"
  local expected_status="$5"
  local expected_executed_quantity="$6"
  local expected_accumulative_quote_qty="$7"
  local output_file="$8"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS -X POST -H "Content-Type: application/json" -d "{\"symbol\":\"${symbol}\",\"startTime\":null,\"endTime\":null,\"limit\":20}" "http://127.0.0.1:8096/v1/user/${owner}/orders")" &&
    printf '%s\n' "$body" | jq -e \
      --argjson price "$price" \
      --argjson quantity "$quantity" \
      --arg expected_status "$expected_status" \
      --argjson expected_executed_quantity "$expected_executed_quantity" \
      --argjson expected_accumulative_quote_qty "$expected_accumulative_quote_qty" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        [
          .[] |
          select(
            .price == $price and
            .quantity == $quantity and
            .status == $expected_status and
            nearly_equal(.executedQuantity; $expected_executed_quantity) and
            nearly_equal(.accumulativeQuoteQty; $expected_accumulative_quote_qty)
          )
        ] | length >= 1
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for owner=$owner symbol=$symbol price=$price quantity=$quantity status=$expected_status executedQuantity=$expected_executed_quantity accumulativeQuoteQty=$expected_accumulative_quote_qty" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_order_book_level() {
  local symbol="$1"
  local direction="$2"
  local price="$3"
  local quantity="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/market/${symbol}/order-book?direction=${direction}&limit=20")" &&
    printf '%s\n' "$body" | jq -e --argjson price "$price" --argjson quantity "$quantity" '
      [.[] | select(.price == $price and .quantity == $quantity)] | length >= 1
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for order book level symbol=$symbol direction=$direction price=$price quantity=$quantity" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 market >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_order_book_empty() {
  local symbol="$1"
  local direction="$2"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/market/${symbol}/order-book?direction=${direction}&limit=20")" &&
    printf '%s\n' "$body" | jq -e 'length == 0' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for empty order book symbol=$symbol direction=$direction" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_order_book_empty_at_price() {
  local symbol="$1"
  local direction="$2"
  local price="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/market/${symbol}/order-book?direction=${direction}&limit=20")" &&
    printf '%s\n' "$body" | jq -e --argjson price "$price" '
      [.[] | select(.price == $price)] | length == 0
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for empty order book price symbol=$symbol direction=$direction price=$price" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_matching_snapshot_order() {
  local label="$1"
  local owner="$2"
  local symbol="$3"
  local direction="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local snapshot
  until snapshot="$("${COMPOSE[@]}" exec -T redis redis-cli --raw HGET OrderbookSnapshots "$symbol" 2>/dev/null)" &&
    jq -e --arg owner "$owner" --arg symbol "$symbol" --arg direction "$direction" '
      (.pair.leftSideName + "_" + .pair.rightSideName) == $symbol and
      ((.processedOrderOuids // []) | length) > 0 and
      ([.orders[]? | select(.uuid == $owner and .direction == $direction)] | length) >= 1
    ' <<<"$snapshot" >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for matching snapshot order: $label owner=$owner symbol=$symbol direction=$direction" >&2
      printf '%s\n' "$snapshot" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-engine redis >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_matching_snapshot_no_order() {
  local label="$1"
  local owner="$2"
  local symbol="$3"
  local direction="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local snapshot
  until snapshot="$("${COMPOSE[@]}" exec -T redis redis-cli --raw HGET OrderbookSnapshots "$symbol" 2>/dev/null)" &&
    jq -e --arg owner "$owner" --arg symbol "$symbol" --arg direction "$direction" '
      (.pair.leftSideName + "_" + .pair.rightSideName) == $symbol and
      ([.orders[]? | select(.uuid == $owner and .direction == $direction)] | length) == 0
    ' <<<"$snapshot" >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for matching snapshot order removal: $label owner=$owner symbol=$symbol direction=$direction" >&2
      printf '%s\n' "$snapshot" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-engine redis >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_best_prices() {
  local symbol="$1"
  local bid_price="$2"
  local ask_price="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/market/best-prices?symbols=${symbol}")" &&
    printf '%s\n' "$body" | jq -e --arg symbol "$symbol" --argjson bid_price "$bid_price" --argjson ask_price "$ask_price" '
      [.[] | select(.symbol == $symbol and .bidPrice == $bid_price and .askPrice == $ask_price)] | length == 1
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for best prices symbol=$symbol bid=$bid_price ask=$ask_price" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 market >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_recent_trades_distribution() {
  local symbol="$1"
  local output_file="$2"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/market/${symbol}/recent-trades?limit=20")" &&
    printf '%s\n' "$body" | jq -e '
      length == 23 and
      ([.[] | select(.price == 90) | .quantity] | add == 0.1) and
      ([.[] | select(.price == 100) | .quantity] | add == 1.4) and
      ([.[] | select(.price == 101) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 102) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 103) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 111) | .quantity] | add == 0.5) and
      ([.[] | select(.price == 112) | .quantity] | add == 0.4) and
      ([.[] | select(.price == 113) | .quantity] | add == 0.3) and
      ([.[] | select(.price == 114) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 115) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 116) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 117) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 120) | .quantity] | add == 0.4) and
      ([.[] | select(.price == 125) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 130) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 140) | .quantity] | add == 0.4) and
      ([.[] | select(.price == 150) | .quantity] | add == 0.1) and
      ([.[] | select(.price == 169) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 172) | .quantity] | add == 0.2) and
      ([.[] | select(.price == 173) | .quantity] | add == 0.2)
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for recent trades distribution symbol=$symbol" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 market matching-engine accountant >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_recent_trade_level() {
  local symbol="$1"
  local price="$2"
  local quantity="$3"
  local output_file="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/market/${symbol}/recent-trades?limit=20")" &&
    printf '%s\n' "$body" | jq -e --argjson price "$price" --argjson quantity "$quantity" '
      [.[] | select(.price == $price and .quantity == $quantity)] | length >= 1
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for recent trade symbol=$symbol price=$price quantity=$quantity" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 market matching-engine accountant >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_exchange_info_symbol() {
  local symbol="$1"
  local base="$2"
  local quote="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8094/v3/exchangeInfo?symbol=${symbol}")" &&
    printf '%s\n' "$body" | jq -e --arg symbol "$symbol" --arg base "$base" --arg quote "$quote" '
      [.symbols[] | select(.symbol == $symbol and .status == "TRADING" and .baseAsset == $base and .quoteAsset == $quote)] | length == 1
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance exchangeInfo symbol=$symbol" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api accountant >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_binance_depth_level() {
  local symbol="$1"
  local side="$2"
  local price="$3"
  local quantity="$4"
  local output_file="$5"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local field body
  case "$side" in
    BID) field="bids" ;;
    ASK) field="asks" ;;
    *) echo "Invalid Binance depth side: $side" >&2; exit 2 ;;
  esac
  until body="$(curl -fsS "http://127.0.0.1:8094/v3/depth?symbol=${symbol}&limit=20")" &&
    printf '%s\n' "$body" | jq -e --arg field "$field" --argjson price "$price" --argjson quantity "$quantity" '
      [.[$field][] | select(.[0] == $price and .[1] == $quantity)] | length >= 1
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance depth symbol=$symbol side=$side price=$price quantity=$quantity" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_recent_trade_level() {
  local symbol="$1"
  local price="$2"
  local quantity="$3"
  local quote_quantity="$4"
  local output_file="$5"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8094/v3/trades?symbol=${symbol}&limit=20")" &&
    printf '%s\n' "$body" | jq -e --argjson price "$price" --argjson quantity "$quantity" --argjson quote_quantity "$quote_quantity" '
      [.[] | select(.price == $price and .qty == $quantity and .quoteQty == $quote_quantity)] | length >= 1
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance recent trade symbol=$symbol price=$price quantity=$quantity quote=$quote_quantity" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_price_ticker() {
  local symbol="$1"
  local price="$2"
  local output_file="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8094/v3/ticker/price?symbol=${symbol}")" &&
    printf '%s\n' "$body" | jq -e --arg symbol "$symbol" --argjson price "$price" '
      length == 1 and .[0].symbol == $symbol and (.[0].price | tonumber) == $price
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance price ticker symbol=$symbol price=$price" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_24h_ticker() {
  local symbol="$1"
  local price="$2"
  local quantity="$3"
  local output_file="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8094/v3/ticker/24h?symbol=${symbol}")" &&
    printf '%s\n' "$body" | jq -e --arg symbol "$symbol" --argjson price "$price" --argjson quantity "$quantity" '
      length == 1 and
      .[0].symbol == $symbol and
      .[0].base == "ETH" and
      .[0].quote == "USDT" and
      (.[0].priceChange | tonumber) == 0 and
      (.[0].priceChangePercent | tonumber) == 0 and
      (.[0].weightedAvgPrice | tonumber) == $price and
      (.[0].lastPrice | tonumber) == $price and
      (.[0].lastQty | tonumber) == $quantity and
      (.[0].openPrice | tonumber) == $price and
      (.[0].highPrice | tonumber) == $price and
      (.[0].lowPrice | tonumber) == $price and
      (.[0].volume | tonumber) == $quantity and
      .[0].count == 1 and
      .[0].openTime > 0 and
      .[0].closeTime > 0
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance 24h ticker symbol=$symbol price=$price quantity=$quantity" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_klines_1m_candle() {
  local symbol="$1"
  local open="$2"
  local high="$3"
  local low="$4"
  local close="$5"
  local volume="$6"
  local quote_volume="$7"
  local trades="$8"
  local taker_buy_base_volume="$9"
  local taker_buy_quote_volume="${10}"
  local output_file="${11}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8094/v3/klines?symbol=${symbol}&interval=1m&limit=1")" &&
    printf '%s\n' "$body" | jq -e \
      --argjson open "$open" \
      --argjson high "$high" \
      --argjson low "$low" \
      --argjson close "$close" \
      --argjson volume "$volume" \
      --argjson quote_volume "$quote_volume" \
      --argjson trades "$trades" \
      --argjson taker_buy_base_volume "$taker_buy_base_volume" \
      --argjson taker_buy_quote_volume "$taker_buy_quote_volume" '
      length == 1 and
      (.[0][1] | tonumber) == $open and
      (.[0][2] | tonumber) == $high and
      (.[0][3] | tonumber) == $low and
      (.[0][4] | tonumber) == $close and
      (.[0][5] | tonumber) == $volume and
      (.[0][7] | tonumber) == $quote_volume and
      .[0][8] == $trades and
      (.[0][9] | tonumber) == $taker_buy_base_volume and
      (.[0][10] | tonumber) == $taker_buy_quote_volume and
      .[0][0] > 0 and
      .[0][6] > 0
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance klines symbol=$symbol" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_latest_kline_matches_market_projection() {
  local symbol="$1"
  local internal_symbol="$2"
  local output_file="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body end_time_body expected open high low close volume quote_volume trades taker_buy_base_volume taker_buy_quote_volume close_time_ms
  until expected="$(psql_query "postgres-market" "
    with latest_bucket as (
      select date_trunc('minute', max(create_date)) as open_time
      from trades
      where symbol = '$internal_symbol'
    ), bucket_trades as (
      select t.id,
             t.create_date,
             t.matched_price,
             t.matched_quantity,
             taker_order.side as taker_side
      from trades t
      join latest_bucket b
        on t.create_date >= b.open_time
       and t.create_date < b.open_time + interval '1 minute'
      left join orders taker_order
        on t.taker_ouid = taker_order.ouid
      where t.symbol = '$internal_symbol'
    )
    select to_char((array_agg(matched_price order by create_date, id))[1], 'FM9999999990.00000000'),
           to_char(max(matched_price), 'FM9999999990.00000000'),
           to_char(min(matched_price), 'FM9999999990.00000000'),
           to_char((array_agg(matched_price order by create_date desc, id desc))[1], 'FM9999999990.00000000'),
           to_char(sum(matched_quantity), 'FM9999999990.00000000'),
           to_char(sum(matched_price * matched_quantity), 'FM9999999990.00000000'),
           count(id),
           to_char(sum(case when taker_side = 'BID' then matched_quantity else 0 end), 'FM9999999990.00000000'),
           to_char(sum(case when taker_side = 'BID' then matched_price * matched_quantity else 0 end), 'FM9999999990.00000000'),
           (select floor(extract(epoch from open_time + interval '1 minute') * 1000)::bigint - 1 from latest_bucket)
    from bucket_trades;
  ")" &&
    [[ -n "$expected" ]] &&
    IFS=',' read -r open high low close volume quote_volume trades taker_buy_base_volume taker_buy_quote_volume close_time_ms <<< "$expected" &&
    body="$(curl -fsS "http://127.0.0.1:8094/v3/klines?symbol=${symbol}&interval=1m&limit=1")" &&
    printf '%s\n' "$body" | jq -e \
      --argjson open "$open" \
      --argjson high "$high" \
      --argjson low "$low" \
      --argjson close "$close" \
      --argjson volume "$volume" \
      --argjson quote_volume "$quote_volume" \
      --argjson trades "$trades" \
      --argjson taker_buy_base_volume "$taker_buy_base_volume" \
      --argjson taker_buy_quote_volume "$taker_buy_quote_volume" \
      --argjson close_time_ms "$close_time_ms" '
      length == 1 and
      (.[0][1] | tonumber) == $open and
      (.[0][2] | tonumber) == $high and
      (.[0][3] | tonumber) == $low and
      (.[0][4] | tonumber) == $close and
      (.[0][5] | tonumber) == $volume and
      .[0][6] == $close_time_ms and
      (.[0][7] | tonumber) == $quote_volume and
      .[0][8] == $trades and
      (.[0][9] | tonumber) == $taker_buy_base_volume and
      (.[0][10] | tonumber) == $taker_buy_quote_volume and
      .[0][0] > 0 and
      .[0][6] > 0
    ' >/dev/null &&
    end_time_body="$(curl -fsS "http://127.0.0.1:8094/v3/klines?symbol=${symbol}&interval=1m&endTime=${close_time_ms}&limit=1")" &&
    printf '%s\n' "$end_time_body" | jq -e \
      --argjson open "$open" \
      --argjson high "$high" \
      --argjson low "$low" \
      --argjson close "$close" \
      --argjson volume "$volume" \
      --argjson quote_volume "$quote_volume" \
      --argjson trades "$trades" \
      --argjson taker_buy_base_volume "$taker_buy_base_volume" \
      --argjson taker_buy_quote_volume "$taker_buy_quote_volume" \
      --argjson close_time_ms "$close_time_ms" '
      length == 1 and
      (.[0][1] | tonumber) == $open and
      (.[0][2] | tonumber) == $high and
      (.[0][3] | tonumber) == $low and
      (.[0][4] | tonumber) == $close and
      (.[0][5] | tonumber) == $volume and
      .[0][6] == $close_time_ms and
      (.[0][7] | tonumber) == $quote_volume and
      .[0][8] == $trades and
      (.[0][9] | tonumber) == $taker_buy_base_volume and
      (.[0][10] | tonumber) == $taker_buy_quote_volume and
      .[0][0] > 0 and
      .[0][6] > 0
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance latest kline to match market projection symbol=$symbol" >&2
      echo "expected: $expected" >&2
      echo "actual: $body" >&2
      echo "actual endTime-only: $end_time_body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

binance_private_get() {
  local owner="$1"
  local path="$2"
  local query="${3:-}"
  local timestamp
  timestamp=$(( $(date +%s) * 1000 ))

  if [[ -n "$query" ]]; then
    curl -fsS -H "X-Opex-User: $owner" "http://127.0.0.1:8094${path}?${query}&timestamp=${timestamp}&recvWindow=60000"
  else
    curl -fsS -H "X-Opex-User: $owner" "http://127.0.0.1:8094${path}?timestamp=${timestamp}&recvWindow=60000"
  fi
}

binance_private_get_status() {
  local owner="$1"
  local path="$2"
  local query="${3:-}"
  local timestamp
  timestamp=$(( $(date +%s) * 1000 ))

  if [[ -n "$query" ]]; then
    query="${query}&timestamp=${timestamp}&recvWindow=60000"
  else
    query="timestamp=${timestamp}&recvWindow=60000"
  fi

  local response_file
  response_file="$(mktemp)"
  local status
  status="$(curl -sS -X GET \
    -H "X-Opex-User: $owner" \
    -w "%{http_code}" \
    -o "$response_file" \
    "http://127.0.0.1:8094${path}?${query}")"
  printf '%s\n' "$status"
  cat "$response_file"
  rm -f "$response_file"
}

binance_private_get_raw_status() {
  local owner="$1"
  local path="$2"
  local query="${3:-}"
  local url="http://127.0.0.1:8094${path}"
  if [[ -n "$query" ]]; then
    url="${url}?${query}"
  fi

  local response_file
  response_file="$(mktemp)"
  local status
  status="$(curl -sS -X GET \
    -H "X-Opex-User: $owner" \
    -w "%{http_code}" \
    -o "$response_file" \
    "$url")"
  printf '%s\n' "$status"
  cat "$response_file"
  rm -f "$response_file"
}

binance_private_post() {
  local owner="$1"
  local path="$2"
  local form="${3:-}"
  local timestamp
  timestamp=$(( $(date +%s) * 1000 ))

  if [[ -n "$form" ]]; then
    form="${form}&timestamp=${timestamp}&recvWindow=60000"
  else
    form="timestamp=${timestamp}&recvWindow=60000"
  fi

  local response_file
  response_file="$(mktemp)"
  local status
  status="$(curl -sS -X POST \
    -H "X-Opex-User: $owner" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -w "%{http_code}" \
    -o "$response_file" \
    -d "$form" \
    "http://127.0.0.1:8094${path}")"
  printf '%s\n' "$status"
  cat "$response_file"
  rm -f "$response_file"
}

binance_private_post_json() {
  local owner="$1"
  local path="$2"
  local body="${3:-{}}"
  local timestamp
  timestamp=$(( $(date +%s) * 1000 ))

  local signed_body
  signed_body="$(jq -c --argjson timestamp "$timestamp" '. + {timestamp: $timestamp, recvWindow: 60000}' <<<"$body")"

  local response_file
  response_file="$(mktemp)"
  local status
  status="$(curl -sS -X POST \
    -H "X-Opex-User: $owner" \
    -H "Content-Type: application/json" \
    -w "%{http_code}" \
    -o "$response_file" \
    -d "$signed_body" \
    "http://127.0.0.1:8094${path}")"
  printf '%s\n' "$status"
  cat "$response_file"
  rm -f "$response_file"
}

binance_private_delete() {
  local owner="$1"
  local path="$2"
  local form="${3:-}"
  local timestamp
  timestamp=$(( $(date +%s) * 1000 ))

  if [[ -n "$form" ]]; then
    form="${form}&timestamp=${timestamp}&recvWindow=60000"
  else
    form="timestamp=${timestamp}&recvWindow=60000"
  fi

  local response_file
  response_file="$(mktemp)"
  local status
  status="$(curl -sS -X DELETE \
    -H "X-Opex-User: $owner" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -w "%{http_code}" \
    -o "$response_file" \
    -d "$form" \
    "http://127.0.0.1:8094${path}")"
  printf '%s\n' "$status"
  cat "$response_file"
  rm -f "$response_file"
}

deposit_via_bc_gateway() {
  local owner="$1"
  local currency="$2"
  local amount="$3"
  local tx_hash="$4"
  local token_address="$5"
  local raw_amount="$6"
  local address="0xe2e${tx_hash//[^[:alnum:]]/}"
  address="${address:0:42}"
  local csv_file
  csv_file="$(mktemp)"
  printf '%s,,ethereum\n' "$address" > "$csv_file"

  curl -fsS -X PUT -F "file=@${csv_file}" "http://127.0.0.1:8095/v1/address" >/dev/null
  rm -f "$csv_file"

  local assigned_body
  assigned_body="$(curl -fsS -X POST \
    -H "Content-Type: application/json" \
    -d "{\"uuid\":\"${owner}\",\"currency\":\"${currency}\",\"chain\":\"test-ethereum\"}" \
    "http://127.0.0.1:8095/v1/address/assign")"
  local assigned_address
  assigned_address="$(jq -r --arg fallback "$address" '.addresses[0].address // $fallback' <<<"$assigned_body")"

  local transfer_body
  transfer_body="$(jq -n \
    --arg txHash "$tx_hash" \
    --arg address "$assigned_address" \
    --arg tokenAddress "$token_address" \
    --argjson amount "$raw_amount" \
    '[{
      txHash: $txHash,
      blockNumber: 1,
      receiver: {address: $address, memo: null},
      isTokenTransfer: true,
      amount: $amount,
      chain: "test-ethereum",
      tokenAddress: $tokenAddress
    }]')"
  expect_2xx "bc-gateway wallet-sync deposit" "$(curl_json PUT "http://127.0.0.1:8095/wallet-sync/test-ethereum" "$transfer_body")" >/dev/null
  assert_wallet_balance "bc-gateway ${currency} deposit" "$owner" "$currency" "$amount"
}

wait_binance_deposit_history() {
  local owner="$1"
  local coin="$2"
  local tx_hash="$3"
  local amount="$4"
  local output_file="$5"
  local body
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until body="$(binance_private_get "$owner" "/v1/capital/deposit/hisrec" "coin=${coin}&status=1&startTime=1&endTime=4102444800000&limit=20&offset=0")" &&
    printf '%s\n' "$body" | jq -e \
      --arg coin "$coin" \
      --arg txHash "$tx_hash" \
      --arg network "test-ethereum" \
      --argjson amount "$amount" '
      any(.[];
        .coin == $coin
        and .txId == $txHash
        and .network == $network
        and .status == 1
        and .transferType == 1
        and .unlockConfirm == "1/1"
        and .confirmTimes == "1/1"
        and ((.amount | tonumber) == $amount)
        and (.address | type == "string" and length > 0)
        and (.insertTime | type == "number")
        and (.time | type == "number")
      )
    ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance deposit history coin=$coin txHash=$tx_hash" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api bc-gateway wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

assert_binance_withdraw_history_body() {
  local body="$1"
  local coin="$2"
  local cancel_id="$3"
  local accept_id="$4"
  local duplicate_ref_id="$5"
  local reject_id="$6"
  local chain_ref="$7"
  printf '%s\n' "$body" | jq -e \
    --arg coin "$coin" \
    --arg chainRef "$chain_ref" \
    --argjson cancelId "$cancel_id" \
    --argjson acceptId "$accept_id" \
    --argjson duplicateRefId "$duplicate_ref_id" \
    --argjson rejectId "$reject_id" '
    def by_id($id): .id == ($id | tostring);
    length == 4
    and all(.[]; .coin == $coin and .network == "test-ethereum" and .transferType == 1 and .confirmNo == 3 and (.time | type == "number"))
    and any(.[]; by_id($cancelId) and .status == -1 and .address == "0xwithdrawcancel" and ((.amount | tonumber) == 2.9) and .transactionFee == "0.1")
    and any(.[]; by_id($acceptId) and .status == 1 and .address == "0xwithdrawaccept" and .txId == $chainRef and ((.amount | tonumber) == 3.9) and .transactionFee == "0.1")
    and any(.[]; by_id($duplicateRefId) and .status == 2 and .address == "0xwithdrawduplicateref" and ((.amount | tonumber) == 1.0) and .transactionFee == "0.1")
    and any(.[]; by_id($rejectId) and .status == 2 and .address == "0xwithdrawreject" and ((.amount | tonumber) == 1.9) and .transactionFee == "0.1")
  ' >/dev/null
}

wait_binance_withdraw_history() {
  local owner="$1"
  local coin="$2"
  local cancel_id="$3"
  local accept_id="$4"
  local duplicate_ref_id="$5"
  local reject_id="$6"
  local chain_ref="$7"
  local output_file="$8"
  local body
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until body="$(binance_private_get "$owner" "/v1/capital/withdraw/history" "coin=${coin}&limit=20&offset=0&startTime=1&endTime=4102444800000")" &&
    assert_binance_withdraw_history_body "$body" "$coin" "$cancel_id" "$accept_id" "$duplicate_ref_id" "$reject_id" "$chain_ref"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance withdraw history coin=$coin owner=$owner" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_withdraw_history_v2() {
  local owner="$1"
  local coin="$2"
  local cancel_id="$3"
  local accept_id="$4"
  local duplicate_ref_id="$5"
  local reject_id="$6"
  local chain_ref="$7"
  local output_file="$8"
  local request
  request="$(jq -n --arg coin "$coin" '{coin: $coin, limit: 20, offset: 0, startTime: 1, endTime: 4102444800000}')"
  local body
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until body="$(expect_2xx "Binance withdraw history v2" "$(binance_private_post_json "$owner" "/v2/capital/withdraw/history" "$request")")" &&
    assert_binance_withdraw_history_body "$body" "$coin" "$cancel_id" "$accept_id" "$duplicate_ref_id" "$reject_id" "$chain_ref"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance withdraw history v2 coin=$coin owner=$owner" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

assert_binance_withdraw_history_status_filter_body() {
  local body="$1"
  local expected_status="$2"
  local expected_count="$3"
  local expected_id="$4"
  printf '%s\n' "$body" | jq -e \
    --argjson expectedStatus "$expected_status" \
    --argjson expectedCount "$expected_count" \
    --arg expectedId "$expected_id" '
    length == $expectedCount
    and all(.[]; .status == $expectedStatus)
    and any(.[]; .id == $expectedId)
  ' >/dev/null
}

wait_binance_withdraw_history_status_filter() {
  local owner="$1"
  local coin="$2"
  local status="$3"
  local expected_count="$4"
  local expected_id="$5"
  local output_file="$6"
  local body
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until body="$(binance_private_get "$owner" "/v1/capital/withdraw/history" "coin=${coin}&status=${status}&limit=20&offset=0&startTime=1&endTime=4102444800000")" &&
    assert_binance_withdraw_history_status_filter_body "$body" "$status" "$expected_count" "$expected_id"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance withdraw history status filter coin=$coin owner=$owner status=$status" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_withdraw_history_status_filter_v2() {
  local owner="$1"
  local coin="$2"
  local status="$3"
  local expected_count="$4"
  local expected_id="$5"
  local output_file="$6"
  local request
  request="$(jq -n --arg coin "$coin" --argjson status "$status" '{coin: $coin, status: $status, limit: 20, offset: 0, startTime: 1, endTime: 4102444800000}')"
  local body
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until body="$(expect_2xx "Binance withdraw history v2 status filter" "$(binance_private_post_json "$owner" "/v2/capital/withdraw/history" "$request")")" &&
    assert_binance_withdraw_history_status_filter_body "$body" "$status" "$expected_count" "$expected_id"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance withdraw history v2 status filter coin=$coin owner=$owner status=$status" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_account_balance() {
  local owner="$1"
  local asset="$2"
  local expected_free="$3"
  local expected_locked="$4"
  local output_file="$5"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/account")" &&
    printf '%s\n' "$body" | jq -e \
      --arg asset "$asset" \
      --argjson expected_free "$expected_free" \
      --argjson expected_locked "$expected_locked" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        .accountType == "SPOT" and
        .canTrade == true and
        .canWithdraw == true and
        .canDeposit == true and
        ([.balances[] | select(.asset == $asset and nearly_equal(.free; $expected_free) and nearly_equal(.locked; $expected_locked))] | length == 1)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance account balance owner=$owner asset=$asset free=$expected_free locked=$expected_locked" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api wallet accountant >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_user_asset_balance() {
  local owner="$1"
  local asset="$2"
  local expected_free="$3"
  local expected_locked="$4"
  local expected_withdrawing="$5"
  local output_file="$6"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v1/asset/getUserAsset" "symbol=${asset}")" &&
    printf '%s\n' "$body" | jq -e \
      --arg asset "$asset" \
      --argjson expected_free "$expected_free" \
      --argjson expected_locked "$expected_locked" \
      --argjson expected_withdrawing "$expected_withdrawing" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        length == 1 and
        .[0].asset == $asset and
        nearly_equal((.[0].free | tonumber); $expected_free) and
        nearly_equal((.[0].locked | tonumber); $expected_locked) and
        nearly_equal((.[0].withdrawing | tonumber); $expected_withdrawing)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance user asset balance owner=$owner asset=$asset free=$expected_free locked=$expected_locked withdrawing=$expected_withdrawing" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api wallet accountant >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_user_asset_valuation() {
  local owner="$1"
  local asset="$2"
  local quote_asset="$3"
  local expected_free="$4"
  local expected_locked="$5"
  local expected_withdrawing="$6"
  local expected_valuation="$7"
  local output_file="$8"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v1/asset/getUserAsset" "symbol=${asset}&quoteAsset=${quote_asset}&calculateEvaluation=true")" &&
    printf '%s\n' "$body" | jq -e \
      --arg asset "$asset" \
      --argjson expected_free "$expected_free" \
      --argjson expected_locked "$expected_locked" \
      --argjson expected_withdrawing "$expected_withdrawing" \
      --argjson expected_valuation "$expected_valuation" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        length == 1 and
        .[0].asset == $asset and
        nearly_equal((.[0].free | tonumber); $expected_free) and
        nearly_equal((.[0].locked | tonumber); $expected_locked) and
        nearly_equal((.[0].withdrawing | tonumber); $expected_withdrawing) and
        nearly_equal((.[0].valuation | tonumber); $expected_valuation)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance user asset valuation owner=$owner asset=$asset quote=$quote_asset free=$expected_free locked=$expected_locked withdrawing=$expected_withdrawing valuation=$expected_valuation" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_assets_estimated_value() {
  local owner="$1"
  local quote_asset="$2"
  local expected_value="$3"
  local output_file="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v1/asset/estimatedValue" "quoteAsset=${quote_asset}")" &&
    printf '%s\n' "$body" | jq -e \
      --arg quote_asset "$quote_asset" \
      --argjson expected_value "$expected_value" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
          .evaluatedWith == $quote_asset and
        nearly_equal((.value | tonumber); $expected_value) and
        ((.zeroValueAssets // []) | length == 0)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance assets estimated value owner=$owner quote=$quote_asset value=$expected_value" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_order_projection() {
  local owner="$1"
  local symbol="$2"
  local price="$3"
  local quantity="$4"
  local expected_status="$5"
  local expected_executed_quantity="$6"
  local expected_quote_quantity="$7"
  local side="$8"
  local output_file="$9"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/allOrders" "symbol=${symbol}&limit=20")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol "$symbol" \
      --argjson price "$price" \
      --argjson quantity "$quantity" \
      --arg expected_status "$expected_status" \
      --argjson expected_executed_quantity "$expected_executed_quantity" \
      --argjson expected_quote_quantity "$expected_quote_quantity" \
      --arg side "$side" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        [
          .[] |
          select(
            .symbol == $symbol and
            .price == $price and
            .origQty == $quantity and
            .status == $expected_status and
            .side == $side and
            .type == "LIMIT" and
            nearly_equal(.executedQty; $expected_executed_quantity) and
            nearly_equal(.cummulativeQuoteQty; $expected_quote_quantity) and
            (.orderId | type == "number") and
            (.orderId > 0)
          )
        ] | length >= 1
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance private order owner=$owner symbol=$symbol price=$price quantity=$quantity status=$expected_status executed=$expected_executed_quantity quote=$expected_quote_quantity side=$side" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_open_order() {
  local owner="$1"
  local symbol="$2"
  local client_order_id="$3"
  local price="$4"
  local quantity="$5"
  local side="$6"
  local output_file="$7"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/openOrders" "symbol=${symbol}&limit=20")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol "$symbol" \
      --arg client_order_id "$client_order_id" \
      --argjson price "$price" \
      --argjson quantity "$quantity" \
      --arg side "$side" '
        [
          .[] |
          select(
            .symbol == $symbol and
            .clientOrderId == $client_order_id and
            .price == $price and
            .origQty == $quantity and
            .status == "NEW" and
            .side == $side and
            .type == "LIMIT" and
            .isWorking == true and
            (.orderId | type == "number") and
            (.orderId > 0)
          )
        ] | length == 1
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance open order owner=$owner symbol=$symbol clientOrderId=$client_order_id price=$price quantity=$quantity side=$side" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_no_open_orders() {
  local owner="$1"
  local symbol="$2"
  local output_file="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/openOrders" "symbol=${symbol}&limit=20")" &&
    printf '%s\n' "$body" | jq -e 'length == 0' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance no open orders owner=$owner symbol=$symbol" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_no_all_orders() {
  local owner="$1"
  local symbol="$2"
  local output_file="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/allOrders" "symbol=${symbol}&limit=20")" &&
    printf '%s\n' "$body" | jq -e 'length == 0' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance no allOrders owner=$owner symbol=$symbol" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_open_orders_across_symbols() {
  local owner="$1"
  local symbol_one="$2"
  local client_order_id_one="$3"
  local price_one="$4"
  local quantity_one="$5"
  local side_one="$6"
  local symbol_two="$7"
  local client_order_id_two="$8"
  local price_two="$9"
  local quantity_two="${10}"
  local side_two="${11}"
  local output_file="${12}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/openOrders" "limit=20")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol_one "$symbol_one" \
      --arg client_order_id_one "$client_order_id_one" \
      --argjson price_one "$price_one" \
      --argjson quantity_one "$quantity_one" \
      --arg side_one "$side_one" \
      --arg symbol_two "$symbol_two" \
      --arg client_order_id_two "$client_order_id_two" \
      --argjson price_two "$price_two" \
      --argjson quantity_two "$quantity_two" \
      --arg side_two "$side_two" '
        def order_matches($symbol; $client_order_id; $price; $quantity; $side):
          .symbol == $symbol and
          .clientOrderId == $client_order_id and
          .price == $price and
          .origQty == $quantity and
          .executedQty == 0 and
          .cummulativeQuoteQty == 0 and
          .status == "NEW" and
          .side == $side and
          .type == "LIMIT" and
          .isWorking == true and
          (.orderId | type == "number") and
          (.orderId > 0);
        length == 2 and
        ([.[] | select(order_matches($symbol_one; $client_order_id_one; $price_one; $quantity_one; $side_one))] | length == 1) and
        ([.[] | select(order_matches($symbol_two; $client_order_id_two; $price_two; $quantity_two; $side_two))] | length == 1)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance all-symbol open orders owner=$owner" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_all_orders_across_symbols() {
  local owner="$1"
  local symbol_one="$2"
  local price_one="$3"
  local quantity_one="$4"
  local status_one="$5"
  local side_one="$6"
  local symbol_two="$7"
  local price_two="$8"
  local quantity_two="$9"
  local status_two="${10}"
  local side_two="${11}"
  local output_file="${12}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/allOrders" "limit=20")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol_one "$symbol_one" \
      --argjson price_one "$price_one" \
      --argjson quantity_one "$quantity_one" \
      --arg status_one "$status_one" \
      --arg side_one "$side_one" \
      --arg symbol_two "$symbol_two" \
      --argjson price_two "$price_two" \
      --argjson quantity_two "$quantity_two" \
      --arg status_two "$status_two" \
      --arg side_two "$side_two" '
        def order_matches($symbol; $price; $quantity; $status; $side):
          .symbol == $symbol and
          .price == $price and
          .origQty == $quantity and
          .executedQty == 0 and
          .cummulativeQuoteQty == 0 and
          .status == $status and
          .side == $side and
          .type == "LIMIT" and
          (.orderId | type == "number") and
          (.orderId > 0);
        length == 2 and
        ([.[] | select(order_matches($symbol_one; $price_one; $quantity_one; $status_one; $side_one))] | length == 1) and
        ([.[] | select(order_matches($symbol_two; $price_two; $quantity_two; $status_two; $side_two))] | length == 1)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance all-symbol all orders owner=$owner" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_order_status_by_order_id() {
  local owner="$1"
  local symbol="$2"
  local order_id="$3"
  local price="$4"
  local quantity="$5"
  local expected_status="$6"
  local expected_executed_quantity="$7"
  local expected_quote_quantity="$8"
  local side="$9"
  local output_file="${10}"
  local expected_type="${11:-LIMIT}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/order" "symbol=${symbol}&orderId=${order_id}")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol "$symbol" \
      --argjson order_id "$order_id" \
      --argjson price "$price" \
      --argjson quantity "$quantity" \
      --arg expected_status "$expected_status" \
      --argjson expected_executed_quantity "$expected_executed_quantity" \
      --argjson expected_quote_quantity "$expected_quote_quantity" \
      --arg side "$side" \
      --arg expected_type "$expected_type" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        .symbol == $symbol and
        .orderId == $order_id and
        .price == $price and
        .origQty == $quantity and
        .status == $expected_status and
        .side == $side and
        .type == $expected_type and
        nearly_equal(.executedQty; $expected_executed_quantity) and
        nearly_equal(.cummulativeQuoteQty; $expected_quote_quantity)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance private order query owner=$owner symbol=$symbol orderId=$order_id status=$expected_status executed=$expected_executed_quantity quote=$expected_quote_quantity" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_order_status_by_client_order_id() {
  local owner="$1"
  local symbol="$2"
  local client_order_id="$3"
  local price="$4"
  local quantity="$5"
  local expected_status="$6"
  local expected_executed_quantity="$7"
  local expected_quote_quantity="$8"
  local side="$9"
  local output_file="${10}"
  local expected_type="${11:-LIMIT}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/order" "symbol=${symbol}&origClientOrderId=${client_order_id}")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol "$symbol" \
      --arg client_order_id "$client_order_id" \
      --argjson price "$price" \
      --argjson quantity "$quantity" \
      --arg expected_status "$expected_status" \
      --argjson expected_executed_quantity "$expected_executed_quantity" \
      --argjson expected_quote_quantity "$expected_quote_quantity" \
      --arg side "$side" \
      --arg expected_type "$expected_type" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        .symbol == $symbol and
        .clientOrderId == $client_order_id and
        .price == $price and
        .origQty == $quantity and
        .status == $expected_status and
        .side == $side and
        .type == $expected_type and
        nearly_equal(.executedQty; $expected_executed_quantity) and
        nearly_equal(.cummulativeQuoteQty; $expected_quote_quantity)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance private order query owner=$owner symbol=$symbol origClientOrderId=$client_order_id status=$expected_status executed=$expected_executed_quantity quote=$expected_quote_quantity" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

assert_binance_cancel_response() {
  local label="$1"
  local file="$2"
  local symbol="$3"
  local order_id="$4"
  local price="$5"
  local quantity="$6"
  local executed_quantity="$7"
  local quote_quantity="$8"
  local side="$9"
  local type="${10:-LIMIT}"
  local expected_orig_client_order_id="${11:-}"
  local expected_client_order_id="${12:-}"
  if ! jq -e \
    --arg symbol "$symbol" \
    --argjson order_id "$order_id" \
    --argjson price "$price" \
    --argjson quantity "$quantity" \
    --argjson executed_quantity "$executed_quantity" \
    --argjson quote_quantity "$quote_quantity" \
    --arg side "$side" \
    --arg type "$type" \
    --arg expected_orig_client_order_id "$expected_orig_client_order_id" \
    --arg expected_client_order_id "$expected_client_order_id" '
      def nearly_equal($actual; $expected):
        (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
      .symbol == $symbol and
      .orderId == $order_id and
      nearly_equal(.price; $price) and
      nearly_equal(.origQty; $quantity) and
      nearly_equal(.executedQty; $executed_quantity) and
      nearly_equal(.cummulativeQuoteQty; $quote_quantity) and
      .status == "CANCELED" and
      .timeInForce == "GTC" and
      .type == $type and
      .side == $side and
      (.origClientOrderId | type == "string") and
      (.clientOrderId | type == "string") and
      ($expected_orig_client_order_id == "" or .origClientOrderId == $expected_orig_client_order_id) and
      ($expected_client_order_id == "" or .clientOrderId == $expected_client_order_id)
    ' "$file" >/dev/null; then
    echo "$label cancel response did not match expected order state" >&2
    cat "$file" >&2
    exit 1
  fi
}

wait_binance_private_trade_projection() {
  local owner="$1"
  local symbol="$2"
  local price="$3"
  local quantity="$4"
  local quote_quantity="$5"
  local commission="$6"
  local commission_asset="$7"
  local is_buyer="$8"
  local is_maker="$9"
  local output_file="${10}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/myTrades" "symbol=${symbol}&limit=20")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol "$symbol" \
      --argjson price "$price" \
      --argjson quantity "$quantity" \
      --argjson quote_quantity "$quote_quantity" \
      --argjson commission "$commission" \
      --arg commission_asset "$commission_asset" \
      --argjson is_buyer "$is_buyer" \
      --argjson is_maker "$is_maker" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        [
          .[] |
          select(
            .symbol == $symbol and
            .price == $price and
            .qty == $quantity and
            .quoteQty == $quote_quantity and
            nearly_equal(.commission; $commission) and
            .commissionAsset == $commission_asset and
            .isBuyer == $is_buyer and
            .isMaker == $is_maker and
            .isBestMatch == true and
            (.id | type == "number") and
            (.id > 0) and
            (.orderId | type == "number") and
            (.orderId > 0)
          )
        ] | length >= 1
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance private trade owner=$owner symbol=$symbol price=$price quantity=$quantity quote=$quote_quantity commission=$commission commissionAsset=$commission_asset isBuyer=$is_buyer isMaker=$is_maker" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_trade_projection_from_id() {
  local owner="$1"
  local symbol="$2"
  local from_id="$3"
  local price="$4"
  local quantity="$5"
  local quote_quantity="$6"
  local commission="$7"
  local commission_asset="$8"
  local is_buyer="$9"
  local is_maker="${10}"
  local output_file="${11}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/myTrades" "symbol=${symbol}&fromId=${from_id}&limit=20")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol "$symbol" \
      --argjson from_id "$from_id" \
      --argjson price "$price" \
      --argjson quantity "$quantity" \
      --argjson quote_quantity "$quote_quantity" \
      --argjson commission "$commission" \
      --arg commission_asset "$commission_asset" \
      --argjson is_buyer "$is_buyer" \
      --argjson is_maker "$is_maker" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        [
          .[] |
          select(
            .symbol == $symbol and
            .id == $from_id and
            .price == $price and
            .qty == $quantity and
            .quoteQty == $quote_quantity and
            nearly_equal(.commission; $commission) and
            .commissionAsset == $commission_asset and
            .isBuyer == $is_buyer and
            .isMaker == $is_maker and
            .isBestMatch == true and
            (.orderId | type == "number") and
            (.orderId > 0)
          )
        ] | length == 1
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance private trade fromId owner=$owner symbol=$symbol fromId=$from_id" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_trade_projection_for_order_id() {
  local owner="$1"
  local symbol="$2"
  local order_id="$3"
  local trade_id="$4"
  local price="$5"
  local quantity="$6"
  local quote_quantity="$7"
  local commission="$8"
  local commission_asset="$9"
  local is_buyer="${10}"
  local is_maker="${11}"
  local output_file="${12}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/myTrades" "symbol=${symbol}&orderId=${order_id}&limit=20")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol "$symbol" \
      --argjson order_id "$order_id" \
      --argjson trade_id "$trade_id" \
      --argjson price "$price" \
      --argjson quantity "$quantity" \
      --argjson quote_quantity "$quote_quantity" \
      --argjson commission "$commission" \
      --arg commission_asset "$commission_asset" \
      --argjson is_buyer "$is_buyer" \
      --argjson is_maker "$is_maker" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        length == 1 and
        .[0].symbol == $symbol and
        .[0].id == $trade_id and
        .[0].orderId == $order_id and
        .[0].price == $price and
        .[0].qty == $quantity and
        .[0].quoteQty == $quote_quantity and
        nearly_equal(.[0].commission; $commission) and
        .[0].commissionAsset == $commission_asset and
        .[0].isBuyer == $is_buyer and
        .[0].isMaker == $is_maker and
        .[0].isBestMatch == true
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance private trade orderId owner=$owner symbol=$symbol orderId=$order_id tradeId=$trade_id" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_private_trades_empty_for_order_id() {
  local owner="$1"
  local symbol="$2"
  local order_id="$3"
  local output_file="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/myTrades" "symbol=${symbol}&orderId=${order_id}&limit=20")" &&
    printf '%s\n' "$body" | jq -e 'length == 0' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for empty Binance private trades owner=$owner symbol=$symbol orderId=$order_id" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_all_orders_time_range_includes_order() {
  local owner="$1"
  local symbol="$2"
  local order_id="$3"
  local output_file="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/allOrders" "symbol=${symbol}&startTime=1&endTime=4102444800000&limit=20")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol "$symbol" \
      --argjson order_id "$order_id" '
        [.[] | select(.symbol == $symbol and .orderId == $order_id)] | length == 1
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance allOrders time range owner=$owner symbol=$symbol orderId=$order_id" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_all_orders_future_time_range_empty() {
  local owner="$1"
  local symbol="$2"
  local output_file="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/allOrders" "symbol=${symbol}&startTime=4102444800000&endTime=4102444801000&limit=20")" &&
    printf '%s\n' "$body" | jq -e 'length == 0' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for empty Binance allOrders future time range owner=$owner symbol=$symbol" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_my_trades_time_range_includes_trade() {
  local owner="$1"
  local symbol="$2"
  local trade_id="$3"
  local output_file="$4"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/myTrades" "symbol=${symbol}&startTime=1&endTime=4102444800000&limit=20")" &&
    printf '%s\n' "$body" | jq -e \
      --arg symbol "$symbol" \
      --argjson trade_id "$trade_id" '
        [.[] | select(.symbol == $symbol and .id == $trade_id)] | length == 1
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for Binance myTrades time range owner=$owner symbol=$symbol tradeId=$trade_id" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_binance_my_trades_future_time_range_empty() {
  local owner="$1"
  local symbol="$2"
  local output_file="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(binance_private_get "$owner" "/v3/myTrades" "symbol=${symbol}&startTime=4102444800000&endTime=4102444801000&limit=20")" &&
    printf '%s\n' "$body" | jq -e 'length == 0' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for empty Binance myTrades future time range owner=$owner symbol=$symbol" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 api market >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_order_status() {
  local owner="$1"
  local ouid="$2"
  local expected_status="$3"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/user/${owner}/order/${ouid}")" &&
    printf '%s\n' "$body" | jq -e --arg expected_status "$expected_status" '.status == $expected_status' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for order $ouid status=$expected_status" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_order_projection() {
  local owner="$1"
  local ouid="$2"
  local expected_status="$3"
  local expected_executed_quantity="$4"
  local expected_accumulative_quote_qty="$5"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  until body="$(curl -fsS "http://127.0.0.1:8096/v1/user/${owner}/order/${ouid}")" &&
    printf '%s\n' "$body" | jq -e \
      --arg expected_status "$expected_status" \
      --argjson expected_executed_quantity "$expected_executed_quantity" \
      --argjson expected_accumulative_quote_qty "$expected_accumulative_quote_qty" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        .status == $expected_status and
        nearly_equal(.executedQuantity; $expected_executed_quantity) and
        nearly_equal(.accumulativeQuoteQty; $expected_accumulative_quote_qty)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for order $ouid status=$expected_status executedQuantity=$expected_executed_quantity accumulativeQuoteQty=$expected_accumulative_quote_qty" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
}

wait_order_projection_by_order_id() {
  local owner="$1"
  local symbol="$2"
  local order_id="$3"
  local expected_status="$4"
  local expected_executed_quantity="$5"
  local expected_accumulative_quote_qty="$6"
  local output_file="$7"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body request
  request="$(jq -nc --arg symbol "$symbol" --argjson orderId "$order_id" '{symbol:$symbol, orderId:$orderId, origClientOrderId:null}')"
  until body="$(curl -fsS -X POST -H "Content-Type: application/json" -d "$request" "http://127.0.0.1:8096/v1/user/${owner}/order/query")" &&
    printf '%s\n' "$body" | jq -e \
      --arg expected_status "$expected_status" \
      --argjson expected_executed_quantity "$expected_executed_quantity" \
      --argjson expected_accumulative_quote_qty "$expected_accumulative_quote_qty" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        .status == $expected_status and
        nearly_equal(.executedQuantity; $expected_executed_quantity) and
        nearly_equal(.accumulativeQuoteQty; $expected_accumulative_quote_qty)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for orderId=$order_id status=$expected_status executedQuantity=$expected_executed_quantity accumulativeQuoteQty=$expected_accumulative_quote_qty" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_vault_e2e_secret() {
  local deadline=$((SECONDS + 900))
  until "${COMPOSE[@]}" exec -T vault sh -c 'VAULT_TOKEN="$(cat /vault/file/tokens.txt 2>/dev/null)" vault kv get secret/opex-wallet >/dev/null 2>&1'; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for vault e2e secrets" >&2
      "${COMPOSE[@]}" logs --tail=200 vault >&2 || true
      exit 1
    fi
    sleep 2
  done
  echo "ready: vault e2e secrets loaded"
}

wait_user_trade_projection() {
  local owner="$1"
  local symbol="$2"
  local price="$3"
  local quantity="$4"
  local quote_quantity="$5"
  local commission="$6"
  local commission_asset="$7"
  local is_buyer="$8"
  local is_maker="$9"
  local is_maker_buyer="${10}"
  local output_file="${11}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  local trade_query
  trade_query="{\"symbol\":\"${symbol}\",\"fromTrade\":null,\"startTime\":null,\"endTime\":null,\"limit\":20}"
  until body="$(curl -fsS -X POST -H "Content-Type: application/json" -d "$trade_query" "http://127.0.0.1:8096/v1/user/${owner}/trades")" &&
    printf '%s\n' "$body" | jq -e \
      --argjson price "$price" \
      --argjson quantity "$quantity" \
      --argjson quote_quantity "$quote_quantity" \
      --argjson commission "$commission" \
      --arg commission_asset "$commission_asset" \
      --argjson is_buyer "$is_buyer" \
      --argjson is_maker "$is_maker" \
      --argjson is_maker_buyer "$is_maker_buyer" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        [
          .[] |
          select(
            .price == $price and
            .quantity == $quantity and
            .quoteQuantity == $quote_quantity and
            nearly_equal(.commission; $commission) and
            .commissionAsset == $commission_asset and
            .isBuyer == $is_buyer and
            .isMaker == $is_maker and
            .isMakerBuyer == $is_maker_buyer and
            .isBestMatch == true and
            (.orderId | type == "number") and
            (.orderId > 0)
          )
        ] | length >= 1
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for user trade projection owner=$owner symbol=$symbol price=$price quantity=$quantity quoteQuantity=$quote_quantity commission=$commission commissionAsset=$commission_asset isBuyer=$is_buyer isMaker=$is_maker isMakerBuyer=$is_maker_buyer" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_user_trade_aggregate() {
  local owner="$1"
  local symbol="$2"
  local price="$3"
  local expected_count="$4"
  local expected_quantity="$5"
  local expected_quote_quantity="$6"
  local expected_commission="$7"
  local commission_asset="$8"
  local is_buyer="$9"
  local is_maker="${10}"
  local is_maker_buyer="${11}"
  local output_file="${12}"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local body
  local trade_query
  trade_query="{\"symbol\":\"${symbol}\",\"fromTrade\":null,\"startTime\":null,\"endTime\":null,\"limit\":20}"
  until body="$(curl -fsS -X POST -H "Content-Type: application/json" -d "$trade_query" "http://127.0.0.1:8096/v1/user/${owner}/trades")" &&
    printf '%s\n' "$body" | jq -e \
      --argjson price "$price" \
      --argjson expected_count "$expected_count" \
      --argjson expected_quantity "$expected_quantity" \
      --argjson expected_quote_quantity "$expected_quote_quantity" \
      --argjson expected_commission "$expected_commission" \
      --arg commission_asset "$commission_asset" \
      --argjson is_buyer "$is_buyer" \
      --argjson is_maker "$is_maker" \
      --argjson is_maker_buyer "$is_maker_buyer" '
        def nearly_equal($actual; $expected):
          (($actual - $expected) as $diff | (if $diff < 0 then -$diff else $diff end) <= 0.000001);
        [
          .[] |
          select(
            .price == $price and
            .commissionAsset == $commission_asset and
            .isBuyer == $is_buyer and
            .isMaker == $is_maker and
            .isMakerBuyer == $is_maker_buyer and
            .isBestMatch == true and
            (.orderId | type == "number") and
            (.orderId > 0)
          )
        ] as $matches |
        def sum_field($field): reduce $matches[] as $trade (0; . + ($trade[$field] // 0));
        ($matches | length) == $expected_count and
        nearly_equal(sum_field("quantity"); $expected_quantity) and
        nearly_equal(sum_field("quoteQuantity"); $expected_quote_quantity) and
        nearly_equal(sum_field("commission"); $expected_commission)
      ' >/dev/null; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for user trade aggregate owner=$owner symbol=$symbol price=$price count=$expected_count quantity=$expected_quantity quoteQuantity=$expected_quote_quantity commission=$expected_commission commissionAsset=$commission_asset isBuyer=$is_buyer isMaker=$is_maker isMakerBuyer=$is_maker_buyer" >&2
      echo "$body" >&2
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  printf '%s\n' "$body" > "$output_file"
}

wait_market_order_status_matches_trade_totals() {
  local label="$1"
  wait_query_eq "$label" "postgres-market" "0" "
    with e2e_trade_totals as (
      select
        trade_orders.ouid,
        trade_orders.uuid,
        sum(trade_orders.matched_quantity) as executed_quantity,
        sum(trade_orders.matched_price * trade_orders.matched_quantity) as accumulative_quote_qty
      from (
        select maker_ouid as ouid, maker_uuid as uuid, matched_price, matched_quantity
        from trades
        where maker_uuid like 'e2e-%'
        union all
        select taker_ouid as ouid, taker_uuid as uuid, matched_price, matched_quantity
        from trades
        where taker_uuid like 'e2e-%'
      ) trade_orders
      group by trade_orders.ouid, trade_orders.uuid
    ),
    latest_order_status as (
      select
        ranked_status.ouid,
        status_totals.executed_quantity,
        status_totals.accumulative_quote_qty
      from (
        select
          os.*,
          row_number() over (partition by ouid order by appearance desc, executed_quantity desc) as rank
        from order_status os
      ) ranked_status
      join (
        select
          ouid,
          coalesce(
            max(case when appearance > 1 then executed_quantity end),
            max(executed_quantity)
          ) as executed_quantity,
          coalesce(
            max(case when appearance > 1 then accumulative_quote_qty end),
            max(accumulative_quote_qty)
          ) as accumulative_quote_qty
        from order_status
        group by ouid
      ) status_totals on status_totals.ouid = ranked_status.ouid
      where rank = 1
    )
    select count(*)
    from orders o
    join latest_order_status os on os.ouid = o.ouid
    left join e2e_trade_totals tt on tt.ouid = o.ouid and tt.uuid = o.uuid
    where o.uuid like 'e2e-%'
      and (
        abs(coalesce(os.executed_quantity, 0) - coalesce(tt.executed_quantity, 0)) > 0.000001
        or abs(coalesce(os.accumulative_quote_qty, 0) - coalesce(tt.accumulative_quote_qty, 0)) > 0.000001
      );
  "
}

wait_accountant_market_order_definitions_match() {
  local label="$1"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local accountant_file market_file diff_file
  accountant_file="$(mktemp)"
  market_file="$(mktemp)"
  diff_file="$(mktemp)"

  while true; do
    psql_query "postgres-accountant" "
      select
        ouid,
        uuid,
        pair,
        direction,
        match_constraint,
        order_type,
        user_level,
        to_char(maker_fee, 'FM9999999990.00000000'),
        to_char(taker_fee, 'FM9999999990.00000000'),
        to_char(left_side_fraction, 'FM9999999990.00000000'),
        to_char(right_side_fraction, 'FM9999999990.00000000'),
        to_char(price * right_side_fraction, 'FM9999999990.00000000'),
        to_char(quantity * left_side_fraction, 'FM9999999990.00000000')
      from orders
      where uuid like 'e2e-%'
        and status <> 3
      order by ouid;
    " > "$accountant_file"

    psql_query "postgres-market" "
      with ranked_order_status as (
        select
          os.*,
          row_number() over (partition by ouid order by appearance desc, executed_quantity desc) as rank
        from order_status os
      ),
      latest_order_status as (
        select
          ouid,
          status
        from ranked_order_status
        where rank = 1
      )
      select
        o.ouid,
        o.uuid,
        o.symbol,
        o.side,
        o.match_constraint,
        o.order_type,
        o.user_level,
        to_char(o.maker_fee, 'FM9999999990.00000000'),
        to_char(o.taker_fee, 'FM9999999990.00000000'),
        to_char(o.left_side_fraction, 'FM9999999990.00000000'),
        to_char(o.right_side_fraction, 'FM9999999990.00000000'),
        to_char(o.price, 'FM9999999990.00000000'),
        to_char(o.quantity, 'FM9999999990.00000000')
      from orders o
      join latest_order_status os on os.ouid = o.ouid
      where o.uuid like 'e2e-%'
        and os.status <> 3
      order by o.ouid;
    " > "$market_file"

    if diff -u "$accountant_file" "$market_file" > "$diff_file"; then
      rm -f "$accountant_file" "$market_file" "$diff_file"
      return 0
    fi

    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label" >&2
      cat "$diff_file" >&2
      rm -f "$accountant_file" "$market_file" "$diff_file"
      "${COMPOSE[@]}" logs --tail=200 accountant market >&2 || true
      exit 1
    fi

    sleep 2
  done
}

wait_accountant_order_actions_match_eventlog_lifecycle() {
  local label="$1"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local expected_file actual_file diff_file
  expected_file="$(mktemp)"
  actual_file="$(mktemp)"
  diff_file="$(mktemp)"

  while true; do
    psql_query "postgres-eventlog" "
      with pair_config(pair, left_fraction, right_fraction) as (
        values
          ('ETH_USDT', 0.000001::decimal, 0.01::decimal),
          ('BTC_USDT', 0.000001::decimal, 0.01::decimal),
          ('SOL_USDT', 0.00001::decimal, 0.01::decimal),
          ('DOGE_USDT', 0.001::decimal, 0.01::decimal),
          ('TON_USDT', 0.0001::decimal, 0.01::decimal)
      ),
      parsed_events as (
        select
          event,
          ouid,
          uuid,
          event_json::jsonb as event_body,
          ((event_json::jsonb -> 'pair' ->> 'leftSideName') || '_' || (event_json::jsonb -> 'pair' ->> 'rightSideName')) as pair,
          event_json::jsonb -> 'pair' ->> 'leftSideName' as base_symbol,
          event_json::jsonb -> 'pair' ->> 'rightSideName' as quote_symbol,
          event_json::jsonb ->> 'direction' as direction,
          nullif(event_json::jsonb ->> 'price', '')::decimal as price,
          nullif(event_json::jsonb ->> 'quantity', '')::decimal as quantity,
          nullif(event_json::jsonb ->> 'oldPrice', '')::decimal as old_price,
          nullif(event_json::jsonb ->> 'oldQuantity', '')::decimal as old_quantity,
          nullif(event_json::jsonb ->> 'remainedQuantity', '')::decimal as remained_quantity,
          event_json::jsonb ->> 'requestedOperation' as requested_operation
        from opex_events
        where uuid like 'e2e-%'
          and event in ('CreateOrderEvent', 'RejectOrderEvent', 'CancelOrderEvent', 'UpdatedOrderEvent')
      ),
      trade_events as (
        select distinct
          ((event_json::jsonb -> 'pair' ->> 'leftSideName') || '_' || (event_json::jsonb -> 'pair' ->> 'rightSideName')) as pair,
          event_json::jsonb ->> 'makerOuid' as maker_ouid,
          event_json::jsonb ->> 'takerOuid' as taker_ouid,
          event_json::jsonb ->> 'makerDirection' as maker_direction,
          event_json::jsonb ->> 'takerDirection' as taker_direction,
          nullif(event_json::jsonb ->> 'makerPrice', '')::decimal as maker_price,
          nullif(event_json::jsonb ->> 'matchedQuantity', '')::decimal as matched_quantity
        from opex_events
        where event = 'TradeEvent'
          and (event_json::jsonb ->> 'makerUuid' like 'e2e-%' or event_json::jsonb ->> 'takerUuid' like 'e2e-%')
      ),
      reservation_changes as (
        select
          pe.ouid,
          case when pe.direction = 'ASK'
            then pe.quantity * pc.left_fraction
            else pe.price * pc.right_fraction * pe.quantity * pc.left_fraction
          end as amount
        from parsed_events pe
        join pair_config pc on pc.pair = pe.pair
        where pe.event in ('CreateOrderEvent', 'RejectOrderEvent')
          and pe.direction is not null
          and (pe.event <> 'RejectOrderEvent' or pe.requested_operation = 'PLACE_ORDER')
        union all
        select
          delta.ouid,
          delta.delta_amount as amount
        from (
          select
            pe.ouid,
            case when pe.direction = 'ASK'
              then (pe.quantity - (pe.old_quantity - pe.remained_quantity)) * pc.left_fraction
              else pe.price * pc.right_fraction * (pe.quantity - (pe.old_quantity - pe.remained_quantity)) * pc.left_fraction
            end -
            case when pe.direction = 'ASK'
              then pe.remained_quantity * pc.left_fraction
              else pe.old_price * pc.right_fraction * pe.remained_quantity * pc.left_fraction
            end as delta_amount
          from parsed_events pe
          join pair_config pc on pc.pair = pe.pair
          where pe.event = 'UpdatedOrderEvent'
            and pe.direction is not null
        ) delta
        where abs(delta.delta_amount) > 0.000001
      ),
      trade_quote_debits as (
        select ouid, sum(amount) as amount
        from (
          select
            te.maker_ouid as ouid,
            te.maker_price * pc.right_fraction * te.matched_quantity * pc.left_fraction as amount
          from trade_events te
          join pair_config pc on pc.pair = te.pair
          where te.maker_direction = 'BID'
          union all
          select
            te.taker_ouid as ouid,
            te.maker_price * pc.right_fraction * te.matched_quantity * pc.left_fraction as amount
          from trade_events te
          join pair_config pc on pc.pair = te.pair
          where te.taker_direction = 'BID'
        ) quote_debits
        group by ouid
      ),
      bid_reserve_remaining as (
        select
          rc.ouid,
          sum(rc.amount) - coalesce(tqd.amount, 0) as remaining_amount
        from reservation_changes rc
        left join trade_quote_debits tqd on tqd.ouid = rc.ouid
        group by rc.ouid, tqd.amount
      ),
      lifecycle_actions as (
        select
          'SubmitOrderEvent' as event_type,
          'ORDER_CREATE' as category_name,
          pe.ouid as pointer,
          case when pe.direction = 'ASK' then pe.base_symbol else pe.quote_symbol end as symbol,
          case when pe.direction = 'ASK'
            then pe.quantity * pc.left_fraction
            else pe.price * pc.right_fraction * pe.quantity * pc.left_fraction
          end as amount,
          pe.uuid as sender,
          'MAIN' as sender_wallet_type,
          pe.uuid as receiver,
          'EXCHANGE' as receiver_wallet_type
        from parsed_events pe
        join pair_config pc on pc.pair = pe.pair
        where pe.event in ('CreateOrderEvent', 'RejectOrderEvent')
          and pe.direction is not null
          and (pe.event <> 'RejectOrderEvent' or pe.requested_operation = 'PLACE_ORDER')
        union all
        select
          pe.event as event_type,
          'ORDER_CANCEL' as category_name,
          pe.ouid as pointer,
          case when pe.direction = 'ASK' then pe.base_symbol else pe.quote_symbol end as symbol,
          case when pe.direction = 'ASK'
            then pe.remained_quantity * pc.left_fraction
            else coalesce(brr.remaining_amount, 0)
          end as amount,
          pe.uuid as sender,
          'EXCHANGE' as sender_wallet_type,
          pe.uuid as receiver,
          'MAIN' as receiver_wallet_type
        from parsed_events pe
        join pair_config pc on pc.pair = pe.pair
        left join bid_reserve_remaining brr on brr.ouid = pe.ouid
        where pe.event = 'CancelOrderEvent'
          and pe.direction is not null
        union all
        select
          'RejectOrderEvent' as event_type,
          'ORDER_CANCEL' as category_name,
          pe.ouid as pointer,
          case when pe.direction = 'ASK' then pe.base_symbol else pe.quote_symbol end as symbol,
          case when pe.direction = 'ASK'
            then pe.quantity * pc.left_fraction
            else pe.price * pc.right_fraction * pe.quantity * pc.left_fraction
          end as amount,
          pe.uuid as sender,
          'EXCHANGE' as sender_wallet_type,
          pe.uuid as receiver,
          'MAIN' as receiver_wallet_type
        from parsed_events pe
        join pair_config pc on pc.pair = pe.pair
        where pe.event = 'RejectOrderEvent'
          and pe.direction is not null
          and pe.requested_operation = 'PLACE_ORDER'
        union all
        select
          'UpdatedOrderEvent' as event_type,
          case when delta.delta_amount > 0 then 'ORDER_CREATE' else 'ORDER_CANCEL' end as category_name,
          delta.ouid as pointer,
          delta.symbol,
          abs(delta.delta_amount) as amount,
          delta.uuid as sender,
          case when delta.delta_amount > 0 then 'MAIN' else 'EXCHANGE' end as sender_wallet_type,
          delta.uuid as receiver,
          case when delta.delta_amount > 0 then 'EXCHANGE' else 'MAIN' end as receiver_wallet_type
        from (
          select
            pe.ouid,
            pe.uuid,
            case when pe.direction = 'ASK' then pe.base_symbol else pe.quote_symbol end as symbol,
            case when pe.direction = 'ASK'
              then (pe.quantity - (pe.old_quantity - pe.remained_quantity)) * pc.left_fraction
              else pe.price * pc.right_fraction * (pe.quantity - (pe.old_quantity - pe.remained_quantity)) * pc.left_fraction
            end -
            case when pe.direction = 'ASK'
              then pe.remained_quantity * pc.left_fraction
              else pe.old_price * pc.right_fraction * pe.remained_quantity * pc.left_fraction
            end as delta_amount
          from parsed_events pe
          join pair_config pc on pc.pair = pe.pair
          where pe.event = 'UpdatedOrderEvent'
            and pe.direction is not null
        ) delta
        where abs(delta.delta_amount) > 0.000001
      )
      select distinct
        event_type,
        category_name,
        pointer,
        symbol,
        to_char(amount, 'FM9999999990.00000000'),
        sender,
        sender_wallet_type,
        receiver,
        receiver_wallet_type
      from lifecycle_actions
      order by pointer, event_type, category_name, symbol, sender, receiver, to_char(amount, 'FM9999999990.00000000');
    " > "$expected_file"

    psql_query "postgres-accountant" "
      select
        event_type,
        category_name,
        pointer,
        symbol,
        to_char(amount, 'FM9999999990.00000000'),
        sender,
        sender_wallet_type,
        receiver,
        receiver_wallet_type
      from fi_actions
      where (sender like 'e2e-%' or receiver like 'e2e-%')
        and event_type in ('SubmitOrderEvent', 'UpdatedOrderEvent', 'CancelOrderEvent', 'RejectOrderEvent')
        and category_name in ('ORDER_CREATE', 'ORDER_CANCEL')
        and status = 'PROCESSED'
      order by pointer, event_type, category_name, symbol, sender, receiver, to_char(amount, 'FM9999999990.00000000');
    " > "$actual_file"

    if diff -u "$expected_file" "$actual_file" > "$diff_file"; then
      rm -f "$expected_file" "$actual_file" "$diff_file"
      return 0
    fi

    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label" >&2
      cat "$diff_file" >&2
      rm -f "$expected_file" "$actual_file" "$diff_file"
      "${COMPOSE[@]}" logs --tail=200 accountant eventlog wallet matching-engine >&2 || true
      exit 1
    fi

    sleep 2
  done
}

wait_accountant_market_order_projections_match() {
  local label="$1"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local accountant_file market_file diff_file
  accountant_file="$(mktemp)"
  market_file="$(mktemp)"
  diff_file="$(mktemp)"

  while true; do
    psql_query "postgres-accountant" "
      select
        ouid,
        uuid,
        pair,
        direction,
        status,
        to_char(filled_orig_quantity, 'FM9999999990.00000000'),
        to_char(accumulative_quote_qty, 'FM9999999990.00000000')
      from orders
      where uuid like 'e2e-%'
        and status <> 3
      order by ouid;
    " > "$accountant_file"

    psql_query "postgres-market" "
      with ranked_order_status as (
        select
          os.*,
          row_number() over (partition by ouid order by appearance desc, executed_quantity desc) as rank
        from order_status os
      ),
      status_totals as (
        select
          ouid,
          coalesce(
            max(case when appearance > 1 then executed_quantity end),
            max(executed_quantity)
          ) as executed_quantity,
          coalesce(
            max(case when appearance > 1 then accumulative_quote_qty end),
            max(accumulative_quote_qty)
          ) as accumulative_quote_qty
        from order_status
        group by ouid
      ),
      latest_order_status as (
        select
          ranked_order_status.ouid,
          status_totals.executed_quantity,
          status_totals.accumulative_quote_qty,
          ranked_order_status.status
        from ranked_order_status
        join status_totals on status_totals.ouid = ranked_order_status.ouid
        where ranked_order_status.rank = 1
      )
      select
        o.ouid,
        o.uuid,
        o.symbol,
        o.side,
        os.status,
        to_char(coalesce(os.executed_quantity, 0), 'FM9999999990.00000000'),
        to_char(coalesce(os.accumulative_quote_qty, 0), 'FM9999999990.00000000')
      from orders o
      join latest_order_status os on os.ouid = o.ouid
      where o.uuid like 'e2e-%'
        and os.status <> 3
      order by o.ouid;
    " > "$market_file"

    if diff -u "$accountant_file" "$market_file" > "$diff_file"; then
      rm -f "$accountant_file" "$market_file" "$diff_file"
      return 0
    fi

    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label" >&2
      cat "$diff_file" >&2
      rm -f "$accountant_file" "$market_file" "$diff_file"
      "${COMPOSE[@]}" logs --tail=200 accountant market >&2 || true
      exit 1
    fi

    sleep 2
  done
}

wait_accountant_trade_transfers_match_market_executions() {
  local label="$1"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local accountant_file market_file diff_file
  accountant_file="$(mktemp)"
  market_file="$(mktemp)"
  diff_file="$(mktemp)"

  while true; do
    psql_query "postgres-market" "
      with expected_transfers as (
        select
          t.taker_ouid as pointer,
          case when taker_order.side = 'ASK' then t.base_asset else t.quote_asset end as symbol,
          case when taker_order.side = 'ASK' then t.matched_quantity else t.maker_price * t.matched_quantity end as amount,
          t.taker_uuid as sender,
          'EXCHANGE' as sender_wallet_type,
          t.maker_uuid as receiver,
          'MAIN' as receiver_wallet_type
        from trades t
        join orders taker_order on taker_order.ouid = t.taker_ouid
        where t.taker_uuid like 'e2e-%' or t.maker_uuid like 'e2e-%'
        union all
        select
          t.maker_ouid as pointer,
          case when maker_order.side = 'ASK' then t.base_asset else t.quote_asset end as symbol,
          case when maker_order.side = 'ASK' then t.matched_quantity else t.maker_price * t.matched_quantity end as amount,
          t.maker_uuid as sender,
          'EXCHANGE' as sender_wallet_type,
          t.taker_uuid as receiver,
          'MAIN' as receiver_wallet_type
        from trades t
        join orders maker_order on maker_order.ouid = t.maker_ouid
        where t.taker_uuid like 'e2e-%' or t.maker_uuid like 'e2e-%'
      )
      select
        pointer,
        symbol,
        to_char(amount, 'FM9999999990.00000000'),
        sender,
        sender_wallet_type,
        receiver,
        receiver_wallet_type,
        count(*)
      from expected_transfers
      group by pointer, symbol, amount, sender, sender_wallet_type, receiver, receiver_wallet_type
      order by pointer, symbol, sender, receiver, to_char(amount, 'FM9999999990.00000000');
    " > "$market_file"

    psql_query "postgres-accountant" "
      select
        pointer,
        symbol,
        to_char(amount, 'FM9999999990.00000000'),
        sender,
        sender_wallet_type,
        receiver,
        receiver_wallet_type,
        count(*)
      from fi_actions
      where event_type = 'TradeEvent'
        and category_name = 'TRADE'
        and status = 'PROCESSED'
        and (sender like 'e2e-%' or receiver like 'e2e-%')
      group by pointer, symbol, amount, sender, sender_wallet_type, receiver, receiver_wallet_type
      order by pointer, symbol, sender, receiver, to_char(amount, 'FM9999999990.00000000');
    " > "$accountant_file"

    if diff -u "$market_file" "$accountant_file" > "$diff_file"; then
      rm -f "$accountant_file" "$market_file" "$diff_file"
      return 0
    fi

    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label" >&2
      cat "$diff_file" >&2
      rm -f "$accountant_file" "$market_file" "$diff_file"
      "${COMPOSE[@]}" logs --tail=200 accountant market >&2 || true
      exit 1
    fi

    sleep 2
  done
}

wait_accountant_trade_fees_match_market_commissions() {
  local label="$1"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local accountant_file market_file diff_file
  accountant_file="$(mktemp)"
  market_file="$(mktemp)"
  diff_file="$(mktemp)"

  while true; do
    psql_query "postgres-market" "
      with expected_fees as (
        select maker_commission_asset as asset, maker_commission as amount
        from trades
        where maker_uuid like 'e2e-%' or taker_uuid like 'e2e-%'
        union all
        select taker_commission_asset as asset, taker_commission as amount
        from trades
        where maker_uuid like 'e2e-%' or taker_uuid like 'e2e-%'
      )
      select
        asset,
        to_char(coalesce(sum(amount), 0), 'FM9999999990.00000000')
      from expected_fees
      where asset is not null
      group by asset
      order by asset;
    " > "$market_file"

    psql_query "postgres-accountant" "
      select
        symbol,
        to_char(coalesce(sum(amount), 0), 'FM9999999990.00000000')
      from fi_actions
      where event_type = 'TradeEvent'
        and status = 'PROCESSED'
        and sender_wallet_type = 'MAIN'
        and receiver = '1'
        and receiver_wallet_type = 'EXCHANGE'
        and sender like 'e2e-%'
      group by symbol
      order by symbol;
    " > "$accountant_file"

    if diff -u "$market_file" "$accountant_file" > "$diff_file"; then
      rm -f "$accountant_file" "$market_file" "$diff_file"
      return 0
    fi

    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label" >&2
      cat "$diff_file" >&2
      rm -f "$accountant_file" "$market_file" "$diff_file"
      "${COMPOSE[@]}" logs --tail=200 accountant market >&2 || true
      exit 1
    fi

    sleep 2
  done
}

wait_wallet_fee_transactions_match_accountant_actions() {
  local label="$1"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local accountant_file wallet_file diff_file
  accountant_file="$(mktemp)"
  wallet_file="$(mktemp)"
  diff_file="$(mktemp)"

  while true; do
    psql_query "postgres-accountant" "
      select
        symbol,
        to_char(coalesce(sum(amount), 0), 'FM9999999990.00000000'),
        count(*)
      from fi_actions
      where event_type = 'TradeEvent'
        and status = 'PROCESSED'
        and sender_wallet_type = 'MAIN'
        and receiver = '1'
        and receiver_wallet_type = 'EXCHANGE'
        and sender like 'e2e-%'
      group by symbol
      order by symbol;
    " > "$accountant_file"

    psql_query "postgres-wallet" "
      select
        dw.currency,
        to_char(coalesce(sum(t.dest_amount), 0), 'FM9999999990.00000000'),
        count(*)
      from transaction t
      join wallet sw on sw.id = t.source_wallet
      join wallet_owner swo on swo.id = sw.owner
      join wallet dw on dw.id = t.dest_wallet
      join wallet_owner dwo on dwo.id = dw.owner
      where t.transfer_category = 'FEE'
        and sw.wallet_type = 'MAIN'
        and dwo.uuid = '1'
        and dw.wallet_type = 'EXCHANGE'
        and swo.uuid like 'e2e-%'
      group by dw.currency
      order by dw.currency;
    " > "$wallet_file"

    if diff -u "$accountant_file" "$wallet_file" > "$diff_file"; then
      rm -f "$accountant_file" "$wallet_file" "$diff_file"
      return 0
    fi

    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label" >&2
      cat "$diff_file" >&2
      rm -f "$accountant_file" "$wallet_file" "$diff_file"
      "${COMPOSE[@]}" logs --tail=200 accountant wallet >&2 || true
      exit 1
    fi

    sleep 2
  done
}

wait_wallet_transactions_match_accountant_actions() {
  local label="$1"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local accountant_file wallet_file diff_file
  accountant_file="$(mktemp)"
  wallet_file="$(mktemp)"
  diff_file="$(mktemp)"

  while true; do
    psql_query "postgres-accountant" "
      select
        concat('accountant:fiActions:', uuid),
        category_name,
        symbol,
        to_char(amount, 'FM9999999990.00000000'),
        sender,
        sender_wallet_type,
        receiver,
        receiver_wallet_type
      from fi_actions
      where status = 'PROCESSED'
        and (sender like 'e2e-%' or receiver like 'e2e-%')
      order by 1;
    " > "$accountant_file"

    psql_query "postgres-wallet" "
      select
        t.transfer_ref,
        t.transfer_category,
        sw.currency,
        to_char(t.dest_amount, 'FM9999999990.00000000'),
        swo.uuid,
        sw.wallet_type,
        dwo.uuid,
        dw.wallet_type
      from transaction t
      join wallet sw on sw.id = t.source_wallet
      join wallet_owner swo on swo.id = sw.owner
      join wallet dw on dw.id = t.dest_wallet
      join wallet_owner dwo on dwo.id = dw.owner
      where t.transfer_ref like 'accountant:fiActions:%'
        and (swo.uuid like 'e2e-%' or dwo.uuid like 'e2e-%')
      order by t.transfer_ref;
    " > "$wallet_file"

    if diff -u "$accountant_file" "$wallet_file" > "$diff_file"; then
      rm -f "$accountant_file" "$wallet_file" "$diff_file"
      return 0
    fi

    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label" >&2
      cat "$diff_file" >&2
      rm -f "$accountant_file" "$wallet_file" "$diff_file"
      "${COMPOSE[@]}" logs --tail=200 accountant wallet >&2 || true
      exit 1
    fi

    sleep 2
  done
}

wait_wallet_user_transactions_match_wallet_transactions() {
  local label="$1"

  wait_query_eq "$label: ledger rows reference signed wallet movements" "postgres-wallet" "0" "
    with invalid_user_transactions as (
      select ut.id
      from user_transaction ut
      join transaction t on t.id = ut.tx_id
      join wallet sw on sw.id = t.source_wallet
      join wallet dw on dw.id = t.dest_wallet
      join wallet_owner wo on wo.id = ut.owner_id
      where wo.uuid like 'e2e-%'
        and not (
          (
            ut.category = 'DEPOSIT'
            and t.transfer_category in ('DEPOSIT', 'DEPOSIT_MANUALLY')
            and ut.owner_id = dw.owner
            and ut.currency = dw.currency
            and abs(ut.balance_change - t.dest_amount) <= 0.000001
          )
          or (
            ut.category = 'FEE'
            and t.transfer_category = 'FEE'
            and ut.owner_id = sw.owner
            and ut.currency = sw.currency
            and abs(ut.balance_change + t.source_amount) <= 0.000001
          )
          or (
            ut.category = 'TRADE'
            and t.transfer_category = 'TRADE'
            and (
              (
                ut.owner_id = sw.owner
                and ut.currency = sw.currency
                and abs(ut.balance_change + t.source_amount) <= 0.000001
              )
              or (
                ut.owner_id = dw.owner
                and ut.currency = dw.currency
                and abs(ut.balance_change - t.dest_amount) <= 0.000001
              )
            )
          )
          or (
            ut.category = 'WITHDRAW'
            and t.transfer_category = 'WITHDRAW_ACCEPT'
            and ut.owner_id = sw.owner
            and ut.currency = sw.currency
            and abs(ut.balance_change + t.source_amount) <= 0.000001
          )
        )
    )
    select count(*) from invalid_user_transactions;
  "

  wait_query_eq "$label: required ledger rows exist" "postgres-wallet" "0" "
    with expected_user_transactions as (
      select
        t.id as tx_id,
        dw.owner as owner_id,
        dw.currency,
        t.dest_amount as balance_change,
        'DEPOSIT' as category
      from transaction t
      join wallet dw on dw.id = t.dest_wallet
      join wallet_owner dwo on dwo.id = dw.owner
      where dwo.uuid like 'e2e-%'
        and t.transfer_category in ('DEPOSIT', 'DEPOSIT_MANUALLY')

      union all

      select
        t.id,
        sw.owner,
        sw.currency,
        -t.source_amount,
        'FEE'
      from transaction t
      join wallet sw on sw.id = t.source_wallet
      join wallet_owner swo on swo.id = sw.owner
      where swo.uuid like 'e2e-%'
        and t.transfer_category = 'FEE'

      union all

      select
        t.id,
        sw.owner,
        sw.currency,
        -t.source_amount,
        'WITHDRAW'
      from transaction t
      join wallet sw on sw.id = t.source_wallet
      join wallet_owner swo on swo.id = sw.owner
      where swo.uuid like 'e2e-%'
        and t.transfer_category = 'WITHDRAW_ACCEPT'

      union all

      select
        t.id,
        sw.owner,
        sw.currency,
        -t.source_amount,
        'TRADE'
      from transaction t
      join wallet sw on sw.id = t.source_wallet
      join wallet_owner swo on swo.id = sw.owner
      where swo.uuid like 'e2e-%'
        and t.transfer_category = 'TRADE'

      union all

      select
        t.id,
        dw.owner,
        dw.currency,
        t.dest_amount,
        'TRADE'
      from transaction t
      join wallet dw on dw.id = t.dest_wallet
      join wallet_owner dwo on dwo.id = dw.owner
      where dwo.uuid like 'e2e-%'
        and t.transfer_category = 'TRADE'
    )
    select count(*)
    from expected_user_transactions e
    left join user_transaction ut on ut.tx_id = e.tx_id
      and ut.owner_id = e.owner_id
      and ut.currency = e.currency
      and ut.category = e.category
      and abs(ut.balance_change - e.balance_change) <= 0.000001
    where ut.id is null;
  "

  wait_query_eq "$label: ledger rows are not duplicated" "postgres-wallet" "0" "
    select count(*)
    from (
      select ut.tx_id, ut.owner_id, ut.currency, ut.category, ut.balance_change
      from user_transaction ut
      join wallet_owner wo on wo.id = ut.owner_id
      where wo.uuid like 'e2e-%'
      group by ut.tx_id, ut.owner_id, ut.currency, ut.category, ut.balance_change
      having count(*) > 1
    ) duplicate_user_transactions;
  "
}

wait_eventlog_trades_match_market_projection() {
  local label="$1"
  local deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  local eventlog_file market_file diff_file
  eventlog_file="$(mktemp)"
  market_file="$(mktemp)"
  diff_file="$(mktemp)"

  while true; do
    psql_query "postgres-market" "
      select
        symbol,
        taker_ouid,
        maker_ouid,
        taker_uuid,
        maker_uuid,
        round(maker_price * 100)::bigint,
        round(matched_quantity * 1000000)::bigint,
        count(*)
      from trades
      where maker_uuid like 'e2e-%' or taker_uuid like 'e2e-%'
      group by symbol, taker_ouid, maker_ouid, taker_uuid, maker_uuid, round(maker_price * 100)::bigint, round(matched_quantity * 1000000)::bigint
      order by symbol, taker_ouid, maker_ouid, taker_uuid, maker_uuid, round(maker_price * 100)::bigint, round(matched_quantity * 1000000)::bigint;
    " > "$market_file"

    psql_query "postgres-eventlog" "
      select
        symbol,
        taker_ouid,
        maker_ouid,
        taker_uuid,
        maker_uuid,
        maker_price,
        matched_quantity,
        count(*)
      from opex_trades
      where maker_uuid like 'e2e-%' or taker_uuid like 'e2e-%'
      group by symbol, taker_ouid, maker_ouid, taker_uuid, maker_uuid, maker_price, matched_quantity
      order by symbol, taker_ouid, maker_ouid, taker_uuid, maker_uuid, maker_price, matched_quantity;
    " > "$eventlog_file"

    if diff -u "$market_file" "$eventlog_file" > "$diff_file"; then
      rm -f "$eventlog_file" "$market_file" "$diff_file"
      return 0
    fi

    if (( SECONDS > deadline )); then
      echo "Timed out waiting for $label" >&2
      cat "$diff_file" >&2
      rm -f "$eventlog_file" "$market_file" "$diff_file"
      "${COMPOSE[@]}" logs --tail=200 eventlog market >&2 || true
      exit 1
    fi

    sleep 2
  done
}

main() {
  write_defaults
  cd "$ROOT_DIR"
  configure_java
  require_command jq

  if (( PACKAGE == 1 )); then
    package_apps
  fi

  if (( BUILD == 1 )); then
    "${COMPOSE_FULL_STACK[@]}" build
  fi

  if (( RESET == 1 )); then
    "${COMPOSE_FULL_STACK[@]}" down --volumes --remove-orphans
  fi
  remove_full_stack_residue

  "${COMPOSE[@]}" up -d zookeeper kafka-1
  remove_full_stack_residue
  wait_kafka_broker_ready
  ensure_exchange_topics_ready

  "${COMPOSE[@]}" up -d vault
  wait_vault_e2e_secret

  "${COMPOSE[@]}" up -d

  wait_http "wallet" "http://127.0.0.1:8091/actuator/health"
  wait_http "accountant" "http://127.0.0.1:8089/actuator/health"
  wait_http "matching-engine" "http://127.0.0.1:8092/actuator/health"
  wait_http "matching-gateway" "http://127.0.0.1:8093/actuator/health"
  wait_http "market" "http://127.0.0.1:8096/actuator/health"
  wait_log "accountant" "accountant ETH_USDT order consumer" "orders_ETH_USDT-0"
  wait_log "accountant" "accountant ETH_USDT event consumer" "events_ETH_USDT-0"
  wait_log "accountant" "accountant ETH_USDT trade consumer" "trades_ETH_USDT-0"
  wait_log "accountant" "accountant BTC_USDT order consumer" "orders_BTC_USDT-0"
  wait_log "accountant" "accountant BTC_USDT event consumer" "events_BTC_USDT-0"
  wait_log "accountant" "accountant BTC_USDT trade consumer" "trades_BTC_USDT-0"
  wait_log "accountant" "accountant SOL_USDT order consumer" "orders_SOL_USDT-0"
  wait_log "accountant" "accountant SOL_USDT event consumer" "events_SOL_USDT-0"
  wait_log "accountant" "accountant SOL_USDT trade consumer" "trades_SOL_USDT-0"
  wait_log "accountant" "accountant DOGE_USDT order consumer" "orders_DOGE_USDT-0"
  wait_log "accountant" "accountant DOGE_USDT event consumer" "events_DOGE_USDT-0"
  wait_log "accountant" "accountant DOGE_USDT trade consumer" "trades_DOGE_USDT-0"
  wait_log "accountant" "accountant TON_USDT order consumer" "orders_TON_USDT-0"
  wait_log "accountant" "accountant TON_USDT event consumer" "events_TON_USDT-0"
  wait_log "accountant" "accountant TON_USDT trade consumer" "trades_TON_USDT-0"
  wait_consumer_group_stable "eventlog" "eventlog consumer group"
  wait_log "matching-engine" "matching-engine ETH_USDT order consumer" "orders_ETH_USDT-0"
  wait_log "matching-engine" "matching-engine BTC_USDT order consumer" "orders_BTC_USDT-0"
  wait_log "matching-engine-duo" "matching-engine-duo SOL_USDT order consumer" "orders_SOL_USDT-0"
  wait_log "matching-engine-duo" "matching-engine-duo DOGE_USDT order consumer" "orders_DOGE_USDT-0"
  wait_log "matching-engine-duo" "matching-engine-duo TON_USDT order consumer" "orders_TON_USDT-0"
  wait_log "market" "market richTrade consumer" "richTrade-0"
  wait_log "market" "market richOrder consumer" "richOrder-0"
  sleep 10

  "${COMPOSE[@]}" up -d postgres-bc-gateway bc-gateway
  wait_http "bc-gateway" "http://127.0.0.1:8095/actuator/health"

  "${COMPOSE[@]}" up -d --no-deps api
  wait_http "api" "http://127.0.0.1:8094/actuator/health"
  wait_binance_exchange_info_symbol "ETHUSDT" "ETH" "USDT"

  local api_signed_owner="e2e-api-signed-$(date +%s)"
  local api_signed_now api_signed_stale api_signed_future
  api_signed_now=$(( $(date +%s) * 1000 ))
  api_signed_stale=$(( api_signed_now - 6001 ))
  api_signed_future=$(( api_signed_now + 120000 ))
  expect_http_status "Binance API stale private timestamp rejected" "400" "$(binance_private_get_raw_status "$api_signed_owner" "/v3/account" "timestamp=${api_signed_stale}&recvWindow=5000")" >/tmp/opex-e2e-binance-api-signed-stale-timestamp.json
  expect_http_status "Binance API future private timestamp rejected" "400" "$(binance_private_get_raw_status "$api_signed_owner" "/v3/account" "timestamp=${api_signed_future}&recvWindow=60000")" >/tmp/opex-e2e-binance-api-signed-future-timestamp.json
  expect_http_status "Binance API oversize recvWindow rejected" "400" "$(binance_private_get_raw_status "$api_signed_owner" "/v3/account" "timestamp=${api_signed_now}&recvWindow=60001")" >/tmp/opex-e2e-binance-api-signed-oversize-recv-window.json
  assert_opex_error "Binance API stale private timestamp error" "InvalidRequestParam" 1020 "$(cat /tmp/opex-e2e-binance-api-signed-stale-timestamp.json)"
  assert_opex_error "Binance API future private timestamp error" "InvalidRequestParam" 1020 "$(cat /tmp/opex-e2e-binance-api-signed-future-timestamp.json)"
  assert_opex_error "Binance API oversize recvWindow error" "InvalidRequestParam" 1020 "$(cat /tmp/opex-e2e-binance-api-signed-oversize-recv-window.json)"

  local seller="e2e-seller-$(date +%s)"
  local buyer="e2e-buyer-$(date +%s)"
  local ref="e2e-$(date +%s)"

  expect_2xx "seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/2_test-ethereum_ETH/${seller}_MAIN?description=e2e&transferRef=${ref}-eth")" >/dev/null
  expect_2xx "buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1000_test-ethereum_USDT/${buyer}_MAIN?description=e2e&transferRef=${ref}-usdt")" >/dev/null

  expect_2xx "seller wallet read" "$(curl_json GET "http://127.0.0.1:8091/v1/owner/${seller}/wallets/ETH")" >/tmp/opex-e2e-seller-wallet.json
  expect_2xx "buyer wallet read" "$(curl_json GET "http://127.0.0.1:8091/v1/owner/${buyer}/wallets/USDT")" >/tmp/opex-e2e-buyer-wallet.json

  local ask='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local bid='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":1,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "seller ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$ask' '$seller'" >/tmp/opex-e2e-ask.json
  expect_2xx_retry "buyer bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$bid' '$buyer'" >/tmp/opex-e2e-bid.json

  wait_user_trade_projection "$seller" "ETH_USDT" "100" "1" "100" "1" "USDT" false true false /tmp/opex-e2e-seller-trades.json
  wait_user_trade_projection "$buyer" "ETH_USDT" "100" "1" "100" "0.01" "ETH" true false false /tmp/opex-e2e-buyer-trades.json
  wait_user_order_projection_by_price "$seller" "ETH_USDT" "100" "1" "FILLED" "1" "100" /tmp/opex-e2e-seller-orders.json
  wait_user_order_projection_by_price "$buyer" "ETH_USDT" "100" "1" "FILLED" "1" "100" /tmp/opex-e2e-buyer-orders.json
  wait_binance_recent_trade_level "ETHUSDT" "100" "1" "100" /tmp/opex-e2e-binance-recent-trades.json
  wait_binance_price_ticker "ETHUSDT" "100" /tmp/opex-e2e-binance-price-ticker.json
  wait_binance_24h_ticker "ETHUSDT" "100" "1" /tmp/opex-e2e-binance-24h-ticker.json
  wait_binance_klines_1m_candle "ETHUSDT" "100" "100" "100" "100" "1" "100" "1" "1" "100" /tmp/opex-e2e-binance-klines.json

  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$seller" "USDT" "99" &&
    try_wallet_balance "$seller" "ETH" "1" &&
    try_wallet_balance "$buyer" "ETH" "0.99" &&
    try_wallet_balance "$buyer" "USDT" "900"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for wallet settlement" >&2
      assert_wallet_balance "seller USDT settlement" "$seller" "USDT" "99" >&2 || true
      assert_wallet_balance "seller ETH settlement" "$seller" "ETH" "1" >&2 || true
      assert_wallet_balance "buyer ETH settlement" "$buyer" "ETH" "0.99" >&2 || true
      assert_wallet_balance "buyer USDT settlement" "$buyer" "USDT" "900" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_binance_account_balance "$seller" "USDT" "99" "0" /tmp/opex-e2e-binance-seller-account.json
  wait_binance_account_balance "$buyer" "ETH" "0.99" "0" /tmp/opex-e2e-binance-buyer-account.json
  wait_binance_user_asset_valuation "$seller" "ETH" "USDT" "100" "0" "0" "100" /tmp/opex-e2e-binance-seller-eth-valuation.json
  wait_binance_user_asset_valuation "$buyer" "ETH" "USDT" "99" "0" "0" "100" /tmp/opex-e2e-binance-buyer-eth-valuation.json
  wait_binance_assets_estimated_value "$seller" "USDT" "199" /tmp/opex-e2e-binance-seller-estimated-value.json
  wait_binance_assets_estimated_value "$buyer" "USDT" "999" /tmp/opex-e2e-binance-buyer-estimated-value.json
  wait_binance_private_order_projection "$seller" "ETHUSDT" "100" "1" "FILLED" "1" "100" "SELL" /tmp/opex-e2e-binance-seller-orders.json
  wait_binance_private_order_projection "$buyer" "ETHUSDT" "100" "1" "FILLED" "1" "100" "BUY" /tmp/opex-e2e-binance-buyer-orders.json
  local seller_binance_order_id
  seller_binance_order_id="$(
    jq -r '
      [
        .[] |
        select(
          .symbol == "ETHUSDT" and
          .price == 100 and
          .origQty == 1 and
          .status == "FILLED" and
          .side == "SELL"
        )
      ][0].orderId
    ' /tmp/opex-e2e-binance-seller-orders.json
  )"
  if [[ -z "$seller_binance_order_id" || "$seller_binance_order_id" == "null" ]]; then
    echo "Binance seller order response did not include an orderId required for myTrades orderId verification" >&2
    cat /tmp/opex-e2e-binance-seller-orders.json >&2
    exit 1
  fi
  wait_binance_private_trade_projection "$seller" "ETHUSDT" "100" "1" "100" "1" "USDT" false true /tmp/opex-e2e-binance-seller-my-trades.json
  local seller_binance_trade_id
  seller_binance_trade_id="$(jq -r '.[0].id' /tmp/opex-e2e-binance-seller-my-trades.json)"
  if [[ -z "$seller_binance_trade_id" || "$seller_binance_trade_id" == "null" ]]; then
    echo "Binance seller trade response did not include an id required for fromId verification" >&2
    cat /tmp/opex-e2e-binance-seller-my-trades.json >&2
    exit 1
  fi
  wait_binance_private_trade_projection_from_id "$seller" "ETHUSDT" "$seller_binance_trade_id" "100" "1" "100" "1" "USDT" false true /tmp/opex-e2e-binance-seller-my-trades-from-id.json
  wait_binance_private_trade_projection_for_order_id "$seller" "ETHUSDT" "$seller_binance_order_id" "$seller_binance_trade_id" "100" "1" "100" "1" "USDT" false true /tmp/opex-e2e-binance-seller-my-trades-order-id.json
  wait_binance_all_orders_time_range_includes_order "$seller" "ETHUSDT" "$seller_binance_order_id" /tmp/opex-e2e-binance-seller-all-orders-time-range.json
  wait_binance_all_orders_future_time_range_empty "$seller" "ETHUSDT" /tmp/opex-e2e-binance-seller-all-orders-future-time-range.json
  wait_binance_my_trades_time_range_includes_trade "$seller" "ETHUSDT" "$seller_binance_trade_id" /tmp/opex-e2e-binance-seller-my-trades-time-range.json
  wait_binance_my_trades_future_time_range_empty "$seller" "ETHUSDT" /tmp/opex-e2e-binance-seller-my-trades-future-time-range.json
  wait_binance_private_trade_projection "$buyer" "ETHUSDT" "100" "1" "100" "0.01" "ETH" true false /tmp/opex-e2e-binance-buyer-my-trades.json

  local api_order_seller="e2e-api-order-seller-$(date +%s)"
  local api_order_buyer="e2e-api-order-buyer-$(date +%s)"
  local api_order_ref="e2e-api-order-$(date +%s)"
  expect_2xx "Binance API seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_order_seller}_MAIN?description=e2e-api-order&transferRef=${api_order_ref}-eth")" >/dev/null
  expect_2xx "Binance API buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${api_order_buyer}_MAIN?description=e2e-api-order&transferRef=${api_order_ref}-usdt")" >/dev/null
  expect_2xx_retry "Binance API seller limit ask" "binance_private_post '$api_order_seller' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=101'" >/tmp/opex-e2e-binance-api-ask.json
  wait_user_open_order "$api_order_seller" "ETH_USDT" "101" "0.2" /tmp/opex-e2e-binance-api-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "101" "0.2"
  wait_binance_account_balance "$api_order_seller" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-seller-reserved-account.json
  wait_binance_user_asset_balance "$api_order_seller" "ETH" "0.8" "0.2" "0" /tmp/opex-e2e-binance-api-seller-reserved-asset.json
  expect_2xx_retry "Binance API buyer limit bid" "binance_private_post '$api_order_buyer' '/v3/order' 'symbol=ETHUSDT&side=BUY&type=LIMIT&timeInForce=GTC&quantity=0.2&price=101'" >/tmp/opex-e2e-binance-api-bid.json
  wait_user_trade_projection "$api_order_seller" "ETH_USDT" "101" "0.2" "20.2" "0.202" "USDT" false true false /tmp/opex-e2e-binance-api-seller-trades.json
  wait_user_trade_projection "$api_order_buyer" "ETH_USDT" "101" "0.2" "20.2" "0.002" "ETH" true false false /tmp/opex-e2e-binance-api-buyer-trades.json
  wait_binance_account_balance "$api_order_seller" "ETH" "0.8" "0" /tmp/opex-e2e-binance-api-seller-eth-account.json
  wait_binance_account_balance "$api_order_seller" "USDT" "19.998" "0" /tmp/opex-e2e-binance-api-seller-usdt-account.json
  wait_binance_account_balance "$api_order_buyer" "ETH" "0.198" "0" /tmp/opex-e2e-binance-api-buyer-eth-account.json
  wait_binance_account_balance "$api_order_buyer" "USDT" "79.8" "0" /tmp/opex-e2e-binance-api-buyer-usdt-account.json
  wait_binance_user_asset_balance "$api_order_seller" "ETH" "0.8" "0" "0" /tmp/opex-e2e-binance-api-seller-eth-asset.json
  wait_binance_user_asset_balance "$api_order_seller" "USDT" "19.998" "0" "0" /tmp/opex-e2e-binance-api-seller-usdt-asset.json
  wait_binance_user_asset_balance "$api_order_buyer" "ETH" "0.198" "0" "0" /tmp/opex-e2e-binance-api-buyer-eth-asset.json
  wait_binance_user_asset_balance "$api_order_buyer" "USDT" "79.8" "0" "0" /tmp/opex-e2e-binance-api-buyer-usdt-asset.json
  wait_binance_private_order_projection "$api_order_seller" "ETHUSDT" "101" "0.2" "FILLED" "0.2" "20.2" "SELL" /tmp/opex-e2e-binance-api-seller-orders.json
  wait_binance_private_order_projection "$api_order_buyer" "ETHUSDT" "101" "0.2" "FILLED" "0.2" "20.2" "BUY" /tmp/opex-e2e-binance-api-buyer-orders.json
  wait_binance_private_trade_projection "$api_order_seller" "ETHUSDT" "101" "0.2" "20.2" "0.202" "USDT" false true /tmp/opex-e2e-binance-api-seller-my-trades.json
  wait_binance_private_trade_projection "$api_order_buyer" "ETHUSDT" "101" "0.2" "20.2" "0.002" "ETH" true false /tmp/opex-e2e-binance-api-buyer-my-trades.json
  local api_order_seller_binance_order_id api_order_seller_binance_trade_id
  api_order_seller_binance_order_id="$(
    jq -r '
      [
        .[] |
        select(
          .symbol == "ETHUSDT" and
          .price == 101 and
          .origQty == 0.2 and
          .status == "FILLED" and
          .side == "SELL"
        )
      ][0].orderId
    ' /tmp/opex-e2e-binance-api-seller-orders.json
  )"
  api_order_seller_binance_trade_id="$(jq -r '.[0].id' /tmp/opex-e2e-binance-api-seller-my-trades.json)"
  if [[ -z "$api_order_seller_binance_order_id" || "$api_order_seller_binance_order_id" == "null" ||
        -z "$api_order_seller_binance_trade_id" || "$api_order_seller_binance_trade_id" == "null" ]]; then
    echo "Binance API seller order/trade response did not include ids required for distinct orderId verification" >&2
    cat /tmp/opex-e2e-binance-api-seller-orders.json >&2
    cat /tmp/opex-e2e-binance-api-seller-my-trades.json >&2
    exit 1
  fi
  if [[ "$api_order_seller_binance_order_id" == "$api_order_seller_binance_trade_id" ]]; then
    echo "Binance API orderId and trade id unexpectedly matched; e2e cannot prove orderId filter is distinct from trade id" >&2
    cat /tmp/opex-e2e-binance-api-seller-orders.json >&2
    cat /tmp/opex-e2e-binance-api-seller-my-trades.json >&2
    exit 1
  fi
  wait_binance_private_trade_projection_for_order_id "$api_order_seller" "ETHUSDT" "$api_order_seller_binance_order_id" "$api_order_seller_binance_trade_id" "101" "0.2" "20.2" "0.202" "USDT" false true /tmp/opex-e2e-binance-api-seller-my-trades-order-id.json
  expect_http_status "Binance API filled order-id cancel rejected" "403" "$(binance_private_delete "$api_order_seller" "/v3/order" "symbol=ETHUSDT&orderId=${api_order_seller_binance_order_id}")" >/tmp/opex-e2e-binance-api-filled-order-id-cancel.json
  assert_opex_error "Binance API filled order-id cancel error" "CancelOrderNotAllowed" 7006 "$(cat /tmp/opex-e2e-binance-api-filled-order-id-cancel.json)"
  wait_binance_private_order_status_by_order_id "$api_order_seller" "ETHUSDT" "$api_order_seller_binance_order_id" "101" "0.2" "FILLED" "0.2" "20.2" "SELL" /tmp/opex-e2e-binance-api-filled-order-id-query-after-cancel-reject.json
  wait_binance_account_balance "$api_order_seller" "ETH" "0.8" "0" /tmp/opex-e2e-binance-api-filled-order-id-eth-account-after-cancel-reject.json
  wait_binance_account_balance "$api_order_seller" "USDT" "19.998" "0" /tmp/opex-e2e-binance-api-filled-order-id-usdt-account-after-cancel-reject.json
  wait_binance_private_trades_empty_for_order_id "$api_order_buyer" "ETHUSDT" "$api_order_seller_binance_order_id" /tmp/opex-e2e-binance-api-buyer-seller-order-id-my-trades.json
  expect_http_status "Binance API buyer seller order lookup forbidden" "403" "$(binance_private_get_status "$api_order_buyer" "/v3/order" "symbol=ETHUSDT&orderId=${api_order_seller_binance_order_id}")" >/tmp/opex-e2e-binance-api-buyer-seller-order-id-query.json
  assert_opex_error "Binance API buyer seller order lookup error" "Forbidden" 1004 "$(cat /tmp/opex-e2e-binance-api-buyer-seller-order-id-query.json)"

  local api_self_trade_owner="e2e-api-self-trade-$(date +%s)"
  local api_self_trade_ref="e2e-api-stp-$(date +%s)"
  local api_self_trade_ask_client_id="e2e-api-stp-ask-$(date +%s)"
  local api_self_trade_bid_client_id="e2e-api-stp-bid-$(date +%s)"
  expect_2xx "Binance API self-trade owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_self_trade_owner}_MAIN?description=e2e-api-self-trade&transferRef=${api_self_trade_ref}-eth")" >/dev/null
  expect_2xx "Binance API self-trade owner USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${api_self_trade_owner}_MAIN?description=e2e-api-self-trade&transferRef=${api_self_trade_ref}-usdt")" >/dev/null
  expect_2xx_retry "Binance API self-trade resting ask" "binance_private_post '$api_self_trade_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.4&price=104&newClientOrderId=${api_self_trade_ask_client_id}'" >/tmp/opex-e2e-binance-api-self-trade-ask.json
  wait_user_open_order "$api_self_trade_owner" "ETH_USDT" "104" "0.4" /tmp/opex-e2e-binance-api-self-trade-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "104" "0.4"
  wait_binance_account_balance "$api_self_trade_owner" "ETH" "0.6" "0.4" /tmp/opex-e2e-binance-api-self-trade-ask-eth-account.json
  wait_binance_account_balance "$api_self_trade_owner" "USDT" "100" "0" /tmp/opex-e2e-binance-api-self-trade-ask-usdt-account.json

  local api_self_trade_ask_order_id
  api_self_trade_ask_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-binance-api-self-trade-open-orders.json)"
  if [[ -z "$api_self_trade_ask_order_id" || "$api_self_trade_ask_order_id" == "null" ]]; then
    echo "Binance API self-trade resting ask did not include orderId" >&2
    cat /tmp/opex-e2e-binance-api-self-trade-open-orders.json >&2
    exit 1
  fi
  wait_binance_private_order_status_by_order_id "$api_self_trade_owner" "ETHUSDT" "$api_self_trade_ask_order_id" "104" "0.4" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-self-trade-ask-query-new.json

  expect_2xx_retry "Binance API self-trade crossing bid rejected async" "binance_private_post '$api_self_trade_owner' '/v3/order' 'symbol=ETHUSDT&side=BUY&type=LIMIT&timeInForce=GTC&quantity=0.2&price=104&newClientOrderId=${api_self_trade_bid_client_id}'" >/tmp/opex-e2e-binance-api-self-trade-bid.json
  wait_query_eq "Binance API self-trade bid reject financial action" "postgres-accountant" "1" "
    select count(*)
    from fi_actions
    where event_type = 'RejectOrderEvent'
      and category_name = 'ORDER_CANCEL'
      and sender = '${api_self_trade_owner}'
      and receiver = '${api_self_trade_owner}'
      and symbol = 'USDT'
      and amount = 20.80000000
      and status = 'PROCESSED';
  "
  wait_query_eq "Binance API self-trade reject eventlog audit" "postgres-eventlog" "SELF_TRADE_PREVENTION,PLACE_ORDER,BID,1,0" "
    select event_json::jsonb ->> 'reason',
           event_json::jsonb ->> 'requestedOperation',
           event_json::jsonb ->> 'direction',
           count(*),
           sum(case when event_json is null or event_json = '' then 1 else 0 end)
    from opex_events
    where event = 'RejectOrderEvent'
      and uuid = '$api_self_trade_owner'
    group by event_json::jsonb ->> 'reason',
             event_json::jsonb ->> 'requestedOperation',
             event_json::jsonb ->> 'direction';
  "
  wait_user_open_order "$api_self_trade_owner" "ETH_USDT" "104" "0.4" /tmp/opex-e2e-binance-api-self-trade-still-open-orders.json
  assert_no_user_order_by_price "$api_self_trade_owner" "ETH_USDT" "104" "0.2"
  wait_binance_private_order_status_by_order_id "$api_self_trade_owner" "ETHUSDT" "$api_self_trade_ask_order_id" "104" "0.4" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-self-trade-ask-query-still-new.json
  wait_query_eq "Binance API self-trade prevention emitted no trade" "postgres-market" "0" "
    select count(*)
    from trades
    where symbol = 'ETH_USDT'
      and maker_uuid = '$api_self_trade_owner'
      and taker_uuid = '$api_self_trade_owner';
  "
  wait_binance_account_balance "$api_self_trade_owner" "ETH" "0.6" "0.4" /tmp/opex-e2e-binance-api-self-trade-eth-account-after-reject.json
  wait_binance_account_balance "$api_self_trade_owner" "USDT" "100" "0" /tmp/opex-e2e-binance-api-self-trade-usdt-account-after-reject.json

  expect_2xx_retry "Binance API self-trade cleanup ask cancel" "binance_private_delete '$api_self_trade_owner' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_self_trade_ask_client_id}'" >/tmp/opex-e2e-binance-api-self-trade-cancel.json
  jq -e \
    --argjson orderId "$api_self_trade_ask_order_id" \
    '.symbol == "ETHUSDT" and .orderId == $orderId and .status == "CANCELED" and .side == "SELL" and .type == "LIMIT"' \
    /tmp/opex-e2e-binance-api-self-trade-cancel.json >/dev/null
  wait_no_user_open_orders "$api_self_trade_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_private_order_status_by_order_id "$api_self_trade_owner" "ETHUSDT" "$api_self_trade_ask_order_id" "104" "0.4" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-self-trade-ask-query-canceled.json
  wait_binance_account_balance "$api_self_trade_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-self-trade-eth-account-released.json
  wait_binance_account_balance "$api_self_trade_owner" "USDT" "100" "0" /tmp/opex-e2e-binance-api-self-trade-usdt-account-released.json

  local api_underfunded_owner="e2e-api-underfunded-$(date +%s)"
  local api_underfunded_ref="e2e-api-underfunded-$(date +%s)"
  local api_underfunded_ask_client_id="e2e-api-underfunded-ask-$(date +%s)"
  local api_underfunded_bid_client_id="e2e-api-underfunded-bid-$(date +%s)"
  expect_2xx "Binance API underfunded owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/0.5_test-ethereum_ETH/${api_underfunded_owner}_MAIN?description=e2e-api-underfunded&transferRef=${api_underfunded_ref}-eth")" >/dev/null
  expect_2xx "Binance API underfunded owner USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/50_test-ethereum_USDT/${api_underfunded_owner}_MAIN?description=e2e-api-underfunded&transferRef=${api_underfunded_ref}-usdt")" >/dev/null
  wait_binance_account_balance "$api_underfunded_owner" "ETH" "0.5" "0" /tmp/opex-e2e-binance-api-underfunded-initial-eth-account.json
  wait_binance_account_balance "$api_underfunded_owner" "USDT" "50" "0" /tmp/opex-e2e-binance-api-underfunded-initial-usdt-account.json
  expect_http_status "Binance API underfunded limit ask rejected" "400" "$(binance_private_post "$api_underfunded_owner" "/v3/order" "symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=1&price=105&newClientOrderId=${api_underfunded_ask_client_id}")" >/tmp/opex-e2e-binance-api-underfunded-ask.json
  expect_http_status "Binance API underfunded limit bid rejected" "400" "$(binance_private_post "$api_underfunded_owner" "/v3/order" "symbol=ETHUSDT&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=105&newClientOrderId=${api_underfunded_bid_client_id}")" >/tmp/opex-e2e-binance-api-underfunded-bid.json
  assert_opex_error "Binance API underfunded limit ask error" "SubmitOrderForbiddenByAccountant" 4001 "$(cat /tmp/opex-e2e-binance-api-underfunded-ask.json)"
  assert_opex_error "Binance API underfunded limit bid error" "SubmitOrderForbiddenByAccountant" 4001 "$(cat /tmp/opex-e2e-binance-api-underfunded-bid.json)"
  wait_binance_private_no_open_orders "$api_underfunded_owner" "ETHUSDT" /tmp/opex-e2e-binance-api-underfunded-open-orders.json
  wait_binance_private_no_all_orders "$api_underfunded_owner" "ETHUSDT" /tmp/opex-e2e-binance-api-underfunded-all-orders.json
  assert_no_user_orders "$api_underfunded_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  wait_query_eq "Binance API underfunded orders emitted no eventlog orders" "postgres-eventlog" "0" "
    select count(*)
    from opex_events
    where uuid = '$api_underfunded_owner'
      and event in ('CreateOrderEvent', 'RejectOrderEvent', 'CancelOrderEvent', 'TradeEvent', 'UpdatedOrderEvent');
  "
  wait_binance_account_balance "$api_underfunded_owner" "ETH" "0.5" "0" /tmp/opex-e2e-binance-api-underfunded-eth-account.json
  wait_binance_account_balance "$api_underfunded_owner" "USDT" "50" "0" /tmp/opex-e2e-binance-api-underfunded-usdt-account.json

  local api_market_no_liq_seller="e2e-api-market-no-liq-s-$(date +%s)"
  local api_market_no_liq_ref="e2e-api-mkt-no-liq-$(date +%s)"
  local api_market_no_liq_client_id="e2e-mkt-no-liq-s-$(date +%s)"
  expect_2xx "Binance API market no-liquidity seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_market_no_liq_seller}_MAIN?description=e2e-api-market-no-liq&transferRef=${api_market_no_liq_ref}-eth")" >/dev/null
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  expect_2xx_retry "Binance API market seller ask no liquidity" "binance_private_post '$api_market_no_liq_seller' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=MARKET&quantity=0.2&newClientOrderId=${api_market_no_liq_client_id}'" >/tmp/opex-e2e-binance-api-market-no-liq-ask.json
  jq -e \
    --arg clientOrderId "$api_market_no_liq_client_id" '
      .symbol == "ETHUSDT" and
      (.orderId | type == "number") and
      .orderId > 0 and
      .orderListId == -1 and
      .clientOrderId == $clientOrderId and
      .price == 0 and
      .origQty == 0.2 and
      .executedQty == 0 and
      .cummulativeQuoteQty == 0 and
      .status == "CANCELED" and
      .type == "MARKET" and
      .side == "SELL" and
      (.transactTime | type == "number") and
      (.fills | type == "array") and
      (.fills | length == 0)
    ' /tmp/opex-e2e-binance-api-market-no-liq-ask.json >/dev/null
  local api_market_no_liq_order_id
  api_market_no_liq_order_id="$(jq -r '.orderId' /tmp/opex-e2e-binance-api-market-no-liq-ask.json)"
  wait_no_user_open_orders "$api_market_no_liq_seller" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  wait_binance_private_order_status_by_client_order_id "$api_market_no_liq_seller" "ETHUSDT" "$api_market_no_liq_client_id" "0" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-market-no-liq-query-canceled.json "MARKET"
  wait_binance_private_trades_empty_for_order_id "$api_market_no_liq_seller" "ETHUSDT" "$api_market_no_liq_order_id" /tmp/opex-e2e-binance-api-market-no-liq-my-trades.json
  wait_binance_account_balance "$api_market_no_liq_seller" "ETH" "1" "0" /tmp/opex-e2e-binance-api-market-no-liq-seller-eth-account.json

  local api_market_seller="e2e-api-market-s-$(date +%s)"
  local api_market_buyer="e2e-api-market-b-$(date +%s)"
  local api_market_ref="e2e-api-mkt-$(date +%s)"
  local api_market_seller_client_id="e2e-mkt-s-$(date +%s)"
  expect_2xx "Binance API market seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_market_seller}_MAIN?description=e2e-api-market&transferRef=${api_market_ref}-eth")" >/dev/null
  expect_2xx "Binance API market buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/40_test-ethereum_USDT/${api_market_buyer}_MAIN?description=e2e-api-market&transferRef=${api_market_ref}-usdt")" >/dev/null
  expect_2xx_retry "Binance API market maker bid" "binance_private_post '$api_market_buyer' '/v3/order' 'symbol=ETHUSDT&side=BUY&type=LIMIT&timeInForce=GTC&quantity=0.3&price=102'" >/tmp/opex-e2e-binance-api-market-bid.json
  wait_user_open_order "$api_market_buyer" "ETH_USDT" "102" "0.3" /tmp/opex-e2e-binance-api-market-open-orders.json
  local api_market_bid_order_id
  api_market_bid_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-binance-api-market-open-orders.json)"
  if [[ -z "$api_market_bid_order_id" || "$api_market_bid_order_id" == "null" ]]; then
    echo "Binance API market maker bid did not include orderId" >&2
    cat /tmp/opex-e2e-binance-api-market-open-orders.json >&2
    exit 1
  fi
  wait_order_book_level "ETH_USDT" "BID" "102" "0.3"
  wait_binance_account_balance "$api_market_buyer" "USDT" "9.4" "30.6" /tmp/opex-e2e-binance-api-market-buyer-reserved-usdt.json
  expect_2xx_retry "Binance API market seller ask" "binance_private_post '$api_market_seller' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=MARKET&quantity=0.2&newClientOrderId=${api_market_seller_client_id}'" >/tmp/opex-e2e-binance-api-market-ask.json
  jq -e \
    --arg clientOrderId "$api_market_seller_client_id" '
      .symbol == "ETHUSDT" and
      (.orderId | type == "number") and
      .orderId > 0 and
      .orderListId == -1 and
      .clientOrderId == $clientOrderId and
      .price == 0 and
      .origQty == 0.2 and
      .executedQty == 0.2 and
      .cummulativeQuoteQty == 20.4 and
      .status == "FILLED" and
      .type == "MARKET" and
      .side == "SELL" and
      (.transactTime | type == "number") and
      (.fills | length == 1) and
      .fills[0].price == 102 and
      .fills[0].qty == 0.2 and
      .fills[0].commission == 0.204 and
      .fills[0].commissionAsset == "USDT"
    ' /tmp/opex-e2e-binance-api-market-ask.json >/dev/null
  wait_user_trade_projection "$api_market_seller" "ETH_USDT" "102" "0.2" "20.4" "0.204" "USDT" false false true /tmp/opex-e2e-binance-api-market-seller-trades.json
  wait_user_trade_projection "$api_market_buyer" "ETH_USDT" "102" "0.2" "20.4" "0.002" "ETH" true true true /tmp/opex-e2e-binance-api-market-buyer-trades.json
  wait_binance_private_order_status_by_client_order_id "$api_market_seller" "ETHUSDT" "$api_market_seller_client_id" "0" "0.2" "FILLED" "0.2" "20.4" "SELL" /tmp/opex-e2e-binance-api-market-seller-query-filled.json "MARKET"
  wait_binance_private_order_status_by_order_id "$api_market_buyer" "ETHUSDT" "$api_market_bid_order_id" "102" "0.3" "PARTIALLY_FILLED" "0.2" "20.4" "BUY" /tmp/opex-e2e-binance-api-market-buyer-query-partial.json
  wait_order_book_level "ETH_USDT" "BID" "102" "0.1"
  wait_binance_account_balance "$api_market_seller" "ETH" "0.8" "0" /tmp/opex-e2e-binance-api-market-seller-eth-account.json
  wait_binance_account_balance "$api_market_seller" "USDT" "20.196" "0" /tmp/opex-e2e-binance-api-market-seller-usdt-account.json
  wait_binance_account_balance "$api_market_buyer" "ETH" "0.198" "0" /tmp/opex-e2e-binance-api-market-buyer-eth-account.json
  wait_binance_account_balance "$api_market_buyer" "USDT" "9.4" "10.2" /tmp/opex-e2e-binance-api-market-buyer-partial-usdt-account.json
  expect_2xx_retry "Binance API market maker order-id cancel" "binance_private_delete '$api_market_buyer' '/v3/order' 'symbol=ETHUSDT&orderId=${api_market_bid_order_id}'" >/tmp/opex-e2e-binance-api-market-cancel-response.json
  assert_binance_cancel_response "Binance API market maker order-id" /tmp/opex-e2e-binance-api-market-cancel-response.json "ETHUSDT" "$api_market_bid_order_id" "102" "0.3" "0.2" "20.4" "BUY"
  wait_no_user_open_orders "$api_market_buyer" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "BID"
  wait_binance_private_order_status_by_order_id "$api_market_buyer" "ETHUSDT" "$api_market_bid_order_id" "102" "0.3" "CANCELED" "0.2" "20.4" "BUY" /tmp/opex-e2e-binance-api-market-buyer-query-canceled.json
  wait_binance_account_balance "$api_market_buyer" "USDT" "19.6" "0" /tmp/opex-e2e-binance-api-market-buyer-released-usdt-account.json

  local api_market_buy_seller="e2e-api-market-buy-s-$(date +%s)"
  local api_market_buy_buyer="e2e-api-market-buy-b-$(date +%s)"
  local api_market_buy_ref="e2e-api-mkt-buy-$(date +%s)"
  local api_market_buy_client_id="e2e-mkt-b-$(date +%s)"
  local api_market_buy_no_liq_client_id="e2e-mkt-b-no-liq-$(date +%s)"
  expect_2xx "Binance API market-buy seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_market_buy_seller}_MAIN?description=e2e-api-market-buy&transferRef=${api_market_buy_ref}-eth")" >/dev/null
  expect_2xx "Binance API market-buy buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/40_test-ethereum_USDT/${api_market_buy_buyer}_MAIN?description=e2e-api-market-buy&transferRef=${api_market_buy_ref}-usdt")" >/dev/null
  expect_http_status "Binance API market-buy without price cap rejected" "400" "$(binance_private_post "$api_market_buy_buyer" "/v3/order" "symbol=ETHUSDT&side=BUY&type=MARKET&quantity=0.2&newClientOrderId=${api_market_buy_client_id}-no-cap")" >/tmp/opex-e2e-binance-api-market-buy-no-cap-reject.json
  assert_opex_error "Binance API market-buy without price cap error" "InvalidRequestParam" 1020 "$(cat /tmp/opex-e2e-binance-api-market-buy-no-cap-reject.json)"
  wait_no_user_open_orders "$api_market_buy_buyer" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  wait_binance_account_balance "$api_market_buy_buyer" "USDT" "40" "0" /tmp/opex-e2e-binance-api-market-buy-no-cap-buyer-usdt-account.json
  expect_2xx_retry "Binance API market-buy bid no liquidity" "binance_private_post '$api_market_buy_buyer' '/v3/order' 'symbol=ETHUSDT&side=BUY&type=MARKET&quantity=0.2&price=103&newClientOrderId=${api_market_buy_no_liq_client_id}'" >/tmp/opex-e2e-binance-api-market-buy-no-liq-bid.json
  jq -e \
    --arg clientOrderId "$api_market_buy_no_liq_client_id" '
      .symbol == "ETHUSDT" and
      (.orderId | type == "number") and
      .orderId > 0 and
      .orderListId == -1 and
      .clientOrderId == $clientOrderId and
      .price == 103 and
      .origQty == 0.2 and
      .executedQty == 0 and
      .cummulativeQuoteQty == 0 and
      .status == "CANCELED" and
      .type == "MARKET" and
      .side == "BUY" and
      (.transactTime | type == "number") and
      (.fills | type == "array") and
      (.fills | length == 0)
    ' /tmp/opex-e2e-binance-api-market-buy-no-liq-bid.json >/dev/null
  local api_market_buy_no_liq_order_id
  api_market_buy_no_liq_order_id="$(jq -r '.orderId' /tmp/opex-e2e-binance-api-market-buy-no-liq-bid.json)"
  wait_no_user_open_orders "$api_market_buy_buyer" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  wait_binance_private_order_status_by_client_order_id "$api_market_buy_buyer" "ETHUSDT" "$api_market_buy_no_liq_client_id" "103" "0.2" "CANCELED" "0" "0" "BUY" /tmp/opex-e2e-binance-api-market-buy-no-liq-query-canceled.json "MARKET"
  wait_binance_private_trades_empty_for_order_id "$api_market_buy_buyer" "ETHUSDT" "$api_market_buy_no_liq_order_id" /tmp/opex-e2e-binance-api-market-buy-no-liq-my-trades.json
  wait_binance_account_balance "$api_market_buy_buyer" "USDT" "40" "0" /tmp/opex-e2e-binance-api-market-buy-no-liq-buyer-usdt-account.json
  expect_2xx_retry "Binance API market-buy maker ask" "binance_private_post '$api_market_buy_seller' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.3&price=103'" >/tmp/opex-e2e-binance-api-market-buy-ask.json
  wait_user_open_order "$api_market_buy_seller" "ETH_USDT" "103" "0.3" /tmp/opex-e2e-binance-api-market-buy-open-orders.json
  local api_market_buy_ask_order_id
  api_market_buy_ask_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-binance-api-market-buy-open-orders.json)"
  if [[ -z "$api_market_buy_ask_order_id" || "$api_market_buy_ask_order_id" == "null" ]]; then
    echo "Binance API market-buy maker ask did not include orderId" >&2
    cat /tmp/opex-e2e-binance-api-market-buy-open-orders.json >&2
    exit 1
  fi
  wait_order_book_level "ETH_USDT" "ASK" "103" "0.3"
  wait_binance_account_balance "$api_market_buy_seller" "ETH" "0.7" "0.3" /tmp/opex-e2e-binance-api-market-buy-seller-reserved-eth.json
  expect_2xx_retry "Binance API market buyer bid" "binance_private_post '$api_market_buy_buyer' '/v3/order' 'symbol=ETHUSDT&side=BUY&type=MARKET&quantity=0.2&price=103&newClientOrderId=${api_market_buy_client_id}&newOrderRespType=FULL'" >/tmp/opex-e2e-binance-api-market-buy-bid.json
  jq -e \
    --arg clientOrderId "$api_market_buy_client_id" '
      .symbol == "ETHUSDT" and
      (.orderId | type == "number") and
      .orderId > 0 and
      .orderListId == -1 and
      .clientOrderId == $clientOrderId and
      .price == 103 and
      .origQty == 0.2 and
      .executedQty == 0.2 and
      .cummulativeQuoteQty == 20.6 and
      .status == "FILLED" and
      .type == "MARKET" and
      .side == "BUY" and
      (.transactTime | type == "number") and
      (.fills | length == 1) and
      .fills[0].price == 103 and
      .fills[0].qty == 0.2 and
      .fills[0].commission == 0.002 and
      .fills[0].commissionAsset == "ETH"
    ' /tmp/opex-e2e-binance-api-market-buy-bid.json >/dev/null
  wait_user_trade_projection "$api_market_buy_seller" "ETH_USDT" "103" "0.2" "20.6" "0.206" "USDT" false true false /tmp/opex-e2e-binance-api-market-buy-seller-trades.json
  wait_user_trade_projection "$api_market_buy_buyer" "ETH_USDT" "103" "0.2" "20.6" "0.002" "ETH" true false false /tmp/opex-e2e-binance-api-market-buy-buyer-trades.json
  wait_binance_private_order_status_by_client_order_id "$api_market_buy_buyer" "ETHUSDT" "$api_market_buy_client_id" "103" "0.2" "FILLED" "0.2" "20.6" "BUY" /tmp/opex-e2e-binance-api-market-buy-buyer-query-filled.json "MARKET"
  wait_binance_private_order_status_by_order_id "$api_market_buy_seller" "ETHUSDT" "$api_market_buy_ask_order_id" "103" "0.3" "PARTIALLY_FILLED" "0.2" "20.6" "SELL" /tmp/opex-e2e-binance-api-market-buy-seller-query-partial.json
  wait_order_book_level "ETH_USDT" "ASK" "103" "0.1"
  wait_binance_account_balance "$api_market_buy_buyer" "ETH" "0.198" "0" /tmp/opex-e2e-binance-api-market-buy-buyer-eth-account.json
  wait_binance_account_balance "$api_market_buy_buyer" "USDT" "19.4" "0" /tmp/opex-e2e-binance-api-market-buy-buyer-usdt-account.json
  wait_binance_account_balance "$api_market_buy_seller" "ETH" "0.7" "0.1" /tmp/opex-e2e-binance-api-market-buy-seller-partial-eth-account.json
  wait_binance_account_balance "$api_market_buy_seller" "USDT" "20.394" "0" /tmp/opex-e2e-binance-api-market-buy-seller-usdt-account.json
  expect_2xx_retry "Binance API market-buy maker order-id cancel" "binance_private_delete '$api_market_buy_seller' '/v3/order' 'symbol=ETHUSDT&orderId=${api_market_buy_ask_order_id}'" >/tmp/opex-e2e-binance-api-market-buy-cancel-response.json
  assert_binance_cancel_response "Binance API market-buy maker order-id" /tmp/opex-e2e-binance-api-market-buy-cancel-response.json "ETHUSDT" "$api_market_buy_ask_order_id" "103" "0.3" "0.2" "20.6" "SELL"
  wait_no_user_open_orders "$api_market_buy_seller" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_private_order_status_by_order_id "$api_market_buy_seller" "ETHUSDT" "$api_market_buy_ask_order_id" "103" "0.3" "CANCELED" "0.2" "20.6" "SELL" /tmp/opex-e2e-binance-api-market-buy-seller-query-canceled.json
  wait_binance_account_balance "$api_market_buy_seller" "ETH" "0.8" "0" /tmp/opex-e2e-binance-api-market-buy-seller-released-eth-account.json

  local api_ioc_limit_owner="e2e-api-ioc-limit-$(date +%s)"
  local api_ioc_limit_ref="e2e-api-ioc-limit-$(date +%s)"
  expect_2xx "Binance API IOC limit owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_ioc_limit_owner}_MAIN?description=e2e-api-ioc-limit&transferRef=${api_ioc_limit_ref}-eth")" >/dev/null
  expect_2xx_retry "Binance API IOC limit ask without liquidity" "binance_private_post '$api_ioc_limit_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=IOC&quantity=0.2&price=171'" >/tmp/opex-e2e-binance-api-ioc-limit-ask.json
  wait_no_user_open_orders "$api_ioc_limit_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_private_order_projection "$api_ioc_limit_owner" "ETHUSDT" "171" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-ioc-limit-orders.json
  wait_binance_account_balance "$api_ioc_limit_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-ioc-limit-owner-released-eth-account.json

  local api_ioc_partial_seller="e2e-api-ioc-partial-s-$(date +%s)"
  local api_ioc_partial_buyer="e2e-api-ioc-partial-b-$(date +%s)"
  local api_ioc_partial_ref="e2e-api-ioc-partial-$(date +%s)"
  expect_2xx "Binance API IOC partial seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_ioc_partial_seller}_MAIN?description=e2e-api-ioc-partial&transferRef=${api_ioc_partial_ref}-eth")" >/dev/null
  expect_2xx "Binance API IOC partial buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${api_ioc_partial_buyer}_MAIN?description=e2e-api-ioc-partial&transferRef=${api_ioc_partial_ref}-usdt")" >/dev/null
  expect_2xx_retry "Binance API IOC partial maker ask" "binance_private_post '$api_ioc_partial_seller' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=172'" >/tmp/opex-e2e-binance-api-ioc-partial-ask.json
  wait_user_open_order "$api_ioc_partial_seller" "ETH_USDT" "172" "0.2" /tmp/opex-e2e-binance-api-ioc-partial-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "172" "0.2"
  wait_binance_account_balance "$api_ioc_partial_seller" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-ioc-partial-seller-reserved-eth.json
  expect_2xx_retry "Binance API IOC partial taker bid" "binance_private_post '$api_ioc_partial_buyer' '/v3/order' 'symbol=ETHUSDT&side=BUY&type=LIMIT&timeInForce=IOC&quantity=0.5&price=172'" >/tmp/opex-e2e-binance-api-ioc-partial-bid.json
  jq -e '
    .symbol == "ETHUSDT" and
    (.orderId | type == "number") and
    .orderListId == -1 and
    (.clientOrderId | type == "string") and
    .price == 172 and
    .origQty == 0.5 and
    .executedQty == 0.2 and
    .cummulativeQuoteQty == 34.4 and
    .status == "CANCELED" and
    .timeInForce == "IOC" and
    .type == "LIMIT" and
    .side == "BUY" and
    (.fills | length == 1) and
    .fills[0].price == 172 and
    .fills[0].qty == 0.2 and
    .fills[0].commission == 0.002 and
    .fills[0].commissionAsset == "ETH"
  ' /tmp/opex-e2e-binance-api-ioc-partial-bid.json >/dev/null
  wait_user_trade_projection "$api_ioc_partial_seller" "ETH_USDT" "172" "0.2" "34.4" "0.344" "USDT" false true false /tmp/opex-e2e-binance-api-ioc-partial-seller-trades.json
  wait_user_trade_projection "$api_ioc_partial_buyer" "ETH_USDT" "172" "0.2" "34.4" "0.002" "ETH" true false false /tmp/opex-e2e-binance-api-ioc-partial-buyer-trades.json
  wait_no_user_open_orders "$api_ioc_partial_seller" "ETH_USDT"
  wait_no_user_open_orders "$api_ioc_partial_buyer" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  wait_binance_private_order_projection "$api_ioc_partial_seller" "ETHUSDT" "172" "0.2" "FILLED" "0.2" "34.4" "SELL" /tmp/opex-e2e-binance-api-ioc-partial-seller-orders.json
  wait_binance_private_order_projection "$api_ioc_partial_buyer" "ETHUSDT" "172" "0.5" "CANCELED" "0.2" "34.4" "BUY" /tmp/opex-e2e-binance-api-ioc-partial-buyer-orders.json
  wait_binance_account_balance "$api_ioc_partial_seller" "ETH" "0.8" "0" /tmp/opex-e2e-binance-api-ioc-partial-seller-eth-account.json
  wait_binance_account_balance "$api_ioc_partial_seller" "USDT" "34.056" "0" /tmp/opex-e2e-binance-api-ioc-partial-seller-usdt-account.json
  wait_binance_account_balance "$api_ioc_partial_buyer" "ETH" "0.198" "0" /tmp/opex-e2e-binance-api-ioc-partial-buyer-eth-account.json
  wait_binance_account_balance "$api_ioc_partial_buyer" "USDT" "65.6" "0" /tmp/opex-e2e-binance-api-ioc-partial-buyer-usdt-account.json

  local api_ioc_partial_sell_seller="e2e-api-iocps-s-$(date +%s)"
  local api_ioc_partial_sell_buyer="e2e-api-iocps-b-$(date +%s)"
  local api_ioc_partial_sell_ref="e2e-api-iocps-$(date +%s)"
  expect_2xx "Binance API IOC partial-sell seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_ioc_partial_sell_seller}_MAIN?description=e2e-api-ioc-partial-sell&transferRef=${api_ioc_partial_sell_ref}-eth")" >/dev/null
  expect_2xx "Binance API IOC partial-sell buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${api_ioc_partial_sell_buyer}_MAIN?description=e2e-api-ioc-partial-sell&transferRef=${api_ioc_partial_sell_ref}-usdt")" >/dev/null
  expect_2xx_retry "Binance API IOC partial-sell maker bid" "binance_private_post '$api_ioc_partial_sell_buyer' '/v3/order' 'symbol=ETHUSDT&side=BUY&type=LIMIT&timeInForce=GTC&quantity=0.2&price=173'" >/tmp/opex-e2e-binance-api-ioc-partial-sell-bid.json
  wait_user_open_order "$api_ioc_partial_sell_buyer" "ETH_USDT" "173" "0.2" /tmp/opex-e2e-binance-api-ioc-partial-sell-open-orders.json
  wait_order_book_level "ETH_USDT" "BID" "173" "0.2"
  wait_binance_account_balance "$api_ioc_partial_sell_buyer" "USDT" "65.4" "34.6" /tmp/opex-e2e-binance-api-ioc-partial-sell-buyer-reserved-usdt.json
  expect_2xx_retry "Binance API IOC partial-sell taker ask" "binance_private_post '$api_ioc_partial_sell_seller' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=IOC&quantity=0.5&price=173'" >/tmp/opex-e2e-binance-api-ioc-partial-sell-ask.json
  jq -e '
    .symbol == "ETHUSDT" and
    (.orderId | type == "number") and
    .orderListId == -1 and
    (.clientOrderId | type == "string") and
    .price == 173 and
    .origQty == 0.5 and
    .executedQty == 0.2 and
    .cummulativeQuoteQty == 34.6 and
    .status == "CANCELED" and
    .timeInForce == "IOC" and
    .type == "LIMIT" and
    .side == "SELL" and
    (.fills | length == 1) and
    .fills[0].price == 173 and
    .fills[0].qty == 0.2 and
    .fills[0].commission == 0.346 and
    .fills[0].commissionAsset == "USDT"
  ' /tmp/opex-e2e-binance-api-ioc-partial-sell-ask.json >/dev/null
  wait_user_trade_projection "$api_ioc_partial_sell_seller" "ETH_USDT" "173" "0.2" "34.6" "0.346" "USDT" false false true /tmp/opex-e2e-binance-api-ioc-partial-sell-seller-trades.json
  wait_user_trade_projection "$api_ioc_partial_sell_buyer" "ETH_USDT" "173" "0.2" "34.6" "0.002" "ETH" true true true /tmp/opex-e2e-binance-api-ioc-partial-sell-buyer-trades.json
  wait_no_user_open_orders "$api_ioc_partial_sell_seller" "ETH_USDT"
  wait_no_user_open_orders "$api_ioc_partial_sell_buyer" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  wait_binance_private_order_projection "$api_ioc_partial_sell_seller" "ETHUSDT" "173" "0.5" "CANCELED" "0.2" "34.6" "SELL" /tmp/opex-e2e-binance-api-ioc-partial-sell-seller-orders.json
  wait_binance_private_order_projection "$api_ioc_partial_sell_buyer" "ETHUSDT" "173" "0.2" "FILLED" "0.2" "34.6" "BUY" /tmp/opex-e2e-binance-api-ioc-partial-sell-buyer-orders.json
  wait_binance_account_balance "$api_ioc_partial_sell_seller" "ETH" "0.8" "0" /tmp/opex-e2e-binance-api-ioc-partial-sell-seller-eth-account.json
  wait_binance_account_balance "$api_ioc_partial_sell_seller" "USDT" "34.254" "0" /tmp/opex-e2e-binance-api-ioc-partial-sell-seller-usdt-account.json
  wait_binance_account_balance "$api_ioc_partial_sell_buyer" "ETH" "0.198" "0" /tmp/opex-e2e-binance-api-ioc-partial-sell-buyer-eth-account.json
  wait_binance_account_balance "$api_ioc_partial_sell_buyer" "USDT" "65.4" "0" /tmp/opex-e2e-binance-api-ioc-partial-sell-buyer-usdt-account.json

  local api_cancel_owner="e2e-api-cancel-$(date +%s)"
  local api_cancel_ref="e2e-api-cancel-$(date +%s)"
  expect_2xx "Binance API cancel ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_cancel_owner}_MAIN?description=e2e-api-cancel&transferRef=${api_cancel_ref}-eth")" >/dev/null
  expect_2xx_retry "Binance API cancel owner limit ask" "binance_private_post '$api_cancel_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.3&price=160'" >/tmp/opex-e2e-binance-api-cancel-ask.json
  wait_user_open_order "$api_cancel_owner" "ETH_USDT" "160" "0.3" /tmp/opex-e2e-binance-api-cancel-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "160" "0.3"
  wait_binance_account_balance "$api_cancel_owner" "ETH" "0.7" "0.3" /tmp/opex-e2e-binance-api-cancel-reserved-account.json

  local api_cancel_order_id
  api_cancel_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-binance-api-cancel-open-orders.json)"
  if [[ -z "$api_cancel_order_id" || "$api_cancel_order_id" == "null" ]]; then
    echo "Binance API cancel open order did not include orderId" >&2
    cat /tmp/opex-e2e-binance-api-cancel-open-orders.json >&2
    exit 1
  fi
  wait_binance_private_order_status_by_order_id "$api_cancel_owner" "ETHUSDT" "$api_cancel_order_id" "160" "0.3" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-cancel-query-new.json
  expect_http_status "Binance API intruder order-id cancel forbidden" "403" "$(binance_private_delete "${api_cancel_owner}-intruder" "/v3/order" "symbol=ETHUSDT&orderId=${api_cancel_order_id}")" >/tmp/opex-e2e-binance-api-cancel-intruder-order-id-cancel.json
  assert_opex_error "Binance API intruder order-id cancel error" "Forbidden" 1004 "$(cat /tmp/opex-e2e-binance-api-cancel-intruder-order-id-cancel.json)"
  wait_binance_private_order_status_by_order_id "$api_cancel_owner" "ETHUSDT" "$api_cancel_order_id" "160" "0.3" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-cancel-query-still-new-after-intruder.json
  wait_order_book_level "ETH_USDT" "ASK" "160" "0.3"
  wait_binance_account_balance "$api_cancel_owner" "ETH" "0.7" "0.3" /tmp/opex-e2e-binance-api-cancel-still-reserved-account.json
  expect_2xx_retry "Binance API cancel owner limit ask" "binance_private_delete '$api_cancel_owner' '/v3/order' 'symbol=ETHUSDT&orderId=${api_cancel_order_id}'" >/tmp/opex-e2e-binance-api-cancel-response.json
  assert_binance_cancel_response "Binance API cancel owner limit ask" /tmp/opex-e2e-binance-api-cancel-response.json "ETHUSDT" "$api_cancel_order_id" "160" "0.3" "0" "0" "SELL"
  wait_no_user_open_orders "$api_cancel_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_cancel_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-cancel-released-account.json
  wait_binance_private_order_status_by_order_id "$api_cancel_owner" "ETHUSDT" "$api_cancel_order_id" "160" "0.3" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-cancel-query-canceled.json
  expect_2xx_retry "Binance API duplicate order-id cancel remains canceled" "binance_private_delete '$api_cancel_owner' '/v3/order' 'symbol=ETHUSDT&orderId=${api_cancel_order_id}'" >/tmp/opex-e2e-binance-api-cancel-duplicate-response.json
  assert_binance_cancel_response "Binance API duplicate order-id cancel" /tmp/opex-e2e-binance-api-cancel-duplicate-response.json "ETHUSDT" "$api_cancel_order_id" "160" "0.3" "0" "0" "SELL"
  wait_no_user_open_orders "$api_cancel_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_cancel_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-cancel-duplicate-account.json
  wait_binance_private_order_status_by_order_id "$api_cancel_owner" "ETHUSDT" "$api_cancel_order_id" "160" "0.3" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-cancel-query-still-canceled-after-duplicate.json
  wait_binance_private_order_projection "$api_cancel_owner" "ETHUSDT" "160" "0.3" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-cancel-orders.json

  local api_partial_cancel_seller="e2e-api-pfc-s-$(date +%s)"
  local api_partial_cancel_buyer="e2e-api-pfc-b-$(date +%s)"
  local api_partial_cancel_ref="e2e-api-pfc-$(date +%s)"
  expect_2xx "Binance API partial-cancel seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_partial_cancel_seller}_MAIN?description=e2e-api-partial-cancel&transferRef=${api_partial_cancel_ref}-eth")" >/dev/null
  expect_2xx "Binance API partial-cancel buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/40_test-ethereum_USDT/${api_partial_cancel_buyer}_MAIN?description=e2e-api-partial-cancel&transferRef=${api_partial_cancel_ref}-usdt")" >/dev/null
  expect_2xx_retry "Binance API partial-cancel seller limit ask" "binance_private_post '$api_partial_cancel_seller' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.5&price=169'" >/tmp/opex-e2e-binance-api-partial-cancel-ask.json
  wait_user_open_order "$api_partial_cancel_seller" "ETH_USDT" "169" "0.5" /tmp/opex-e2e-binance-api-partial-cancel-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "169" "0.5"
  wait_binance_account_balance "$api_partial_cancel_seller" "ETH" "0.5" "0.5" /tmp/opex-e2e-binance-api-partial-cancel-reserved-account.json

  local api_partial_cancel_order_id
  api_partial_cancel_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-binance-api-partial-cancel-open-orders.json)"
  if [[ -z "$api_partial_cancel_order_id" || "$api_partial_cancel_order_id" == "null" ]]; then
    echo "Binance API partial-cancel open order did not include orderId" >&2
    cat /tmp/opex-e2e-binance-api-partial-cancel-open-orders.json >&2
    exit 1
  fi
  expect_2xx_retry "Binance API partial-cancel buyer limit bid" "binance_private_post '$api_partial_cancel_buyer' '/v3/order' 'symbol=ETHUSDT&side=BUY&type=LIMIT&timeInForce=GTC&quantity=0.2&price=169'" >/tmp/opex-e2e-binance-api-partial-cancel-bid.json
  wait_user_trade_projection "$api_partial_cancel_seller" "ETH_USDT" "169" "0.2" "33.8" "0.338" "USDT" false true false /tmp/opex-e2e-binance-api-partial-cancel-seller-trades.json
  wait_user_trade_projection "$api_partial_cancel_buyer" "ETH_USDT" "169" "0.2" "33.8" "0.002" "ETH" true false false /tmp/opex-e2e-binance-api-partial-cancel-buyer-trades.json
  wait_binance_private_order_status_by_order_id "$api_partial_cancel_seller" "ETHUSDT" "$api_partial_cancel_order_id" "169" "0.5" "PARTIALLY_FILLED" "0.2" "33.8" "SELL" /tmp/opex-e2e-binance-api-partial-cancel-query-partial.json
  wait_order_book_level "ETH_USDT" "ASK" "169" "0.3"
  wait_binance_account_balance "$api_partial_cancel_seller" "ETH" "0.5" "0.3" /tmp/opex-e2e-binance-api-partial-cancel-seller-account-partial-eth.json
  wait_binance_account_balance "$api_partial_cancel_seller" "USDT" "33.462" "0" /tmp/opex-e2e-binance-api-partial-cancel-seller-account-partial-usdt.json
  expect_2xx_retry "Binance API partial-cancel seller order-id cancel" "binance_private_delete '$api_partial_cancel_seller' '/v3/order' 'symbol=ETHUSDT&orderId=${api_partial_cancel_order_id}'" >/tmp/opex-e2e-binance-api-partial-cancel-response.json
  assert_binance_cancel_response "Binance API partial-cancel seller order-id" /tmp/opex-e2e-binance-api-partial-cancel-response.json "ETHUSDT" "$api_partial_cancel_order_id" "169" "0.5" "0.2" "33.8" "SELL"
  wait_no_user_open_orders "$api_partial_cancel_seller" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_private_order_status_by_order_id "$api_partial_cancel_seller" "ETHUSDT" "$api_partial_cancel_order_id" "169" "0.5" "CANCELED" "0.2" "33.8" "SELL" /tmp/opex-e2e-binance-api-partial-cancel-query-canceled.json
  wait_binance_account_balance "$api_partial_cancel_seller" "ETH" "0.8" "0" /tmp/opex-e2e-binance-api-partial-cancel-seller-account-canceled-eth.json
  wait_binance_account_balance "$api_partial_cancel_seller" "USDT" "33.462" "0" /tmp/opex-e2e-binance-api-partial-cancel-seller-account-canceled-usdt.json

  local api_client_cancel_owner="e2e-api-client-cancel-$(date +%s)"
  local api_client_cancel_ref="e2e-api-client-cancel-$(date +%s)"
  local api_client_cancel_id="e2e-client-cancel-$(date +%s)"
  local api_client_cancel_new_id="e2e-cancel-reply-$(date +%s)"
  expect_2xx "Binance API client cancel ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_client_cancel_owner}_MAIN?description=e2e-api-client-cancel&transferRef=${api_client_cancel_ref}-eth")" >/dev/null
  expect_2xx_retry "Binance API client-id cancel owner limit ask" "binance_private_post '$api_client_cancel_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=161&newClientOrderId=${api_client_cancel_id}'" >/tmp/opex-e2e-binance-api-client-cancel-ask.json
  jq -e \
    --arg clientOrderId "$api_client_cancel_id" '
      .symbol == "ETHUSDT" and
      (.orderId | type == "number") and
      .orderId > 0 and
      .orderListId == -1 and
      .clientOrderId == $clientOrderId and
      .price == 161 and
      .origQty == 0.2 and
      .executedQty == 0 and
      .cummulativeQuoteQty == 0 and
      .status == "NEW" and
      .timeInForce == "GTC" and
      .type == "LIMIT" and
      .side == "SELL" and
      (.transactTime | type == "number") and
      (.fills | type == "array") and
      (.fills | length == 0)
    ' /tmp/opex-e2e-binance-api-client-cancel-ask.json >/dev/null
  wait_user_open_order "$api_client_cancel_owner" "ETH_USDT" "161" "0.2" /tmp/opex-e2e-binance-api-client-cancel-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "161" "0.2"
  wait_binance_account_balance "$api_client_cancel_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-client-cancel-reserved-account.json
  wait_binance_private_order_status_by_client_order_id "$api_client_cancel_owner" "ETHUSDT" "$api_client_cancel_id" "161" "0.2" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-client-cancel-query-new.json
  expect_http_status "Binance API intruder client-id order lookup not found" "404" "$(binance_private_get_status "${api_client_cancel_owner}-intruder" "/v3/order" "symbol=ETHUSDT&origClientOrderId=${api_client_cancel_id}")" >/tmp/opex-e2e-binance-api-client-cancel-intruder-query.json
  expect_http_status "Binance API intruder client-id cancel not found" "404" "$(binance_private_delete "${api_client_cancel_owner}-intruder" "/v3/order" "symbol=ETHUSDT&origClientOrderId=${api_client_cancel_id}")" >/tmp/opex-e2e-binance-api-client-cancel-intruder-cancel.json
  wait_binance_private_order_status_by_client_order_id "$api_client_cancel_owner" "ETHUSDT" "$api_client_cancel_id" "161" "0.2" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-client-cancel-query-still-new-after-intruder.json
  wait_binance_account_balance "$api_client_cancel_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-client-cancel-still-reserved-account.json
  expect_2xx_retry "Binance API client-id cancel owner limit ask" "binance_private_delete '$api_client_cancel_owner' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_client_cancel_id}&newClientOrderId=${api_client_cancel_new_id}'" >/tmp/opex-e2e-binance-api-client-cancel-response.json
  assert_binance_cancel_response "Binance API client-id cancel owner limit ask" /tmp/opex-e2e-binance-api-client-cancel-response.json "ETHUSDT" "$(jq -r '.orderId' /tmp/opex-e2e-binance-api-client-cancel-response.json)" "161" "0.2" "0" "0" "SELL" "LIMIT" "$api_client_cancel_id" "$api_client_cancel_new_id"
  wait_no_user_open_orders "$api_client_cancel_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_client_cancel_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-client-cancel-released-account.json
  wait_binance_private_order_status_by_client_order_id "$api_client_cancel_owner" "ETHUSDT" "$api_client_cancel_id" "161" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-client-cancel-query-canceled.json
  wait_binance_private_order_projection "$api_client_cancel_owner" "ETHUSDT" "161" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-client-cancel-orders.json

  local api_generated_client_owner="e2e-api-generated-client-$(date +%s)"
  local api_generated_client_ref="e2e-api-generated-client-$(date +%s)"
  expect_2xx "Binance API generated client owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_generated_client_owner}_MAIN?description=e2e-api-generated-client&transferRef=${api_generated_client_ref}-eth")" >/dev/null
  expect_2xx_retry "Binance API generated client-id limit ask" "binance_private_post '$api_generated_client_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=167'" >/tmp/opex-e2e-binance-api-generated-client-ask.json
  local api_generated_client_id
  api_generated_client_id="$(jq -r '.clientOrderId // empty' /tmp/opex-e2e-binance-api-generated-client-ask.json)"
  if [[ -z "$api_generated_client_id" || "$api_generated_client_id" == "null" || "${#api_generated_client_id}" -gt 72 ]]; then
    echo "Binance API generated client order id invalid: '$api_generated_client_id'" >&2
    cat /tmp/opex-e2e-binance-api-generated-client-ask.json >&2
    exit 1
  fi
  jq -e \
    --arg clientOrderId "$api_generated_client_id" '
      .symbol == "ETHUSDT" and
      (.orderId | type == "number") and
      .orderId > 0 and
      .orderListId == -1 and
      .clientOrderId == $clientOrderId and
      .price == 167 and
      .origQty == 0.2 and
      .executedQty == 0 and
      .cummulativeQuoteQty == 0 and
      .status == "NEW" and
      .timeInForce == "GTC" and
      .type == "LIMIT" and
      .side == "SELL" and
      (.transactTime | type == "number") and
      (.fills | type == "array") and
      (.fills | length == 0)
    ' /tmp/opex-e2e-binance-api-generated-client-ask.json >/dev/null
  wait_user_open_order "$api_generated_client_owner" "ETH_USDT" "167" "0.2" /tmp/opex-e2e-binance-api-generated-client-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "167" "0.2"
  wait_binance_account_balance "$api_generated_client_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-generated-client-reserved-account.json
  wait_binance_private_open_order "$api_generated_client_owner" "ETHUSDT" "$api_generated_client_id" "167" "0.2" "SELL" /tmp/opex-e2e-binance-api-generated-client-open-orders-rest.json
  wait_binance_private_order_status_by_client_order_id "$api_generated_client_owner" "ETHUSDT" "$api_generated_client_id" "167" "0.2" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-generated-client-query-new.json
  expect_2xx_retry "Binance API generated client-id cleanup cancel" "binance_private_delete '$api_generated_client_owner' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_generated_client_id}'" >/tmp/opex-e2e-binance-api-generated-client-cancel-response.json
  assert_binance_cancel_response "Binance API generated client-id cleanup" /tmp/opex-e2e-binance-api-generated-client-cancel-response.json "ETHUSDT" "$(jq -r '.orderId' /tmp/opex-e2e-binance-api-generated-client-cancel-response.json)" "167" "0.2" "0" "0" "SELL" "LIMIT" "$api_generated_client_id" "$api_generated_client_id"
  wait_no_user_open_orders "$api_generated_client_owner" "ETH_USDT"
  wait_binance_private_no_open_orders "$api_generated_client_owner" "ETHUSDT" /tmp/opex-e2e-binance-api-generated-client-open-orders-empty.json
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_generated_client_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-generated-client-released-account.json
  wait_binance_private_order_status_by_client_order_id "$api_generated_client_owner" "ETHUSDT" "$api_generated_client_id" "167" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-generated-client-query-canceled.json

  local api_ack_owner="e2e-api-ack-$(date +%s)"
  local api_ack_ref="e2e-api-ack-$(date +%s)"
  local api_ack_client_id="e2e-ack-$(date +%s)"
  expect_2xx "Binance API ACK owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_ack_owner}_MAIN?description=e2e-api-ack&transferRef=${api_ack_ref}-eth")" >/dev/null
  expect_2xx_retry "Binance API ACK response limit ask" "binance_private_post '$api_ack_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=170&newClientOrderId=${api_ack_client_id}&newOrderRespType=ACK'" >/tmp/opex-e2e-binance-api-ack-ask.json
  jq -e \
    --arg clientOrderId "$api_ack_client_id" '
      .symbol == "ETHUSDT" and
      (.orderId | type == "number") and
      .orderId > 0 and
      .orderListId == -1 and
      .clientOrderId == $clientOrderId and
      (.transactTime | type == "number") and
      (.price == null) and
      (.origQty == null) and
      (.executedQty == null) and
      (.cummulativeQuoteQty == null) and
      (.status == null) and
      (.fills == null)
    ' /tmp/opex-e2e-binance-api-ack-ask.json >/dev/null
  wait_user_open_order "$api_ack_owner" "ETH_USDT" "170" "0.2" /tmp/opex-e2e-binance-api-ack-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "170" "0.2"
  wait_binance_account_balance "$api_ack_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-ack-reserved-account.json
  wait_binance_private_order_status_by_client_order_id "$api_ack_owner" "ETHUSDT" "$api_ack_client_id" "170" "0.2" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-ack-query-new.json
  expect_2xx_retry "Binance API ACK cleanup cancel" "binance_private_delete '$api_ack_owner' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_ack_client_id}'" >/tmp/opex-e2e-binance-api-ack-cancel-response.json
  assert_binance_cancel_response "Binance API ACK cleanup" /tmp/opex-e2e-binance-api-ack-cancel-response.json "ETHUSDT" "$(jq -r '.orderId' /tmp/opex-e2e-binance-api-ack-cancel-response.json)" "170" "0.2" "0" "0" "SELL" "LIMIT" "$api_ack_client_id" "$api_ack_client_id"
  wait_no_user_open_orders "$api_ack_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_ack_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-ack-released-account.json
  wait_binance_private_order_status_by_client_order_id "$api_ack_owner" "ETHUSDT" "$api_ack_client_id" "170" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-ack-query-canceled.json

  local api_result_owner="e2e-api-result-$(date +%s)"
  local api_result_ref="e2e-api-result-$(date +%s)"
  local api_result_client_id="e2e-result-$(date +%s)"
  expect_2xx "Binance API RESULT owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_result_owner}_MAIN?description=e2e-api-result&transferRef=${api_result_ref}-eth")" >/dev/null
  expect_2xx_retry "Binance API RESULT response limit ask" "binance_private_post '$api_result_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=168&newClientOrderId=${api_result_client_id}&newOrderRespType=RESULT'" >/tmp/opex-e2e-binance-api-result-ask.json
  jq -e \
    --arg clientOrderId "$api_result_client_id" '
      .symbol == "ETHUSDT" and
      (.orderId | type == "number") and
      .orderId > 0 and
      .orderListId == -1 and
      .clientOrderId == $clientOrderId and
      .price == 168 and
      .origQty == 0.2 and
      .executedQty == 0 and
      .cummulativeQuoteQty == 0 and
      .status == "NEW" and
      .timeInForce == "GTC" and
      .type == "LIMIT" and
      .side == "SELL" and
      (.transactTime | type == "number") and
      (.fills == null)
    ' /tmp/opex-e2e-binance-api-result-ask.json >/dev/null
  wait_user_open_order "$api_result_owner" "ETH_USDT" "168" "0.2" /tmp/opex-e2e-binance-api-result-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "168" "0.2"
  wait_binance_account_balance "$api_result_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-result-reserved-account.json
  wait_binance_private_order_status_by_client_order_id "$api_result_owner" "ETHUSDT" "$api_result_client_id" "168" "0.2" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-result-query-new.json
  expect_2xx_retry "Binance API RESULT cleanup cancel" "binance_private_delete '$api_result_owner' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_result_client_id}'" >/tmp/opex-e2e-binance-api-result-cancel-response.json
  assert_binance_cancel_response "Binance API RESULT cleanup" /tmp/opex-e2e-binance-api-result-cancel-response.json "ETHUSDT" "$(jq -r '.orderId' /tmp/opex-e2e-binance-api-result-cancel-response.json)" "168" "0.2" "0" "0" "SELL" "LIMIT" "$api_result_client_id" "$api_result_client_id"
  wait_no_user_open_orders "$api_result_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_result_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-result-released-account.json
  wait_binance_private_order_status_by_client_order_id "$api_result_owner" "ETHUSDT" "$api_result_client_id" "168" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-result-query-canceled.json

  local api_full_owner="e2e-api-full-$(date +%s)"
  local api_full_ref="e2e-api-full-$(date +%s)"
  local api_full_client_id="e2e-full-$(date +%s)"
  expect_2xx "Binance API FULL owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_full_owner}_MAIN?description=e2e-api-full&transferRef=${api_full_ref}-eth")" >/dev/null
  expect_2xx_retry "Binance API FULL response limit ask" "binance_private_post '$api_full_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=169&newClientOrderId=${api_full_client_id}&newOrderRespType=FULL'" >/tmp/opex-e2e-binance-api-full-ask.json
  jq -e \
    --arg clientOrderId "$api_full_client_id" '
      .symbol == "ETHUSDT" and
      (.orderId | type == "number") and
      .orderId > 0 and
      .orderListId == -1 and
      .clientOrderId == $clientOrderId and
      .price == 169 and
      .origQty == 0.2 and
      .executedQty == 0 and
      .cummulativeQuoteQty == 0 and
      .status == "NEW" and
      .timeInForce == "GTC" and
      .type == "LIMIT" and
      .side == "SELL" and
      (.transactTime | type == "number") and
      (.fills | type == "array") and
      (.fills | length == 0)
    ' /tmp/opex-e2e-binance-api-full-ask.json >/dev/null
  wait_user_open_order "$api_full_owner" "ETH_USDT" "169" "0.2" /tmp/opex-e2e-binance-api-full-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "169" "0.2"
  wait_binance_account_balance "$api_full_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-full-reserved-account.json
  wait_binance_private_order_status_by_client_order_id "$api_full_owner" "ETHUSDT" "$api_full_client_id" "169" "0.2" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-full-query-new.json
  expect_2xx_retry "Binance API FULL cleanup cancel" "binance_private_delete '$api_full_owner' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_full_client_id}'" >/tmp/opex-e2e-binance-api-full-cancel-response.json
  assert_binance_cancel_response "Binance API FULL cleanup" /tmp/opex-e2e-binance-api-full-cancel-response.json "ETHUSDT" "$(jq -r '.orderId' /tmp/opex-e2e-binance-api-full-cancel-response.json)" "169" "0.2" "0" "0" "SELL" "LIMIT" "$api_full_client_id" "$api_full_client_id"
  wait_no_user_open_orders "$api_full_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_full_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-full-released-account.json
  wait_binance_private_order_status_by_client_order_id "$api_full_owner" "ETHUSDT" "$api_full_client_id" "169" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-full-query-canceled.json

  local api_scoped_client_owner_one="e2e-api-scoped-client-1-$(date +%s)"
  local api_scoped_client_owner_two="e2e-api-scoped-client-2-$(date +%s)"
  local api_scoped_client_ref="e2e-api-scoped-client-$(date +%s)"
  local api_scoped_client_id="e2e-shared-client-$(date +%s)"
  expect_2xx "Binance API scoped client owner one ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_scoped_client_owner_one}_MAIN?description=e2e-api-scoped-client&transferRef=${api_scoped_client_ref}-eth-1")" >/dev/null
  expect_2xx "Binance API scoped client owner two ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_scoped_client_owner_two}_MAIN?description=e2e-api-scoped-client&transferRef=${api_scoped_client_ref}-eth-2")" >/dev/null
  expect_2xx_retry "Binance API scoped client owner one limit ask" "binance_private_post '$api_scoped_client_owner_one' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=162&newClientOrderId=${api_scoped_client_id}'" >/tmp/opex-e2e-binance-api-scoped-client-ask-1.json
  expect_2xx_retry "Binance API scoped client owner two limit ask" "binance_private_post '$api_scoped_client_owner_two' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.3&price=163&newClientOrderId=${api_scoped_client_id}'" >/tmp/opex-e2e-binance-api-scoped-client-ask-2.json
  wait_user_open_order "$api_scoped_client_owner_one" "ETH_USDT" "162" "0.2" /tmp/opex-e2e-binance-api-scoped-client-open-orders-1.json
  wait_user_open_order "$api_scoped_client_owner_two" "ETH_USDT" "163" "0.3" /tmp/opex-e2e-binance-api-scoped-client-open-orders-2.json
  wait_order_book_level "ETH_USDT" "ASK" "162" "0.2"
  wait_order_book_level "ETH_USDT" "ASK" "163" "0.3"
  wait_binance_account_balance "$api_scoped_client_owner_one" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-scoped-client-reserved-account-1.json
  wait_binance_account_balance "$api_scoped_client_owner_two" "ETH" "0.7" "0.3" /tmp/opex-e2e-binance-api-scoped-client-reserved-account-2.json
  wait_binance_private_order_status_by_client_order_id "$api_scoped_client_owner_one" "ETHUSDT" "$api_scoped_client_id" "162" "0.2" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-scoped-client-query-new-1.json
  wait_binance_private_order_status_by_client_order_id "$api_scoped_client_owner_two" "ETHUSDT" "$api_scoped_client_id" "163" "0.3" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-scoped-client-query-new-2.json
  expect_2xx_retry "Binance API scoped client owner one cancel" "binance_private_delete '$api_scoped_client_owner_one' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_scoped_client_id}'" >/tmp/opex-e2e-binance-api-scoped-client-cancel-response-1.json
  assert_binance_cancel_response "Binance API scoped client owner one" /tmp/opex-e2e-binance-api-scoped-client-cancel-response-1.json "ETHUSDT" "$(jq -r '.orderId' /tmp/opex-e2e-binance-api-scoped-client-cancel-response-1.json)" "162" "0.2" "0" "0" "SELL" "LIMIT" "$api_scoped_client_id" "$api_scoped_client_id"
  wait_no_user_open_orders "$api_scoped_client_owner_one" "ETH_USDT"
  wait_order_book_empty_at_price "ETH_USDT" "ASK" "162"
  wait_order_book_level "ETH_USDT" "ASK" "163" "0.3"
  wait_binance_account_balance "$api_scoped_client_owner_one" "ETH" "1" "0" /tmp/opex-e2e-binance-api-scoped-client-released-account-1.json
  wait_binance_private_order_status_by_client_order_id "$api_scoped_client_owner_one" "ETHUSDT" "$api_scoped_client_id" "162" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-scoped-client-query-canceled-1.json
  wait_binance_private_order_status_by_client_order_id "$api_scoped_client_owner_two" "ETHUSDT" "$api_scoped_client_id" "163" "0.3" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-scoped-client-query-still-new-2.json
  expect_2xx_retry "Binance API scoped client owner two cancel" "binance_private_delete '$api_scoped_client_owner_two' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_scoped_client_id}'" >/tmp/opex-e2e-binance-api-scoped-client-cancel-response-2.json
  assert_binance_cancel_response "Binance API scoped client owner two" /tmp/opex-e2e-binance-api-scoped-client-cancel-response-2.json "ETHUSDT" "$(jq -r '.orderId' /tmp/opex-e2e-binance-api-scoped-client-cancel-response-2.json)" "163" "0.3" "0" "0" "SELL" "LIMIT" "$api_scoped_client_id" "$api_scoped_client_id"
  wait_no_user_open_orders "$api_scoped_client_owner_two" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_scoped_client_owner_two" "ETH" "1" "0" /tmp/opex-e2e-binance-api-scoped-client-released-account-2.json
  wait_binance_private_order_status_by_client_order_id "$api_scoped_client_owner_two" "ETHUSDT" "$api_scoped_client_id" "163" "0.3" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-scoped-client-query-canceled-2.json
  wait_binance_private_order_projection "$api_scoped_client_owner_one" "ETHUSDT" "162" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-scoped-client-orders-1.json
  wait_binance_private_order_projection "$api_scoped_client_owner_two" "ETHUSDT" "163" "0.3" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-scoped-client-orders-2.json

  local api_duplicate_client_owner="e2e-api-duplicate-client-$(date +%s)"
  local api_duplicate_client_ref="e2e-api-duplicate-client-$(date +%s)"
  local api_duplicate_client_id="e2e-duplicate-client-$(date +%s)"
  expect_2xx "Binance API duplicate client owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_duplicate_client_owner}_MAIN?description=e2e-api-duplicate-client&transferRef=${api_duplicate_client_ref}-eth")" >/dev/null
  expect_2xx_retry "Binance API duplicate client first ask" "binance_private_post '$api_duplicate_client_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=164&newClientOrderId=${api_duplicate_client_id}'" >/tmp/opex-e2e-binance-api-duplicate-client-first-ask.json
  wait_user_open_order "$api_duplicate_client_owner" "ETH_USDT" "164" "0.2" /tmp/opex-e2e-binance-api-duplicate-client-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "164" "0.2"
  wait_binance_account_balance "$api_duplicate_client_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-duplicate-client-reserved-account.json
  wait_binance_private_order_status_by_client_order_id "$api_duplicate_client_owner" "ETHUSDT" "$api_duplicate_client_id" "164" "0.2" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-duplicate-client-query-new.json
  expect_http_status "Binance API duplicate open client order id" "400" "$(binance_private_post "$api_duplicate_client_owner" "/v3/order" "symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=165&newClientOrderId=${api_duplicate_client_id}")" >/tmp/opex-e2e-binance-api-duplicate-client-reject.json
  assert_opex_error "Binance API duplicate open client order id error" "BadRequest" 1002 "$(cat /tmp/opex-e2e-binance-api-duplicate-client-reject.json)"
  wait_user_open_order "$api_duplicate_client_owner" "ETH_USDT" "164" "0.2" /tmp/opex-e2e-binance-api-duplicate-client-still-open-orders.json
  assert_no_user_order_by_price "$api_duplicate_client_owner" "ETH_USDT" "165" "0.2"
  wait_order_book_empty_at_price "ETH_USDT" "ASK" "165"
  wait_binance_account_balance "$api_duplicate_client_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-duplicate-client-unchanged-account.json
  expect_2xx_retry "Binance API duplicate client cleanup cancel" "binance_private_delete '$api_duplicate_client_owner' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_duplicate_client_id}'" >/tmp/opex-e2e-binance-api-duplicate-client-cancel-response.json
  wait_no_user_open_orders "$api_duplicate_client_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_duplicate_client_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-duplicate-client-released-account.json
  wait_binance_private_order_status_by_client_order_id "$api_duplicate_client_owner" "ETHUSDT" "$api_duplicate_client_id" "164" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-duplicate-client-query-canceled.json
  expect_2xx_retry "Binance API canceled client id reuse ask" "binance_private_post '$api_duplicate_client_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=166&newClientOrderId=${api_duplicate_client_id}'" >/tmp/opex-e2e-binance-api-duplicate-client-reuse-ask.json
  wait_user_open_order "$api_duplicate_client_owner" "ETH_USDT" "166" "0.2" /tmp/opex-e2e-binance-api-duplicate-client-reuse-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "166" "0.2"
  wait_binance_account_balance "$api_duplicate_client_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-duplicate-client-reuse-reserved-account.json
  wait_binance_private_order_status_by_client_order_id "$api_duplicate_client_owner" "ETHUSDT" "$api_duplicate_client_id" "166" "0.2" "NEW" "0" "0" "SELL" /tmp/opex-e2e-binance-api-duplicate-client-reuse-query-new.json
  expect_2xx_retry "Binance API reused client id cleanup cancel" "binance_private_delete '$api_duplicate_client_owner' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_duplicate_client_id}'" >/tmp/opex-e2e-binance-api-duplicate-client-reuse-cancel-response.json
  wait_no_user_open_orders "$api_duplicate_client_owner" "ETH_USDT"
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_binance_account_balance "$api_duplicate_client_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-duplicate-client-reuse-released-account.json
  wait_binance_private_order_status_by_client_order_id "$api_duplicate_client_owner" "ETHUSDT" "$api_duplicate_client_id" "166" "0.2" "CANCELED" "0" "0" "SELL" /tmp/opex-e2e-binance-api-duplicate-client-reuse-query-canceled.json

  local api_all_symbol_owner="e2e-api-all-symbol-$(date +%s)"
  local api_all_symbol_ref="e2e-api-all-symbol-$(date +%s)"
  local api_all_symbol_eth_client_id="e2e-all-symbol-eth-$(date +%s)"
  local api_all_symbol_btc_client_id="e2e-all-symbol-btc-$(date +%s)"
  expect_2xx "Binance API all-symbol ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${api_all_symbol_owner}_MAIN?description=e2e-api-all-symbol&transferRef=${api_all_symbol_ref}-eth")" >/dev/null
  expect_2xx "Binance API all-symbol BTC deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/0.002_test-bitcoin_BTC/${api_all_symbol_owner}_MAIN?description=e2e-api-all-symbol&transferRef=${api_all_symbol_ref}-btc")" >/dev/null
  expect_2xx_retry "Binance API all-symbol ETH ask" "binance_private_post '$api_all_symbol_owner' '/v3/order' 'symbol=ETHUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.2&price=168&newClientOrderId=${api_all_symbol_eth_client_id}'" >/tmp/opex-e2e-binance-api-all-symbol-eth-ask.json
  expect_2xx_retry "Binance API all-symbol BTC ask" "binance_private_post '$api_all_symbol_owner' '/v3/order' 'symbol=BTCUSDT&side=SELL&type=LIMIT&timeInForce=GTC&quantity=0.001&price=23000&newClientOrderId=${api_all_symbol_btc_client_id}'" >/tmp/opex-e2e-binance-api-all-symbol-btc-ask.json
  wait_order_book_level "ETH_USDT" "ASK" "168" "0.2"
  wait_order_book_level "BTC_USDT" "ASK" "23000" "0.001"
  wait_binance_account_balance "$api_all_symbol_owner" "ETH" "0.8" "0.2" /tmp/opex-e2e-binance-api-all-symbol-eth-reserved-account.json
  wait_binance_account_balance "$api_all_symbol_owner" "BTC" "0.001" "0.001" /tmp/opex-e2e-binance-api-all-symbol-btc-reserved-account.json
  wait_binance_private_open_orders_across_symbols \
    "$api_all_symbol_owner" \
    "ETHUSDT" "$api_all_symbol_eth_client_id" "168" "0.2" "SELL" \
    "BTCUSDT" "$api_all_symbol_btc_client_id" "23000" "0.001" "SELL" \
    /tmp/opex-e2e-binance-api-all-symbol-open-orders.json
  wait_binance_private_all_orders_across_symbols \
    "$api_all_symbol_owner" \
    "ETHUSDT" "168" "0.2" "NEW" "SELL" \
    "BTCUSDT" "23000" "0.001" "NEW" "SELL" \
    /tmp/opex-e2e-binance-api-all-symbol-all-orders-new.json
  expect_2xx_retry "Binance API all-symbol ETH cleanup cancel" "binance_private_delete '$api_all_symbol_owner' '/v3/order' 'symbol=ETHUSDT&origClientOrderId=${api_all_symbol_eth_client_id}'" >/tmp/opex-e2e-binance-api-all-symbol-eth-cancel-response.json
  expect_2xx_retry "Binance API all-symbol BTC cleanup cancel" "binance_private_delete '$api_all_symbol_owner' '/v3/order' 'symbol=BTCUSDT&origClientOrderId=${api_all_symbol_btc_client_id}'" >/tmp/opex-e2e-binance-api-all-symbol-btc-cancel-response.json
  wait_order_book_empty_at_price "ETH_USDT" "ASK" "168"
  wait_order_book_empty_at_price "BTC_USDT" "ASK" "23000"
  wait_binance_private_no_open_orders "$api_all_symbol_owner" "ETHUSDT" /tmp/opex-e2e-binance-api-all-symbol-eth-open-orders-empty.json
  wait_binance_private_no_open_orders "$api_all_symbol_owner" "BTCUSDT" /tmp/opex-e2e-binance-api-all-symbol-btc-open-orders-empty.json
  wait_binance_account_balance "$api_all_symbol_owner" "ETH" "1" "0" /tmp/opex-e2e-binance-api-all-symbol-eth-released-account.json
  wait_binance_account_balance "$api_all_symbol_owner" "BTC" "0.002" "0" /tmp/opex-e2e-binance-api-all-symbol-btc-released-account.json
  wait_binance_private_all_orders_across_symbols \
    "$api_all_symbol_owner" \
    "ETHUSDT" "168" "0.2" "CANCELED" "SELL" \
    "BTCUSDT" "23000" "0.001" "CANCELED" "SELL" \
    /tmp/opex-e2e-binance-api-all-symbol-all-orders-canceled.json

  local engine_restart_seller="e2e-engine-restart-seller-$(date +%s)"
  local engine_restart_buyer="e2e-engine-restart-buyer-$(date +%s)"
  local engine_restart_ref="e2e-engine-restart-$(date +%s)"
  expect_2xx "engine-restart seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${engine_restart_seller}_MAIN?description=e2e-engine-restart&transferRef=${engine_restart_ref}-eth")" >/dev/null
  expect_2xx "engine-restart buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${engine_restart_buyer}_MAIN?description=e2e-engine-restart&transferRef=${engine_restart_ref}-usdt")" >/dev/null

  local engine_restart_ask='{"uuid":null,"pair":"ETH_USDT","price":111,"quantity":0.5,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local engine_restart_bid='{"uuid":null,"pair":"ETH_USDT","price":111,"quantity":0.5,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "engine-restart resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$engine_restart_ask' '$engine_restart_seller'" >/tmp/opex-e2e-engine-restart-ask.json
  wait_user_open_order "$engine_restart_seller" "ETH_USDT" "111" "0.5" /tmp/opex-e2e-engine-restart-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "111" "0.5"
  wait_matching_snapshot_order "engine-restart snapshot before restart" "$engine_restart_seller" "ETH_USDT" "ASK"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$engine_restart_seller" "ETH" "0.5"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for engine-restart resting ask reservation" >&2
      assert_wallet_balance "engine-restart seller reserved ETH" "$engine_restart_seller" "ETH" "0.5" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  restart_matching_engine_and_wait
  wait_matching_snapshot_order "engine-restart snapshot after restart" "$engine_restart_seller" "ETH_USDT" "ASK"
  wait_user_open_order "$engine_restart_seller" "ETH_USDT" "111" "0.5" /tmp/opex-e2e-engine-restart-open-orders-after-restart.json
  wait_order_book_level "ETH_USDT" "ASK" "111" "0.5"
  expect_2xx_retry "engine-restart crossing bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$engine_restart_bid' '$engine_restart_buyer'" >/tmp/opex-e2e-engine-restart-bid.json
  wait_no_user_open_orders "$engine_restart_seller" "ETH_USDT"
  wait_matching_snapshot_no_order "engine-restart snapshot after fill" "$engine_restart_seller" "ETH_USDT" "ASK"
  wait_user_trade_projection "$engine_restart_seller" "ETH_USDT" "111" "0.5" "55.5" "0.555" "USDT" false true false /tmp/opex-e2e-engine-restart-seller-trades.json
  wait_user_trade_projection "$engine_restart_buyer" "ETH_USDT" "111" "0.5" "55.5" "0.005" "ETH" true false false /tmp/opex-e2e-engine-restart-buyer-trades.json
  wait_user_order_projection_by_price "$engine_restart_seller" "ETH_USDT" "111" "0.5" "FILLED" "0.5" "55.5" /tmp/opex-e2e-engine-restart-seller-orders.json
  wait_user_order_projection_by_price "$engine_restart_buyer" "ETH_USDT" "111" "0.5" "FILLED" "0.5" "55.5" /tmp/opex-e2e-engine-restart-buyer-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$engine_restart_seller" "ETH" "0.5" &&
    try_wallet_balance "$engine_restart_seller" "USDT" "54.945" &&
    try_wallet_balance "$engine_restart_buyer" "ETH" "0.495" &&
    try_wallet_balance "$engine_restart_buyer" "USDT" "44.5"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for engine-restart post-fill wallet settlement" >&2
      assert_wallet_balance "engine-restart seller ETH remainder" "$engine_restart_seller" "ETH" "0.5" >&2 || true
      assert_wallet_balance "engine-restart seller USDT proceeds" "$engine_restart_seller" "USDT" "54.945" >&2 || true
      assert_wallet_balance "engine-restart buyer ETH received" "$engine_restart_buyer" "ETH" "0.495" >&2 || true
      assert_wallet_balance "engine-restart buyer USDT remainder" "$engine_restart_buyer" "USDT" "44.5" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway >&2 || true
      exit 1
    fi
    sleep 2
  done

  local wallet_restart_seller="e2e-wallet-restart-seller-$(date +%s)"
  local wallet_restart_buyer="e2e-wallet-restart-buyer-$(date +%s)"
  local wallet_restart_ref="e2e-wallet-restart-$(date +%s)"
  expect_2xx "wallet-restart seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${wallet_restart_seller}_MAIN?description=e2e-wallet-restart&transferRef=${wallet_restart_ref}-eth")" >/dev/null
  expect_2xx "wallet-restart buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${wallet_restart_buyer}_MAIN?description=e2e-wallet-restart&transferRef=${wallet_restart_ref}-usdt")" >/dev/null

  local wallet_restart_ask='{"uuid":null,"pair":"ETH_USDT","price":112,"quantity":0.4,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local wallet_restart_bid='{"uuid":null,"pair":"ETH_USDT","price":112,"quantity":0.4,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "wallet-restart resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$wallet_restart_ask' '$wallet_restart_seller'" >/tmp/opex-e2e-wallet-restart-ask.json
  wait_user_open_order "$wallet_restart_seller" "ETH_USDT" "112" "0.4" /tmp/opex-e2e-wallet-restart-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "112" "0.4"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$wallet_restart_seller" "ETH" "0.6"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for wallet-restart resting ask reservation" >&2
      assert_wallet_balance "wallet-restart seller reserved ETH" "$wallet_restart_seller" "ETH" "0.6" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  restart_wallet_and_wait
  assert_wallet_balance "wallet-restart seller reservation after wallet restart" "$wallet_restart_seller" "ETH" "0.6"
  expect_2xx_retry "wallet-restart crossing bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$wallet_restart_bid' '$wallet_restart_buyer'" >/tmp/opex-e2e-wallet-restart-bid.json
  wait_no_user_open_orders "$wallet_restart_seller" "ETH_USDT"
  wait_user_trade_projection "$wallet_restart_seller" "ETH_USDT" "112" "0.4" "44.8" "0.448" "USDT" false true false /tmp/opex-e2e-wallet-restart-seller-trades.json
  wait_user_trade_projection "$wallet_restart_buyer" "ETH_USDT" "112" "0.4" "44.8" "0.004" "ETH" true false false /tmp/opex-e2e-wallet-restart-buyer-trades.json
  wait_user_order_projection_by_price "$wallet_restart_seller" "ETH_USDT" "112" "0.4" "FILLED" "0.4" "44.8" /tmp/opex-e2e-wallet-restart-seller-orders.json
  wait_user_order_projection_by_price "$wallet_restart_buyer" "ETH_USDT" "112" "0.4" "FILLED" "0.4" "44.8" /tmp/opex-e2e-wallet-restart-buyer-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$wallet_restart_seller" "ETH" "0.6" &&
    try_wallet_balance "$wallet_restart_seller" "USDT" "44.352" &&
    try_wallet_balance "$wallet_restart_buyer" "ETH" "0.396" &&
    try_wallet_balance "$wallet_restart_buyer" "USDT" "55.2"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for wallet-restart post-fill wallet settlement" >&2
      assert_wallet_balance "wallet-restart seller ETH remainder" "$wallet_restart_seller" "ETH" "0.6" >&2 || true
      assert_wallet_balance "wallet-restart seller USDT proceeds" "$wallet_restart_seller" "USDT" "44.352" >&2 || true
      assert_wallet_balance "wallet-restart buyer ETH received" "$wallet_restart_buyer" "ETH" "0.396" >&2 || true
      assert_wallet_balance "wallet-restart buyer USDT remainder" "$wallet_restart_buyer" "USDT" "55.2" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway >&2 || true
      exit 1
    fi
    sleep 2
  done

  local accountant_restart_seller="e2e-acct-rs-s-$(date +%s)"
  local accountant_restart_buyer="e2e-acct-rs-b-$(date +%s)"
  local accountant_restart_ref="e2e-acct-rs-$(date +%s)"
  expect_2xx "accountant-restart seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${accountant_restart_seller}_MAIN?description=e2e-accountant-restart&transferRef=${accountant_restart_ref}-eth")" >/dev/null
  expect_2xx "accountant-restart buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${accountant_restart_buyer}_MAIN?description=e2e-accountant-restart&transferRef=${accountant_restart_ref}-usdt")" >/dev/null

  local accountant_restart_ask='{"uuid":null,"pair":"ETH_USDT","price":113,"quantity":0.3,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local accountant_restart_bid='{"uuid":null,"pair":"ETH_USDT","price":113,"quantity":0.3,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "accountant-restart resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$accountant_restart_ask' '$accountant_restart_seller'" >/tmp/opex-e2e-accountant-restart-ask.json
  wait_user_open_order "$accountant_restart_seller" "ETH_USDT" "113" "0.3" /tmp/opex-e2e-accountant-restart-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "113" "0.3"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$accountant_restart_seller" "ETH" "0.7"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for accountant-restart resting ask reservation" >&2
      assert_wallet_balance "accountant-restart seller reserved ETH" "$accountant_restart_seller" "ETH" "0.7" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  restart_accountant_and_wait
  expect_2xx_retry "accountant-restart crossing bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$accountant_restart_bid' '$accountant_restart_buyer'" >/tmp/opex-e2e-accountant-restart-bid.json
  wait_no_user_open_orders "$accountant_restart_seller" "ETH_USDT"
  wait_user_trade_projection "$accountant_restart_seller" "ETH_USDT" "113" "0.3" "33.9" "0.339" "USDT" false true false /tmp/opex-e2e-accountant-restart-seller-trades.json
  wait_user_trade_projection "$accountant_restart_buyer" "ETH_USDT" "113" "0.3" "33.9" "0.003" "ETH" true false false /tmp/opex-e2e-accountant-restart-buyer-trades.json
  wait_user_order_projection_by_price "$accountant_restart_seller" "ETH_USDT" "113" "0.3" "FILLED" "0.3" "33.9" /tmp/opex-e2e-accountant-restart-seller-orders.json
  wait_user_order_projection_by_price "$accountant_restart_buyer" "ETH_USDT" "113" "0.3" "FILLED" "0.3" "33.9" /tmp/opex-e2e-accountant-restart-buyer-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$accountant_restart_seller" "ETH" "0.7" &&
    try_wallet_balance "$accountant_restart_seller" "USDT" "33.561" &&
    try_wallet_balance "$accountant_restart_buyer" "ETH" "0.297" &&
    try_wallet_balance "$accountant_restart_buyer" "USDT" "66.1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for accountant-restart post-fill wallet settlement" >&2
      assert_wallet_balance "accountant-restart seller ETH remainder" "$accountant_restart_seller" "ETH" "0.7" >&2 || true
      assert_wallet_balance "accountant-restart seller USDT proceeds" "$accountant_restart_seller" "USDT" "33.561" >&2 || true
      assert_wallet_balance "accountant-restart buyer ETH received" "$accountant_restart_buyer" "ETH" "0.297" >&2 || true
      assert_wallet_balance "accountant-restart buyer USDT remainder" "$accountant_restart_buyer" "USDT" "66.1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway >&2 || true
      exit 1
    fi
    sleep 2
  done

  local gateway_restart_seller="e2e-gw-rs-s-$(date +%s)"
  local gateway_restart_buyer="e2e-gw-rs-b-$(date +%s)"
  local gateway_restart_ref="e2e-gw-rs-$(date +%s)"
  expect_2xx "gateway-restart seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${gateway_restart_seller}_MAIN?description=e2e-gateway-restart&transferRef=${gateway_restart_ref}-eth")" >/dev/null
  expect_2xx "gateway-restart buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${gateway_restart_buyer}_MAIN?description=e2e-gateway-restart&transferRef=${gateway_restart_ref}-usdt")" >/dev/null

  restart_matching_gateway_and_wait
  local gateway_restart_ask='{"uuid":null,"pair":"ETH_USDT","price":114,"quantity":0.2,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local gateway_restart_bid='{"uuid":null,"pair":"ETH_USDT","price":114,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "gateway-restart ask order after restart" "curl_json POST 'http://127.0.0.1:8093/order' '$gateway_restart_ask' '$gateway_restart_seller'" >/tmp/opex-e2e-gateway-restart-ask.json
  wait_user_open_order "$gateway_restart_seller" "ETH_USDT" "114" "0.2" /tmp/opex-e2e-gateway-restart-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "114" "0.2"
  expect_2xx_retry "gateway-restart bid order after restart" "curl_json POST 'http://127.0.0.1:8093/order' '$gateway_restart_bid' '$gateway_restart_buyer'" >/tmp/opex-e2e-gateway-restart-bid.json
  wait_no_user_open_orders "$gateway_restart_seller" "ETH_USDT"
  wait_user_trade_projection "$gateway_restart_seller" "ETH_USDT" "114" "0.2" "22.8" "0.228" "USDT" false true false /tmp/opex-e2e-gateway-restart-seller-trades.json
  wait_user_trade_projection "$gateway_restart_buyer" "ETH_USDT" "114" "0.2" "22.8" "0.002" "ETH" true false false /tmp/opex-e2e-gateway-restart-buyer-trades.json
  wait_user_order_projection_by_price "$gateway_restart_seller" "ETH_USDT" "114" "0.2" "FILLED" "0.2" "22.8" /tmp/opex-e2e-gateway-restart-seller-orders.json
  wait_user_order_projection_by_price "$gateway_restart_buyer" "ETH_USDT" "114" "0.2" "FILLED" "0.2" "22.8" /tmp/opex-e2e-gateway-restart-buyer-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$gateway_restart_seller" "ETH" "0.8" &&
    try_wallet_balance "$gateway_restart_seller" "USDT" "22.572" &&
    try_wallet_balance "$gateway_restart_buyer" "ETH" "0.198" &&
    try_wallet_balance "$gateway_restart_buyer" "USDT" "77.2"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for gateway-restart wallet settlement" >&2
      assert_wallet_balance "gateway-restart seller ETH remainder" "$gateway_restart_seller" "ETH" "0.8" >&2 || true
      assert_wallet_balance "gateway-restart seller USDT proceeds" "$gateway_restart_seller" "USDT" "22.572" >&2 || true
      assert_wallet_balance "gateway-restart buyer ETH received" "$gateway_restart_buyer" "ETH" "0.198" >&2 || true
      assert_wallet_balance "gateway-restart buyer USDT remainder" "$gateway_restart_buyer" "USDT" "77.2" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway >&2 || true
      exit 1
    fi
    sleep 2
  done

  local core_restart_seller="e2e-core-rs-s-$(date +%s)"
  local core_restart_buyer="e2e-core-rs-b-$(date +%s)"
  local core_restart_ref="e2e-core-rs-$(date +%s)"
  restart_core_services_and_wait
  expect_2xx_retry "core-restart seller ETH deposit after restart" "curl_json POST 'http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${core_restart_seller}_MAIN?description=e2e-core-restart&transferRef=${core_restart_ref}-eth'" >/dev/null
  expect_2xx_retry "core-restart buyer USDT deposit after restart" "curl_json POST 'http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${core_restart_buyer}_MAIN?description=e2e-core-restart&transferRef=${core_restart_ref}-usdt'" >/dev/null

  local core_restart_ask='{"uuid":null,"pair":"ETH_USDT","price":115,"quantity":0.2,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local core_restart_bid='{"uuid":null,"pair":"ETH_USDT","price":115,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "core-restart ask order after core restart" "curl_json POST 'http://127.0.0.1:8093/order' '$core_restart_ask' '$core_restart_seller'" >/tmp/opex-e2e-core-restart-ask.json
  wait_user_open_order "$core_restart_seller" "ETH_USDT" "115" "0.2" /tmp/opex-e2e-core-restart-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "115" "0.2"
  expect_2xx_retry "core-restart bid order after core restart" "curl_json POST 'http://127.0.0.1:8093/order' '$core_restart_bid' '$core_restart_buyer'" >/tmp/opex-e2e-core-restart-bid.json
  wait_no_user_open_orders "$core_restart_seller" "ETH_USDT"
  wait_user_trade_projection "$core_restart_seller" "ETH_USDT" "115" "0.2" "23" "0.23" "USDT" false true false /tmp/opex-e2e-core-restart-seller-trades.json
  wait_user_trade_projection "$core_restart_buyer" "ETH_USDT" "115" "0.2" "23" "0.002" "ETH" true false false /tmp/opex-e2e-core-restart-buyer-trades.json
  wait_user_order_projection_by_price "$core_restart_seller" "ETH_USDT" "115" "0.2" "FILLED" "0.2" "23" /tmp/opex-e2e-core-restart-seller-orders.json
  wait_user_order_projection_by_price "$core_restart_buyer" "ETH_USDT" "115" "0.2" "FILLED" "0.2" "23" /tmp/opex-e2e-core-restart-buyer-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$core_restart_seller" "ETH" "0.8" &&
    try_wallet_balance "$core_restart_seller" "USDT" "22.77" &&
    try_wallet_balance "$core_restart_buyer" "ETH" "0.198" &&
    try_wallet_balance "$core_restart_buyer" "USDT" "77"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for core-restart wallet settlement" >&2
      assert_wallet_balance "core-restart seller ETH remainder" "$core_restart_seller" "ETH" "0.8" >&2 || true
      assert_wallet_balance "core-restart seller USDT proceeds" "$core_restart_seller" "USDT" "22.77" >&2 || true
      assert_wallet_balance "core-restart buyer ETH received" "$core_restart_buyer" "ETH" "0.198" >&2 || true
      assert_wallet_balance "core-restart buyer USDT remainder" "$core_restart_buyer" "USDT" "77" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway >&2 || true
      exit 1
    fi
    sleep 2
  done

  local kafka_restart_seller="e2e-kafka-rs-s-$(date +%s)"
  local kafka_restart_buyer="e2e-kafka-rs-b-$(date +%s)"
  local kafka_restart_ref="e2e-kafka-rs-$(date +%s)"
  expect_2xx_retry "kafka-restart seller ETH deposit before restart" "curl_json POST 'http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${kafka_restart_seller}_MAIN?description=e2e-kafka-restart&transferRef=${kafka_restart_ref}-eth'" >/dev/null
  expect_2xx_retry "kafka-restart buyer USDT deposit before restart" "curl_json POST 'http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${kafka_restart_buyer}_MAIN?description=e2e-kafka-restart&transferRef=${kafka_restart_ref}-usdt'" >/dev/null

  local kafka_down_ask='{"uuid":null,"pair":"ETH_USDT","price":116,"quantity":0.2,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  restart_kafka_with_gateway_rejection_check "$kafka_down_ask" "$kafka_restart_seller" "ETH" "1"
  local kafka_restart_ask='{"uuid":null,"pair":"ETH_USDT","price":116,"quantity":0.2,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local kafka_restart_bid='{"uuid":null,"pair":"ETH_USDT","price":116,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "kafka-restart ask order after broker restart" "curl_json POST 'http://127.0.0.1:8093/order' '$kafka_restart_ask' '$kafka_restart_seller'" >/tmp/opex-e2e-kafka-restart-ask.json
  wait_user_open_order "$kafka_restart_seller" "ETH_USDT" "116" "0.2" /tmp/opex-e2e-kafka-restart-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "116" "0.2"
  expect_2xx_retry "kafka-restart bid order after broker restart" "curl_json POST 'http://127.0.0.1:8093/order' '$kafka_restart_bid' '$kafka_restart_buyer'" >/tmp/opex-e2e-kafka-restart-bid.json
  wait_no_user_open_orders "$kafka_restart_seller" "ETH_USDT"
  wait_user_trade_projection "$kafka_restart_seller" "ETH_USDT" "116" "0.2" "23.2" "0.232" "USDT" false true false /tmp/opex-e2e-kafka-restart-seller-trades.json
  wait_user_trade_projection "$kafka_restart_buyer" "ETH_USDT" "116" "0.2" "23.2" "0.002" "ETH" true false false /tmp/opex-e2e-kafka-restart-buyer-trades.json
  wait_user_order_projection_by_price "$kafka_restart_seller" "ETH_USDT" "116" "0.2" "FILLED" "0.2" "23.2" /tmp/opex-e2e-kafka-restart-seller-orders.json
  wait_user_order_projection_by_price "$kafka_restart_buyer" "ETH_USDT" "116" "0.2" "FILLED" "0.2" "23.2" /tmp/opex-e2e-kafka-restart-buyer-orders.json
  deadline=$((SECONDS + 120))
  until try_wallet_balance "$kafka_restart_seller" "ETH" "0.8" &&
    try_wallet_balance "$kafka_restart_seller" "USDT" "22.968" &&
    try_wallet_balance "$kafka_restart_buyer" "ETH" "0.198" &&
    try_wallet_balance "$kafka_restart_buyer" "USDT" "76.8"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for kafka-restart wallet settlement" >&2
      assert_wallet_balance "kafka-restart seller ETH remainder" "$kafka_restart_seller" "ETH" "0.8" >&2 || true
      assert_wallet_balance "kafka-restart seller USDT proceeds" "$kafka_restart_seller" "USDT" "22.968" >&2 || true
      assert_wallet_balance "kafka-restart buyer ETH received" "$kafka_restart_buyer" "ETH" "0.198" >&2 || true
      assert_wallet_balance "kafka-restart buyer USDT remainder" "$kafka_restart_buyer" "USDT" "76.8" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 kafka-1 accountant wallet market matching-engine matching-gateway >&2 || true
      exit 1
    fi
    sleep 2
  done

  local postgres_restart_seller="e2e-pg-rs-s-$(date +%s)"
  local postgres_restart_buyer="e2e-pg-rs-b-$(date +%s)"
  local postgres_restart_ref="e2e-pg-rs-$(date +%s)"
  restart_postgres_datastores_and_wait
  expect_2xx_retry "postgres-restart seller ETH deposit after datastore restart" "curl_json POST 'http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${postgres_restart_seller}_MAIN?description=e2e-postgres-restart&transferRef=${postgres_restart_ref}-eth'" >/dev/null
  expect_2xx_retry "postgres-restart buyer USDT deposit after datastore restart" "curl_json POST 'http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${postgres_restart_buyer}_MAIN?description=e2e-postgres-restart&transferRef=${postgres_restart_ref}-usdt'" >/dev/null

  local postgres_restart_ask='{"uuid":null,"pair":"ETH_USDT","price":117,"quantity":0.2,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local postgres_restart_bid='{"uuid":null,"pair":"ETH_USDT","price":117,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "postgres-restart ask order after datastore restart" "curl_json POST 'http://127.0.0.1:8093/order' '$postgres_restart_ask' '$postgres_restart_seller'" >/tmp/opex-e2e-postgres-restart-ask.json
  wait_user_open_order "$postgres_restart_seller" "ETH_USDT" "117" "0.2" /tmp/opex-e2e-postgres-restart-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "117" "0.2"
  expect_2xx_retry "postgres-restart bid order after datastore restart" "curl_json POST 'http://127.0.0.1:8093/order' '$postgres_restart_bid' '$postgres_restart_buyer'" >/tmp/opex-e2e-postgres-restart-bid.json
  wait_no_user_open_orders "$postgres_restart_seller" "ETH_USDT"
  wait_user_trade_projection "$postgres_restart_seller" "ETH_USDT" "117" "0.2" "23.4" "0.234" "USDT" false true false /tmp/opex-e2e-postgres-restart-seller-trades.json
  wait_user_trade_projection "$postgres_restart_buyer" "ETH_USDT" "117" "0.2" "23.4" "0.002" "ETH" true false false /tmp/opex-e2e-postgres-restart-buyer-trades.json
  wait_user_order_projection_by_price "$postgres_restart_seller" "ETH_USDT" "117" "0.2" "FILLED" "0.2" "23.4" /tmp/opex-e2e-postgres-restart-seller-orders.json
  wait_user_order_projection_by_price "$postgres_restart_buyer" "ETH_USDT" "117" "0.2" "FILLED" "0.2" "23.4" /tmp/opex-e2e-postgres-restart-buyer-orders.json
  deadline=$((SECONDS + 120))
  until try_wallet_balance "$postgres_restart_seller" "ETH" "0.8" &&
    try_wallet_balance "$postgres_restart_seller" "USDT" "23.166" &&
    try_wallet_balance "$postgres_restart_buyer" "ETH" "0.198" &&
    try_wallet_balance "$postgres_restart_buyer" "USDT" "76.6"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for postgres-restart wallet settlement" >&2
      assert_wallet_balance "postgres-restart seller ETH remainder" "$postgres_restart_seller" "ETH" "0.8" >&2 || true
      assert_wallet_balance "postgres-restart seller USDT proceeds" "$postgres_restart_seller" "USDT" "23.166" >&2 || true
      assert_wallet_balance "postgres-restart buyer ETH received" "$postgres_restart_buyer" "ETH" "0.198" >&2 || true
      assert_wallet_balance "postgres-restart buyer USDT remainder" "$postgres_restart_buyer" "USDT" "76.6" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 postgres-wallet postgres-accountant postgres-market accountant wallet market matching-engine matching-gateway >&2 || true
      exit 1
    fi
    sleep 2
  done

  local cancel_owner="e2e-cancel-$(date +%s)"
  local cancel_ref="e2e-cancel-$(date +%s)"
  expect_2xx "cancel scenario ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${cancel_owner}_MAIN?description=e2e-cancel&transferRef=${cancel_ref}-eth")" >/dev/null

  local unmatched_ask='{"uuid":null,"pair":"ETH_USDT","price":150,"quantity":0.25,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "unmatched ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$unmatched_ask' '$cancel_owner'" >/tmp/opex-e2e-unmatched-ask.json

  wait_user_open_order "$cancel_owner" "ETH_USDT" "150" "0.25" /tmp/opex-e2e-cancel-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "150" "0.25"
  wait_binance_depth_level "ETHUSDT" "ASK" "150" "0.25" /tmp/opex-e2e-binance-depth.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$cancel_owner" "ETH" "0.75"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for unmatched order reservation" >&2
      assert_wallet_balance "cancel owner reserved ETH" "$cancel_owner" "ETH" "0.75" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local cancel_ouid cancel_order_id cancel_request
  cancel_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-cancel-open-orders.json)"
  cancel_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-cancel-open-orders.json)"
  if [[ -z "$cancel_ouid" || "$cancel_ouid" == "null" || -z "$cancel_order_id" || "$cancel_order_id" == "null" ]]; then
    echo "Open order did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-cancel-open-orders.json >&2
    exit 1
  fi

  local cancel_query_request cancel_query_missing_lookup cancel_query_zero_id
  cancel_query_request="$(jq -nc --argjson orderId "$cancel_order_id" '{symbol:"ETH_USDT", orderId:$orderId, origClientOrderId:null}')"
  expect_2xx_retry "market owner query order by orderId" "curl_json POST 'http://127.0.0.1:8096/v1/user/${cancel_owner}/order/query' '$cancel_query_request'" >/tmp/opex-e2e-cancel-query-order.json
  jq -e --arg ouid "$cancel_ouid" --argjson orderId "$cancel_order_id" '
    .ouid == $ouid and
    .orderId == $orderId and
    .symbol == "ETH_USDT" and
    .status == "NEW" and
    .price == 150 and
    .quantity == 0.25 and
    .executedQuantity == 0 and
    .accumulativeQuoteQty == 0
  ' /tmp/opex-e2e-cancel-query-order.json >/dev/null

  cancel_query_missing_lookup='{"symbol":"ETH_USDT","orderId":null,"origClientOrderId":null}'
  cancel_query_zero_id='{"symbol":"ETH_USDT","orderId":0,"origClientOrderId":null}'
  expect_http_status "market order query missing lookup" "400" "$(curl_json POST "http://127.0.0.1:8096/v1/user/${cancel_owner}/order/query" "$cancel_query_missing_lookup")" >/tmp/opex-e2e-cancel-query-missing-lookup.json
  expect_http_status "market order query zero order id" "400" "$(curl_json POST "http://127.0.0.1:8096/v1/user/${cancel_owner}/order/query" "$cancel_query_zero_id")" >/tmp/opex-e2e-cancel-query-zero-id.json
  expect_http_status "market order query wrong owner forbidden" "403" "$(curl_json POST "http://127.0.0.1:8096/v1/user/${cancel_owner}-intruder/order/query" "$cancel_query_request")" >/tmp/opex-e2e-cancel-query-forbidden.json

  cancel_request="$(jq -nc --arg ouid "$cancel_ouid" --arg uuid "$cancel_owner" --argjson orderId "$cancel_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel unmatched ask order" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$cancel_request' '$cancel_owner'" >/tmp/opex-e2e-cancel-order.json

  wait_no_user_open_orders "$cancel_owner" "ETH_USDT"
  wait_order_projection "$cancel_owner" "$cancel_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$cancel_owner" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for cancel release settlement" >&2
      assert_wallet_balance "cancel owner released ETH" "$cancel_owner" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local partial_seller="e2e-partial-seller-$(date +%s)"
  local partial_buyer="e2e-partial-buyer-$(date +%s)"
  local partial_ref="e2e-partial-$(date +%s)"
  expect_2xx "partial seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/2_test-ethereum_ETH/${partial_seller}_MAIN?description=e2e-partial&transferRef=${partial_ref}-eth")" >/dev/null
  expect_2xx "partial buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/50_test-ethereum_USDT/${partial_buyer}_MAIN?description=e2e-partial&transferRef=${partial_ref}-usdt")" >/dev/null

  local partial_ask='{"uuid":null,"pair":"ETH_USDT","price":120,"quantity":1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local partial_bid='{"uuid":null,"pair":"ETH_USDT","price":120,"quantity":0.4,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "partial ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$partial_ask' '$partial_seller'" >/tmp/opex-e2e-partial-ask.json
  wait_user_open_order "$partial_seller" "ETH_USDT" "120" "1" /tmp/opex-e2e-partial-open-orders.json
  local partial_ask_ouid partial_ask_order_id partial_cancel_request
  partial_ask_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-partial-open-orders.json)"
  partial_ask_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-partial-open-orders.json)"
  if [[ -z "$partial_ask_ouid" || "$partial_ask_ouid" == "null" || -z "$partial_ask_order_id" || "$partial_ask_order_id" == "null" ]]; then
    echo "Partial ask open order did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-partial-open-orders.json >&2
    exit 1
  fi

  expect_2xx_retry "partial bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$partial_bid' '$partial_buyer'" >/tmp/opex-e2e-partial-bid.json
  wait_order_projection "$partial_seller" "$partial_ask_ouid" "PARTIALLY_FILLED" "0.4" "48"
  wait_order_book_level "ETH_USDT" "ASK" "120" "0.6"
  wait_user_trade_projection "$partial_seller" "ETH_USDT" "120" "0.4" "48" "0.48" "USDT" false true false /tmp/opex-e2e-partial-seller-trades.json
  wait_user_trade_projection "$partial_buyer" "ETH_USDT" "120" "0.4" "48" "0.004" "ETH" true false false /tmp/opex-e2e-partial-buyer-trades.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$partial_seller" "ETH" "1" &&
    try_wallet_balance "$partial_seller" "USDT" "47.52" &&
    try_wallet_balance "$partial_buyer" "ETH" "0.396" &&
    try_wallet_balance "$partial_buyer" "USDT" "2"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for partial-fill wallet settlement" >&2
      assert_wallet_balance "partial seller reserved ETH" "$partial_seller" "ETH" "1" >&2 || true
      assert_wallet_balance "partial seller USDT proceeds" "$partial_seller" "USDT" "47.52" >&2 || true
      assert_wallet_balance "partial buyer ETH received" "$partial_buyer" "ETH" "0.396" >&2 || true
      assert_wallet_balance "partial buyer USDT remainder" "$partial_buyer" "USDT" "2" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  partial_cancel_request="$(jq -nc --arg ouid "$partial_ask_ouid" --arg uuid "$partial_seller" --argjson orderId "$partial_ask_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel partial ask remainder" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$partial_cancel_request' '$partial_seller'" >/tmp/opex-e2e-partial-cancel-order.json
  wait_no_user_open_orders "$partial_seller" "ETH_USDT"
  wait_order_projection "$partial_seller" "$partial_ask_ouid" "CANCELED" "0.4" "48"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$partial_seller" "ETH" "1.6"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for partial cancel release settlement" >&2
      assert_wallet_balance "partial seller released ETH" "$partial_seller" "ETH" "1.6" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local ioc_owner="e2e-ioc-$(date +%s)"
  local ioc_ref="e2e-ioc-$(date +%s)"
  expect_2xx "ioc owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${ioc_owner}_MAIN?description=e2e-ioc&transferRef=${ioc_ref}-eth")" >/dev/null
  local ioc_ask='{"uuid":null,"pair":"ETH_USDT","price":999,"quantity":0.5,"direction":"ASK","matchConstraint":"IOC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "ioc ask no liquidity" "curl_json POST 'http://127.0.0.1:8093/order' '$ioc_ask' '$ioc_owner'" >/tmp/opex-e2e-ioc-ask.json
  wait_no_user_open_orders "$ioc_owner" "ETH_USDT"
  wait_user_order_status_by_price "$ioc_owner" "ETH_USDT" "999" "0.5" "CANCELED"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$ioc_owner" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for IOC no-liquidity release settlement" >&2
      assert_wallet_balance "ioc owner released ETH" "$ioc_owner" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local market_seller="e2e-market-seller-$(date +%s)"
  local market_buyer="e2e-market-buyer-$(date +%s)"
  local market_ref="e2e-market-$(date +%s)"
  expect_2xx "market seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${market_seller}_MAIN?description=e2e-market&transferRef=${market_ref}-eth")" >/dev/null
  expect_2xx "market buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/40_test-ethereum_USDT/${market_buyer}_MAIN?description=e2e-market&transferRef=${market_ref}-usdt")" >/dev/null

  local market_bid='{"uuid":null,"pair":"ETH_USDT","price":130,"quantity":0.3,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local market_ask='{"uuid":null,"pair":"ETH_USDT","price":0,"quantity":0.2,"direction":"ASK","matchConstraint":"IOC","orderType":"MARKET_ORDER","userLevel":"*"}'
  expect_2xx_retry "market maker bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$market_bid' '$market_buyer'" >/tmp/opex-e2e-market-bid.json
  wait_user_open_order "$market_buyer" "ETH_USDT" "130" "0.3" /tmp/opex-e2e-market-maker-open-orders.json
  local market_bid_ouid market_bid_order_id market_cancel_request
  market_bid_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-market-maker-open-orders.json)"
  market_bid_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-market-maker-open-orders.json)"
  if [[ -z "$market_bid_ouid" || "$market_bid_ouid" == "null" || -z "$market_bid_order_id" || "$market_bid_order_id" == "null" ]]; then
    echo "Market maker bid did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-market-maker-open-orders.json >&2
    exit 1
  fi

  expect_2xx_retry "market taker ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$market_ask' '$market_seller'" >/tmp/opex-e2e-market-ask.json
  wait_user_order_status_by_price "$market_seller" "ETH_USDT" "0" "0.2" "FILLED"
  wait_order_projection "$market_buyer" "$market_bid_ouid" "PARTIALLY_FILLED" "0.2" "26"
  wait_order_book_level "ETH_USDT" "BID" "130" "0.1"
  wait_user_trade_projection "$market_seller" "ETH_USDT" "130" "0.2" "26" "0.26" "USDT" false false true /tmp/opex-e2e-market-seller-trades.json
  wait_user_trade_projection "$market_buyer" "ETH_USDT" "130" "0.2" "26" "0.002" "ETH" true true true /tmp/opex-e2e-market-buyer-trades.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$market_seller" "ETH" "0.8" &&
    try_wallet_balance "$market_seller" "USDT" "25.74" &&
    try_wallet_balance "$market_buyer" "ETH" "0.198" &&
    try_wallet_balance "$market_buyer" "USDT" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for IOC market-fill wallet settlement" >&2
      assert_wallet_balance "market seller ETH remainder" "$market_seller" "ETH" "0.8" >&2 || true
      assert_wallet_balance "market seller USDT proceeds" "$market_seller" "USDT" "25.74" >&2 || true
      assert_wallet_balance "market buyer ETH received" "$market_buyer" "ETH" "0.198" >&2 || true
      assert_wallet_balance "market buyer USDT main remainder" "$market_buyer" "USDT" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  market_cancel_request="$(jq -nc --arg ouid "$market_bid_ouid" --arg uuid "$market_buyer" --argjson orderId "$market_bid_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel market maker bid remainder" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$market_cancel_request' '$market_buyer'" >/tmp/opex-e2e-market-cancel-order.json
  wait_no_user_open_orders "$market_buyer" "ETH_USDT"
  wait_order_projection "$market_buyer" "$market_bid_ouid" "CANCELED" "0.2" "26"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$market_buyer" "USDT" "14"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for market maker cancel release settlement" >&2
      assert_wallet_balance "market buyer released USDT" "$market_buyer" "USDT" "14" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local sweep_seller="e2e-sweep-seller-$(date +%s)"
  local sweep_high_buyer="e2e-sweep-high-$(date +%s)"
  local sweep_low_buyer="e2e-sweep-low-$(date +%s)"
  local sweep_ref="e2e-sweep-$(date +%s)"
  expect_2xx "sweep seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${sweep_seller}_MAIN?description=e2e-sweep&transferRef=${sweep_ref}-eth")" >/dev/null
  expect_2xx "sweep high buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/20_test-ethereum_USDT/${sweep_high_buyer}_MAIN?description=e2e-sweep&transferRef=${sweep_ref}-high-usdt")" >/dev/null
  expect_2xx "sweep low buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/50_test-ethereum_USDT/${sweep_low_buyer}_MAIN?description=e2e-sweep&transferRef=${sweep_ref}-low-usdt")" >/dev/null

  local sweep_high_bid='{"uuid":null,"pair":"ETH_USDT","price":150,"quantity":0.1,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local sweep_low_bid='{"uuid":null,"pair":"ETH_USDT","price":140,"quantity":0.3,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local sweep_market_ask='{"uuid":null,"pair":"ETH_USDT","price":0,"quantity":0.3,"direction":"ASK","matchConstraint":"IOC","orderType":"MARKET_ORDER","userLevel":"*"}'
  expect_2xx_retry "sweep high bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$sweep_high_bid' '$sweep_high_buyer'" >/tmp/opex-e2e-sweep-high-bid.json
  wait_user_open_order "$sweep_high_buyer" "ETH_USDT" "150" "0.1" /tmp/opex-e2e-sweep-high-open-orders.json
  expect_2xx_retry "sweep low bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$sweep_low_bid' '$sweep_low_buyer'" >/tmp/opex-e2e-sweep-low-bid.json
  wait_user_open_order "$sweep_low_buyer" "ETH_USDT" "140" "0.3" /tmp/opex-e2e-sweep-low-open-orders.json

  local sweep_low_ouid sweep_low_order_id sweep_low_cancel_request
  sweep_low_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-sweep-low-open-orders.json)"
  sweep_low_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-sweep-low-open-orders.json)"
  if [[ -z "$sweep_low_ouid" || "$sweep_low_ouid" == "null" || -z "$sweep_low_order_id" || "$sweep_low_order_id" == "null" ]]; then
    echo "Sweep low bid did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-sweep-low-open-orders.json >&2
    exit 1
  fi

  expect_2xx_retry "sweep market taker ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$sweep_market_ask' '$sweep_seller'" >/tmp/opex-e2e-sweep-market-ask.json
  wait_user_order_status_by_price "$sweep_seller" "ETH_USDT" "0" "0.3" "FILLED"
  wait_no_user_open_orders "$sweep_high_buyer" "ETH_USDT"
  wait_order_projection "$sweep_low_buyer" "$sweep_low_ouid" "PARTIALLY_FILLED" "0.2" "28"
  wait_order_book_level "ETH_USDT" "BID" "140" "0.1"
  wait_user_trade_projection "$sweep_seller" "ETH_USDT" "150" "0.1" "15" "0.15" "USDT" false false true /tmp/opex-e2e-sweep-seller-high-trades.json
  wait_user_trade_projection "$sweep_seller" "ETH_USDT" "140" "0.2" "28" "0.28" "USDT" false false true /tmp/opex-e2e-sweep-seller-low-trades.json
  wait_user_trade_projection "$sweep_high_buyer" "ETH_USDT" "150" "0.1" "15" "0.001" "ETH" true true true /tmp/opex-e2e-sweep-high-buyer-trades.json
  wait_user_trade_projection "$sweep_low_buyer" "ETH_USDT" "140" "0.2" "28" "0.002" "ETH" true true true /tmp/opex-e2e-sweep-low-buyer-trades.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$sweep_seller" "ETH" "0.7" &&
    try_wallet_balance "$sweep_seller" "USDT" "42.57" &&
    try_wallet_balance "$sweep_high_buyer" "ETH" "0.099" &&
    try_wallet_balance "$sweep_high_buyer" "USDT" "5" &&
    try_wallet_balance "$sweep_low_buyer" "ETH" "0.198" &&
    try_wallet_balance "$sweep_low_buyer" "USDT" "8"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for multi-level sweep settlement" >&2
      assert_wallet_balance "sweep seller ETH remainder" "$sweep_seller" "ETH" "0.7" >&2 || true
      assert_wallet_balance "sweep seller USDT proceeds" "$sweep_seller" "USDT" "42.57" >&2 || true
      assert_wallet_balance "sweep high buyer ETH received" "$sweep_high_buyer" "ETH" "0.099" >&2 || true
      assert_wallet_balance "sweep high buyer USDT remainder" "$sweep_high_buyer" "USDT" "5" >&2 || true
      assert_wallet_balance "sweep low buyer ETH received" "$sweep_low_buyer" "ETH" "0.198" >&2 || true
      assert_wallet_balance "sweep low buyer USDT reserved remainder" "$sweep_low_buyer" "USDT" "8" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  sweep_low_cancel_request="$(jq -nc --arg ouid "$sweep_low_ouid" --arg uuid "$sweep_low_buyer" --argjson orderId "$sweep_low_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel sweep low bid remainder" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$sweep_low_cancel_request' '$sweep_low_buyer'" >/tmp/opex-e2e-sweep-low-cancel.json
  wait_no_user_open_orders "$sweep_low_buyer" "ETH_USDT"
  wait_order_projection "$sweep_low_buyer" "$sweep_low_ouid" "CANCELED" "0.2" "28"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$sweep_low_buyer" "USDT" "22"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for sweep low-bid cancel release settlement" >&2
      assert_wallet_balance "sweep low buyer released USDT" "$sweep_low_buyer" "USDT" "22" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local bid_sweep_buyer="e2e-bid-sweep-buyer-$(date +%s)"
  local bid_sweep_low_seller="e2e-bid-sweep-low-$(date +%s)"
  local bid_sweep_high_seller="e2e-bid-sweep-high-$(date +%s)"
  local bid_sweep_ref="e2e-bid-sweep-$(date +%s)"
  expect_2xx "bid-sweep buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/80_test-ethereum_USDT/${bid_sweep_buyer}_MAIN?description=e2e-bid-sweep&transferRef=${bid_sweep_ref}-usdt")" >/dev/null
  expect_2xx "bid-sweep low seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${bid_sweep_low_seller}_MAIN?description=e2e-bid-sweep&transferRef=${bid_sweep_ref}-low-eth")" >/dev/null
  expect_2xx "bid-sweep high seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${bid_sweep_high_seller}_MAIN?description=e2e-bid-sweep&transferRef=${bid_sweep_ref}-high-eth")" >/dev/null

  local bid_sweep_low_ask='{"uuid":null,"pair":"ETH_USDT","price":90,"quantity":0.1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local bid_sweep_high_ask='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0.3,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local bid_sweep_market_bid='{"uuid":null,"pair":"ETH_USDT","price":200,"quantity":0.3,"direction":"BID","matchConstraint":"IOC","orderType":"MARKET_ORDER","userLevel":"*"}'
  expect_2xx_retry "bid-sweep low ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$bid_sweep_low_ask' '$bid_sweep_low_seller'" >/tmp/opex-e2e-bid-sweep-low-ask.json
  wait_user_open_order "$bid_sweep_low_seller" "ETH_USDT" "90" "0.1" /tmp/opex-e2e-bid-sweep-low-open-orders.json
  expect_2xx_retry "bid-sweep high ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$bid_sweep_high_ask' '$bid_sweep_high_seller'" >/tmp/opex-e2e-bid-sweep-high-ask.json
  wait_user_open_order "$bid_sweep_high_seller" "ETH_USDT" "100" "0.3" /tmp/opex-e2e-bid-sweep-high-open-orders.json

  local bid_sweep_high_ouid bid_sweep_high_order_id bid_sweep_high_cancel_request
  bid_sweep_high_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-bid-sweep-high-open-orders.json)"
  bid_sweep_high_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-bid-sweep-high-open-orders.json)"
  if [[ -z "$bid_sweep_high_ouid" || "$bid_sweep_high_ouid" == "null" || -z "$bid_sweep_high_order_id" || "$bid_sweep_high_order_id" == "null" ]]; then
    echo "Bid-sweep high ask did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-bid-sweep-high-open-orders.json >&2
    exit 1
  fi

  expect_2xx_retry "bid-sweep market taker bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$bid_sweep_market_bid' '$bid_sweep_buyer'" >/tmp/opex-e2e-bid-sweep-market-bid.json
  wait_user_order_status_by_price "$bid_sweep_buyer" "ETH_USDT" "200" "0.3" "FILLED"
  wait_no_user_open_orders "$bid_sweep_low_seller" "ETH_USDT"
  wait_order_projection "$bid_sweep_high_seller" "$bid_sweep_high_ouid" "PARTIALLY_FILLED" "0.2" "20"
  wait_order_book_level "ETH_USDT" "ASK" "100" "0.1"
  wait_user_trade_projection "$bid_sweep_low_seller" "ETH_USDT" "90" "0.1" "9" "0.09" "USDT" false true false /tmp/opex-e2e-bid-sweep-low-seller-trades.json
  wait_user_trade_projection "$bid_sweep_high_seller" "ETH_USDT" "100" "0.2" "20" "0.2" "USDT" false true false /tmp/opex-e2e-bid-sweep-high-seller-trades.json
  wait_user_trade_projection "$bid_sweep_buyer" "ETH_USDT" "90" "0.1" "9" "0.001" "ETH" true false false /tmp/opex-e2e-bid-sweep-buyer-low-trades.json
  wait_user_trade_projection "$bid_sweep_buyer" "ETH_USDT" "100" "0.2" "20" "0.002" "ETH" true false false /tmp/opex-e2e-bid-sweep-buyer-high-trades.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$bid_sweep_buyer" "ETH" "0.297" &&
    try_wallet_balance "$bid_sweep_buyer" "USDT" "51" &&
    try_wallet_balance "$bid_sweep_low_seller" "ETH" "0.9" &&
    try_wallet_balance "$bid_sweep_low_seller" "USDT" "8.91" &&
    try_wallet_balance "$bid_sweep_high_seller" "ETH" "0.7" &&
    try_wallet_balance "$bid_sweep_high_seller" "USDT" "19.8"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for multi-level bid sweep settlement" >&2
      assert_wallet_balance "bid-sweep buyer ETH received" "$bid_sweep_buyer" "ETH" "0.297" >&2 || true
      assert_wallet_balance "bid-sweep buyer USDT remainder" "$bid_sweep_buyer" "USDT" "51" >&2 || true
      assert_wallet_balance "bid-sweep low seller ETH remainder" "$bid_sweep_low_seller" "ETH" "0.9" >&2 || true
      assert_wallet_balance "bid-sweep low seller USDT proceeds" "$bid_sweep_low_seller" "USDT" "8.91" >&2 || true
      assert_wallet_balance "bid-sweep high seller ETH reserved remainder" "$bid_sweep_high_seller" "ETH" "0.7" >&2 || true
      assert_wallet_balance "bid-sweep high seller USDT proceeds" "$bid_sweep_high_seller" "USDT" "19.8" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  bid_sweep_high_cancel_request="$(jq -nc --arg ouid "$bid_sweep_high_ouid" --arg uuid "$bid_sweep_high_seller" --argjson orderId "$bid_sweep_high_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel bid-sweep high ask remainder" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$bid_sweep_high_cancel_request' '$bid_sweep_high_seller'" >/tmp/opex-e2e-bid-sweep-high-cancel.json
  wait_no_user_open_orders "$bid_sweep_high_seller" "ETH_USDT"
  wait_order_projection "$bid_sweep_high_seller" "$bid_sweep_high_ouid" "CANCELED" "0.2" "20"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$bid_sweep_high_seller" "ETH" "0.8"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for bid-sweep high-ask cancel release settlement" >&2
      assert_wallet_balance "bid-sweep high seller released ETH" "$bid_sweep_high_seller" "ETH" "0.8" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local priority_seller="e2e-priority-seller-$(date +%s)"
  local priority_low_buyer="e2e-priority-low-$(date +%s)"
  local priority_high_buyer="e2e-priority-high-$(date +%s)"
  local priority_ref="e2e-priority-$(date +%s)"
  expect_2xx "priority seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${priority_seller}_MAIN?description=e2e-priority&transferRef=${priority_ref}-eth")" >/dev/null
  expect_2xx "priority low buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${priority_low_buyer}_MAIN?description=e2e-priority&transferRef=${priority_ref}-low-usdt")" >/dev/null
  expect_2xx "priority high buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${priority_high_buyer}_MAIN?description=e2e-priority&transferRef=${priority_ref}-high-usdt")" >/dev/null

  local low_bid='{"uuid":null,"pair":"ETH_USDT","price":110,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local high_bid='{"uuid":null,"pair":"ETH_USDT","price":140,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local priority_ask='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0.2,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "priority low bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$low_bid' '$priority_low_buyer'" >/tmp/opex-e2e-priority-low-bid.json
  wait_user_open_order "$priority_low_buyer" "ETH_USDT" "110" "0.2" /tmp/opex-e2e-priority-low-open-orders.json
  local priority_low_ouid priority_low_order_id priority_low_cancel_request
  priority_low_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-priority-low-open-orders.json)"
  priority_low_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-priority-low-open-orders.json)"
  if [[ -z "$priority_low_ouid" || "$priority_low_ouid" == "null" || -z "$priority_low_order_id" || "$priority_low_order_id" == "null" ]]; then
    echo "Priority low bid did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-priority-low-open-orders.json >&2
    exit 1
  fi

  expect_2xx_retry "priority high bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$high_bid' '$priority_high_buyer'" >/tmp/opex-e2e-priority-high-bid.json
  wait_user_open_order "$priority_high_buyer" "ETH_USDT" "140" "0.2" /tmp/opex-e2e-priority-high-open-orders.json
  expect_2xx_retry "priority taker ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$priority_ask' '$priority_seller'" >/tmp/opex-e2e-priority-ask.json
  wait_user_trade_projection "$priority_seller" "ETH_USDT" "140" "0.2" "28" "0.28" "USDT" false false true /tmp/opex-e2e-priority-seller-trades.json
  wait_user_trade_projection "$priority_high_buyer" "ETH_USDT" "140" "0.2" "28" "0.002" "ETH" true true true /tmp/opex-e2e-priority-high-buyer-trades.json
  wait_no_user_open_orders "$priority_high_buyer" "ETH_USDT"
  wait_order_projection "$priority_low_buyer" "$priority_low_ouid" "NEW" "0" "0"
  wait_order_book_level "ETH_USDT" "BID" "110" "0.2"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$priority_seller" "ETH" "0.8" &&
    try_wallet_balance "$priority_seller" "USDT" "27.72" &&
    try_wallet_balance "$priority_high_buyer" "ETH" "0.198" &&
    try_wallet_balance "$priority_high_buyer" "USDT" "2" &&
    try_wallet_balance "$priority_low_buyer" "USDT" "8"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for price-priority settlement" >&2
      assert_wallet_balance "priority seller ETH remainder" "$priority_seller" "ETH" "0.8" >&2 || true
      assert_wallet_balance "priority seller USDT proceeds" "$priority_seller" "USDT" "27.72" >&2 || true
      assert_wallet_balance "priority high buyer ETH received" "$priority_high_buyer" "ETH" "0.198" >&2 || true
      assert_wallet_balance "priority high buyer USDT remainder" "$priority_high_buyer" "USDT" "2" >&2 || true
      assert_wallet_balance "priority low buyer reserved USDT" "$priority_low_buyer" "USDT" "8" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  priority_low_cancel_request="$(jq -nc --arg ouid "$priority_low_ouid" --arg uuid "$priority_low_buyer" --argjson orderId "$priority_low_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel priority low bid" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$priority_low_cancel_request' '$priority_low_buyer'" >/tmp/opex-e2e-priority-low-cancel.json
  wait_no_user_open_orders "$priority_low_buyer" "ETH_USDT"
  wait_order_projection "$priority_low_buyer" "$priority_low_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$priority_low_buyer" "USDT" "30"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for priority low-bid cancel release settlement" >&2
      assert_wallet_balance "priority low buyer released USDT" "$priority_low_buyer" "USDT" "30" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local fifo_seller="e2e-fifo-seller-$(date +%s)"
  local fifo_first_buyer="e2e-fifo-first-$(date +%s)"
  local fifo_second_buyer="e2e-fifo-second-$(date +%s)"
  local fifo_ref="e2e-fifo-$(date +%s)"
  expect_2xx "fifo seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${fifo_seller}_MAIN?description=e2e-fifo&transferRef=${fifo_ref}-eth")" >/dev/null
  expect_2xx "fifo first buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${fifo_first_buyer}_MAIN?description=e2e-fifo&transferRef=${fifo_ref}-first-usdt")" >/dev/null
  expect_2xx "fifo second buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${fifo_second_buyer}_MAIN?description=e2e-fifo&transferRef=${fifo_ref}-second-usdt")" >/dev/null

  local fifo_first_bid='{"uuid":null,"pair":"ETH_USDT","price":125,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local fifo_second_bid='{"uuid":null,"pair":"ETH_USDT","price":125,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local fifo_ask='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0.2,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "fifo first bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$fifo_first_bid' '$fifo_first_buyer'" >/tmp/opex-e2e-fifo-first-bid.json
  wait_user_open_order "$fifo_first_buyer" "ETH_USDT" "125" "0.2" /tmp/opex-e2e-fifo-first-open-orders.json
  expect_2xx_retry "fifo second bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$fifo_second_bid' '$fifo_second_buyer'" >/tmp/opex-e2e-fifo-second-bid.json
  wait_user_open_order "$fifo_second_buyer" "ETH_USDT" "125" "0.2" /tmp/opex-e2e-fifo-second-open-orders.json
  wait_binance_depth_level "ETHUSDT" "BID" "125" "0.4" /tmp/opex-e2e-binance-depth-aggregated-fifo.json

  local fifo_second_ouid fifo_second_order_id fifo_second_cancel_request
  fifo_second_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-fifo-second-open-orders.json)"
  fifo_second_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-fifo-second-open-orders.json)"
  if [[ -z "$fifo_second_ouid" || "$fifo_second_ouid" == "null" || -z "$fifo_second_order_id" || "$fifo_second_order_id" == "null" ]]; then
    echo "FIFO second bid did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-fifo-second-open-orders.json >&2
    exit 1
  fi

  expect_2xx_retry "fifo taker ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$fifo_ask' '$fifo_seller'" >/tmp/opex-e2e-fifo-ask.json
  wait_user_trade_projection "$fifo_seller" "ETH_USDT" "125" "0.2" "25" "0.25" "USDT" false false true /tmp/opex-e2e-fifo-seller-trades.json
  wait_user_trade_projection "$fifo_first_buyer" "ETH_USDT" "125" "0.2" "25" "0.002" "ETH" true true true /tmp/opex-e2e-fifo-first-buyer-trades.json
  wait_no_user_open_orders "$fifo_first_buyer" "ETH_USDT"
  wait_order_projection "$fifo_second_buyer" "$fifo_second_ouid" "NEW" "0" "0"
  wait_order_book_level "ETH_USDT" "BID" "125" "0.2"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$fifo_seller" "ETH" "0.8" &&
    try_wallet_balance "$fifo_seller" "USDT" "24.75" &&
    try_wallet_balance "$fifo_first_buyer" "ETH" "0.198" &&
    try_wallet_balance "$fifo_first_buyer" "USDT" "5" &&
    try_wallet_balance "$fifo_second_buyer" "USDT" "5"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for FIFO settlement" >&2
      assert_wallet_balance "fifo seller ETH remainder" "$fifo_seller" "ETH" "0.8" >&2 || true
      assert_wallet_balance "fifo seller USDT proceeds" "$fifo_seller" "USDT" "24.75" >&2 || true
      assert_wallet_balance "fifo first buyer ETH received" "$fifo_first_buyer" "ETH" "0.198" >&2 || true
      assert_wallet_balance "fifo first buyer USDT remainder" "$fifo_first_buyer" "USDT" "5" >&2 || true
      assert_wallet_balance "fifo second buyer reserved USDT" "$fifo_second_buyer" "USDT" "5" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  fifo_second_cancel_request="$(jq -nc --arg ouid "$fifo_second_ouid" --arg uuid "$fifo_second_buyer" --argjson orderId "$fifo_second_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel fifo second bid" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$fifo_second_cancel_request' '$fifo_second_buyer'" >/tmp/opex-e2e-fifo-second-cancel.json
  wait_no_user_open_orders "$fifo_second_buyer" "ETH_USDT"
  wait_order_projection "$fifo_second_buyer" "$fifo_second_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$fifo_second_buyer" "USDT" "30"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for FIFO second-bid cancel release settlement" >&2
      assert_wallet_balance "fifo second buyer released USDT" "$fifo_second_buyer" "USDT" "30" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local overreserve_owner="e2e-overreserve-$(date +%s)"
  local overreserve_ref="e2e-overreserve-$(date +%s)"
  expect_2xx "overreserve owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${overreserve_owner}_MAIN?description=e2e-overreserve&transferRef=${overreserve_ref}-eth")" >/dev/null

  local overreserve_first_ask='{"uuid":null,"pair":"ETH_USDT","price":160,"quantity":0.7,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local overreserve_second_ask='{"uuid":null,"pair":"ETH_USDT","price":161,"quantity":0.5,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "overreserve first ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$overreserve_first_ask' '$overreserve_owner'" >/tmp/opex-e2e-overreserve-first-ask.json
  wait_user_open_order "$overreserve_owner" "ETH_USDT" "160" "0.7" /tmp/opex-e2e-overreserve-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "160" "0.7"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$overreserve_owner" "ETH" "0.3"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for overreserve first-order reservation" >&2
      assert_wallet_balance "overreserve owner reserved ETH" "$overreserve_owner" "ETH" "0.3" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  expect_http_status "overreserve second ask order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$overreserve_second_ask" "$overreserve_owner")" >/tmp/opex-e2e-overreserve-reject.json
  wait_user_open_order "$overreserve_owner" "ETH_USDT" "160" "0.7" /tmp/opex-e2e-overreserve-open-orders.json
  assert_no_user_order_by_price "$overreserve_owner" "ETH_USDT" "161" "0.5"
  assert_wallet_balance "overreserve owner rejected-order ETH unchanged" "$overreserve_owner" "ETH" "0.3"

  local overreserve_ouid overreserve_order_id overreserve_cancel_request
  overreserve_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-overreserve-open-orders.json)"
  overreserve_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-overreserve-open-orders.json)"
  if [[ -z "$overreserve_ouid" || "$overreserve_ouid" == "null" || -z "$overreserve_order_id" || "$overreserve_order_id" == "null" ]]; then
    echo "Overreserve first ask did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-overreserve-open-orders.json >&2
    exit 1
  fi

  overreserve_cancel_request="$(jq -nc --arg ouid "$overreserve_ouid" --arg uuid "$overreserve_owner" --argjson orderId "$overreserve_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel overreserve first ask" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$overreserve_cancel_request' '$overreserve_owner'" >/tmp/opex-e2e-overreserve-cancel.json
  wait_no_user_open_orders "$overreserve_owner" "ETH_USDT"
  wait_order_projection "$overreserve_owner" "$overreserve_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$overreserve_owner" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for overreserve cancel release settlement" >&2
      assert_wallet_balance "overreserve owner released ETH" "$overreserve_owner" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local bid_overreserve_owner="e2e-bid-overreserve-$(date +%s)"
  local bid_overreserve_ref="e2e-bid-overreserve-$(date +%s)"
  expect_2xx "bid-overreserve owner USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${bid_overreserve_owner}_MAIN?description=e2e-bid-overreserve&transferRef=${bid_overreserve_ref}-usdt")" >/dev/null

  local bid_overreserve_first_bid='{"uuid":null,"pair":"ETH_USDT","price":80,"quantity":0.8,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local bid_overreserve_second_bid='{"uuid":null,"pair":"ETH_USDT","price":80,"quantity":0.5,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "bid-overreserve first bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$bid_overreserve_first_bid' '$bid_overreserve_owner'" >/tmp/opex-e2e-bid-overreserve-first-bid.json
  wait_user_open_order "$bid_overreserve_owner" "ETH_USDT" "80" "0.8" /tmp/opex-e2e-bid-overreserve-open-orders.json
  wait_order_book_level "ETH_USDT" "BID" "80" "0.8"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$bid_overreserve_owner" "USDT" "36"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for bid-overreserve first-order reservation" >&2
      assert_wallet_balance "bid-overreserve owner reserved USDT" "$bid_overreserve_owner" "USDT" "36" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  expect_http_status "bid-overreserve second bid order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$bid_overreserve_second_bid" "$bid_overreserve_owner")" >/tmp/opex-e2e-bid-overreserve-reject.json
  wait_user_open_order "$bid_overreserve_owner" "ETH_USDT" "80" "0.8" /tmp/opex-e2e-bid-overreserve-open-orders.json
  assert_no_user_order_by_price "$bid_overreserve_owner" "ETH_USDT" "80" "0.5"
  assert_wallet_balance "bid-overreserve owner rejected-order USDT unchanged" "$bid_overreserve_owner" "USDT" "36"

  local bid_overreserve_ouid bid_overreserve_order_id bid_overreserve_cancel_request
  bid_overreserve_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-bid-overreserve-open-orders.json)"
  bid_overreserve_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-bid-overreserve-open-orders.json)"
  if [[ -z "$bid_overreserve_ouid" || "$bid_overreserve_ouid" == "null" || -z "$bid_overreserve_order_id" || "$bid_overreserve_order_id" == "null" ]]; then
    echo "Bid-overreserve first bid did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-bid-overreserve-open-orders.json >&2
    exit 1
  fi

  bid_overreserve_cancel_request="$(jq -nc --arg ouid "$bid_overreserve_ouid" --arg uuid "$bid_overreserve_owner" --argjson orderId "$bid_overreserve_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel bid-overreserve first bid" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$bid_overreserve_cancel_request' '$bid_overreserve_owner'" >/tmp/opex-e2e-bid-overreserve-cancel.json
  wait_no_user_open_orders "$bid_overreserve_owner" "ETH_USDT"
  wait_order_projection "$bid_overreserve_owner" "$bid_overreserve_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$bid_overreserve_owner" "USDT" "100"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for bid-overreserve cancel release settlement" >&2
      assert_wallet_balance "bid-overreserve owner released USDT" "$bid_overreserve_owner" "USDT" "100" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local cancel_auth_owner="e2e-cancel-auth-owner-$(date +%s)"
  local cancel_auth_intruder="e2e-cancel-auth-intruder-$(date +%s)"
  local cancel_auth_ref="e2e-cancel-auth-$(date +%s)"
  expect_2xx "cancel-auth owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${cancel_auth_owner}_MAIN?description=e2e-cancel-auth&transferRef=${cancel_auth_ref}-eth")" >/dev/null

  local cancel_auth_ask='{"uuid":null,"pair":"ETH_USDT","price":170,"quantity":0.4,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "cancel-auth owner ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$cancel_auth_ask' '$cancel_auth_owner'" >/tmp/opex-e2e-cancel-auth-ask.json
  wait_user_open_order "$cancel_auth_owner" "ETH_USDT" "170" "0.4" /tmp/opex-e2e-cancel-auth-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "170" "0.4"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$cancel_auth_owner" "ETH" "0.6"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for cancel-auth owner reservation" >&2
      assert_wallet_balance "cancel-auth owner reserved ETH" "$cancel_auth_owner" "ETH" "0.6" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local cancel_auth_ouid cancel_auth_order_id cancel_auth_intruder_request cancel_auth_owner_request
  cancel_auth_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-cancel-auth-open-orders.json)"
  cancel_auth_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-cancel-auth-open-orders.json)"
  if [[ -z "$cancel_auth_ouid" || "$cancel_auth_ouid" == "null" || -z "$cancel_auth_order_id" || "$cancel_auth_order_id" == "null" ]]; then
    echo "Cancel-auth owner ask did not include ouid/orderId required for cancel" >&2
    cat /tmp/opex-e2e-cancel-auth-open-orders.json >&2
    exit 1
  fi

  cancel_auth_intruder_request="$(jq -nc --arg ouid "$cancel_auth_ouid" --arg uuid "$cancel_auth_intruder" --argjson orderId "$cancel_auth_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "intruder cancel owner order submit" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$cancel_auth_intruder_request' '$cancel_auth_intruder'" >/tmp/opex-e2e-cancel-auth-intruder-submit.json
  sleep 5
  wait_user_open_order "$cancel_auth_owner" "ETH_USDT" "170" "0.4" /tmp/opex-e2e-cancel-auth-open-orders.json
  assert_wallet_balance "cancel-auth owner ETH still reserved" "$cancel_auth_owner" "ETH" "0.6"

  local cancel_auth_intruder_edit_request
  cancel_auth_intruder_edit_request="$(jq -nc --arg ouid "$cancel_auth_ouid" --arg uuid "$cancel_auth_intruder" --argjson orderId "$cancel_auth_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT", price:171, quantity:0.3}')"
  expect_2xx_retry "intruder edit owner order submit" "curl_json POST 'http://127.0.0.1:8093/order/edit' '$cancel_auth_intruder_edit_request' '$cancel_auth_intruder'" >/tmp/opex-e2e-cancel-auth-intruder-edit.json
  sleep 5
  wait_user_open_order "$cancel_auth_owner" "ETH_USDT" "170" "0.4" /tmp/opex-e2e-cancel-auth-open-orders.json
  assert_no_user_order_by_price "$cancel_auth_owner" "ETH_USDT" "171" "0.3"
  wait_no_user_open_orders "$cancel_auth_intruder" "ETH_USDT"
  assert_wallet_balance "cancel-auth owner ETH still reserved after intruder edit" "$cancel_auth_owner" "ETH" "0.6"
  wait_query_eq "intruder edit reject eventlog audit" "postgres-eventlog" "ORDER_NOT_FOUND,EDIT_ORDER,1,0" "
    select event_json::jsonb ->> 'reason',
           event_json::jsonb ->> 'requestedOperation',
           count(*),
           sum(case when event_json is null or event_json = '' then 1 else 0 end)
    from opex_events
    where event = 'RejectOrderEvent'
      and uuid = '$cancel_auth_intruder'
      and event_json::jsonb ->> 'requestedOperation' = 'EDIT_ORDER'
    group by event_json::jsonb ->> 'reason',
             event_json::jsonb ->> 'requestedOperation';
  "

  cancel_auth_owner_request="$(jq -nc --arg ouid "$cancel_auth_ouid" --arg uuid "$cancel_auth_owner" --argjson orderId "$cancel_auth_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "owner cancel after intruder reject" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$cancel_auth_owner_request' '$cancel_auth_owner'" >/tmp/opex-e2e-cancel-auth-owner-cancel.json
  wait_no_user_open_orders "$cancel_auth_owner" "ETH_USDT"
  wait_order_projection "$cancel_auth_owner" "$cancel_auth_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$cancel_auth_owner" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for cancel-auth owner release settlement" >&2
      assert_wallet_balance "cancel-auth owner released ETH" "$cancel_auth_owner" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  expect_2xx_retry "duplicate owner cancel after release" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$cancel_auth_owner_request' '$cancel_auth_owner'" >/tmp/opex-e2e-cancel-auth-duplicate-cancel.json
  sleep 5
  wait_no_user_open_orders "$cancel_auth_owner" "ETH_USDT"
  assert_wallet_balance "cancel-auth owner ETH unchanged after duplicate cancel" "$cancel_auth_owner" "ETH" "1"

  local malformed_cancel_owner="e2e-bad-cancel-$(date +%s)"
  local malformed_cancel_ref="e2e-bad-cancel-$(date +%s)"
  expect_2xx "malformed-cancel owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${malformed_cancel_owner}_MAIN?description=e2e-bad-cancel&transferRef=${malformed_cancel_ref}-eth")" >/dev/null

  local malformed_cancel_ask='{"uuid":null,"pair":"ETH_USDT","price":171,"quantity":0.4,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "malformed-cancel owner ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$malformed_cancel_ask' '$malformed_cancel_owner'" >/tmp/opex-e2e-bad-cancel-ask.json
  wait_user_open_order "$malformed_cancel_owner" "ETH_USDT" "171" "0.4" /tmp/opex-e2e-bad-cancel-open-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$malformed_cancel_owner" "ETH" "0.6"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for malformed-cancel owner reservation" >&2
      assert_wallet_balance "malformed-cancel owner ETH reserved before invalid cancels" "$malformed_cancel_owner" "ETH" "0.6" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local malformed_cancel_ouid malformed_cancel_order_id malformed_cancel_bad_symbol malformed_cancel_negative_id malformed_cancel_owner_request
  malformed_cancel_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-bad-cancel-open-orders.json)"
  malformed_cancel_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-bad-cancel-open-orders.json)"
  malformed_cancel_bad_symbol="$(jq -nc --arg ouid "$malformed_cancel_ouid" --arg uuid "$malformed_cancel_owner" --argjson orderId "$malformed_cancel_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETHUSDT"}')"
  malformed_cancel_negative_id="$(jq -nc --arg ouid "$malformed_cancel_ouid" --arg uuid "$malformed_cancel_owner" '{ouid:$ouid, uuid:$uuid, orderId:-1, symbol:"ETH_USDT"}')"
  expect_http_status "malformed cancel bad symbol" "400" "$(curl_json POST "http://127.0.0.1:8093/order/cancel" "$malformed_cancel_bad_symbol" "$malformed_cancel_owner")" >/tmp/opex-e2e-bad-cancel-symbol.json
  expect_http_status "malformed cancel negative order id" "400" "$(curl_json POST "http://127.0.0.1:8093/order/cancel" "$malformed_cancel_negative_id" "$malformed_cancel_owner")" >/tmp/opex-e2e-bad-cancel-negative-id.json
  wait_user_open_order "$malformed_cancel_owner" "ETH_USDT" "171" "0.4" /tmp/opex-e2e-bad-cancel-open-orders.json
  assert_wallet_balance "malformed-cancel owner ETH still reserved after invalid cancels" "$malformed_cancel_owner" "ETH" "0.6"

  malformed_cancel_owner_request="$(jq -nc --arg ouid "$malformed_cancel_ouid" --arg uuid "$malformed_cancel_owner" --argjson orderId "$malformed_cancel_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "malformed-cancel owner cleanup cancel" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$malformed_cancel_owner_request' '$malformed_cancel_owner'" >/tmp/opex-e2e-bad-cancel-cleanup.json
  wait_no_user_open_orders "$malformed_cancel_owner" "ETH_USDT"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$malformed_cancel_owner" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for malformed-cancel owner release settlement" >&2
      assert_wallet_balance "malformed-cancel owner ETH released after cleanup" "$malformed_cancel_owner" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local malformed_edit_owner="e2e-bad-edit-$(date +%s)"
  local malformed_edit_ref="e2e-bad-edit-$(date +%s)"
  expect_2xx "malformed-edit owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${malformed_edit_owner}_MAIN?description=e2e-bad-edit&transferRef=${malformed_edit_ref}-eth")" >/dev/null

  local malformed_edit_ask='{"uuid":null,"pair":"ETH_USDT","price":172,"quantity":0.4,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "malformed-edit owner ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$malformed_edit_ask' '$malformed_edit_owner'" >/tmp/opex-e2e-bad-edit-ask.json
  wait_user_open_order "$malformed_edit_owner" "ETH_USDT" "172" "0.4" /tmp/opex-e2e-bad-edit-open-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$malformed_edit_owner" "ETH" "0.6"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for malformed-edit owner reservation" >&2
      assert_wallet_balance "malformed-edit owner ETH reserved before invalid edits" "$malformed_edit_owner" "ETH" "0.6" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-gateway >&2 || true
      exit 1
    fi
    sleep 2
  done

  local malformed_edit_ouid malformed_edit_order_id malformed_edit_bad_symbol malformed_edit_negative_id malformed_edit_zero_price malformed_edit_bad_precision malformed_edit_owner_request
  malformed_edit_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-bad-edit-open-orders.json)"
  malformed_edit_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-bad-edit-open-orders.json)"
  malformed_edit_bad_symbol="$(jq -nc --arg ouid "$malformed_edit_ouid" --arg uuid "$malformed_edit_owner" --argjson orderId "$malformed_edit_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETHUSDT", price:173, quantity:0.3}')"
  malformed_edit_negative_id="$(jq -nc --arg ouid "$malformed_edit_ouid" --arg uuid "$malformed_edit_owner" '{ouid:$ouid, uuid:$uuid, orderId:-1, symbol:"ETH_USDT", price:173, quantity:0.3}')"
  malformed_edit_zero_price="$(jq -nc --arg ouid "$malformed_edit_ouid" --arg uuid "$malformed_edit_owner" --argjson orderId "$malformed_edit_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT", price:0, quantity:0.3}')"
  malformed_edit_bad_precision="$(jq -nc --arg ouid "$malformed_edit_ouid" --arg uuid "$malformed_edit_owner" --argjson orderId "$malformed_edit_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT", price:173.001, quantity:0.3}')"
  expect_http_status "malformed edit bad symbol" "400" "$(curl_json POST "http://127.0.0.1:8093/order/edit" "$malformed_edit_bad_symbol" "$malformed_edit_owner")" >/tmp/opex-e2e-bad-edit-symbol.json
  expect_http_status "malformed edit negative order id" "400" "$(curl_json POST "http://127.0.0.1:8093/order/edit" "$malformed_edit_negative_id" "$malformed_edit_owner")" >/tmp/opex-e2e-bad-edit-negative-id.json
  expect_http_status "malformed edit zero price" "400" "$(curl_json POST "http://127.0.0.1:8093/order/edit" "$malformed_edit_zero_price" "$malformed_edit_owner")" >/tmp/opex-e2e-bad-edit-zero-price.json
  expect_http_status "malformed edit price precision" "400" "$(curl_json POST "http://127.0.0.1:8093/order/edit" "$malformed_edit_bad_precision" "$malformed_edit_owner")" >/tmp/opex-e2e-bad-edit-price-precision.json
  wait_user_open_order "$malformed_edit_owner" "ETH_USDT" "172" "0.4" /tmp/opex-e2e-bad-edit-open-orders.json
  assert_no_user_order_by_price "$malformed_edit_owner" "ETH_USDT" "173" "0.3"
  assert_wallet_balance "malformed-edit owner ETH still reserved after invalid edits" "$malformed_edit_owner" "ETH" "0.6"

  malformed_edit_owner_request="$(jq -nc --arg ouid "$malformed_edit_ouid" --arg uuid "$malformed_edit_owner" --argjson orderId "$malformed_edit_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "malformed-edit owner cleanup cancel" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$malformed_edit_owner_request' '$malformed_edit_owner'" >/tmp/opex-e2e-bad-edit-cleanup.json
  wait_no_user_open_orders "$malformed_edit_owner" "ETH_USDT"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$malformed_edit_owner" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for malformed-edit owner release settlement" >&2
      assert_wallet_balance "malformed-edit owner ETH released after cleanup" "$malformed_edit_owner" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done

  local fok_owner="e2e-fok-$(date +%s)"
  local fok_ref="e2e-fok-$(date +%s)"
  expect_2xx "fok owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${fok_owner}_MAIN?description=e2e-fok&transferRef=${fok_ref}-eth")" >/dev/null
  local fok_ask='{"uuid":null,"pair":"ETH_USDT","price":777,"quantity":0.5,"direction":"ASK","matchConstraint":"FOK","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_http_status "unsupported fok ask" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$fok_ask" "$fok_owner")" >/tmp/opex-e2e-fok-ask.json
  wait_no_user_open_orders "$fok_owner" "ETH_USDT"
  assert_no_user_orders "$fok_owner" "ETH_USDT"
  assert_wallet_balance "fok owner ETH unchanged after gateway reject" "$fok_owner" "ETH" "1"

  local self_trade_owner="e2e-self-trade-$(date +%s)"
  local self_trade_ref="e2e-self-trade-$(date +%s)"
  expect_2xx "self-trade owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${self_trade_owner}_MAIN?description=e2e-self-trade&transferRef=${self_trade_ref}-eth")" >/dev/null
  expect_2xx "self-trade owner USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${self_trade_owner}_MAIN?description=e2e-self-trade&transferRef=${self_trade_ref}-usdt")" >/dev/null

  local self_trade_ask='{"uuid":null,"pair":"ETH_USDT","price":118,"quantity":0.4,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local self_trade_bid='{"uuid":null,"pair":"ETH_USDT","price":118,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "self-trade resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$self_trade_ask' '$self_trade_owner'" >/tmp/opex-e2e-self-trade-ask.json
  wait_user_open_order "$self_trade_owner" "ETH_USDT" "118" "0.4" /tmp/opex-e2e-self-trade-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "118" "0.4"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$self_trade_owner" "ETH" "0.6"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for self-trade resting ask reservation" >&2
      assert_wallet_balance "self-trade owner ETH reserved before rejected bid" "$self_trade_owner" "ETH" "0.6" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  expect_2xx_retry "self-trade crossing bid rejected async" "curl_json POST 'http://127.0.0.1:8093/order' '$self_trade_bid' '$self_trade_owner'" >/tmp/opex-e2e-self-trade-bid.json
  wait_query_eq "self-trade bid reject financial action" "postgres-accountant" "1" "
    select count(*)
    from fi_actions
    where event_type = 'RejectOrderEvent'
      and category_name = 'ORDER_CANCEL'
      and sender = '${self_trade_owner}'
      and receiver = '${self_trade_owner}'
      and symbol = 'USDT'
      and amount = 23.60000000
      and status = 'PROCESSED';
  "
  wait_query_eq "self-trade reject eventlog audit" "postgres-eventlog" "SELF_TRADE_PREVENTION,PLACE_ORDER,BID,1,0" "
    select event_json::jsonb ->> 'reason',
           event_json::jsonb ->> 'requestedOperation',
           event_json::jsonb ->> 'direction',
           count(*),
           sum(case when event_json is null or event_json = '' then 1 else 0 end)
    from opex_events
    where event = 'RejectOrderEvent'
      and uuid = '$self_trade_owner'
    group by event_json::jsonb ->> 'reason',
             event_json::jsonb ->> 'requestedOperation',
             event_json::jsonb ->> 'direction';
  "
  wait_user_open_order "$self_trade_owner" "ETH_USDT" "118" "0.4" /tmp/opex-e2e-self-trade-open-orders.json
  assert_no_user_order_by_price "$self_trade_owner" "ETH_USDT" "118" "0.2"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$self_trade_owner" "USDT" "100"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for self-trade rejected bid release" >&2
      assert_wallet_balance "self-trade owner USDT released after rejected bid" "$self_trade_owner" "USDT" "100" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "self-trade prevention emitted no trade" "postgres-market" "0" "
    select count(*)
    from trades
    where symbol = 'ETH_USDT'
      and maker_uuid = '$self_trade_owner'
      and taker_uuid = '$self_trade_owner';
  "

  local self_trade_ouid self_trade_order_id self_trade_cancel_request
  self_trade_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-self-trade-open-orders.json)"
  self_trade_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-self-trade-open-orders.json)"
  self_trade_cancel_request="$(jq -nc --arg ouid "$self_trade_ouid" --arg uuid "$self_trade_owner" --argjson orderId "$self_trade_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel self-trade resting ask" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$self_trade_cancel_request' '$self_trade_owner'" >/tmp/opex-e2e-self-trade-cancel.json
  wait_no_user_open_orders "$self_trade_owner" "ETH_USDT"
  wait_order_projection "$self_trade_owner" "$self_trade_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$self_trade_owner" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for self-trade resting ask cancel release" >&2
      assert_wallet_balance "self-trade owner ETH released after cleanup" "$self_trade_owner" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  local self_trade_edit_bid='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local self_trade_edit_ask='{"uuid":null,"pair":"ETH_USDT","price":110,"quantity":0.3,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "self-trade edit resting bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$self_trade_edit_bid' '$self_trade_owner'" >/tmp/opex-e2e-self-trade-edit-bid.json
  expect_2xx_retry "self-trade edit resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$self_trade_edit_ask' '$self_trade_owner'" >/tmp/opex-e2e-self-trade-edit-ask.json
  wait_user_open_order "$self_trade_owner" "ETH_USDT" "100" "0.2" /tmp/opex-e2e-self-trade-edit-bid-open-orders.json
  wait_user_open_order "$self_trade_owner" "ETH_USDT" "110" "0.3" /tmp/opex-e2e-self-trade-edit-ask-open-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$self_trade_owner" "ETH" "0.7" &&
    try_wallet_balance "$self_trade_owner" "USDT" "80"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for self-trade edit pre-edit reservations" >&2
      assert_wallet_balance "self-trade edit owner ETH reserved before rejected edit" "$self_trade_owner" "ETH" "0.7" >&2 || true
      assert_wallet_balance "self-trade edit owner USDT reserved before rejected edit" "$self_trade_owner" "USDT" "80" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done
  local self_trade_edit_bid_ouid self_trade_edit_bid_order_id self_trade_edit_ask_ouid self_trade_edit_ask_order_id self_trade_edit_request
  self_trade_edit_bid_ouid="$(jq -r '[.[] | select(.price == 100 and .quantity == 0.2)][0].ouid' /tmp/opex-e2e-self-trade-edit-bid-open-orders.json)"
  self_trade_edit_bid_order_id="$(jq -r '[.[] | select(.price == 100 and .quantity == 0.2)][0].orderId' /tmp/opex-e2e-self-trade-edit-bid-open-orders.json)"
  self_trade_edit_ask_ouid="$(jq -r '[.[] | select(.price == 110 and .quantity == 0.3)][0].ouid' /tmp/opex-e2e-self-trade-edit-ask-open-orders.json)"
  self_trade_edit_ask_order_id="$(jq -r '[.[] | select(.price == 110 and .quantity == 0.3)][0].orderId' /tmp/opex-e2e-self-trade-edit-ask-open-orders.json)"
  self_trade_edit_request="$(jq -nc --arg ouid "$self_trade_edit_ask_ouid" --arg uuid "$self_trade_owner" --argjson orderId "$self_trade_edit_ask_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT", price:100, quantity:0.2}')"
  expect_2xx_retry "self-trade edit crossing ask rejected async" "curl_json POST 'http://127.0.0.1:8093/order/edit' '$self_trade_edit_request' '$self_trade_owner'" >/tmp/opex-e2e-self-trade-edit-response.json
  wait_query_eq "self-trade edit reject eventlog audit" "postgres-eventlog" "SELF_TRADE_PREVENTION,EDIT_ORDER,ASK,1,0" "
    select event_json::jsonb ->> 'reason',
           event_json::jsonb ->> 'requestedOperation',
           event_json::jsonb ->> 'direction',
           count(*),
           sum(case when event_json is null or event_json = '' then 1 else 0 end)
    from opex_events
    where event = 'RejectOrderEvent'
      and uuid = '$self_trade_owner'
      and event_json::jsonb ->> 'requestedOperation' = 'EDIT_ORDER'
    group by event_json::jsonb ->> 'reason',
             event_json::jsonb ->> 'requestedOperation',
             event_json::jsonb ->> 'direction';
  "
  wait_user_open_order "$self_trade_owner" "ETH_USDT" "100" "0.2" /tmp/opex-e2e-self-trade-edit-bid-still-open-orders.json
  wait_user_open_order "$self_trade_owner" "ETH_USDT" "110" "0.3" /tmp/opex-e2e-self-trade-edit-ask-still-open-orders.json
  wait_order_projection "$self_trade_owner" "$self_trade_edit_bid_ouid" "NEW" "0" "0"
  wait_order_projection "$self_trade_owner" "$self_trade_edit_ask_ouid" "NEW" "0" "0"
  wait_query_eq "self-trade edit prevention emitted no trade" "postgres-market" "0" "
    select count(*)
    from trades
    where symbol = 'ETH_USDT'
      and maker_uuid = '$self_trade_owner'
      and taker_uuid = '$self_trade_owner';
  "
  assert_wallet_balance "self-trade edit owner ETH unchanged after rejected edit" "$self_trade_owner" "ETH" "0.7"
  assert_wallet_balance "self-trade edit owner USDT unchanged after rejected edit" "$self_trade_owner" "USDT" "80"
  local self_trade_edit_bid_cancel_request self_trade_edit_ask_cancel_request
  self_trade_edit_bid_cancel_request="$(jq -nc --arg ouid "$self_trade_edit_bid_ouid" --arg uuid "$self_trade_owner" --argjson orderId "$self_trade_edit_bid_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  self_trade_edit_ask_cancel_request="$(jq -nc --arg ouid "$self_trade_edit_ask_ouid" --arg uuid "$self_trade_owner" --argjson orderId "$self_trade_edit_ask_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel self-trade edit resting bid" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$self_trade_edit_bid_cancel_request' '$self_trade_owner'" >/tmp/opex-e2e-self-trade-edit-bid-cancel.json
  expect_2xx_retry "cancel self-trade edit resting ask" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$self_trade_edit_ask_cancel_request' '$self_trade_owner'" >/tmp/opex-e2e-self-trade-edit-ask-cancel.json
  wait_no_user_open_orders "$self_trade_owner" "ETH_USDT"
  wait_order_projection "$self_trade_owner" "$self_trade_edit_bid_ouid" "CANCELED" "0" "0"
  wait_order_projection "$self_trade_owner" "$self_trade_edit_ask_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$self_trade_owner" "ETH" "1" &&
    try_wallet_balance "$self_trade_owner" "USDT" "100"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for self-trade edit cleanup release" >&2
      assert_wallet_balance "self-trade edit owner ETH released after cleanup" "$self_trade_owner" "ETH" "1" >&2 || true
      assert_wallet_balance "self-trade edit owner USDT released after cleanup" "$self_trade_owner" "USDT" "100" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done

  local layered_self_trade_owner="e2e-layered-self-trade-$(date +%s)"
  local layered_external_seller="e2e-layered-stp-maker-$(date +%s)"
  local layered_self_trade_ref="e2e-layered-self-trade-$(date +%s)"
  expect_2xx "layered-stp owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${layered_self_trade_owner}_MAIN?description=e2e-layered-stp&transferRef=${layered_self_trade_ref}-owner-eth")" >/dev/null
  expect_2xx "layered-stp owner USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${layered_self_trade_owner}_MAIN?description=e2e-layered-stp&transferRef=${layered_self_trade_ref}-owner-usdt")" >/dev/null
  expect_2xx "layered-stp external ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/0.1_test-ethereum_ETH/${layered_external_seller}_MAIN?description=e2e-layered-stp&transferRef=${layered_self_trade_ref}-external-eth")" >/dev/null

  local layered_external_ask='{"uuid":null,"pair":"ETH_USDT","price":119,"quantity":0.1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local layered_self_ask='{"uuid":null,"pair":"ETH_USDT","price":120,"quantity":0.4,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local layered_self_bid='{"uuid":null,"pair":"ETH_USDT","price":120,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "layered-stp external resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$layered_external_ask' '$layered_external_seller'" >/tmp/opex-e2e-layered-stp-external-ask.json
  expect_2xx_retry "layered-stp owner resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$layered_self_ask' '$layered_self_trade_owner'" >/tmp/opex-e2e-layered-stp-owner-ask.json
  wait_user_open_order "$layered_external_seller" "ETH_USDT" "119" "0.1" /tmp/opex-e2e-layered-stp-external-open-orders.json
  wait_user_open_order "$layered_self_trade_owner" "ETH_USDT" "120" "0.4" /tmp/opex-e2e-layered-stp-owner-open-orders.json
  wait_order_book_level "ETH_USDT" "ASK" "119" "0.1"
  wait_order_book_level "ETH_USDT" "ASK" "120" "0.4"

  expect_2xx_retry "layered-stp crossing bid rejected before partial fill" "curl_json POST 'http://127.0.0.1:8093/order' '$layered_self_bid' '$layered_self_trade_owner'" >/tmp/opex-e2e-layered-stp-bid.json
  wait_query_eq "layered-stp bid reject financial action" "postgres-accountant" "1" "
    select count(*)
    from fi_actions
    where event_type = 'RejectOrderEvent'
      and category_name = 'ORDER_CANCEL'
      and sender = '${layered_self_trade_owner}'
      and receiver = '${layered_self_trade_owner}'
      and symbol = 'USDT'
      and amount = 24.00000000
      and status = 'PROCESSED';
  "
  wait_query_eq "layered-stp reject eventlog audit" "postgres-eventlog" "SELF_TRADE_PREVENTION,PLACE_ORDER,BID,1,0" "
    select event_json::jsonb ->> 'reason',
           event_json::jsonb ->> 'requestedOperation',
           event_json::jsonb ->> 'direction',
           count(*),
           sum(case when event_json is null or event_json = '' then 1 else 0 end)
    from opex_events
    where event = 'RejectOrderEvent'
      and uuid = '$layered_self_trade_owner'
    group by event_json::jsonb ->> 'reason',
             event_json::jsonb ->> 'requestedOperation',
             event_json::jsonb ->> 'direction';
  "
  wait_user_open_order "$layered_external_seller" "ETH_USDT" "119" "0.1" /tmp/opex-e2e-layered-stp-external-open-orders.json
  wait_user_open_order "$layered_self_trade_owner" "ETH_USDT" "120" "0.4" /tmp/opex-e2e-layered-stp-owner-open-orders.json
  assert_no_user_order_by_price "$layered_self_trade_owner" "ETH_USDT" "120" "0.2"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$layered_self_trade_owner" "USDT" "100"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for layered self-trade rejected bid release" >&2
      assert_wallet_balance "layered-stp owner USDT released after rejected bid" "$layered_self_trade_owner" "USDT" "100" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "layered self-trade prevention emitted no partial external fill" "postgres-market" "0" "
    select count(*)
    from trades
    where symbol = 'ETH_USDT'
      and maker_uuid = '$layered_external_seller'
      and taker_uuid = '$layered_self_trade_owner';
  "
  wait_query_eq "layered self-trade prevention emitted no owner self trade" "postgres-market" "0" "
    select count(*)
    from trades
    where symbol = 'ETH_USDT'
      and maker_uuid = '$layered_self_trade_owner'
      and taker_uuid = '$layered_self_trade_owner';
  "

  local layered_external_ouid layered_external_order_id layered_external_cancel_request
  layered_external_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-layered-stp-external-open-orders.json)"
  layered_external_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-layered-stp-external-open-orders.json)"
  layered_external_cancel_request="$(jq -nc --arg ouid "$layered_external_ouid" --arg uuid "$layered_external_seller" --argjson orderId "$layered_external_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel layered-stp external ask" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$layered_external_cancel_request' '$layered_external_seller'" >/tmp/opex-e2e-layered-stp-external-cancel.json
  wait_no_user_open_orders "$layered_external_seller" "ETH_USDT"
  wait_order_projection "$layered_external_seller" "$layered_external_ouid" "CANCELED" "0" "0"

  local layered_owner_ouid layered_owner_order_id layered_owner_cancel_request
  layered_owner_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-layered-stp-owner-open-orders.json)"
  layered_owner_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-layered-stp-owner-open-orders.json)"
  layered_owner_cancel_request="$(jq -nc --arg ouid "$layered_owner_ouid" --arg uuid "$layered_self_trade_owner" --argjson orderId "$layered_owner_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel layered-stp owner ask" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$layered_owner_cancel_request' '$layered_self_trade_owner'" >/tmp/opex-e2e-layered-stp-owner-cancel.json
  wait_no_user_open_orders "$layered_self_trade_owner" "ETH_USDT"
  wait_order_projection "$layered_self_trade_owner" "$layered_owner_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$layered_external_seller" "ETH" "0.1" &&
    try_wallet_balance "$layered_self_trade_owner" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for layered self-trade cleanup release" >&2
      assert_wallet_balance "layered-stp external ETH released after cleanup" "$layered_external_seller" "ETH" "0.1" >&2 || true
      assert_wallet_balance "layered-stp owner ETH released after cleanup" "$layered_self_trade_owner" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine >&2 || true
      exit 1
    fi
    sleep 2
  done

  local edit_owner="e2e-edit-$(date +%s)"
  local edit_ref="e2e-edit-$(date +%s)"
  expect_2xx "edit owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${edit_owner}_MAIN?description=e2e-edit&transferRef=${edit_ref}-eth")" >/dev/null
  local edit_ask='{"uuid":null,"pair":"ETH_USDT","price":121,"quantity":0.4,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "edit owner resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$edit_ask' '$edit_owner'" >/tmp/opex-e2e-edit-ask.json
  wait_user_open_order "$edit_owner" "ETH_USDT" "121" "0.4" /tmp/opex-e2e-edit-open-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$edit_owner" "ETH" "0.6"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for edit owner ETH reservation before edit" >&2
      assert_wallet_balance "edit owner ETH reserved before edit" "$edit_owner" "ETH" "0.6" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done

  local edit_ouid edit_order_id edit_request
  edit_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-edit-open-orders.json)"
  edit_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-edit-open-orders.json)"
  edit_request="$(jq -nc --arg ouid "$edit_ouid" --arg uuid "$edit_owner" --argjson orderId "$edit_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT", price:122, quantity:0.3}')"
  expect_2xx_retry "edit owner reduce resting ask" "curl_json POST 'http://127.0.0.1:8093/order/edit' '$edit_request' '$edit_owner'" >/tmp/opex-e2e-edit-response.json
  wait_user_open_order "$edit_owner" "ETH_USDT" "122" "0.3" /tmp/opex-e2e-edit-updated-open-orders.json
  assert_no_user_order_by_price "$edit_owner" "ETH_USDT" "121" "0.4"
  wait_order_book_level "ETH_USDT" "ASK" "122" "0.3"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$edit_owner" "ETH" "0.7"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for edit owner ETH release after reduced ask" >&2
      assert_wallet_balance "edit owner ETH released after reduced ask" "$edit_owner" "ETH" "0.7" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "edit order accountant projection" "postgres-accountant" "122.00000000,0.30000000,0.30000000,0.30000000" "
    select to_char(orig_price, 'FM9999999990.00000000'),
           to_char(orig_quantity, 'FM9999999990.00000000'),
           to_char((quantity - filled_quantity) * left_side_fraction, 'FM9999999990.00000000'),
           to_char(remained_transfer_amount, 'FM9999999990.00000000')
    from orders
    where uuid = '$edit_owner'
      and ouid = '$edit_ouid';
  "
  wait_query_eq "edit order eventlog update event" "postgres-eventlog" "UpdatedOrderEvent,1,0" "
    select event,
           count(*),
           sum(case when event_json is null or event_json = '' then 1 else 0 end)
    from opex_events
    where event = 'UpdatedOrderEvent'
      and uuid = '$edit_owner'
      and event_json::jsonb ->> 'price' = '12200'
      and event_json::jsonb ->> 'quantity' = '300000'
      and event_json::jsonb ->> 'oldPrice' = '12100'
      and event_json::jsonb ->> 'oldQuantity' = '400000'
    group by event;
  "
  local edit_cancel_request
  edit_cancel_request="$(jq -nc --arg ouid "$edit_ouid" --arg uuid "$edit_owner" --argjson orderId "$edit_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel edited ask" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$edit_cancel_request' '$edit_owner'" >/tmp/opex-e2e-edit-cancel.json
  wait_no_user_open_orders "$edit_owner" "ETH_USDT"
  wait_order_projection "$edit_owner" "$edit_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$edit_owner" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for edit owner ETH release after cleanup" >&2
      assert_wallet_balance "edit owner ETH released after cleanup" "$edit_owner" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done

  local edit_bid_owner="e2e-edit-bid-$(date +%s)"
  local edit_bid_ref="e2e-edit-bid-$(date +%s)"
  expect_2xx "edit bid owner USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-tether_USDT/${edit_bid_owner}_MAIN?description=e2e-edit-bid&transferRef=${edit_bid_ref}-usdt")" >/dev/null
  local edit_bid='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0.5,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "edit bid owner resting bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$edit_bid' '$edit_bid_owner'" >/tmp/opex-e2e-edit-bid.json
  wait_user_open_order "$edit_bid_owner" "ETH_USDT" "100" "0.5" /tmp/opex-e2e-edit-bid-open-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$edit_bid_owner" "USDT" "50"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for edit bid owner USDT reservation before edit" >&2
      assert_wallet_balance "edit bid owner USDT reserved before edit" "$edit_bid_owner" "USDT" "50" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done

  local edit_bid_ouid edit_bid_order_id edit_bid_request
  edit_bid_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-edit-bid-open-orders.json)"
  edit_bid_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-edit-bid-open-orders.json)"
  edit_bid_request="$(jq -nc --arg ouid "$edit_bid_ouid" --arg uuid "$edit_bid_owner" --argjson orderId "$edit_bid_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT", price:90, quantity:0.4}')"
  expect_2xx_retry "edit owner reduce resting bid" "curl_json POST 'http://127.0.0.1:8093/order/edit' '$edit_bid_request' '$edit_bid_owner'" >/tmp/opex-e2e-edit-bid-response.json
  wait_user_open_order "$edit_bid_owner" "ETH_USDT" "90" "0.4" /tmp/opex-e2e-edit-bid-updated-open-orders.json
  assert_no_user_order_by_price "$edit_bid_owner" "ETH_USDT" "100" "0.5"
  wait_order_book_level "ETH_USDT" "BID" "90" "0.4"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$edit_bid_owner" "USDT" "64"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for edit bid owner USDT release after reduced bid" >&2
      assert_wallet_balance "edit bid owner USDT released after reduced bid" "$edit_bid_owner" "USDT" "64" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "edit bid order accountant projection" "postgres-accountant" "90.00000000,0.40000000,0.40000000,36.00000000" "
    select to_char(orig_price, 'FM9999999990.00000000'),
           to_char(orig_quantity, 'FM9999999990.00000000'),
           to_char((quantity - filled_quantity) * left_side_fraction, 'FM9999999990.00000000'),
           to_char(remained_transfer_amount, 'FM9999999990.00000000')
    from orders
    where uuid = '$edit_bid_owner'
      and ouid = '$edit_bid_ouid';
  "
  wait_query_eq "edit bid order eventlog update event" "postgres-eventlog" "UpdatedOrderEvent,1,0" "
    select event,
           count(*),
           sum(case when event_json is null or event_json = '' then 1 else 0 end)
    from opex_events
    where event = 'UpdatedOrderEvent'
      and uuid = '$edit_bid_owner'
      and event_json::jsonb ->> 'price' = '9000'
      and event_json::jsonb ->> 'quantity' = '400000'
      and event_json::jsonb ->> 'oldPrice' = '10000'
      and event_json::jsonb ->> 'oldQuantity' = '500000'
    group by event;
  "
  local edit_bid_cancel_request
  edit_bid_cancel_request="$(jq -nc --arg ouid "$edit_bid_ouid" --arg uuid "$edit_bid_owner" --argjson orderId "$edit_bid_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel edited bid" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$edit_bid_cancel_request' '$edit_bid_owner'" >/tmp/opex-e2e-edit-bid-cancel.json
  wait_no_user_open_orders "$edit_bid_owner" "ETH_USDT"
  wait_order_projection "$edit_bid_owner" "$edit_bid_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$edit_bid_owner" "USDT" "100"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for edit bid owner USDT release after cleanup" >&2
      assert_wallet_balance "edit bid owner USDT released after cleanup" "$edit_bid_owner" "USDT" "100" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done

  local edit_cross_seller="e2e-edit-cross-seller-$(date +%s)"
  local edit_cross_buyer="e2e-edit-cross-buyer-$(date +%s)"
  local edit_cross_ref="e2e-edit-cross-$(date +%s)"
  expect_2xx "edit crossing seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${edit_cross_seller}_MAIN?description=e2e-edit-cross&transferRef=${edit_cross_ref}-eth")" >/dev/null
  expect_2xx "edit crossing buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-tether_USDT/${edit_cross_buyer}_MAIN?description=e2e-edit-cross&transferRef=${edit_cross_ref}-usdt")" >/dev/null
  local edit_cross_bid='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0.2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local edit_cross_ask='{"uuid":null,"pair":"ETH_USDT","price":110,"quantity":0.3,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "edit crossing buyer resting bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$edit_cross_bid' '$edit_cross_buyer'" >/tmp/opex-e2e-edit-cross-bid.json
  expect_2xx_retry "edit crossing seller resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$edit_cross_ask' '$edit_cross_seller'" >/tmp/opex-e2e-edit-cross-ask.json
  wait_user_open_order "$edit_cross_buyer" "ETH_USDT" "100" "0.2" /tmp/opex-e2e-edit-cross-bid-open-orders.json
  wait_user_open_order "$edit_cross_seller" "ETH_USDT" "110" "0.3" /tmp/opex-e2e-edit-cross-ask-open-orders.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$edit_cross_buyer" "USDT" "80" &&
    try_wallet_balance "$edit_cross_seller" "ETH" "0.7"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for edit crossing pre-edit reservations" >&2
      assert_wallet_balance "edit crossing buyer USDT reserved before edit" "$edit_cross_buyer" "USDT" "80" >&2 || true
      assert_wallet_balance "edit crossing seller ETH reserved before edit" "$edit_cross_seller" "ETH" "0.7" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done
  local edit_cross_ask_ouid edit_cross_ask_order_id edit_cross_bid_ouid edit_cross_request
  edit_cross_ask_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-edit-cross-ask-open-orders.json)"
  edit_cross_ask_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-edit-cross-ask-open-orders.json)"
  edit_cross_bid_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-edit-cross-bid-open-orders.json)"
  edit_cross_request="$(jq -nc --arg ouid "$edit_cross_ask_ouid" --arg uuid "$edit_cross_seller" --argjson orderId "$edit_cross_ask_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT", price:100, quantity:0.2}')"
  expect_2xx_retry "edit crossing ask into resting bid" "curl_json POST 'http://127.0.0.1:8093/order/edit' '$edit_cross_request' '$edit_cross_seller'" >/tmp/opex-e2e-edit-cross-response.json
  wait_no_user_open_orders "$edit_cross_seller" "ETH_USDT"
  wait_no_user_open_orders "$edit_cross_buyer" "ETH_USDT"
  wait_order_projection "$edit_cross_seller" "$edit_cross_ask_ouid" "FILLED" "0.2" "20"
  wait_order_projection "$edit_cross_buyer" "$edit_cross_bid_ouid" "FILLED" "0.2" "20"
  wait_user_trade_projection "$edit_cross_seller" "ETH_USDT" "100" "0.2" "20" "0.2" "USDT" false false true /tmp/opex-e2e-edit-cross-seller-trades.json
  wait_user_trade_projection "$edit_cross_buyer" "ETH_USDT" "100" "0.2" "20" "0.002" "ETH" true true true /tmp/opex-e2e-edit-cross-buyer-trades.json
  wait_binance_recent_trade_level "ETHUSDT" "100" "0.2" "20" /tmp/opex-e2e-edit-cross-binance-trades.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$edit_cross_seller" "ETH" "0.8" &&
    try_wallet_balance "$edit_cross_seller" "USDT" "19.8" &&
    try_wallet_balance "$edit_cross_buyer" "ETH" "0.198" &&
    try_wallet_balance "$edit_cross_buyer" "USDT" "80"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for edit crossing settlement" >&2
      assert_wallet_balance "edit crossing seller ETH remainder" "$edit_cross_seller" "ETH" "0.8" >&2 || true
      assert_wallet_balance "edit crossing seller USDT proceeds" "$edit_cross_seller" "USDT" "19.8" >&2 || true
      assert_wallet_balance "edit crossing buyer ETH received" "$edit_cross_buyer" "ETH" "0.198" >&2 || true
      assert_wallet_balance "edit crossing buyer USDT remainder" "$edit_cross_buyer" "USDT" "80" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway eventlog >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "edit crossing eventlog update event" "postgres-eventlog" "UpdatedOrderEvent,1,0" "
    select event,
           count(*),
           sum(case when event_json is null or event_json = '' then 1 else 0 end)
    from opex_events
    where event = 'UpdatedOrderEvent'
      and uuid = '$edit_cross_seller'
      and event_json::jsonb ->> 'price' = '10000'
      and event_json::jsonb ->> 'quantity' = '200000'
      and event_json::jsonb ->> 'oldPrice' = '11000'
      and event_json::jsonb ->> 'oldQuantity' = '300000'
    group by event;
  "

  local reject_owner="e2e-reject-$(date +%s)"
  local underfunded_ask='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_http_status "underfunded ask order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$underfunded_ask" "$reject_owner")" >/tmp/opex-e2e-reject-order.json
  wait_no_user_open_orders "$reject_owner" "ETH_USDT"
  assert_no_user_orders "$reject_owner" "ETH_USDT"

  local bid_reject_owner="e2e-bid-reject-$(date +%s)"
  local underfunded_bid='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":1,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_http_status "underfunded bid order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$underfunded_bid" "$bid_reject_owner")" >/tmp/opex-e2e-bid-reject-order.json
  wait_no_user_open_orders "$bid_reject_owner" "ETH_USDT"
  assert_no_user_orders "$bid_reject_owner" "ETH_USDT"

  local invalid_owner="e2e-invalid-$(date +%s)"
  local invalid_ref="e2e-invalid-$(date +%s)"
  expect_2xx "invalid owner ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${invalid_owner}_MAIN?description=e2e-invalid&transferRef=${invalid_ref}-eth")" >/dev/null
  expect_2xx "invalid owner USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/100_test-ethereum_USDT/${invalid_owner}_MAIN?description=e2e-invalid&transferRef=${invalid_ref}-usdt")" >/dev/null

  local zero_quantity_ask='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local zero_price_ask='{"uuid":null,"pair":"ETH_USDT","price":0,"quantity":1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local negative_price_bid='{"uuid":null,"pair":"ETH_USDT","price":-1,"quantity":1,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local zero_price_market_bid='{"uuid":null,"pair":"ETH_USDT","price":0,"quantity":1,"direction":"BID","matchConstraint":"IOC","orderType":"MARKET_ORDER","userLevel":"*"}'
  local gtc_market_ask='{"uuid":null,"pair":"ETH_USDT","price":0,"quantity":1,"direction":"ASK","matchConstraint":"GTC","orderType":"MARKET_ORDER","userLevel":"*"}'
  local malformed_pair_bid='{"uuid":null,"pair":"ETHUSDT","price":100,"quantity":1,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local invalid_price_precision_ask='{"uuid":null,"pair":"ETH_USDT","price":100.001,"quantity":1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local invalid_quantity_precision_ask='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0.0000001,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_http_status "zero quantity ask order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$zero_quantity_ask" "$invalid_owner")" >/tmp/opex-e2e-invalid-zero-quantity.json
  expect_http_status "zero price ask order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$zero_price_ask" "$invalid_owner")" >/tmp/opex-e2e-invalid-zero-price.json
  expect_http_status "negative price bid order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$negative_price_bid" "$invalid_owner")" >/tmp/opex-e2e-invalid-negative-price.json
  expect_http_status "zero price market bid order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$zero_price_market_bid" "$invalid_owner")" >/tmp/opex-e2e-invalid-zero-price-market-bid.json
  expect_http_status "gtc market ask order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$gtc_market_ask" "$invalid_owner")" >/tmp/opex-e2e-invalid-gtc-market-ask.json
  expect_http_status "malformed pair bid order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$malformed_pair_bid" "$invalid_owner")" >/tmp/opex-e2e-invalid-malformed-pair.json
  expect_http_status "invalid price precision ask order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$invalid_price_precision_ask" "$invalid_owner")" >/tmp/opex-e2e-invalid-price-precision.json
  expect_http_status "invalid quantity precision ask order" "400" "$(curl_json POST "http://127.0.0.1:8093/order" "$invalid_quantity_precision_ask" "$invalid_owner")" >/tmp/opex-e2e-invalid-quantity-precision.json
  wait_no_user_open_orders "$invalid_owner" "ETH_USDT"
  assert_no_user_orders "$invalid_owner" "ETH_USDT"
  assert_wallet_balance "invalid owner ETH unchanged" "$invalid_owner" "ETH" "1"
  assert_wallet_balance "invalid owner USDT unchanged" "$invalid_owner" "USDT" "100"

  local duplicate_deposit_owner="e2e-dup-deposit-$(date +%s)"
  local duplicate_deposit_ref="e2e-dup-deposit-$(date +%s)"
  expect_2xx "duplicate-deposit first USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/5_test-ethereum_USDT/${duplicate_deposit_owner}_MAIN?description=e2e-duplicate-deposit&transferRef=${duplicate_deposit_ref}")" >/dev/null
  assert_wallet_balance "duplicate-deposit owner credited once" "$duplicate_deposit_owner" "USDT" "5"
  expect_http_status "duplicate-deposit second USDT deposit" "400" "$(curl_json POST "http://127.0.0.1:8091/deposit/5_test-ethereum_USDT/${duplicate_deposit_owner}_MAIN?description=e2e-duplicate-deposit&transferRef=${duplicate_deposit_ref}")" >/tmp/opex-e2e-duplicate-deposit-reject.json
  assert_wallet_balance "duplicate-deposit owner unchanged after duplicate ref" "$duplicate_deposit_owner" "USDT" "5"
  wait_query_eq "duplicate deposit ledger row credited once" "postgres-wallet" "1" "
    select count(*)
    from transaction t
    join wallet dw on dw.id = t.dest_wallet
    join wallet_owner dwo on dwo.id = dw.owner
    where t.transfer_ref = '$duplicate_deposit_ref'
      and t.transfer_category = 'DEPOSIT'
      and dwo.uuid = '$duplicate_deposit_owner'
      and dw.wallet_type = 'MAIN'
      and dw.currency = 'USDT'
      and abs(t.dest_amount - 5) <= 0.000001;
  "
  local duplicate_deposit_history_body
  local duplicate_deposit_history_deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until duplicate_deposit_history_body="$(expect_2xx "duplicate deposit transaction history" "$(curl_json POST "http://127.0.0.1:8091/v2/transaction" '{"currency":"USDT","category":"DEPOSIT","limit":10,"offset":0,"ascendingByTime":false}' "$duplicate_deposit_owner")")" &&
    printf '%s\n' "$duplicate_deposit_history_body" | jq -e --arg owner "$duplicate_deposit_owner" '
      length == 1
      and .[0].userId == $owner
      and .[0].currency == "USDT"
      and .[0].category == "DEPOSIT"
      and (.[0].balance | tonumber) >= 4.999999
      and (.[0].balance | tonumber) <= 5.000001
      and (.[0].balanceChange | tonumber) >= 4.999999
      and (.[0].balanceChange | tonumber) <= 5.000001
    ' >/dev/null; do
    if (( SECONDS > duplicate_deposit_history_deadline )); then
      echo "Timed out waiting for duplicate deposit transaction history to show one credited deposit" >&2
      printf '%s\n' "$duplicate_deposit_history_body" >&2
      exit 1
    fi
    sleep 1
  done
  printf '%s\n' "$duplicate_deposit_history_body" >/tmp/opex-e2e-duplicate-deposit-transaction-history.json

  local duplicate_deposit_legacy_history_body
  local duplicate_deposit_legacy_history_deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until duplicate_deposit_legacy_history_body="$(expect_2xx "duplicate deposit legacy transaction history" "$(curl_json POST "http://127.0.0.1:8091/transaction/deposit/${duplicate_deposit_owner}" '{"coin":"USDT","category":"DEPOSIT","limit":10,"offset":0,"ascendingByTime":false}')")" &&
    printf '%s\n' "$duplicate_deposit_legacy_history_body" | jq -e --arg ref "$duplicate_deposit_ref" '
      length == 1
      and .[0].currency == "USDT"
      and .[0].wallet == "MAIN"
      and .[0].category == "DEPOSIT"
      and .[0].description == "e2e-duplicate-deposit"
      and .[0].ref == $ref
      and (.[0].amount | tonumber) >= 4.999999
      and (.[0].amount | tonumber) <= 5.000001
    ' >/dev/null; do
    if (( SECONDS > duplicate_deposit_legacy_history_deadline )); then
      echo "Timed out waiting for duplicate deposit legacy transaction history to show one credited deposit" >&2
      printf '%s\n' "$duplicate_deposit_legacy_history_body" >&2
      exit 1
    fi
    sleep 1
  done
  printf '%s\n' "$duplicate_deposit_legacy_history_body" >/tmp/opex-e2e-duplicate-deposit-legacy-history.json

  local transfer_sender="e2e-transfer-sender-$(date +%s)"
  local transfer_receiver="e2e-transfer-receiver-$(date +%s)"
  local transfer_ref="e2e-transfer-$(date +%s)"
  local transfer_body
  transfer_body="$(jq -nc --arg transferRef "$transfer_ref" '{description:"e2e wallet transfer", transferRef:$transferRef, transferCategory:"NORMAL"}')"
  expect_2xx "transfer sender USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/12_test-ethereum_USDT/${transfer_sender}_MAIN?description=e2e-transfer&transferRef=${transfer_ref}-deposit")" >/dev/null
  assert_wallet_balance "transfer sender initial USDT" "$transfer_sender" "USDT" "12"
  expect_2xx "wallet v2 transfer success" "$(curl_json POST "http://127.0.0.1:8091/v2/transfer/3_USDT/from/${transfer_sender}_MAIN/to/${transfer_receiver}_MAIN" "$transfer_body")" >/tmp/opex-e2e-wallet-transfer.json
  assert_wallet_balance "transfer sender debited" "$transfer_sender" "USDT" "9"
  assert_wallet_balance "transfer receiver credited" "$transfer_receiver" "USDT" "3"
  expect_http_status "wallet v2 duplicate transfer ref rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/v2/transfer/3_USDT/from/${transfer_sender}_MAIN/to/${transfer_receiver}_MAIN" "$transfer_body")" >/tmp/opex-e2e-wallet-transfer-duplicate-ref.json
  expect_http_status "wallet v2 negative transfer rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/v2/transfer/-1_USDT/from/${transfer_sender}_MAIN/to/${transfer_receiver}_MAIN" "$(jq -nc --arg transferRef "${transfer_ref}-negative" '{description:"e2e negative transfer", transferRef:$transferRef, transferCategory:"NORMAL"}')")" >/tmp/opex-e2e-wallet-transfer-negative.json
  expect_http_status "wallet v2 overbalance transfer rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/v2/transfer/10_USDT/from/${transfer_sender}_MAIN/to/${transfer_receiver}_MAIN" "$(jq -nc --arg transferRef "${transfer_ref}-overbalance" '{description:"e2e overbalance transfer", transferRef:$transferRef, transferCategory:"NORMAL"}')")" >/tmp/opex-e2e-wallet-transfer-overbalance.json
  assert_wallet_balance "transfer sender unchanged after rejected transfers" "$transfer_sender" "USDT" "9"
  assert_wallet_balance "transfer receiver unchanged after rejected transfers" "$transfer_receiver" "USDT" "3"
  wait_query_eq "wallet v2 transfer ledger row" "postgres-wallet" "1" "
    select count(*)
    from transaction t
    join wallet sw on sw.id = t.source_wallet
    join wallet_owner swo on swo.id = sw.owner
    join wallet dw on dw.id = t.dest_wallet
    join wallet_owner dwo on dwo.id = dw.owner
    where t.transfer_ref = '$transfer_ref'
      and t.transfer_category = 'NORMAL'
      and swo.uuid = '$transfer_sender'
      and sw.wallet_type = 'MAIN'
      and sw.currency = 'USDT'
      and dwo.uuid = '$transfer_receiver'
      and dw.wallet_type = 'MAIN'
      and dw.currency = 'USDT'
      and abs(t.source_amount - 3) <= 0.000001
      and abs(t.dest_amount - 3) <= 0.000001;
  "
  wait_query_eq "wallet v2 transfer duplicate refs absent" "postgres-wallet" "0" "
    select count(*)
    from (
      select transfer_ref
      from transaction
      where transfer_ref = '$transfer_ref'
      group by transfer_ref
      having count(*) > 1
    ) duplicate_transfer_refs;
  "
  wait_query_eq "wallet v2 rejected transfer refs absent" "postgres-wallet" "0" "
    select count(*)
    from transaction
    where transfer_ref in ('${transfer_ref}-negative', '${transfer_ref}-overbalance');
  "
  local transfer_sender_history_body transfer_receiver_history_body
  local transfer_history_deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until transfer_sender_history_body="$(expect_2xx "wallet transfer sender transaction history" "$(curl_json POST "http://127.0.0.1:8091/transaction/${transfer_sender}" '{"coin":"USDT","category":"NORMAL","limit":10,"offset":0,"ascendingByTime":false}')")" &&
    printf '%s\n' "$transfer_sender_history_body" | jq -e \
      --arg sender "$transfer_sender" \
      --arg receiver "$transfer_receiver" \
      --arg transferRef "$transfer_ref" '
      length == 1
      and .[0].senderUuid == $sender
      and .[0].receiverUuid == $receiver
      and .[0].currency == "USDT"
      and .[0].category == "NORMAL"
      and .[0].srcWalletType == "MAIN"
      and .[0].destWalletType == "MAIN"
      and .[0].ref == $transferRef
      and .[0].description == "e2e wallet transfer"
      and .[0].withdraw == true
      and ((.[0].amount | tonumber) >= 2.999999)
      and ((.[0].amount | tonumber) <= 3.000001)
    ' >/dev/null &&
    transfer_receiver_history_body="$(expect_2xx "wallet transfer receiver transaction history" "$(curl_json POST "http://127.0.0.1:8091/transaction/${transfer_receiver}" '{"coin":"USDT","category":"NORMAL","limit":10,"offset":0,"ascendingByTime":false}')")" &&
    printf '%s\n' "$transfer_receiver_history_body" | jq -e \
      --arg sender "$transfer_sender" \
      --arg receiver "$transfer_receiver" \
      --arg transferRef "$transfer_ref" '
      length == 1
      and .[0].senderUuid == $sender
      and .[0].receiverUuid == $receiver
      and .[0].currency == "USDT"
      and .[0].category == "NORMAL"
      and .[0].srcWalletType == "MAIN"
      and .[0].destWalletType == "MAIN"
      and .[0].ref == $transferRef
      and .[0].description == "e2e wallet transfer"
      and .[0].withdraw == false
      and ((.[0].amount | tonumber) >= 2.999999)
      and ((.[0].amount | tonumber) <= 3.000001)
    ' >/dev/null; do
    if (( SECONDS > transfer_history_deadline )); then
      echo "Timed out waiting for wallet transfer transaction history to reflect both sides" >&2
      printf 'sender history: %s\n' "$transfer_sender_history_body" >&2
      printf 'receiver history: %s\n' "$transfer_receiver_history_body" >&2
      exit 1
    fi
    sleep 1
  done
  printf '%s\n' "$transfer_sender_history_body" >/tmp/opex-e2e-wallet-transfer-sender-history.json
  printf '%s\n' "$transfer_receiver_history_body" >/tmp/opex-e2e-wallet-transfer-receiver-history.json

  local withdraw_owner="e2e-withdraw-$(date +%s)"
  local withdraw_ref="e2e-withdraw-$(date +%s)"
  deposit_via_bc_gateway "$withdraw_owner" "USDT" "10" "${withdraw_ref}-usdt" "0x110a13FC3efE6A245B50102D2d79B3E76125Ae83" "10000000"
  assert_wallet_balance "withdraw owner initial USDT" "$withdraw_owner" "USDT" "10"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "10" "0" "0" /tmp/opex-e2e-withdraw-initial-asset.json
  wait_binance_deposit_history "$withdraw_owner" "USDT" "${withdraw_ref}-usdt" "10" /tmp/opex-e2e-binance-deposit-history.json

  local withdraw_below_minimum_body='{"currency":"USDT","amount":0.5,"destSymbol":"USDT","destAddress":"0xwithdrawbelowminimum","destNetwork":"test-ethereum","destNote":"below-minimum","description":"e2e withdraw below minimum"}'
  local withdraw_net_below_minimum_body='{"currency":"USDT","amount":1.05,"destSymbol":"USDT","destAddress":"0xwithdrawnetbelowminimum","destNetwork":"test-ethereum","destNote":"net-below-minimum","description":"e2e withdraw net below minimum"}'
  local withdraw_zero_amount_body='{"currency":"USDT","amount":0,"destSymbol":"USDT","destAddress":"0xwithdrawzero","destNetwork":"test-ethereum","destNote":"zero","description":"e2e withdraw zero"}'
  local withdraw_overbalance_body='{"currency":"USDT","amount":11,"destSymbol":"USDT","destAddress":"0xwithdrawoverbalance","destNetwork":"test-ethereum","destNote":"overbalance","description":"e2e withdraw overbalance"}'
  expect_http_status "withdraw below minimum rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/withdraw" "$withdraw_below_minimum_body" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-below-minimum.json
  expect_http_status "withdraw net below minimum rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/withdraw" "$withdraw_net_below_minimum_body" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-net-below-minimum.json
  expect_http_status "withdraw zero amount rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/withdraw" "$withdraw_zero_amount_body" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-zero-amount.json
  expect_http_status "withdraw overbalance rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/withdraw" "$withdraw_overbalance_body" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-overbalance.json
  assert_wallet_balance "withdraw owner unchanged after invalid requests" "$withdraw_owner" "USDT" "10"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "10" "0" "0" /tmp/opex-e2e-withdraw-after-invalid-asset.json
  wait_query_eq "invalid withdraw requests absent from ledger" "postgres-wallet" "0" "
    select count(*)
    from withdraws
    where uuid = '$withdraw_owner'
      and dest_address in (
        '0xwithdrawbelowminimum',
        '0xwithdrawnetbelowminimum',
        '0xwithdrawzero',
        '0xwithdrawoverbalance'
      );
  "

  local withdraw_cancel_body='{"currency":"USDT","amount":3,"destSymbol":"USDT","destAddress":"0xwithdrawcancel","destNetwork":"test-ethereum","destNote":"cancel","description":"e2e withdraw cancel"}'
  expect_2xx "withdraw cancel request" "$(curl_json POST "http://127.0.0.1:8091/withdraw" "$withdraw_cancel_body" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-cancel-request.json
  local withdraw_cancel_id
  withdraw_cancel_id="$(jq -r '.withdrawId' /tmp/opex-e2e-withdraw-cancel-request.json)"
  wait_withdraw_status "withdraw cancel created" "$withdraw_cancel_id" "CREATED" /tmp/opex-e2e-withdraw-cancel-created.json
  assert_wallet_balance "withdraw owner reserved for cancel" "$withdraw_owner" "USDT" "7"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "7" "0" "3" /tmp/opex-e2e-withdraw-cancel-created-asset.json
  expect_http_status "withdraw intruder cancel rejected" "403" "$(curl_json POST "http://127.0.0.1:8091/withdraw/${withdraw_cancel_id}/cancel" "" "${withdraw_owner}-intruder")" >/tmp/opex-e2e-withdraw-intruder-cancel.json
  wait_withdraw_status "withdraw cancel still created after intruder cancel" "$withdraw_cancel_id" "CREATED" /tmp/opex-e2e-withdraw-cancel-after-intruder.json
  assert_wallet_balance "withdraw owner unchanged after intruder cancel" "$withdraw_owner" "USDT" "7"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "7" "0" "3" /tmp/opex-e2e-withdraw-cancel-after-intruder-asset.json
  expect_2xx "withdraw cancel action" "$(curl_json POST "http://127.0.0.1:8091/withdraw/${withdraw_cancel_id}/cancel" "" "$withdraw_owner")" >/dev/null
  wait_withdraw_status "withdraw cancel canceled" "$withdraw_cancel_id" "CANCELED" /tmp/opex-e2e-withdraw-cancel-canceled.json
  assert_wallet_balance "withdraw owner restored after cancel" "$withdraw_owner" "USDT" "10"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "10" "0" "0" /tmp/opex-e2e-withdraw-cancel-canceled-asset.json
  expect_http_status "withdraw canceled cannot process" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_cancel_id}/process")" >/tmp/opex-e2e-withdraw-canceled-process.json
  expect_http_status "withdraw canceled cannot reject" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_cancel_id}/reject?reason=e2e-canceled-reject")" >/tmp/opex-e2e-withdraw-canceled-reject.json
  wait_withdraw_status "withdraw cancel remains canceled" "$withdraw_cancel_id" "CANCELED" /tmp/opex-e2e-withdraw-cancel-terminal.json
  assert_wallet_balance "withdraw owner unchanged after canceled terminal attempts" "$withdraw_owner" "USDT" "10"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "10" "0" "0" /tmp/opex-e2e-withdraw-cancel-terminal-asset.json

  local withdraw_accept_body='{"currency":"USDT","amount":4,"destSymbol":"USDT","destAddress":"0xwithdrawaccept","destNetwork":"test-ethereum","destNote":"accept","description":"e2e withdraw accept"}'
  expect_2xx "withdraw accept request" "$(curl_json POST "http://127.0.0.1:8091/withdraw" "$withdraw_accept_body" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-accept-request.json
  local withdraw_accept_id
  withdraw_accept_id="$(jq -r '.withdrawId' /tmp/opex-e2e-withdraw-accept-request.json)"
  wait_withdraw_status "withdraw accept created" "$withdraw_accept_id" "CREATED" /tmp/opex-e2e-withdraw-accept-created.json
  assert_wallet_balance "withdraw owner reserved for accept" "$withdraw_owner" "USDT" "6"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "6" "0" "4" /tmp/opex-e2e-withdraw-accept-created-asset.json
  expect_2xx "withdraw process action" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_accept_id}/process")" >/tmp/opex-e2e-withdraw-processing.json
  wait_withdraw_status "withdraw processing" "$withdraw_accept_id" "PROCESSING" /tmp/opex-e2e-withdraw-processing-state.json
  expect_http_status "withdraw processing user cancel rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/withdraw/${withdraw_accept_id}/cancel" "" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-processing-cancel.json
  wait_withdraw_status "withdraw remains processing after cancel attempt" "$withdraw_accept_id" "PROCESSING" /tmp/opex-e2e-withdraw-processing-after-cancel.json
  assert_wallet_balance "withdraw owner still reserved while processing" "$withdraw_owner" "USDT" "6"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "6" "0" "4" /tmp/opex-e2e-withdraw-processing-asset.json
  expect_http_status "withdraw zero dest amount accept rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_accept_id}/accept?destTransactionRef=${withdraw_ref}-zero-dest&destAmount=0")" >/tmp/opex-e2e-withdraw-zero-dest-accept.json
  expect_http_status "withdraw excessive dest amount accept rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_accept_id}/accept?destTransactionRef=${withdraw_ref}-excessive-dest&destAmount=4.01")" >/tmp/opex-e2e-withdraw-excessive-dest-accept.json
  wait_withdraw_status "withdraw remains processing after invalid accept attempts" "$withdraw_accept_id" "PROCESSING" /tmp/opex-e2e-withdraw-processing-after-invalid-accept.json
  assert_wallet_balance "withdraw owner still reserved after invalid accept attempts" "$withdraw_owner" "USDT" "6"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "6" "0" "4" /tmp/opex-e2e-withdraw-processing-after-invalid-accept-asset.json
  expect_2xx "withdraw accept action" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_accept_id}/accept?destTransactionRef=${withdraw_ref}-chain&destAmount=3.9")" >/tmp/opex-e2e-withdraw-done.json
  wait_withdraw_status "withdraw done" "$withdraw_accept_id" "DONE" /tmp/opex-e2e-withdraw-done-state.json
  assert_wallet_balance "withdraw owner final after accept" "$withdraw_owner" "USDT" "6"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "6" "0" "0" /tmp/opex-e2e-withdraw-done-asset.json
  expect_http_status "withdraw duplicate accept rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_accept_id}/accept?destTransactionRef=${withdraw_ref}-chain-duplicate&destAmount=3.9")" >/tmp/opex-e2e-withdraw-duplicate-accept.json
  expect_http_status "withdraw done cannot process" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_accept_id}/process")" >/tmp/opex-e2e-withdraw-done-process.json
  expect_http_status "withdraw done cannot reject" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_accept_id}/reject?reason=e2e-done-reject")" >/tmp/opex-e2e-withdraw-done-reject.json
  expect_http_status "withdraw done user cancel rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/withdraw/${withdraw_accept_id}/cancel" "" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-done-cancel.json
  wait_withdraw_status "withdraw remains done after terminal attempts" "$withdraw_accept_id" "DONE" /tmp/opex-e2e-withdraw-done-terminal.json
  assert_wallet_balance "withdraw owner unchanged after done terminal attempts" "$withdraw_owner" "USDT" "6"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "6" "0" "0" /tmp/opex-e2e-withdraw-done-terminal-asset.json

  local withdraw_duplicate_ref_body='{"currency":"USDT","amount":1.1,"destSymbol":"USDT","destAddress":"0xwithdrawduplicateref","destNetwork":"test-ethereum","destNote":"duplicate-ref","description":"e2e withdraw duplicate destination ref"}'
  expect_2xx "withdraw duplicate destination ref request" "$(curl_json POST "http://127.0.0.1:8091/withdraw" "$withdraw_duplicate_ref_body" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-duplicate-ref-request.json
  local withdraw_duplicate_ref_id
  withdraw_duplicate_ref_id="$(jq -r '.withdrawId' /tmp/opex-e2e-withdraw-duplicate-ref-request.json)"
  wait_withdraw_status "withdraw duplicate destination ref created" "$withdraw_duplicate_ref_id" "CREATED" /tmp/opex-e2e-withdraw-duplicate-ref-created.json
  assert_wallet_balance "withdraw owner reserved for duplicate destination ref" "$withdraw_owner" "USDT" "4.9"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "4.9" "0" "1.1" /tmp/opex-e2e-withdraw-duplicate-ref-created-asset.json
  expect_2xx "withdraw duplicate destination ref process action" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_duplicate_ref_id}/process")" >/tmp/opex-e2e-withdraw-duplicate-ref-processing.json
  wait_withdraw_status "withdraw duplicate destination ref processing" "$withdraw_duplicate_ref_id" "PROCESSING" /tmp/opex-e2e-withdraw-duplicate-ref-processing-state.json
  expect_http_status "withdraw duplicate destination ref accept rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_duplicate_ref_id}/accept?destTransactionRef=${withdraw_ref}-chain&destAmount=1.0")" >/tmp/opex-e2e-withdraw-duplicate-ref-accept.json
  wait_withdraw_status "withdraw duplicate destination ref remains processing" "$withdraw_duplicate_ref_id" "PROCESSING" /tmp/opex-e2e-withdraw-duplicate-ref-after-accept.json
  assert_wallet_balance "withdraw owner still reserved after duplicate destination ref" "$withdraw_owner" "USDT" "4.9"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "4.9" "0" "1.1" /tmp/opex-e2e-withdraw-duplicate-ref-after-accept-asset.json
  expect_2xx "withdraw duplicate destination ref reject action" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_duplicate_ref_id}/reject?reason=e2e-duplicate-ref")" >/tmp/opex-e2e-withdraw-duplicate-ref-rejected.json
  wait_withdraw_status "withdraw duplicate destination ref rejected" "$withdraw_duplicate_ref_id" "REJECTED" /tmp/opex-e2e-withdraw-duplicate-ref-rejected-state.json
  assert_wallet_balance "withdraw owner restored after duplicate destination ref reject" "$withdraw_owner" "USDT" "6"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "6" "0" "0" /tmp/opex-e2e-withdraw-duplicate-ref-rejected-asset.json

  local withdraw_reject_body='{"currency":"USDT","amount":2,"destSymbol":"USDT","destAddress":"0xwithdrawreject","destNetwork":"test-ethereum","destNote":"reject","description":"e2e withdraw reject"}'
  expect_2xx "withdraw reject request" "$(curl_json POST "http://127.0.0.1:8091/withdraw" "$withdraw_reject_body" "$withdraw_owner")" >/tmp/opex-e2e-withdraw-reject-request.json
  local withdraw_reject_id
  withdraw_reject_id="$(jq -r '.withdrawId' /tmp/opex-e2e-withdraw-reject-request.json)"
  wait_withdraw_status "withdraw reject created" "$withdraw_reject_id" "CREATED" /tmp/opex-e2e-withdraw-reject-created.json
  assert_wallet_balance "withdraw owner reserved for reject" "$withdraw_owner" "USDT" "4"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "4" "0" "2" /tmp/opex-e2e-withdraw-reject-created-asset.json
  expect_2xx "withdraw reject process action" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_reject_id}/process")" >/tmp/opex-e2e-withdraw-reject-processing.json
  wait_withdraw_status "withdraw reject processing" "$withdraw_reject_id" "PROCESSING" /tmp/opex-e2e-withdraw-reject-processing-state.json
  expect_2xx "withdraw reject action" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_reject_id}/reject?reason=e2e-reject")" >/tmp/opex-e2e-withdraw-rejected.json
  wait_withdraw_status "withdraw rejected" "$withdraw_reject_id" "REJECTED" /tmp/opex-e2e-withdraw-rejected-state.json
  assert_wallet_balance "withdraw owner restored after reject" "$withdraw_owner" "USDT" "6"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "6" "0" "0" /tmp/opex-e2e-withdraw-rejected-asset.json
  expect_http_status "withdraw rejected cannot process" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_reject_id}/process")" >/tmp/opex-e2e-withdraw-rejected-process.json
  expect_http_status "withdraw rejected cannot accept" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_reject_id}/accept?destTransactionRef=${withdraw_ref}-rejected-chain&destAmount=1.9")" >/tmp/opex-e2e-withdraw-rejected-accept.json
  expect_http_status "withdraw rejected duplicate reject rejected" "400" "$(curl_json POST "http://127.0.0.1:8091/admin/withdraw/${withdraw_reject_id}/reject?reason=e2e-reject-duplicate")" >/tmp/opex-e2e-withdraw-rejected-duplicate-reject.json
  wait_withdraw_status "withdraw remains rejected after terminal attempts" "$withdraw_reject_id" "REJECTED" /tmp/opex-e2e-withdraw-rejected-terminal.json
  assert_wallet_balance "withdraw owner unchanged after rejected terminal attempts" "$withdraw_owner" "USDT" "6"
  wait_binance_user_asset_balance "$withdraw_owner" "USDT" "6" "0" "0" /tmp/opex-e2e-withdraw-rejected-terminal-asset.json
  local withdraw_history_body
  local withdraw_history_deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until withdraw_history_body="$(expect_2xx "withdraw user history" "$(curl_json POST "http://127.0.0.1:8091/withdraw/history" '{"currency":"USDT","limit":10,"offset":0,"ascendingByTime":false}' "$withdraw_owner")")" &&
    printf '%s\n' "$withdraw_history_body" | jq -e \
      --arg owner "$withdraw_owner" \
      --arg chain_ref "${withdraw_ref}-chain" \
      --arg reject_reason "e2e-reject" \
      --argjson cancel_id "$withdraw_cancel_id" \
      --argjson accept_id "$withdraw_accept_id" \
      --argjson duplicate_ref_id "$withdraw_duplicate_ref_id" \
      --argjson reject_id "$withdraw_reject_id" '
      length == 4
      and all(.[]; .uuid == $owner and .currency == "USDT")
      and ([.[] | select(.status == "CANCELED")] | length) == 1
      and ([.[] | select(.status == "DONE")] | length) == 1
      and ([.[] | select(.status == "REJECTED")] | length) == 2
      and any(.[]; .withdrawId == $cancel_id and .status == "CANCELED" and ((.amount | tonumber) == 2.9) and ((.appliedFee | tonumber) == 0.1))
      and any(.[]; .withdrawId == $accept_id and .status == "DONE" and .destTransactionRef == $chain_ref and ((.amount | tonumber) == 3.9) and ((.appliedFee | tonumber) == 0.1) and ((.destAmount | tonumber) >= 3.899999) and ((.destAmount | tonumber) <= 3.900001))
      and any(.[]; .withdrawId == $duplicate_ref_id and .status == "REJECTED" and .statusReason == "e2e-duplicate-ref" and ((.amount | tonumber) == 1.0) and ((.appliedFee | tonumber) == 0.1))
      and any(.[]; .withdrawId == $reject_id and .status == "REJECTED" and .statusReason == $reject_reason and ((.amount | tonumber) == 1.9) and ((.appliedFee | tonumber) == 0.1))
    ' >/dev/null; do
    if (( SECONDS > withdraw_history_deadline )); then
      echo "Timed out waiting for withdraw user history to reflect terminal states" >&2
      printf '%s\n' "$withdraw_history_body" >&2
      exit 1
    fi
    sleep 1
  done
  printf '%s\n' "$withdraw_history_body" >/tmp/opex-e2e-withdraw-history.json
  wait_binance_withdraw_history "$withdraw_owner" "USDT" "$withdraw_cancel_id" "$withdraw_accept_id" "$withdraw_duplicate_ref_id" "$withdraw_reject_id" "${withdraw_ref}-chain" /tmp/opex-e2e-binance-withdraw-history.json
  wait_binance_withdraw_history_v2 "$withdraw_owner" "USDT" "$withdraw_cancel_id" "$withdraw_accept_id" "$withdraw_duplicate_ref_id" "$withdraw_reject_id" "${withdraw_ref}-chain" /tmp/opex-e2e-binance-withdraw-history-v2.json
  wait_binance_withdraw_history_status_filter "$withdraw_owner" "USDT" -1 1 "$withdraw_cancel_id" /tmp/opex-e2e-binance-withdraw-history-status-canceled.json
  wait_binance_withdraw_history_status_filter "$withdraw_owner" "USDT" 1 1 "$withdraw_accept_id" /tmp/opex-e2e-binance-withdraw-history-status-done.json
  wait_binance_withdraw_history_status_filter "$withdraw_owner" "USDT" 2 2 "$withdraw_reject_id" /tmp/opex-e2e-binance-withdraw-history-status-rejected.json
  wait_binance_withdraw_history_status_filter_v2 "$withdraw_owner" "USDT" -1 1 "$withdraw_cancel_id" /tmp/opex-e2e-binance-withdraw-history-v2-status-canceled.json
  wait_binance_withdraw_history_status_filter_v2 "$withdraw_owner" "USDT" 1 1 "$withdraw_accept_id" /tmp/opex-e2e-binance-withdraw-history-v2-status-done.json
  wait_binance_withdraw_history_status_filter_v2 "$withdraw_owner" "USDT" 2 2 "$withdraw_reject_id" /tmp/opex-e2e-binance-withdraw-history-v2-status-rejected.json
  local withdraw_transaction_history_body
  local withdraw_transaction_history_deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until withdraw_transaction_history_body="$(expect_2xx "withdraw transaction history" "$(curl_json POST "http://127.0.0.1:8091/v2/transaction" '{"currency":"USDT","category":"WITHDRAW","limit":10,"offset":0,"ascendingByTime":false}' "$withdraw_owner")")" &&
    printf '%s\n' "$withdraw_transaction_history_body" | jq -e --arg owner "$withdraw_owner" '
      length == 1
      and .[0].userId == $owner
      and .[0].currency == "USDT"
      and .[0].category == "WITHDRAW"
      and ((.[0].balance | tonumber) >= 5.999999)
      and ((.[0].balance | tonumber) <= 6.000001)
      and ((.[0].balanceChange | tonumber) >= -4.000001)
      and ((.[0].balanceChange | tonumber) <= -3.999999)
    ' >/dev/null; do
    if (( SECONDS > withdraw_transaction_history_deadline )); then
      echo "Timed out waiting for withdraw transaction history to show one accepted withdraw" >&2
      printf '%s\n' "$withdraw_transaction_history_body" >&2
      exit 1
    fi
    sleep 1
  done
  printf '%s\n' "$withdraw_transaction_history_body" >/tmp/opex-e2e-withdraw-transaction-history.json

  local withdraw_legacy_accept_history_body withdraw_legacy_cancel_history_body withdraw_legacy_reject_history_body
  local withdraw_legacy_history_deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until withdraw_legacy_accept_history_body="$(expect_2xx "withdraw legacy accept transaction history" "$(curl_json POST "http://127.0.0.1:8091/transaction/${withdraw_owner}" '{"coin":"USDT","category":"WITHDRAW_ACCEPT","limit":10,"offset":0,"ascendingByTime":false}')")" &&
    printf '%s\n' "$withdraw_legacy_accept_history_body" | jq -e \
      --arg owner "$withdraw_owner" \
      --arg refPrefix "wallet:withdraw:${withdraw_owner}:DONE:" '
      length == 1
      and .[0].senderUuid == $owner
      and .[0].receiverUuid != $owner
      and .[0].currency == "USDT"
      and .[0].category == "WITHDRAW_ACCEPT"
      and .[0].srcWalletType == "CASHOUT"
      and .[0].destWalletType == "MAIN"
      and .[0].description == null
      and (.[0].ref | startswith($refPrefix))
      and .[0].withdraw == true
      and ((.[0].amount | tonumber) >= 3.999999)
      and ((.[0].amount | tonumber) <= 4.000001)
    ' >/dev/null &&
    withdraw_legacy_cancel_history_body="$(expect_2xx "withdraw legacy cancel transaction history" "$(curl_json POST "http://127.0.0.1:8091/transaction/${withdraw_owner}" '{"coin":"USDT","category":"WITHDRAW_CANCEL","limit":10,"offset":0,"ascendingByTime":false}')")" &&
    printf '%s\n' "$withdraw_legacy_cancel_history_body" | jq -e \
      --arg owner "$withdraw_owner" \
      --arg refPrefix "wallet:withdraw:${withdraw_cancel_id}:CANCELED:" '
      length == 1
      and .[0].senderUuid == $owner
      and .[0].receiverUuid == $owner
      and .[0].currency == "USDT"
      and .[0].category == "WITHDRAW_CANCEL"
      and .[0].srcWalletType == "CASHOUT"
      and .[0].destWalletType == "MAIN"
      and .[0].description == null
      and (.[0].ref | startswith($refPrefix))
      and .[0].withdraw == false
      and ((.[0].amount | tonumber) >= 2.999999)
      and ((.[0].amount | tonumber) <= 3.000001)
    ' >/dev/null &&
    withdraw_legacy_reject_history_body="$(expect_2xx "withdraw legacy reject transaction history" "$(curl_json POST "http://127.0.0.1:8091/transaction/${withdraw_owner}" '{"coin":"USDT","category":"WITHDRAW_REJECT","limit":10,"offset":0,"ascendingByTime":false}')")" &&
    printf '%s\n' "$withdraw_legacy_reject_history_body" | jq -e \
      --arg owner "$withdraw_owner" \
      --arg duplicateRefPrefix "wallet:withdraw:${withdraw_duplicate_ref_id}:REJECTED:" \
      --arg rejectRefPrefix "wallet:withdraw:${withdraw_reject_id}:REJECTED:" '
      length == 2
      and all(.[]; .senderUuid == $owner and .receiverUuid == $owner and .currency == "USDT" and .category == "WITHDRAW_REJECT" and .srcWalletType == "CASHOUT" and .destWalletType == "MAIN" and .withdraw == false)
      and any(.[]; .description == "e2e-duplicate-ref" and (.ref | startswith($duplicateRefPrefix)) and ((.amount | tonumber) >= 1.099999) and ((.amount | tonumber) <= 1.100001))
      and any(.[]; .description == "e2e-reject" and (.ref | startswith($rejectRefPrefix)) and ((.amount | tonumber) >= 1.999999) and ((.amount | tonumber) <= 2.000001))
    ' >/dev/null; do
    if (( SECONDS > withdraw_legacy_history_deadline )); then
      echo "Timed out waiting for withdraw legacy transaction history to reflect terminal ledger actions" >&2
      printf 'accept history: %s\n' "$withdraw_legacy_accept_history_body" >&2
      printf 'cancel history: %s\n' "$withdraw_legacy_cancel_history_body" >&2
      printf 'reject history: %s\n' "$withdraw_legacy_reject_history_body" >&2
      exit 1
    fi
    sleep 1
  done
  printf '%s\n' "$withdraw_legacy_accept_history_body" >/tmp/opex-e2e-withdraw-legacy-accept-history.json
  printf '%s\n' "$withdraw_legacy_cancel_history_body" >/tmp/opex-e2e-withdraw-legacy-cancel-history.json
  printf '%s\n' "$withdraw_legacy_reject_history_body" >/tmp/opex-e2e-withdraw-legacy-reject-history.json

  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  replay_order_request_duplicate
  replay_cancel_request_duplicate
  wait_eventlog_trades_match_market_projection "eventlog trade audit rows match market trade projection before replay"
  replay_accountant_event_duplicates
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"
  wait_recent_trades_distribution "ETH_USDT" /tmp/opex-e2e-recent-trades.json
  wait_binance_latest_kline_matches_market_projection "ETHUSDT" "ETH_USDT" /tmp/opex-e2e-binance-latest-kline.json
  wait_query_eq "wallet transaction category ledger" "postgres-wallet" $'DEPOSIT,83\nFEE,46\nNORMAL,1\nORDER_CANCEL,47\nORDER_CREATE,79\nORDER_FINALIZED,1\nTRADE,46\nWITHDRAW_ACCEPT,1\nWITHDRAW_CANCEL,1\nWITHDRAW_REJECT,2\nWITHDRAW_REQUEST,4' "
    select t.transfer_category, count(*)
    from transaction t
    join wallet sw on sw.id = t.source_wallet
    join wallet_owner swo on swo.id = sw.owner
    join wallet dw on dw.id = t.dest_wallet
    join wallet_owner dwo on dwo.id = dw.owner
    where swo.uuid like 'e2e-%' or dwo.uuid like 'e2e-%'
    group by t.transfer_category
    order by t.transfer_category;
  "
  wait_query_eq "wallet aggregate balances" "postgres-wallet" $'BTC,0.00200000\nETH,48.54000000\nUSDT,3245.90400000' "
    select w.currency, to_char(sum(w.balance), 'FM9999999990.00000000')
    from wallet w
    join wallet_owner wo on wo.id = w.owner
    where wo.uuid like 'e2e-%'
    group by w.currency
    order by w.currency;
  "
  wait_query_eq "wallet withdraw status ledger" "postgres-wallet" $'CANCELED,1,2.90000000,0.10000000\nDONE,1,3.90000000,0.10000000\nREJECTED,2,2.90000000,0.20000000' "
    select status,
           count(*),
           to_char(sum(amount), 'FM9999999990.00000000'),
           to_char(sum(applied_fee), 'FM9999999990.00000000')
    from withdraws
    where uuid = '$withdraw_owner'
    group by status
    order by status;
  "
  wait_query_eq "wallet accepted withdraw chain reference" "postgres-wallet" "1" "
    select count(*)
    from withdraws
    where uuid = '$withdraw_owner'
      and status = 'DONE'
      and dest_transaction_ref = '${withdraw_ref}-chain'
      and dest_amount = 3.9
      and final_transaction_id is not null
      and accept_date is not null;
  "
  wait_query_eq "wallet rejected withdraw reason and release tx" "postgres-wallet" "1" "
    select count(*)
    from withdraws
    where uuid = '$withdraw_owner'
      and status = 'REJECTED'
      and status_reason = 'e2e-reject'
      and final_transaction_id is not null
      and dest_transaction_ref is null;
  "
  wait_query_eq "wallet withdraw destination refs unique" "postgres-wallet" "0" "
    select count(*)
    from (
      select dest_transaction_ref
      from withdraws
      where uuid like 'e2e-%'
        and dest_transaction_ref is not null
      group by dest_transaction_ref
      having count(*) > 1
    ) duplicate_withdraw_refs;
  "
  wait_query_eq "wallet exchange balances fully released" "postgres-wallet" "0" "
    select count(*)
    from wallet w
    join wallet_owner wo on wo.id = w.owner
    where wo.uuid like 'e2e-%'
      and w.wallet_type = 'EXCHANGE'
      and abs(w.balance) > 0.000001;
  "
  wait_query_eq "wallet cashout balances fully released" "postgres-wallet" "0" "
    select count(*)
    from wallet w
    join wallet_owner wo on wo.id = w.owner
    where wo.uuid like 'e2e-%'
      and w.wallet_type = 'CASHOUT'
      and abs(w.balance) > 0.000001;
  "
  wait_query_eq "accountant processed financial actions" "postgres-accountant" $'CancelOrderEvent,PROCESSED,41\nRejectOrderEvent,PROCESSED,3\nSubmitOrderEvent,PROCESSED,79\nTradeEvent,PROCESSED,93\nUpdatedOrderEvent,PROCESSED,3' "
    select event_type, status, count(*)
    from fi_actions
    where sender like 'e2e-%' or receiver like 'e2e-%'
    group by event_type, status
    order by event_type, status;
  "
  wait_market_count_apis_match_database "market count APIs match database after ETH scenarios"
  wait_query_eq "accountant retry queue drained" "postgres-accountant" "0,0" "
    select
      count(*) filter (where is_resolved = false and has_given_up = false),
      count(*) filter (where has_given_up = true)
    from fi_action_retry;
  "
  wait_query_eq "wallet e2e balances never negative" "postgres-wallet" "0" "
    select count(*)
    from wallet w
    join wallet_owner wo on wo.id = w.owner
    where wo.uuid like 'e2e-%'
      and w.balance < -0.000001;
  "
  wait_query_eq "wallet e2e ledger transfer refs unique" "postgres-wallet" "0" "
    select count(*)
    from (
      select t.transfer_ref
      from transaction t
      join wallet sw on sw.id = t.source_wallet
      join wallet_owner swo on swo.id = sw.owner
      join wallet dw on dw.id = t.dest_wallet
      join wallet_owner dwo on dwo.id = dw.owner
      where swo.uuid like 'e2e-%' or dwo.uuid like 'e2e-%'
      group by t.transfer_ref
      having count(*) > 1
    ) duplicate_refs;
  "
  wait_query_eq "accountant e2e actions all processed" "postgres-accountant" "0" "
    select count(*)
    from fi_actions
    where (sender like 'e2e-%' or receiver like 'e2e-%')
      and coalesce(status, '') <> 'PROCESSED';
  "
  wait_query_eq "market open orders table empty" "postgres-market" "0" "
    select count(*) from open_orders;
  "
  wait_query_eq "market e2e trades have positive price and quantity" "postgres-market" "0" "
    select count(*)
    from trades
    where (maker_uuid like 'e2e-%' or taker_uuid like 'e2e-%')
      and (matched_price <= 0 or matched_quantity <= 0);
  "
  wait_query_eq "market e2e trade projections unique" "postgres-market" "0" "
    select count(*)
    from (
      select symbol, trade_id, taker_ouid, maker_ouid, matched_quantity, maker_price
      from trades
      where maker_uuid like 'e2e-%' or taker_uuid like 'e2e-%'
      group by symbol, trade_id, taker_ouid, maker_ouid, matched_quantity, maker_price
      having count(*) > 1
    ) duplicate_trade_events;
  "
  wait_query_eq "market e2e trade projections internally consistent" "postgres-market" "0" "
    select count(*)
    from trades
    where (maker_uuid like 'e2e-%' or taker_uuid like 'e2e-%')
      and (
        maker_uuid = taker_uuid
        or maker_ouid = taker_ouid
        or maker_uuid = ''
        or taker_uuid = ''
        or maker_ouid = ''
        or taker_ouid = ''
        or base_asset <> split_part(symbol, '_', 1)
        or quote_asset <> split_part(symbol, '_', 2)
        or matched_price <> maker_price
        or coalesce(maker_commission, 0) < 0
        or coalesce(taker_commission, 0) < 0
        or coalesce(maker_commission_asset, '') not in (base_asset, quote_asset)
        or coalesce(taker_commission_asset, '') not in (base_asset, quote_asset)
      );
  "
  wait_market_order_status_matches_trade_totals "market e2e order status matches trade totals"
  wait_accountant_market_order_definitions_match "accountant and market e2e order definitions match"
  wait_accountant_order_actions_match_eventlog_lifecycle "accountant order actions match eventlog lifecycle"
  wait_accountant_market_order_projections_match "accountant and market e2e order projections match"
  wait_accountant_trade_transfers_match_market_executions "accountant e2e trade transfers match market executions"
  wait_accountant_trade_fees_match_market_commissions "accountant e2e trade fee actions match market commissions"
  wait_wallet_fee_transactions_match_accountant_actions "wallet e2e fee transactions match accountant fee actions"
  wait_wallet_transactions_match_accountant_actions "wallet e2e transactions match accountant financial actions"
  wait_wallet_user_transactions_match_wallet_transactions "wallet e2e user transactions match wallet movements"
  wait_query_eq "market persisted trade distribution" "postgres-market" $'90.00,0.10000000\n100.00,1.40000000\n101.00,0.20000000\n102.00,0.20000000\n103.00,0.20000000\n111.00,0.50000000\n112.00,0.40000000\n113.00,0.30000000\n114.00,0.20000000\n115.00,0.20000000\n116.00,0.20000000\n117.00,0.20000000\n120.00,0.40000000\n125.00,0.20000000\n130.00,0.20000000\n140.00,0.40000000\n150.00,0.10000000\n169.00,0.20000000\n172.00,0.20000000\n173.00,0.20000000' "
    select
      to_char(matched_price, 'FM9999999990.00'),
      to_char(sum(matched_quantity), 'FM9999999990.00000000')
    from trades
    group by matched_price
    order by matched_price;
  "
  replay_market_projection_duplicates
  wait_query_eq "market open orders unchanged after duplicate richOrder replay" "postgres-market" "0" "
    select count(*) from open_orders;
  "
  wait_query_eq "market trade distribution unchanged after duplicate richTrade replay" "postgres-market" $'90.00,0.10000000\n100.00,1.40000000\n101.00,0.20000000\n102.00,0.20000000\n103.00,0.20000000\n111.00,0.50000000\n112.00,0.40000000\n113.00,0.30000000\n114.00,0.20000000\n115.00,0.20000000\n116.00,0.20000000\n117.00,0.20000000\n120.00,0.40000000\n125.00,0.20000000\n130.00,0.20000000\n140.00,0.40000000\n150.00,0.10000000\n169.00,0.20000000\n172.00,0.20000000\n173.00,0.20000000' "
    select
      to_char(matched_price, 'FM9999999990.00'),
      to_char(sum(matched_quantity), 'FM9999999990.00000000')
    from trades
    where symbol = 'ETH_USDT'
    group by matched_price
    order by matched_price;
  "
  restart_market_and_verify_public_state

  local best_price_low_bidder="e2e-best-low-bid-$(date +%s)"
  local best_price_high_bidder="e2e-best-high-bid-$(date +%s)"
  local best_price_low_asker="e2e-best-low-ask-$(date +%s)"
  local best_price_high_asker="e2e-best-high-ask-$(date +%s)"
  local best_price_ref="e2e-best-price-$(date +%s)"
  expect_2xx "best-price low bidder USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/20_test-ethereum_USDT/${best_price_low_bidder}_MAIN?description=e2e-best-price&transferRef=${best_price_ref}-low-bid-usdt")" >/dev/null
  expect_2xx "best-price high bidder USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/20_test-ethereum_USDT/${best_price_high_bidder}_MAIN?description=e2e-best-price&transferRef=${best_price_ref}-high-bid-usdt")" >/dev/null
  expect_2xx "best-price low asker ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/0.1_test-ethereum_ETH/${best_price_low_asker}_MAIN?description=e2e-best-price&transferRef=${best_price_ref}-low-ask-eth")" >/dev/null
  expect_2xx "best-price high asker ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/0.1_test-ethereum_ETH/${best_price_high_asker}_MAIN?description=e2e-best-price&transferRef=${best_price_ref}-high-ask-eth")" >/dev/null

  local best_price_low_bid='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0.1,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local best_price_high_bid='{"uuid":null,"pair":"ETH_USDT","price":110,"quantity":0.1,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local best_price_low_ask='{"uuid":null,"pair":"ETH_USDT","price":120,"quantity":0.1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local best_price_high_ask='{"uuid":null,"pair":"ETH_USDT","price":130,"quantity":0.1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "best-price low bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$best_price_low_bid' '$best_price_low_bidder'" >/tmp/opex-e2e-best-price-low-bid.json
  wait_user_open_order "$best_price_low_bidder" "ETH_USDT" "100" "0.1" /tmp/opex-e2e-best-price-low-bid-open-orders.json
  expect_2xx_retry "best-price high bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$best_price_high_bid' '$best_price_high_bidder'" >/tmp/opex-e2e-best-price-high-bid.json
  wait_user_open_order "$best_price_high_bidder" "ETH_USDT" "110" "0.1" /tmp/opex-e2e-best-price-high-bid-open-orders.json
  expect_2xx_retry "best-price low ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$best_price_low_ask' '$best_price_low_asker'" >/tmp/opex-e2e-best-price-low-ask.json
  wait_user_open_order "$best_price_low_asker" "ETH_USDT" "120" "0.1" /tmp/opex-e2e-best-price-low-ask-open-orders.json
  expect_2xx_retry "best-price high ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$best_price_high_ask' '$best_price_high_asker'" >/tmp/opex-e2e-best-price-high-ask.json
  wait_user_open_order "$best_price_high_asker" "ETH_USDT" "130" "0.1" /tmp/opex-e2e-best-price-high-ask-open-orders.json
  wait_best_prices "ETH_USDT" "110" "120"

  for best_price_owner_file in \
    "$best_price_low_bidder:/tmp/opex-e2e-best-price-low-bid-open-orders.json" \
    "$best_price_high_bidder:/tmp/opex-e2e-best-price-high-bid-open-orders.json" \
    "$best_price_low_asker:/tmp/opex-e2e-best-price-low-ask-open-orders.json" \
    "$best_price_high_asker:/tmp/opex-e2e-best-price-high-ask-open-orders.json"; do
    best_price_owner="${best_price_owner_file%%:*}"
    best_price_file="${best_price_owner_file#*:}"
    best_price_ouid="$(jq -r '.[0].ouid' "$best_price_file")"
    best_price_order_id="$(jq -r '.[0].orderId' "$best_price_file")"
    if [[ -z "$best_price_ouid" || "$best_price_ouid" == "null" || -z "$best_price_order_id" || "$best_price_order_id" == "null" ]]; then
      echo "Best-price open order did not include ouid/orderId required for cancel owner=$best_price_owner" >&2
      cat "$best_price_file" >&2
      exit 1
    fi
    best_price_cancel_request="$(jq -nc --arg ouid "$best_price_ouid" --arg uuid "$best_price_owner" --argjson orderId "$best_price_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
    expect_2xx_retry "cancel best-price order" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$best_price_cancel_request' '$best_price_owner'" >/tmp/opex-e2e-best-price-cancel.json
    wait_no_user_open_orders "$best_price_owner" "ETH_USDT"
    wait_order_projection "$best_price_owner" "$best_price_ouid" "CANCELED" "0" "0"
  done
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"

  local market_bid_cap_buyer="e2e-mkt-bid-cap-buyer-$(date +%s)"
  local market_bid_cap_low_seller="e2e-mkt-bid-cap-low-$(date +%s)"
  local market_bid_cap_high_seller="e2e-mkt-bid-cap-high-$(date +%s)"
  local market_bid_cap_ref="e2e-mkt-bid-cap-$(date +%s)"
  expect_2xx "market-bid-cap buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/19_test-ethereum_USDT/${market_bid_cap_buyer}_MAIN?description=e2e-market-bid-cap&transferRef=${market_bid_cap_ref}-usdt")" >/dev/null
  expect_2xx "market-bid-cap low seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${market_bid_cap_low_seller}_MAIN?description=e2e-market-bid-cap&transferRef=${market_bid_cap_ref}-low-eth")" >/dev/null
  expect_2xx "market-bid-cap high seller ETH deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-ethereum_ETH/${market_bid_cap_high_seller}_MAIN?description=e2e-market-bid-cap&transferRef=${market_bid_cap_ref}-high-eth")" >/dev/null

  local market_bid_cap_low_ask='{"uuid":null,"pair":"ETH_USDT","price":90,"quantity":0.1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local market_bid_cap_high_ask='{"uuid":null,"pair":"ETH_USDT","price":100,"quantity":0.1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local market_bid_cap_bid='{"uuid":null,"pair":"ETH_USDT","price":95,"quantity":0.2,"direction":"BID","matchConstraint":"IOC","orderType":"MARKET_ORDER","userLevel":"*"}'
  expect_2xx_retry "market-bid-cap low ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$market_bid_cap_low_ask' '$market_bid_cap_low_seller'" >/tmp/opex-e2e-market-bid-cap-low-ask.json
  wait_user_open_order "$market_bid_cap_low_seller" "ETH_USDT" "90" "0.1" /tmp/opex-e2e-market-bid-cap-low-open-orders.json
  expect_2xx_retry "market-bid-cap high ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$market_bid_cap_high_ask' '$market_bid_cap_high_seller'" >/tmp/opex-e2e-market-bid-cap-high-ask.json
  wait_user_open_order "$market_bid_cap_high_seller" "ETH_USDT" "100" "0.1" /tmp/opex-e2e-market-bid-cap-high-open-orders.json

  local market_bid_cap_low_ouid market_bid_cap_high_ouid market_bid_cap_high_order_id market_bid_cap_high_cancel_request
  market_bid_cap_low_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-market-bid-cap-low-open-orders.json)"
  market_bid_cap_high_ouid="$(jq -r '.[0].ouid' /tmp/opex-e2e-market-bid-cap-high-open-orders.json)"
  market_bid_cap_high_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-market-bid-cap-high-open-orders.json)"
  if [[ -z "$market_bid_cap_low_ouid" || "$market_bid_cap_low_ouid" == "null" ||
    -z "$market_bid_cap_high_ouid" || "$market_bid_cap_high_ouid" == "null" ||
    -z "$market_bid_cap_high_order_id" || "$market_bid_cap_high_order_id" == "null" ]]; then
    echo "Market-bid-cap open orders did not include ouid/orderId required for status checks" >&2
    cat /tmp/opex-e2e-market-bid-cap-low-open-orders.json >&2
    cat /tmp/opex-e2e-market-bid-cap-high-open-orders.json >&2
    exit 1
  fi

  expect_2xx_retry "market-bid-cap market bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$market_bid_cap_bid' '$market_bid_cap_buyer'" >/tmp/opex-e2e-market-bid-cap-bid.json
  wait_no_user_open_orders "$market_bid_cap_low_seller" "ETH_USDT"
  wait_order_projection "$market_bid_cap_low_seller" "$market_bid_cap_low_ouid" "FILLED" "0.1" "9"
  wait_order_projection "$market_bid_cap_high_seller" "$market_bid_cap_high_ouid" "NEW" "0" "0"
  wait_order_book_level "ETH_USDT" "ASK" "100" "0.1"
  wait_user_trade_projection "$market_bid_cap_low_seller" "ETH_USDT" "90" "0.1" "9" "0.09" "USDT" false true false /tmp/opex-e2e-market-bid-cap-low-seller-trades.json
  wait_user_trade_projection "$market_bid_cap_buyer" "ETH_USDT" "90" "0.1" "9" "0.001" "ETH" true false false /tmp/opex-e2e-market-bid-cap-buyer-trades.json
  market_bid_cap_buyer_order_id="$(jq -r '.[0].orderId' /tmp/opex-e2e-market-bid-cap-buyer-trades.json)"
  if [[ -z "$market_bid_cap_buyer_order_id" || "$market_bid_cap_buyer_order_id" == "null" ]]; then
    echo "Market-bid-cap buyer trade did not include orderId required for order query" >&2
    cat /tmp/opex-e2e-market-bid-cap-buyer-trades.json >&2
    exit 1
  fi
  wait_order_projection_by_order_id "$market_bid_cap_buyer" "ETH_USDT" "$market_bid_cap_buyer_order_id" "CANCELED" "0.1" "9" /tmp/opex-e2e-market-bid-cap-buyer-order.json
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$market_bid_cap_buyer" "ETH" "0.099" &&
    try_wallet_balance "$market_bid_cap_buyer" "USDT" "10" &&
    try_wallet_balance "$market_bid_cap_low_seller" "ETH" "0.9" &&
    try_wallet_balance "$market_bid_cap_low_seller" "USDT" "8.91" &&
    try_wallet_balance "$market_bid_cap_high_seller" "ETH" "0.9"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for market-bid-cap settlement" >&2
      assert_wallet_balance "market-bid-cap buyer ETH received" "$market_bid_cap_buyer" "ETH" "0.099" >&2 || true
      assert_wallet_balance "market-bid-cap buyer USDT remainder" "$market_bid_cap_buyer" "USDT" "10" >&2 || true
      assert_wallet_balance "market-bid-cap low seller ETH remainder" "$market_bid_cap_low_seller" "ETH" "0.9" >&2 || true
      assert_wallet_balance "market-bid-cap low seller USDT proceeds" "$market_bid_cap_low_seller" "USDT" "8.91" >&2 || true
      assert_wallet_balance "market-bid-cap high seller ETH still reserved" "$market_bid_cap_high_seller" "ETH" "0.9" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market matching-engine matching-gateway >&2 || true
      exit 1
    fi
    sleep 2
  done

  market_bid_cap_high_cancel_request="$(jq -nc --arg ouid "$market_bid_cap_high_ouid" --arg uuid "$market_bid_cap_high_seller" --argjson orderId "$market_bid_cap_high_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"ETH_USDT"}')"
  expect_2xx_retry "cancel market-bid-cap high ask" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$market_bid_cap_high_cancel_request' '$market_bid_cap_high_seller'" >/tmp/opex-e2e-market-bid-cap-high-cancel.json
  wait_no_user_open_orders "$market_bid_cap_high_seller" "ETH_USDT"
  wait_order_projection "$market_bid_cap_high_seller" "$market_bid_cap_high_ouid" "CANCELED" "0" "0"
  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$market_bid_cap_high_seller" "ETH" "1"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for market-bid-cap high ask cancel release" >&2
      assert_wallet_balance "market-bid-cap high seller released ETH" "$market_bid_cap_high_seller" "ETH" "1" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 accountant wallet market >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"

  local btc_seller="e2e-btc-seller-$(date +%s)"
  local btc_buyer="e2e-btc-buyer-$(date +%s)"
  local btc_ref="e2e-btc-$(date +%s)"
  expect_2xx "btc-usdt seller BTC deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/0.01_test-bitcoin_BTC/${btc_seller}_MAIN?description=e2e-btc-usdt&transferRef=${btc_ref}-btc")" >/dev/null
  expect_2xx "btc-usdt buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/50_test-ethereum_USDT/${btc_buyer}_MAIN?description=e2e-btc-usdt&transferRef=${btc_ref}-usdt")" >/dev/null

  local btc_ask='{"uuid":null,"pair":"BTC_USDT","price":20000,"quantity":0.001,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local btc_bid='{"uuid":null,"pair":"BTC_USDT","price":20000,"quantity":0.001,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "btc-usdt ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$btc_ask' '$btc_seller'" >/tmp/opex-e2e-btc-usdt-ask.json
  wait_user_open_order "$btc_seller" "BTC_USDT" "20000" "0.001" /tmp/opex-e2e-btc-usdt-open-orders.json
  wait_order_book_level "BTC_USDT" "ASK" "20000" "0.001"
  expect_2xx_retry "btc-usdt bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$btc_bid' '$btc_buyer'" >/tmp/opex-e2e-btc-usdt-bid.json
  wait_no_user_open_orders "$btc_seller" "BTC_USDT"
  wait_user_trade_projection "$btc_seller" "BTC_USDT" "20000" "0.001" "20" "0.2" "USDT" false true false /tmp/opex-e2e-btc-usdt-seller-trades.json
  wait_user_trade_projection "$btc_buyer" "BTC_USDT" "20000" "0.001" "20" "0.00001" "BTC" true false false /tmp/opex-e2e-btc-usdt-buyer-trades.json
  wait_recent_trade_level "BTC_USDT" "20000" "0.001" /tmp/opex-e2e-btc-usdt-recent-trades.json
  wait_order_book_empty "BTC_USDT" "ASK"
  wait_order_book_empty "BTC_USDT" "BID"

  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$btc_seller" "BTC" "0.009" &&
    try_wallet_balance "$btc_seller" "USDT" "19.8" &&
    try_wallet_balance "$btc_buyer" "BTC" "0.00099" &&
    try_wallet_balance "$btc_buyer" "USDT" "30"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for BTC_USDT wallet settlement" >&2
      assert_wallet_balance "btc-usdt seller BTC remainder" "$btc_seller" "BTC" "0.009" >&2 || true
      assert_wallet_balance "btc-usdt seller USDT proceeds" "$btc_seller" "USDT" "19.8" >&2 || true
      assert_wallet_balance "btc-usdt buyer BTC received" "$btc_buyer" "BTC" "0.00099" >&2 || true
      assert_wallet_balance "btc-usdt buyer USDT remainder" "$btc_buyer" "USDT" "30" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "market BTC_USDT persisted trade count" "postgres-market" "1" "
    select count(*) from trades
    where symbol = 'BTC_USDT'
      and maker_uuid like 'e2e-btc-%'
      and taker_uuid like 'e2e-btc-%';
  "
  wait_order_book_empty "ETH_USDT" "ASK"
  wait_order_book_empty "ETH_USDT" "BID"

  local sol_seller="e2e-sol-seller-$(date +%s)"
  local sol_buyer="e2e-sol-buyer-$(date +%s)"
  local sol_ref="e2e-sol-$(date +%s)"
  expect_2xx "sol-usdt seller SOL deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/1_test-bsc_SOL/${sol_seller}_MAIN?description=e2e-sol-usdt&transferRef=${sol_ref}-sol")" >/dev/null
  expect_2xx "sol-usdt buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/20_test-ethereum_USDT/${sol_buyer}_MAIN?description=e2e-sol-usdt&transferRef=${sol_ref}-usdt")" >/dev/null

  local sol_ask='{"uuid":null,"pair":"SOL_USDT","price":10,"quantity":1,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local sol_bid='{"uuid":null,"pair":"SOL_USDT","price":10,"quantity":1,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "sol-usdt ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$sol_ask' '$sol_seller'" >/tmp/opex-e2e-sol-usdt-ask.json
  wait_user_open_order "$sol_seller" "SOL_USDT" "10" "1" /tmp/opex-e2e-sol-usdt-open-orders.json
  wait_order_book_level "SOL_USDT" "ASK" "10" "1"
  expect_2xx_retry "sol-usdt bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$sol_bid' '$sol_buyer'" >/tmp/opex-e2e-sol-usdt-bid.json
  wait_no_user_open_orders "$sol_seller" "SOL_USDT"
  wait_user_trade_projection "$sol_seller" "SOL_USDT" "10" "1" "10" "0.1" "USDT" false true false /tmp/opex-e2e-sol-usdt-seller-trades.json
  wait_user_trade_projection "$sol_buyer" "SOL_USDT" "10" "1" "10" "0.01" "SOL" true false false /tmp/opex-e2e-sol-usdt-buyer-trades.json
  wait_recent_trade_level "SOL_USDT" "10" "1" /tmp/opex-e2e-sol-usdt-recent-trades.json
  wait_order_book_empty "SOL_USDT" "ASK"
  wait_order_book_empty "SOL_USDT" "BID"

  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$sol_seller" "SOL" "0" &&
    try_wallet_balance "$sol_seller" "USDT" "9.9" &&
    try_wallet_balance "$sol_buyer" "SOL" "0.99" &&
    try_wallet_balance "$sol_buyer" "USDT" "10"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for SOL_USDT wallet settlement" >&2
      assert_wallet_balance "sol-usdt seller SOL fully sold" "$sol_seller" "SOL" "0" >&2 || true
      assert_wallet_balance "sol-usdt seller USDT proceeds" "$sol_seller" "USDT" "9.9" >&2 || true
      assert_wallet_balance "sol-usdt buyer SOL received" "$sol_buyer" "SOL" "0.99" >&2 || true
      assert_wallet_balance "sol-usdt buyer USDT remainder" "$sol_buyer" "USDT" "10" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine matching-engine-duo accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "market SOL_USDT persisted trade count" "postgres-market" "1" "
    select count(*) from trades
    where symbol = 'SOL_USDT'
      and maker_uuid = '$sol_seller'
      and taker_uuid = '$sol_buyer';
  "
  wait_query_eq "SOL_USDT secondary engine wallet balances by type" "postgres-wallet" $'SOL,EXCHANGE,0.00000000\nSOL,MAIN,0.99000000\nUSDT,EXCHANGE,0.00000000\nUSDT,MAIN,19.90000000' "
    select w.currency, w.wallet_type, to_char(sum(w.balance), 'FM9999999990.00000000')
    from wallet w
    join wallet_owner wo on wo.id = w.owner
    where wo.uuid in ('$sol_seller', '$sol_buyer')
    group by w.currency, w.wallet_type
    order by w.currency, w.wallet_type;
  "

  local doge_seller="e2e-doge-seller-$(date +%s)"
  local doge_buyer="e2e-doge-buyer-$(date +%s)"
  local doge_ref="e2e-doge-$(date +%s)"
  expect_2xx "doge-usdt seller DOGE deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/10_test-dogecoin_DOGE/${doge_seller}_MAIN?description=e2e-doge-usdt&transferRef=${doge_ref}-doge")" >/dev/null
  expect_2xx "doge-usdt buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/20_test-ethereum_USDT/${doge_buyer}_MAIN?description=e2e-doge-usdt&transferRef=${doge_ref}-usdt")" >/dev/null

  local doge_ask='{"uuid":null,"pair":"DOGE_USDT","price":1,"quantity":10,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local doge_bid='{"uuid":null,"pair":"DOGE_USDT","price":1,"quantity":10,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "doge-usdt ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$doge_ask' '$doge_seller'" >/tmp/opex-e2e-doge-usdt-ask.json
  wait_user_open_order "$doge_seller" "DOGE_USDT" "1" "10" /tmp/opex-e2e-doge-usdt-open-orders.json
  wait_order_book_level "DOGE_USDT" "ASK" "1" "10"
  expect_2xx_retry "doge-usdt bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$doge_bid' '$doge_buyer'" >/tmp/opex-e2e-doge-usdt-bid.json
  wait_no_user_open_orders "$doge_seller" "DOGE_USDT"
  wait_user_trade_projection "$doge_seller" "DOGE_USDT" "1" "10" "10" "0.1" "USDT" false true false /tmp/opex-e2e-doge-usdt-seller-trades.json
  wait_user_trade_projection "$doge_buyer" "DOGE_USDT" "1" "10" "10" "0.1" "DOGE" true false false /tmp/opex-e2e-doge-usdt-buyer-trades.json
  wait_recent_trade_level "DOGE_USDT" "1" "10" /tmp/opex-e2e-doge-usdt-recent-trades.json
  wait_order_book_empty "DOGE_USDT" "ASK"
  wait_order_book_empty "DOGE_USDT" "BID"

  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$doge_seller" "DOGE" "0" &&
    try_wallet_balance "$doge_seller" "USDT" "9.9" &&
    try_wallet_balance "$doge_buyer" "DOGE" "9.9" &&
    try_wallet_balance "$doge_buyer" "USDT" "10"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for DOGE_USDT wallet settlement" >&2
      assert_wallet_balance "doge-usdt seller DOGE fully sold" "$doge_seller" "DOGE" "0" >&2 || true
      assert_wallet_balance "doge-usdt seller USDT proceeds" "$doge_seller" "USDT" "9.9" >&2 || true
      assert_wallet_balance "doge-usdt buyer DOGE received" "$doge_buyer" "DOGE" "9.9" >&2 || true
      assert_wallet_balance "doge-usdt buyer USDT remainder" "$doge_buyer" "USDT" "10" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine matching-engine-duo accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "market DOGE_USDT persisted trade count" "postgres-market" "1" "
    select count(*) from trades
    where symbol = 'DOGE_USDT'
      and maker_uuid = '$doge_seller'
      and taker_uuid = '$doge_buyer';
  "
  wait_query_eq "DOGE_USDT secondary engine wallet balances by type" "postgres-wallet" $'DOGE,EXCHANGE,0.00000000\nDOGE,MAIN,9.90000000\nUSDT,EXCHANGE,0.00000000\nUSDT,MAIN,19.90000000' "
    select w.currency, w.wallet_type, to_char(sum(w.balance), 'FM9999999990.00000000')
    from wallet w
    join wallet_owner wo on wo.id = w.owner
    where wo.uuid in ('$doge_seller', '$doge_buyer')
    group by w.currency, w.wallet_type
    order by w.currency, w.wallet_type;
  "

  local ton_seller="e2e-ton-seller-$(date +%s)"
  local ton_buyer="e2e-ton-buyer-$(date +%s)"
  local ton_ref="e2e-ton-$(date +%s)"
  expect_2xx "ton-usdt seller TON deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/2_test-ethereum_TON/${ton_seller}_MAIN?description=e2e-ton-usdt&transferRef=${ton_ref}-ton")" >/dev/null
  expect_2xx "ton-usdt buyer USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/20_test-ethereum_USDT/${ton_buyer}_MAIN?description=e2e-ton-usdt&transferRef=${ton_ref}-usdt")" >/dev/null

  local ton_ask='{"uuid":null,"pair":"TON_USDT","price":5,"quantity":2,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local ton_bid='{"uuid":null,"pair":"TON_USDT","price":5,"quantity":2,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "ton-usdt ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$ton_ask' '$ton_seller'" >/tmp/opex-e2e-ton-usdt-ask.json
  wait_user_open_order "$ton_seller" "TON_USDT" "5" "2" /tmp/opex-e2e-ton-usdt-open-orders.json
  wait_order_book_level "TON_USDT" "ASK" "5" "2"
  expect_2xx_retry "ton-usdt bid order" "curl_json POST 'http://127.0.0.1:8093/order' '$ton_bid' '$ton_buyer'" >/tmp/opex-e2e-ton-usdt-bid.json
  wait_no_user_open_orders "$ton_seller" "TON_USDT"
  wait_user_trade_projection "$ton_seller" "TON_USDT" "5" "2" "10" "0.1" "USDT" false true false /tmp/opex-e2e-ton-usdt-seller-trades.json
  wait_user_trade_projection "$ton_buyer" "TON_USDT" "5" "2" "10" "0.02" "TON" true false false /tmp/opex-e2e-ton-usdt-buyer-trades.json
  wait_recent_trade_level "TON_USDT" "5" "2" /tmp/opex-e2e-ton-usdt-recent-trades.json
  wait_order_book_empty "TON_USDT" "ASK"
  wait_order_book_empty "TON_USDT" "BID"

  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$ton_seller" "TON" "0" &&
    try_wallet_balance "$ton_seller" "USDT" "9.9" &&
    try_wallet_balance "$ton_buyer" "TON" "1.98" &&
    try_wallet_balance "$ton_buyer" "USDT" "10"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for TON_USDT wallet settlement" >&2
      assert_wallet_balance "ton-usdt seller TON fully sold" "$ton_seller" "TON" "0" >&2 || true
      assert_wallet_balance "ton-usdt seller USDT proceeds" "$ton_seller" "USDT" "9.9" >&2 || true
      assert_wallet_balance "ton-usdt buyer TON received" "$ton_buyer" "TON" "1.98" >&2 || true
      assert_wallet_balance "ton-usdt buyer USDT remainder" "$ton_buyer" "USDT" "10" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine matching-engine-duo accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "market TON_USDT persisted trade count" "postgres-market" "1" "
    select count(*) from trades
    where symbol = 'TON_USDT'
      and maker_uuid = '$ton_seller'
      and taker_uuid = '$ton_buyer';
  "
  wait_query_eq "TON_USDT secondary engine wallet balances by type" "postgres-wallet" $'TON,EXCHANGE,0.00000000\nTON,MAIN,1.98000000\nUSDT,EXCHANGE,0.00000000\nUSDT,MAIN,19.90000000' "
    select w.currency, w.wallet_type, to_char(sum(w.balance), 'FM9999999990.00000000')
    from wallet w
    join wallet_owner wo on wo.id = w.owner
    where wo.uuid in ('$ton_seller', '$ton_buyer')
    group by w.currency, w.wallet_type
    order by w.currency, w.wallet_type;
  "

  local concurrent_seller="e2e-concurrent-seller-$(date +%s)"
  local concurrent_buyer_one="e2e-concurrent-buyer-1-$(date +%s)"
  local concurrent_buyer_two="e2e-concurrent-buyer-2-$(date +%s)"
  local concurrent_buyer_three="e2e-concurrent-buyer-3-$(date +%s)"
  local concurrent_ref="e2e-concurrent-$(date +%s)"
  expect_2xx "concurrent seller BTC deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/0.003_test-bitcoin_BTC/${concurrent_seller}_MAIN?description=e2e-concurrent&transferRef=${concurrent_ref}-btc")" >/dev/null
  expect_2xx "concurrent buyer one USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${concurrent_buyer_one}_MAIN?description=e2e-concurrent&transferRef=${concurrent_ref}-buyer-1-usdt")" >/dev/null
  expect_2xx "concurrent buyer two USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${concurrent_buyer_two}_MAIN?description=e2e-concurrent&transferRef=${concurrent_ref}-buyer-2-usdt")" >/dev/null
  expect_2xx "concurrent buyer three USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${concurrent_buyer_three}_MAIN?description=e2e-concurrent&transferRef=${concurrent_ref}-buyer-3-usdt")" >/dev/null

  local concurrent_ask='{"uuid":null,"pair":"BTC_USDT","price":21000,"quantity":0.003,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local concurrent_bid='{"uuid":null,"pair":"BTC_USDT","price":21000,"quantity":0.001,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "concurrent resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$concurrent_ask' '$concurrent_seller'" >/tmp/opex-e2e-concurrent-ask.json
  wait_user_open_order "$concurrent_seller" "BTC_USDT" "21000" "0.003" /tmp/opex-e2e-concurrent-open-orders.json
  wait_order_book_level "BTC_USDT" "ASK" "21000" "0.003"
  wait_wallet_type_balance "concurrent seller MAIN BTC fully reserved" "$concurrent_seller" "MAIN" "BTC" "0.00000000"
  wait_wallet_type_balance "concurrent seller EXCHANGE BTC reservation" "$concurrent_seller" "EXCHANGE" "BTC" "0.00300000"

  expect_2xx_retry "concurrent buyer one bid" "curl_json POST 'http://127.0.0.1:8093/order' '$concurrent_bid' '$concurrent_buyer_one'" >/tmp/opex-e2e-concurrent-bid-1.json &
  local concurrent_bid_pid_one=$!
  expect_2xx_retry "concurrent buyer two bid" "curl_json POST 'http://127.0.0.1:8093/order' '$concurrent_bid' '$concurrent_buyer_two'" >/tmp/opex-e2e-concurrent-bid-2.json &
  local concurrent_bid_pid_two=$!
  expect_2xx_retry "concurrent buyer three bid" "curl_json POST 'http://127.0.0.1:8093/order' '$concurrent_bid' '$concurrent_buyer_three'" >/tmp/opex-e2e-concurrent-bid-3.json &
  local concurrent_bid_pid_three=$!
  wait "$concurrent_bid_pid_one"
  wait "$concurrent_bid_pid_two"
  wait "$concurrent_bid_pid_three"

  wait_no_user_open_orders "$concurrent_seller" "BTC_USDT"
  wait_user_trade_projection "$concurrent_seller" "BTC_USDT" "21000" "0.001" "21" "0.21" "USDT" false true false /tmp/opex-e2e-concurrent-seller-trades.json
  wait_user_trade_projection "$concurrent_buyer_one" "BTC_USDT" "21000" "0.001" "21" "0.00001" "BTC" true false false /tmp/opex-e2e-concurrent-buyer-one-trades.json
  wait_user_trade_projection "$concurrent_buyer_two" "BTC_USDT" "21000" "0.001" "21" "0.00001" "BTC" true false false /tmp/opex-e2e-concurrent-buyer-two-trades.json
  wait_user_trade_projection "$concurrent_buyer_three" "BTC_USDT" "21000" "0.001" "21" "0.00001" "BTC" true false false /tmp/opex-e2e-concurrent-buyer-three-trades.json
  wait_user_trade_aggregate "$concurrent_seller" "BTC_USDT" "21000" "3" "0.003" "63" "0.63" "USDT" false true false /tmp/opex-e2e-concurrent-seller-aggregate-trades.json
  wait_order_book_empty "BTC_USDT" "ASK"
  wait_order_book_empty "BTC_USDT" "BID"
  wait_wallet_type_balance "concurrent seller EXCHANGE BTC fully settled" "$concurrent_seller" "EXCHANGE" "BTC" "0.00000000"
  wait_wallet_type_balance "concurrent buyer one EXCHANGE USDT fully settled" "$concurrent_buyer_one" "EXCHANGE" "USDT" "0.00000000"
  wait_wallet_type_balance "concurrent buyer two EXCHANGE USDT fully settled" "$concurrent_buyer_two" "EXCHANGE" "USDT" "0.00000000"
  wait_wallet_type_balance "concurrent buyer three EXCHANGE USDT fully settled" "$concurrent_buyer_three" "EXCHANGE" "USDT" "0.00000000"

  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$concurrent_seller" "BTC" "0" &&
    try_wallet_balance "$concurrent_seller" "USDT" "62.37" &&
    try_wallet_balance "$concurrent_buyer_one" "BTC" "0.00099" &&
    try_wallet_balance "$concurrent_buyer_one" "USDT" "9" &&
    try_wallet_balance "$concurrent_buyer_two" "BTC" "0.00099" &&
    try_wallet_balance "$concurrent_buyer_two" "USDT" "9" &&
    try_wallet_balance "$concurrent_buyer_three" "BTC" "0.00099" &&
    try_wallet_balance "$concurrent_buyer_three" "USDT" "9"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for concurrent BTC_USDT wallet settlement" >&2
      assert_wallet_balance "concurrent seller BTC fully sold" "$concurrent_seller" "BTC" "0" >&2 || true
      assert_wallet_balance "concurrent seller USDT proceeds" "$concurrent_seller" "USDT" "62.37" >&2 || true
      assert_wallet_balance "concurrent buyer one BTC received" "$concurrent_buyer_one" "BTC" "0.00099" >&2 || true
      assert_wallet_balance "concurrent buyer one USDT remainder" "$concurrent_buyer_one" "USDT" "9" >&2 || true
      assert_wallet_balance "concurrent buyer two BTC received" "$concurrent_buyer_two" "BTC" "0.00099" >&2 || true
      assert_wallet_balance "concurrent buyer two USDT remainder" "$concurrent_buyer_two" "USDT" "9" >&2 || true
      assert_wallet_balance "concurrent buyer three BTC received" "$concurrent_buyer_three" "BTC" "0.00099" >&2 || true
      assert_wallet_balance "concurrent buyer three USDT remainder" "$concurrent_buyer_three" "USDT" "9" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done
  wait_query_eq "concurrent BTC_USDT persisted trade count" "postgres-market" "3" "
    select count(*) from trades
    where symbol = 'BTC_USDT'
      and maker_uuid = '$concurrent_seller'
      and taker_uuid like 'e2e-concurrent-buyer-%';
  "
  wait_query_eq "concurrent BTC_USDT total matched quantity" "postgres-market" "0.00300000" "
    select to_char(sum(matched_quantity), 'FM9999999990.00000000')
    from trades
    where symbol = 'BTC_USDT'
      and maker_uuid = '$concurrent_seller'
      and taker_uuid like 'e2e-concurrent-buyer-%';
  "
  wait_query_eq "market BTC_USDT total persisted trade count" "postgres-market" "4" "
    select count(*) from trades
    where symbol = 'BTC_USDT'
      and (maker_uuid like 'e2e-btc-%' or maker_uuid like 'e2e-concurrent-%')
      and (taker_uuid like 'e2e-btc-%' or taker_uuid like 'e2e-concurrent-buyer-%');
  "

  local overfill_seller="e2e-overfill-seller-$(date +%s)"
  local overfill_buyer_one="e2e-overfill-buyer-1-$(date +%s)"
  local overfill_buyer_two="e2e-overfill-buyer-2-$(date +%s)"
  local overfill_buyer_three="e2e-overfill-buyer-3-$(date +%s)"
  local overfill_ref="e2e-overfill-$(date +%s)"
  expect_2xx "overfill seller BTC deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/0.002_test-bitcoin_BTC/${overfill_seller}_MAIN?description=e2e-overfill&transferRef=${overfill_ref}-btc")" >/dev/null
  expect_2xx "overfill buyer one USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${overfill_buyer_one}_MAIN?description=e2e-overfill&transferRef=${overfill_ref}-buyer-1-usdt")" >/dev/null
  expect_2xx "overfill buyer two USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${overfill_buyer_two}_MAIN?description=e2e-overfill&transferRef=${overfill_ref}-buyer-2-usdt")" >/dev/null
  expect_2xx "overfill buyer three USDT deposit" "$(curl_json POST "http://127.0.0.1:8091/deposit/30_test-ethereum_USDT/${overfill_buyer_three}_MAIN?description=e2e-overfill&transferRef=${overfill_ref}-buyer-3-usdt")" >/dev/null

  local overfill_ask='{"uuid":null,"pair":"BTC_USDT","price":22000,"quantity":0.002,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  local overfill_bid='{"uuid":null,"pair":"BTC_USDT","price":22000,"quantity":0.001,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}'
  expect_2xx_retry "overfill resting ask order" "curl_json POST 'http://127.0.0.1:8093/order' '$overfill_ask' '$overfill_seller'" >/tmp/opex-e2e-overfill-ask.json
  wait_user_open_order "$overfill_seller" "BTC_USDT" "22000" "0.002" /tmp/opex-e2e-overfill-open-orders.json
  wait_order_book_level "BTC_USDT" "ASK" "22000" "0.002"
  wait_wallet_type_balance "overfill seller MAIN BTC fully reserved" "$overfill_seller" "MAIN" "BTC" "0.00000000"
  wait_wallet_type_balance "overfill seller EXCHANGE BTC reservation" "$overfill_seller" "EXCHANGE" "BTC" "0.00200000"

  expect_2xx_retry "overfill buyer one bid" "curl_json POST 'http://127.0.0.1:8093/order' '$overfill_bid' '$overfill_buyer_one'" >/tmp/opex-e2e-overfill-bid-1.json &
  local overfill_bid_pid_one=$!
  expect_2xx_retry "overfill buyer two bid" "curl_json POST 'http://127.0.0.1:8093/order' '$overfill_bid' '$overfill_buyer_two'" >/tmp/opex-e2e-overfill-bid-2.json &
  local overfill_bid_pid_two=$!
  expect_2xx_retry "overfill buyer three bid" "curl_json POST 'http://127.0.0.1:8093/order' '$overfill_bid' '$overfill_buyer_three'" >/tmp/opex-e2e-overfill-bid-3.json &
  local overfill_bid_pid_three=$!
  wait "$overfill_bid_pid_one"
  wait "$overfill_bid_pid_two"
  wait "$overfill_bid_pid_three"

  wait_no_user_open_orders "$overfill_seller" "BTC_USDT"
  wait_order_book_empty "BTC_USDT" "ASK"
  wait_order_book_level "BTC_USDT" "BID" "22000" "0.001"
  wait_user_trade_projection "$overfill_seller" "BTC_USDT" "22000" "0.001" "22" "0.22" "USDT" false true false /tmp/opex-e2e-overfill-seller-trades.json
  wait_user_trade_aggregate "$overfill_seller" "BTC_USDT" "22000" "2" "0.002" "44" "0.44" "USDT" false true false /tmp/opex-e2e-overfill-seller-aggregate-trades.json
  wait_query_eq "overfill BTC_USDT persisted trade count" "postgres-market" "2" "
    select count(*) from trades
    where symbol = 'BTC_USDT'
      and maker_uuid = '$overfill_seller'
      and taker_uuid like 'e2e-overfill-buyer-%';
  "
  wait_query_eq "overfill BTC_USDT total matched quantity" "postgres-market" "0.00200000" "
    select to_char(sum(matched_quantity), 'FM9999999990.00000000')
    from trades
    where symbol = 'BTC_USDT'
      and maker_uuid = '$overfill_seller'
      and taker_uuid like 'e2e-overfill-buyer-%';
  "

  local overfill_open_owner="" overfill_open_ouid="" overfill_open_order_id=""
  local overfill_filled_buyers=()
  for overfill_buyer in "$overfill_buyer_one" "$overfill_buyer_two" "$overfill_buyer_three"; do
    curl -fsS "http://127.0.0.1:8096/v1/user/${overfill_buyer}/orders/BTC_USDT/open?limit=20" >"/tmp/opex-e2e-overfill-${overfill_buyer}-open-orders.json"
    if jq -e --argjson price 22000 --argjson quantity 0.001 '[.[] | select(.price == $price and .quantity == $quantity and (.status == "NEW" or .status == "PARTIALLY_FILLED"))] | length == 1' "/tmp/opex-e2e-overfill-${overfill_buyer}-open-orders.json" >/dev/null; then
      if [[ -n "$overfill_open_owner" ]]; then
        echo "Expected exactly one residual overfill bid, found at least two: $overfill_open_owner and $overfill_buyer" >&2
        exit 1
      fi
      overfill_open_owner="$overfill_buyer"
      overfill_open_ouid="$(jq -r '.[0].ouid' "/tmp/opex-e2e-overfill-${overfill_buyer}-open-orders.json")"
      overfill_open_order_id="$(jq -r '.[0].orderId' "/tmp/opex-e2e-overfill-${overfill_buyer}-open-orders.json")"
    else
      overfill_filled_buyers+=("$overfill_buyer")
    fi
  done
  if [[ -z "$overfill_open_owner" || "${#overfill_filled_buyers[@]}" -ne 2 ]]; then
    echo "Expected two filled overfill buyers and one residual open bid" >&2
    echo "openOwner=$overfill_open_owner filledCount=${#overfill_filled_buyers[@]}" >&2
    exit 1
  fi
  wait_wallet_type_balance "overfill seller EXCHANGE BTC fully settled" "$overfill_seller" "EXCHANGE" "BTC" "0.00000000"
  wait_wallet_type_balance "overfill residual buyer MAIN USDT reserved" "$overfill_open_owner" "MAIN" "USDT" "8.00000000"
  wait_wallet_type_balance "overfill residual buyer EXCHANGE USDT reservation" "$overfill_open_owner" "EXCHANGE" "USDT" "22.00000000"
  for overfill_filled_buyer in "${overfill_filled_buyers[@]}"; do
    wait_user_trade_projection "$overfill_filled_buyer" "BTC_USDT" "22000" "0.001" "22" "0.00001" "BTC" true false false "/tmp/opex-e2e-overfill-${overfill_filled_buyer}-trades.json"
    wait_wallet_type_balance "overfill filled buyer EXCHANGE USDT fully settled" "$overfill_filled_buyer" "EXCHANGE" "USDT" "0.00000000"
  done
  local overfill_cancel_request
  overfill_cancel_request="$(jq -nc --arg ouid "$overfill_open_ouid" --arg uuid "$overfill_open_owner" --argjson orderId "$overfill_open_order_id" '{ouid:$ouid, uuid:$uuid, orderId:$orderId, symbol:"BTC_USDT"}')"
  expect_2xx_retry "cancel overfill residual bid" "curl_json POST 'http://127.0.0.1:8093/order/cancel' '$overfill_cancel_request' '$overfill_open_owner'" >/tmp/opex-e2e-overfill-cancel.json
  wait_no_user_open_orders "$overfill_open_owner" "BTC_USDT"
  wait_order_book_empty "BTC_USDT" "BID"
  wait_wallet_type_balance "overfill residual buyer EXCHANGE USDT released" "$overfill_open_owner" "EXCHANGE" "USDT" "0.00000000"

  deadline=$((SECONDS + EVENTUAL_TIMEOUT))
  until try_wallet_balance "$overfill_seller" "BTC" "0" &&
    try_wallet_balance "$overfill_seller" "USDT" "43.56" &&
    try_wallet_balance "${overfill_filled_buyers[0]}" "BTC" "0.00099" &&
    try_wallet_balance "${overfill_filled_buyers[0]}" "USDT" "8" &&
    try_wallet_balance "${overfill_filled_buyers[1]}" "BTC" "0.00099" &&
    try_wallet_balance "${overfill_filled_buyers[1]}" "USDT" "8" &&
    try_wallet_balance "$overfill_open_owner" "USDT" "30"; do
    if (( SECONDS > deadline )); then
      echo "Timed out waiting for overfill BTC_USDT wallet settlement" >&2
      assert_wallet_balance "overfill seller BTC fully sold" "$overfill_seller" "BTC" "0" >&2 || true
      assert_wallet_balance "overfill seller USDT proceeds" "$overfill_seller" "USDT" "43.56" >&2 || true
      assert_wallet_balance "overfill filled buyer 1 BTC received" "${overfill_filled_buyers[0]}" "BTC" "0.00099" >&2 || true
      assert_wallet_balance "overfill filled buyer 1 USDT remainder" "${overfill_filled_buyers[0]}" "USDT" "8" >&2 || true
      assert_wallet_balance "overfill filled buyer 2 BTC received" "${overfill_filled_buyers[1]}" "BTC" "0.00099" >&2 || true
      assert_wallet_balance "overfill filled buyer 2 USDT remainder" "${overfill_filled_buyers[1]}" "USDT" "8" >&2 || true
      assert_wallet_balance "overfill residual buyer USDT released" "$overfill_open_owner" "USDT" "30" >&2 || true
      "${COMPOSE[@]}" logs --tail=200 matching-gateway matching-engine accountant market wallet >&2 || true
      exit 1
    fi
    sleep 2
  done

  wait_query_eq "BTC_USDT scenario wallet transaction ledger" "postgres-wallet" $'DEPOSIT,10\nFEE,12\nORDER_CANCEL,1\nORDER_CREATE,10\nTRADE,12' "
    select t.transfer_category, count(*)
    from transaction t
    join wallet sw on sw.id = t.source_wallet
    join wallet_owner swo on swo.id = sw.owner
    join wallet dw on dw.id = t.dest_wallet
    join wallet_owner dwo on dwo.id = dw.owner
    where swo.uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
       or dwo.uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
    group by t.transfer_category
    order by t.transfer_category;
  "
  wait_query_eq "BTC_USDT scenario wallet balances by type" "postgres-wallet" $'BTC,EXCHANGE,0.00000000\nBTC,MAIN,0.01494000\nUSDT,EXCHANGE,0.00000000\nUSDT,MAIN,228.73000000' "
    select w.currency, w.wallet_type, to_char(sum(w.balance), 'FM9999999990.00000000')
    from wallet w
    join wallet_owner wo on wo.id = w.owner
    where wo.uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
    group by w.currency, w.wallet_type
    order by w.currency, w.wallet_type;
  "
  wait_query_eq "BTC_USDT scenario accountant actions" "postgres-accountant" $'CancelOrderEvent,PROCESSED,1\nSubmitOrderEvent,PROCESSED,10\nTradeEvent,PROCESSED,24' "
    select event_type, status, count(*)
    from fi_actions
    where sender in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
       or receiver in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
    group by event_type, status
    order by event_type, status;
  "
  wait_query_eq "BTC_USDT scenario accountant order status ledger" "postgres-accountant" $'CANCELED,1\nFILLED,9' "
    select case status when 2 then 'CANCELED' when 5 then 'FILLED' else status::text end as status_name, count(*)
    from orders
    where uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
    group by status_name
    order by status_name;
  "
  wait_query_eq "BTC_USDT scenario accountant order quantity ledger" "postgres-accountant" "10,0.01300000,0.01200000,22.00000000" "
    select count(*), to_char(sum(orig_quantity), 'FM9999999990.00000000'), to_char(sum(filled_orig_quantity), 'FM9999999990.00000000'), to_char(sum(remained_transfer_amount), 'FM9999999990.00000000')
    from orders
    where uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three');
  "
  wait_query_eq "BTC_USDT scenario accountant orders terminal and bounded" "postgres-accountant" "0" "
    select count(*)
    from orders
    where uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
      and (
        status not in (2, 5)
        or matching_engine_id is null
        or filled_orig_quantity > orig_quantity
        or filled_orig_quantity < 0
        or (status = 5 and remained_transfer_amount <> 0)
        or (status = 2 and filled_orig_quantity <> 0)
      );
  "
  wait_query_eq "BTC_USDT scenario market trades" "postgres-market" "BTC_USDT,6,0.00600000" "
    select symbol, count(*), to_char(sum(matched_quantity), 'FM9999999990.00000000')
    from trades
    where maker_uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
       or taker_uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
    group by symbol
    order by symbol;
  "
  wait_query_eq "BTC_USDT scenario eventlog order requests" "postgres-eventlog" "10" "
    select count(*)
    from opex_orders
    where uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three');
  "
  wait_query_eq "BTC_USDT scenario eventlog order events" "postgres-eventlog" $'CancelOrderEvent,1\nCreateOrderEvent,10\nSubmitOrderEvent,10' "
    select event, count(*)
    from opex_order_events
    where uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
    group by event
    order by event;
  "
  wait_query_eq "BTC_USDT scenario eventlog raw audit events" "postgres-eventlog" $'CancelOrderEvent,1,0\nCreateOrderEvent,10,0\nTradeEvent,12,0' "
    select event,
           count(*),
           sum(
             case
               when event_json is null or event_json = '' then 1
               when event_json::jsonb is null then 1
               else 0
             end
           )
    from opex_events
    where uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
    group by event
    order by event;
  "
  wait_query_eq "BTC_USDT scenario eventlog trades" "postgres-eventlog" "BTC_USDT,6,6000" "
    select symbol, count(*), sum(matched_quantity)
    from opex_trades
    where maker_uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
       or taker_uuid in ('$btc_seller', '$btc_buyer', '$concurrent_seller', '$concurrent_buyer_one', '$concurrent_buyer_two', '$concurrent_buyer_three', '$overfill_seller', '$overfill_buyer_one', '$overfill_buyer_two', '$overfill_buyer_three')
    group by symbol
    order by symbol;
  "
  wait_query_eq "all e2e market trade projections internally consistent after BTC" "postgres-market" "0" "
    select count(*)
    from trades
    where (maker_uuid like 'e2e-%' or taker_uuid like 'e2e-%')
      and (
        maker_uuid = taker_uuid
        or maker_ouid = taker_ouid
        or maker_uuid = ''
        or taker_uuid = ''
        or maker_ouid = ''
        or taker_ouid = ''
        or base_asset <> split_part(symbol, '_', 1)
        or quote_asset <> split_part(symbol, '_', 2)
        or matched_price <> maker_price
        or coalesce(maker_commission, 0) < 0
        or coalesce(taker_commission, 0) < 0
        or coalesce(maker_commission_asset, '') not in (base_asset, quote_asset)
        or coalesce(taker_commission_asset, '') not in (base_asset, quote_asset)
      );
  "
  wait_market_order_status_matches_trade_totals "all e2e market order status matches trade totals after BTC"
  wait_accountant_market_order_definitions_match "all e2e accountant and market order definitions match after BTC"
  wait_accountant_order_actions_match_eventlog_lifecycle "all e2e accountant order actions match eventlog lifecycle after BTC"
  wait_accountant_market_order_projections_match "all e2e accountant and market order projections match after BTC"
  wait_accountant_trade_transfers_match_market_executions "all e2e accountant trade transfers match market executions after BTC"
  wait_accountant_trade_fees_match_market_commissions "all e2e accountant trade fee actions match market commissions after BTC"
  wait_wallet_fee_transactions_match_accountant_actions "all e2e wallet fee transactions match accountant fee actions after BTC"
  wait_wallet_transactions_match_accountant_actions "all e2e wallet transactions match accountant financial actions after BTC"
  wait_wallet_user_transactions_match_wallet_transactions "all e2e wallet user transactions match wallet movements after BTC"
  wait_market_count_apis_match_database "market count APIs stay fresh after BTC/concurrency scenarios"
  wait_order_book_empty "BTC_USDT" "ASK"
  wait_order_book_empty "BTC_USDT" "BID"

  echo "E2E exchange flow passed"
  echo "seller=$seller buyer=$buyer engineRestartSeller=$engine_restart_seller engineRestartBuyer=$engine_restart_buyer walletRestartSeller=$wallet_restart_seller walletRestartBuyer=$wallet_restart_buyer accountantRestartSeller=$accountant_restart_seller accountantRestartBuyer=$accountant_restart_buyer gatewayRestartSeller=$gateway_restart_seller gatewayRestartBuyer=$gateway_restart_buyer coreRestartSeller=$core_restart_seller coreRestartBuyer=$core_restart_buyer kafkaRestartSeller=$kafka_restart_seller kafkaRestartBuyer=$kafka_restart_buyer postgresRestartSeller=$postgres_restart_seller postgresRestartBuyer=$postgres_restart_buyer cancelOwner=$cancel_owner apiGeneratedClientOwner=$api_generated_client_owner partialSeller=$partial_seller partialBuyer=$partial_buyer iocOwner=$ioc_owner marketSeller=$market_seller marketBuyer=$market_buyer sweepSeller=$sweep_seller sweepHighBuyer=$sweep_high_buyer sweepLowBuyer=$sweep_low_buyer bidSweepBuyer=$bid_sweep_buyer bidSweepLowSeller=$bid_sweep_low_seller bidSweepHighSeller=$bid_sweep_high_seller prioritySeller=$priority_seller priorityHighBuyer=$priority_high_buyer priorityLowBuyer=$priority_low_buyer fifoSeller=$fifo_seller fifoFirstBuyer=$fifo_first_buyer fifoSecondBuyer=$fifo_second_buyer overreserveOwner=$overreserve_owner bidOverreserveOwner=$bid_overreserve_owner cancelAuthOwner=$cancel_auth_owner cancelAuthIntruder=$cancel_auth_intruder malformedEditOwner=$malformed_edit_owner fokOwner=$fok_owner selfTradeOwner=$self_trade_owner layeredSelfTradeOwner=$layered_self_trade_owner layeredExternalSeller=$layered_external_seller editBidOwner=$edit_bid_owner rejectOwner=$reject_owner bidRejectOwner=$bid_reject_owner invalidOwner=$invalid_owner duplicateDepositOwner=$duplicate_deposit_owner transferSender=$transfer_sender transferReceiver=$transfer_receiver withdrawOwner=$withdraw_owner btcSeller=$btc_seller btcBuyer=$btc_buyer solSeller=$sol_seller solBuyer=$sol_buyer dogeSeller=$doge_seller dogeBuyer=$doge_buyer tonSeller=$ton_seller tonBuyer=$ton_buyer concurrentSeller=$concurrent_seller concurrentBuyerOne=$concurrent_buyer_one concurrentBuyerTwo=$concurrent_buyer_two concurrentBuyerThree=$concurrent_buyer_three overfillSeller=$overfill_seller overfillResidualBuyer=$overfill_open_owner"
  cat > /tmp/opex-e2e-summary.json <<EOF
{
  "status": "passed",
  "seller": "$seller",
  "buyer": "$buyer",
  "engineRestartSeller": "$engine_restart_seller",
  "engineRestartBuyer": "$engine_restart_buyer",
  "walletRestartSeller": "$wallet_restart_seller",
  "walletRestartBuyer": "$wallet_restart_buyer",
  "accountantRestartSeller": "$accountant_restart_seller",
  "accountantRestartBuyer": "$accountant_restart_buyer",
  "gatewayRestartSeller": "$gateway_restart_seller",
  "gatewayRestartBuyer": "$gateway_restart_buyer",
  "coreRestartSeller": "$core_restart_seller",
  "coreRestartBuyer": "$core_restart_buyer",
  "kafkaRestartSeller": "$kafka_restart_seller",
  "kafkaRestartBuyer": "$kafka_restart_buyer",
  "postgresRestartSeller": "$postgres_restart_seller",
  "postgresRestartBuyer": "$postgres_restart_buyer",
  "cancelOwner": "$cancel_owner",
  "apiGeneratedClientOwner": "$api_generated_client_owner",
  "apiGeneratedClientOrderId": "$api_generated_client_id",
  "partialSeller": "$partial_seller",
  "partialBuyer": "$partial_buyer",
  "iocOwner": "$ioc_owner",
  "marketSeller": "$market_seller",
  "marketBuyer": "$market_buyer",
  "sweepSeller": "$sweep_seller",
  "sweepHighBuyer": "$sweep_high_buyer",
  "sweepLowBuyer": "$sweep_low_buyer",
  "bidSweepBuyer": "$bid_sweep_buyer",
  "bidSweepLowSeller": "$bid_sweep_low_seller",
  "bidSweepHighSeller": "$bid_sweep_high_seller",
  "prioritySeller": "$priority_seller",
  "priorityHighBuyer": "$priority_high_buyer",
  "priorityLowBuyer": "$priority_low_buyer",
  "fifoSeller": "$fifo_seller",
  "fifoFirstBuyer": "$fifo_first_buyer",
  "fifoSecondBuyer": "$fifo_second_buyer",
  "overreserveOwner": "$overreserve_owner",
  "bidOverreserveOwner": "$bid_overreserve_owner",
  "cancelAuthOwner": "$cancel_auth_owner",
  "cancelAuthIntruder": "$cancel_auth_intruder",
  "malformedEditOwner": "$malformed_edit_owner",
  "fokOwner": "$fok_owner",
  "selfTradeOwner": "$self_trade_owner",
  "layeredSelfTradeOwner": "$layered_self_trade_owner",
  "layeredExternalSeller": "$layered_external_seller",
  "editBidOwner": "$edit_bid_owner",
  "editCrossSeller": "$edit_cross_seller",
  "editCrossBuyer": "$edit_cross_buyer",
  "rejectOwner": "$reject_owner",
  "bidRejectOwner": "$bid_reject_owner",
  "invalidOwner": "$invalid_owner",
  "duplicateDepositOwner": "$duplicate_deposit_owner",
  "transferSender": "$transfer_sender",
  "transferReceiver": "$transfer_receiver",
  "withdrawOwner": "$withdraw_owner",
  "btcSeller": "$btc_seller",
  "btcBuyer": "$btc_buyer",
  "solSeller": "$sol_seller",
  "solBuyer": "$sol_buyer",
  "dogeSeller": "$doge_seller",
  "dogeBuyer": "$doge_buyer",
  "tonSeller": "$ton_seller",
  "tonBuyer": "$ton_buyer",
  "concurrentSeller": "$concurrent_seller",
  "concurrentBuyerOne": "$concurrent_buyer_one",
  "concurrentBuyerTwo": "$concurrent_buyer_two",
  "concurrentBuyerThree": "$concurrent_buyer_three",
  "overfillSeller": "$overfill_seller",
  "overfillBuyerOne": "$overfill_buyer_one",
  "overfillBuyerTwo": "$overfill_buyer_two",
  "overfillBuyerThree": "$overfill_buyer_three",
  "overfillResidualBuyer": "$overfill_open_owner",
  "apiDuplicateClientOwner": "$api_duplicate_client_owner",
  "pair": "ETH_USDT",
  "price": 100,
  "quantity": 1,
  "binancePriceTicker": {
    "symbol": "ETHUSDT",
    "price": 100
  },
  "binance24hTicker": {
    "symbol": "ETHUSDT",
    "lastPrice": 100,
    "volume": 1,
    "count": 1
  },
  "binanceKlines": {
    "symbol": "ETHUSDT",
    "interval": "1m",
    "open": 100,
    "high": 100,
    "low": 100,
    "close": 100,
    "volume": 1,
    "quoteVolume": 100,
    "trades": 1,
    "takerBuyBaseVolume": 1,
    "takerBuyQuoteVolume": 100
  },
  "binanceLatestKline": {
    "symbol": "ETHUSDT",
    "interval": "1m",
    "matchesMarketProjection": true,
    "endTimeOnlyMatchesMarketProjection": true
  },
  "binanceMyTradesFromId": {
    "symbol": "ETHUSDT",
    "fromId": $seller_binance_trade_id,
    "inclusiveTradeIdVerified": true
  },
  "binanceMyTradesOrderId": {
    "symbol": "ETHUSDT",
    "orderId": $seller_binance_order_id,
    "tradeId": $seller_binance_trade_id,
    "orderTradeFilterVerified": true
  },
  "binanceMyTradesDistinctOrderId": {
    "symbol": "ETHUSDT",
    "orderId": $api_order_seller_binance_order_id,
    "tradeId": $api_order_seller_binance_trade_id,
    "orderIdDiffersFromTradeId": true,
    "orderTradeFilterVerified": true
  },
  "binanceFilledOrderIdCancel": {
    "symbol": "ETHUSDT",
    "owner": "$api_order_seller",
    "orderId": $api_order_seller_binance_order_id,
    "cancelStatus": "HTTP_403",
    "ownerOrderStatusAfterCancelReject": "FILLED",
    "ownerEthAvailableAfterCancelReject": 0.8,
    "ownerUsdtAvailableAfterCancelReject": 19.998
  },
  "binanceMarketSellScenario": {
    "symbol": "ETHUSDT",
    "seller": "$api_market_seller",
    "buyer": "$api_market_buyer",
    "makerOrderId": $api_market_bid_order_id,
    "marketClientOrderId": "$api_market_seller_client_id",
    "makerBidPrice": 102,
    "makerOrigQty": 0.3,
    "executedQty": 0.2,
    "quoteQty": 20.4,
    "sellerUsdtAfterFee": 20.196,
    "buyerEthAfterFee": 0.198,
    "remainingMakerQtyCanceled": 0.1,
    "finalMakerStatus": "CANCELED",
    "marketOrderStatus": "FILLED"
  },
  "binanceMarketBuyScenario": {
    "symbol": "ETHUSDT",
    "seller": "$api_market_buy_seller",
    "buyer": "$api_market_buy_buyer",
    "makerOrderId": $api_market_buy_ask_order_id,
    "marketClientOrderId": "$api_market_buy_client_id",
    "marketBidPriceCap": 103,
    "makerOrigQty": 0.3,
    "executedQty": 0.2,
    "quoteQty": 20.6,
    "sellerUsdtAfterFee": 20.394,
    "buyerEthAfterFee": 0.198,
    "remainingMakerQtyCanceled": 0.1,
    "finalMakerStatus": "CANCELED",
    "marketOrderStatus": "FILLED"
  },
  "binanceMarketBuyWithoutPriceCapRejection": {
    "symbol": "ETHUSDT",
    "buyer": "$api_market_buy_buyer",
    "status": "HTTP_400",
    "buyerUsdtAfterReject": 40,
    "buyerLockedUsdtAfterReject": 0,
    "bookUnchangedAfterReject": true
  },
  "binanceMarketBuyNoLiquidityScenario": {
    "symbol": "ETHUSDT",
    "buyer": "$api_market_buy_buyer",
    "orderId": $api_market_buy_no_liq_order_id,
    "marketClientOrderId": "$api_market_buy_no_liq_client_id",
    "marketBidPriceCap": 103,
    "executedQty": 0,
    "quoteQty": 0,
    "finalStatus": "CANCELED",
    "buyerUsdtAfterRelease": 40,
    "buyerLockedUsdtAfterRelease": 0,
    "tradesVisible": 0
  },
  "binanceMarketSellNoLiquidityScenario": {
    "symbol": "ETHUSDT",
    "seller": "$api_market_no_liq_seller",
    "orderId": $api_market_no_liq_order_id,
    "marketClientOrderId": "$api_market_no_liq_client_id",
    "executedQty": 0,
    "quoteQty": 0,
    "finalStatus": "CANCELED",
    "sellerEthAfterRelease": 1,
    "sellerLockedEthAfterRelease": 0,
    "tradesVisible": 0
  },
  "binanceIocLimitNoLiquidityScenario": {
    "symbol": "ETHUSDT",
    "owner": "$api_ioc_limit_owner",
    "price": 171,
    "quantity": 0.2,
    "finalStatus": "CANCELED",
    "ownerEthAfterCancel": 1,
    "ownerLockedEthAfterCancel": 0
  },
  "binanceIocLimitPartialFillScenario": {
    "symbol": "ETHUSDT",
    "seller": "$api_ioc_partial_seller",
    "buyer": "$api_ioc_partial_buyer",
    "price": 172,
    "makerQty": 0.2,
    "takerOrigQty": 0.5,
    "takerExecutedQty": 0.2,
    "takerCanceledQty": 0.3,
    "quoteQty": 34.4,
    "buyerUsdtAfterRelease": 65.6,
    "buyerLockedUsdtAfterRelease": 0,
    "buyerFinalStatus": "CANCELED",
    "sellerFinalStatus": "FILLED"
  },
  "binanceIocLimitSellPartialFillScenario": {
    "symbol": "ETHUSDT",
    "seller": "$api_ioc_partial_sell_seller",
    "buyer": "$api_ioc_partial_sell_buyer",
    "price": 173,
    "makerQty": 0.2,
    "takerOrigQty": 0.5,
    "takerExecutedQty": 0.2,
    "takerCanceledQty": 0.3,
    "quoteQty": 34.6,
    "sellerEthAfterRelease": 0.8,
    "sellerLockedEthAfterRelease": 0,
    "sellerFinalStatus": "CANCELED",
    "buyerFinalStatus": "FILLED"
  },
  "binanceMyTradesOrderIdIsolation": {
    "symbol": "ETHUSDT",
    "owner": "$api_order_buyer",
    "foreignOrderId": $api_order_seller_binance_order_id,
    "foreignOrderTradesVisible": 0,
    "status": "passed"
  },
  "binanceOrderIdLookupIsolation": {
    "symbol": "ETHUSDT",
    "owner": "$api_order_buyer",
    "foreignOrderId": $api_order_seller_binance_order_id,
    "status": "HTTP_403"
  },
  "binanceOrderIdCancelIsolation": {
    "symbol": "ETHUSDT",
    "owner": "$api_cancel_owner",
    "intruder": "${api_cancel_owner}-intruder",
    "orderId": $api_cancel_order_id,
    "cancelStatus": "HTTP_403",
    "ownerOrderStatusAfterIntruderCancel": "NEW",
    "ownerLockedBalanceAfterIntruderCancel": 0.3
  },
  "binanceDuplicateOrderIdCancel": {
    "symbol": "ETHUSDT",
    "owner": "$api_cancel_owner",
    "orderId": $api_cancel_order_id,
    "duplicateCancelStatus": "CANCELED",
    "ownerOrderStatusAfterDuplicateCancel": "CANCELED",
    "ownerAvailableBalanceAfterDuplicateCancel": 1,
    "ownerLockedBalanceAfterDuplicateCancel": 0
  },
  "binancePartialFillOrderIdCancel": {
    "symbol": "ETHUSDT",
    "seller": "$api_partial_cancel_seller",
    "buyer": "$api_partial_cancel_buyer",
    "orderId": $api_partial_cancel_order_id,
    "price": 169,
    "origQty": 0.5,
    "executedQtyBeforeCancel": 0.2,
    "remainingQtyCanceled": 0.3,
    "quoteQty": 33.8,
    "sellerEthAfterCancel": 0.8,
    "sellerUsdtAfterFee": 33.462,
    "finalStatus": "CANCELED"
  },
  "cancelScenario": {
    "price": 150,
    "quantity": 0.25,
    "status": "CANCELED",
    "queryByOrderId": "NEW",
    "missingLookupStatus": "HTTP_400",
    "zeroOrderIdStatus": "HTTP_400",
    "wrongOwnerStatus": "HTTP_403"
  },
  "binanceClientOrderIdIsolation": {
    "symbol": "ETHUSDT",
    "owner": "$api_client_cancel_owner",
    "intruder": "${api_client_cancel_owner}-intruder",
    "clientOrderId": "$api_client_cancel_id",
    "createResponseIncludedOrderId": true,
    "createResponseStatus": "NEW",
    "lookupStatus": "HTTP_404",
    "cancelStatus": "HTTP_404",
    "ownerOrderStatusAfterIntruderCancel": "NEW",
    "ownerLockedBalanceAfterIntruderCancel": 0.2
  },
  "binanceDuplicateClientOrderIdScenario": {
    "price": 164,
    "quantity": 0.2,
    "duplicatePrice": 165,
    "duplicateStatus": "HTTP_400",
    "reusedAfterCancelPrice": 166,
    "reusedAfterCancelStatus": "CANCELED",
    "finalStatus": "CANCELED"
  },
  "binanceGeneratedClientOrderIdScenario": {
    "price": 167,
    "quantity": 0.2,
    "clientOrderId": "$api_generated_client_id",
    "createResponseIncludedOrderId": true,
    "createResponseStatus": "NEW",
    "openOrdersVerified": true,
    "finalStatus": "CANCELED"
  },
  "binanceAckOrderResponseScenario": {
    "price": 170,
    "quantity": 0.2,
    "clientOrderId": "$api_ack_client_id",
    "ackResponseIncludedOrderId": true,
    "fullFieldsOmitted": true,
    "finalStatus": "CANCELED"
  },
  "binanceResultOrderResponseScenario": {
    "price": 168,
    "quantity": 0.2,
    "clientOrderId": "$api_result_client_id",
    "createResponseIncludedOrderId": true,
    "createResponseStatus": "NEW",
    "finalStatus": "CANCELED"
  },
  "binanceFullOrderResponseScenario": {
    "price": 169,
    "quantity": 0.2,
    "clientOrderId": "$api_full_client_id",
    "createResponseIncludedOrderId": true,
    "createResponseStatus": "NEW",
    "finalStatus": "CANCELED"
  },
  "binanceAllSymbolOrderQueryScenario": {
    "owner": "$api_all_symbol_owner",
    "symbols": ["ETHUSDT", "BTCUSDT"],
    "openOrdersWithoutSymbolVerified": true,
    "allOrdersWithoutSymbolFinalStatus": "CANCELED"
  },
  "binanceSignedRequestValidation": {
    "staleTimestampStatus": "HTTP_400",
    "futureTimestampStatus": "HTTP_400",
    "oversizeRecvWindowStatus": "HTTP_400"
  },
  "matchingEngineRestartScenario": {
    "restingAskPrice": 111,
    "restingAskQuantity": 0.5,
    "status": "FILLED_AFTER_RESTART"
  },
  "walletRestartScenario": {
    "restingAskPrice": 112,
    "restingAskQuantity": 0.4,
    "status": "SETTLED_AFTER_RESTART"
  },
  "accountantRestartScenario": {
    "restingAskPrice": 113,
    "restingAskQuantity": 0.3,
    "status": "SETTLED_AFTER_RESTART"
  },
  "matchingGatewayRestartScenario": {
    "price": 114,
    "quantity": 0.2,
    "status": "SETTLED_AFTER_RESTART"
  },
  "coreServicesRestartScenario": {
    "price": 115,
    "quantity": 0.2,
    "status": "SETTLED_AFTER_RESTART"
  },
  "kafkaBrokerRestartScenario": {
    "price": 116,
    "quantity": 0.2,
    "gatewayRejectedOrderWhileKafkaDown": true,
    "rejectedOrderStatus": 503,
    "status": "SETTLED_AFTER_RESTART"
  },
  "postgresDatastoreRestartScenario": {
    "price": 117,
    "quantity": 0.2,
    "status": "SETTLED_AFTER_RESTART"
  },
  "partialScenario": {
    "price": 120,
    "askQuantity": 1,
    "filledQuantity": 0.4,
    "canceledRemainingQuantity": 0.6,
    "status": "CANCELED"
  },
  "iocNoLiquidityScenario": {
    "price": 999,
    "quantity": 0.5,
    "status": "CANCELED"
  },
  "marketIocScenario": {
    "makerBidPrice": 130,
    "makerBidQuantity": 0.3,
    "marketAskQuantity": 0.2,
    "canceledRemainingBidQuantity": 0.1,
    "status": "FILLED"
  },
  "multiLevelSweepScenario": {
    "highBidPrice": 150,
    "highBidQuantity": 0.1,
    "lowBidPrice": 140,
    "lowBidQuantity": 0.3,
    "marketAskQuantity": 0.3,
    "filledHighQuantity": 0.1,
    "filledLowQuantity": 0.2,
    "canceledRemainingLowBidQuantity": 0.1,
    "status": "FILLED"
  },
  "multiLevelBidSweepScenario": {
    "lowAskPrice": 90,
    "lowAskQuantity": 0.1,
    "highAskPrice": 100,
    "highAskQuantity": 0.3,
    "marketBidLimitPrice": 200,
    "marketBidQuantity": 0.3,
    "filledLowAskQuantity": 0.1,
    "filledHighAskQuantity": 0.2,
    "canceledRemainingHighAskQuantity": 0.1,
    "status": "FILLED"
  },
  "pricePriorityScenario": {
    "lowBidPrice": 110,
    "highBidPrice": 140,
    "askPrice": 100,
    "quantity": 0.2,
    "matchedPrice": 140,
    "canceledLowBidQuantity": 0.2
  },
  "timePriorityScenario": {
    "firstBidPrice": 125,
    "secondBidPrice": 125,
    "askPrice": 100,
    "quantity": 0.2,
    "matchedPrice": 125,
    "canceledSecondBidQuantity": 0.2
  },
  "overreserveScenario": {
    "firstAskPrice": 160,
    "firstAskQuantity": 0.7,
    "rejectedAskPrice": 161,
    "rejectedAskQuantity": 0.5,
    "status": "REJECTED"
  },
  "bidOverreserveScenario": {
    "firstBidPrice": 80,
    "firstBidQuantity": 0.8,
    "rejectedBidPrice": 80,
    "rejectedBidQuantity": 0.5,
    "status": "REJECTED"
  },
  "cancelAuthorizationScenario": {
    "askPrice": 170,
    "askQuantity": 0.4,
    "intruderCancelStatus": "REJECTED_ASYNC",
    "intruderEditPrice": 171,
    "intruderEditQuantity": 0.3,
    "intruderEditStatus": "REJECTED_ASYNC_NO_BOOK_OR_BALANCE_CHANGE",
    "ownerCancelStatus": "CANCELED",
    "duplicateOwnerCancelStatus": "REJECTED_ASYNC_NO_BALANCE_CHANGE"
  },
  "malformedEditScenario": {
    "restingAskPrice": 172,
    "restingAskQuantity": 0.4,
    "rejectedEditPrice": 173,
    "rejectedEditQuantity": 0.3,
    "status": "HTTP_400_GATEWAY_REJECT_NO_BOOK_OR_BALANCE_CHANGE"
  },
  "unsupportedFokScenario": {
    "price": 777,
    "quantity": 0.5,
    "status": "HTTP_400_GATEWAY_REJECT"
  },
  "selfTradePreventionScenario": {
    "restingAskPrice": 118,
    "restingAskQuantity": 0.4,
    "rejectedBidPrice": 118,
    "rejectedBidQuantity": 0.2,
    "status": "REJECTED_NO_TRADE"
  },
  "layeredSelfTradePreventionScenario": {
    "externalAskPrice": 119,
    "externalAskQuantity": 0.1,
    "ownerAskPrice": 120,
    "ownerAskQuantity": 0.4,
    "rejectedBidPrice": 120,
    "rejectedBidQuantity": 0.2,
    "status": "REJECTED_BEFORE_EXTERNAL_PARTIAL_FILL"
  },
  "editOrderScenario": {
    "initialAskPrice": 121,
    "initialAskQuantity": 0.4,
    "editedAskPrice": 122,
    "editedAskQuantity": 0.3,
    "releasedBaseQuantity": 0.1,
    "initialBidPrice": 100,
    "initialBidQuantity": 0.5,
    "editedBidPrice": 90,
    "editedBidQuantity": 0.4,
    "releasedQuoteAmount": 14,
    "crossingInitialAskPrice": 110,
    "crossingInitialAskQuantity": 0.3,
    "crossingEditedAskPrice": 100,
    "crossingEditedAskQuantity": 0.2,
    "crossingTradeQuantity": 0.2,
    "status": "UPDATED_AND_CANCELED"
  },
  "rejectScenario": {
    "direction": "ASK",
    "price": 100,
    "quantity": 1,
    "reason": "underfunded"
  },
  "bidRejectScenario": {
    "direction": "BID",
    "price": 100,
    "quantity": 1,
    "reason": "underfunded"
  },
  "invalidOrderScenario": {
    "zeroQuantityAskStatus": "REJECTED",
    "zeroPriceAskStatus": "REJECTED",
    "negativePriceBidStatus": "REJECTED",
    "malformedPairBidStatus": "REJECTED",
    "invalidPricePrecisionStatus": "REJECTED",
    "invalidQuantityPrecisionStatus": "REJECTED",
    "reason": "invalid_parameters"
  },
  "duplicateDepositScenario": {
    "symbol": "USDT",
    "amount": 5,
    "duplicateTransferRefStatus": "REJECTED",
    "finalBalance": 5
  },
  "withdrawScenario": {
    "invalidRequests": {
      "belowMinimumStatus": "REJECTED",
      "zeroAmountStatus": "REJECTED",
      "overBalanceStatus": "REJECTED",
      "balanceAfterInvalidRequests": 10
    },
    "cancelFlow": {
      "requestedAmount": 3,
      "intruderCancelRejected": true,
      "finalStatus": "CANCELED",
      "terminalTransitionsRejected": true,
      "balanceAfterCancel": 10
    },
    "acceptFlow": {
      "requestedAmount": 4,
      "fee": 0.1,
      "destAmount": 3.9,
      "processingCancelRejected": true,
      "invalidDestAmountAcceptRejected": true,
      "finalStatus": "DONE",
      "duplicateAcceptRejected": true,
      "terminalTransitionsRejected": true,
      "finalBalance": 6
    },
    "duplicateDestinationRefFlow": {
      "requestedAmount": 1.1,
      "duplicateDestinationRefRejected": true,
      "finalStatus": "REJECTED",
      "balanceAfterReject": 6
    },
    "rejectFlow": {
      "requestedAmount": 2,
      "finalStatus": "REJECTED",
      "terminalTransitionsRejected": true,
      "balanceAfterReject": 6
    },
    "status": "passed"
  },
  "finalOrderBookScenario": {
    "askLevels": 0,
    "bidLevels": 0
  },
  "recentTradesScenario": {
    "count": 23,
    "quantitiesByPrice": {
      "90": 0.1,
      "100": 1.4,
      "101": 0.2,
      "102": 0.2,
      "103": 0.2,
      "111": 0.5,
      "112": 0.4,
      "113": 0.3,
      "114": 0.2,
      "115": 0.2,
      "116": 0.2,
      "117": 0.2,
      "120": 0.4,
      "125": 0.2,
      "130": 0.2,
      "140": 0.4,
      "150": 0.1,
      "169": 0.2,
      "172": 0.2,
      "173": 0.2
    }
  },
  "databaseInvariantScenario": {
    "walletTransactionCategories": {
      "DEPOSIT": 83,
      "FEE": 46,
      "ORDER_CANCEL": 47,
      "ORDER_CREATE": 79,
      "ORDER_FINALIZED": 1,
      "TRADE": 46,
      "WITHDRAW_ACCEPT": 1,
      "WITHDRAW_CANCEL": 1,
      "WITHDRAW_REJECT": 2,
      "WITHDRAW_REQUEST": 4
    },
    "walletAggregateBalances": {
      "BTC": 0.002,
      "ETH": 48.54,
      "USDT": 3245.904
    },
    "walletWithdrawStatuses": {
      "CANCELED": 1,
      "DONE": 1,
      "REJECTED": 2
    },
    "walletAcceptedWithdrawChainReference": true,
    "walletRejectedWithdrawReason": true,
    "walletDuplicateWithdrawDestinationRefs": 0,
    "walletExchangeBalancesReleased": true,
    "walletCashoutBalancesReleased": true,
    "accountantProcessedFinancialActions": {
      "CancelOrderEvent": 41,
      "RejectOrderEvent": 3,
      "SubmitOrderEvent": 79,
      "TradeEvent": 93,
      "UpdatedOrderEvent": 3
    },
    "accountantRetryQueueDrained": true,
    "walletNegativeBalances": 0,
    "walletDuplicateTransferRefs": 0,
    "accountantUnprocessedFinancialActions": 0,
    "marketOpenOrders": 0,
    "marketInvalidTrades": 0,
    "marketDuplicateTradeEvents": 0,
    "marketPersistedTrades": 23
  },
  "marketKafkaReplayIdempotencyScenario": {
    "replayedTopics": ["richOrder", "richTrade"],
    "poisonRecordsSkipped": true,
    "marketOpenOrdersAfterReplay": 0,
    "marketPersistedTradesAfterReplay": 23,
    "status": "passed"
  },
  "marketRestartScenario": {
    "status": "passed",
    "askLevelsAfterRestart": 0,
    "bidLevelsAfterRestart": 0,
    "recentTradesAfterRestart": 23
  },
  "multiMarketScenario": {
    "pair": "BTC_USDT",
    "price": 20000,
    "quantity": 0.001,
    "marketPersistedTrades": 1,
    "ethOrderBookStillEmpty": true,
    "status": "passed"
  },
  "secondaryEngineMarketScenario": {
    "pairs": ["SOL_USDT", "DOGE_USDT", "TON_USDT"],
    "marketPersistedTrades": 3,
    "status": "passed"
  },
  "concurrentTakerScenario": {
    "pair": "BTC_USDT",
    "restingAskPrice": 21000,
    "restingAskQuantity": 0.003,
    "concurrentBuyerCount": 3,
    "perBuyerQuantity": 0.001,
    "matchedQuantity": 0.003,
    "persistedTrades": 3,
    "btcUsdtPersistedTradesAfterScenario": 4,
    "status": "passed"
  },
  "concurrentOverfillScenario": {
    "pair": "BTC_USDT",
    "restingAskPrice": 22000,
    "restingAskQuantity": 0.002,
    "concurrentBuyerCount": 3,
    "perBuyerQuantity": 0.001,
    "matchedBuyerCount": 2,
    "residualBuyerCount": 1,
    "matchedQuantity": 0.002,
    "residualQuantityCanceled": 0.001,
    "persistedTrades": 2,
    "status": "passed"
  },
  "expectedBalances": {
    "seller": {"ETH": 1, "USDT": 99},
    "buyer": {"ETH": 0.99, "USDT": 900},
    "engineRestartSeller": {"ETH": 0.5, "USDT": 54.945},
    "engineRestartBuyer": {"ETH": 0.495, "USDT": 44.5},
    "walletRestartSeller": {"ETH": 0.6, "USDT": 44.352},
    "walletRestartBuyer": {"ETH": 0.396, "USDT": 55.2},
    "accountantRestartSeller": {"ETH": 0.7, "USDT": 33.561},
    "accountantRestartBuyer": {"ETH": 0.297, "USDT": 66.1},
    "gatewayRestartSeller": {"ETH": 0.8, "USDT": 22.572},
    "gatewayRestartBuyer": {"ETH": 0.198, "USDT": 77.2},
    "coreRestartSeller": {"ETH": 0.8, "USDT": 22.77},
    "coreRestartBuyer": {"ETH": 0.198, "USDT": 77},
    "kafkaRestartSeller": {"ETH": 0.8, "USDT": 22.968},
    "kafkaRestartBuyer": {"ETH": 0.198, "USDT": 76.8},
    "postgresRestartSeller": {"ETH": 0.8, "USDT": 23.166},
    "postgresRestartBuyer": {"ETH": 0.198, "USDT": 76.6},
    "cancelOwner": {"ETH": 1},
    "apiGeneratedClientOwner": {"ETH": 1},
    "apiDuplicateClientOwner": {"ETH": 1},
    "partialSeller": {"ETH": 1.6, "USDT": 47.52},
    "partialBuyer": {"ETH": 0.396, "USDT": 2},
    "iocOwner": {"ETH": 1},
    "marketSeller": {"ETH": 0.8, "USDT": 25.74},
    "marketBuyer": {"ETH": 0.198, "USDT": 14},
    "sweepSeller": {"ETH": 0.7, "USDT": 42.57},
    "sweepHighBuyer": {"ETH": 0.099, "USDT": 5},
    "sweepLowBuyer": {"ETH": 0.198, "USDT": 22},
    "bidSweepBuyer": {"ETH": 0.297, "USDT": 51},
    "bidSweepLowSeller": {"ETH": 0.9, "USDT": 8.91},
    "bidSweepHighSeller": {"ETH": 0.8, "USDT": 19.8},
    "prioritySeller": {"ETH": 0.8, "USDT": 27.72},
    "priorityHighBuyer": {"ETH": 0.198, "USDT": 2},
    "priorityLowBuyer": {"USDT": 30},
    "fifoSeller": {"ETH": 0.8, "USDT": 24.75},
    "fifoFirstBuyer": {"ETH": 0.198, "USDT": 5},
    "fifoSecondBuyer": {"USDT": 30},
    "overreserveOwner": {"ETH": 1},
    "bidOverreserveOwner": {"USDT": 100},
    "cancelAuthOwner": {"ETH": 1},
    "malformedEditOwner": {"ETH": 1},
    "fokOwner": {"ETH": 1},
    "selfTradeOwner": {"ETH": 1, "USDT": 100},
    "layeredSelfTradeOwner": {"ETH": 1, "USDT": 100},
    "layeredExternalSeller": {"ETH": 0.1},
    "duplicateDepositOwner": {"USDT": 5},
    "withdrawOwner": {"USDT": 6},
    "btcSeller": {"BTC": 0.009, "USDT": 19.8},
    "btcBuyer": {"BTC": 0.00099, "USDT": 30},
    "solSeller": {"SOL": 0, "USDT": 9.9},
    "solBuyer": {"SOL": 0.99, "USDT": 10},
    "dogeSeller": {"DOGE": 0, "USDT": 9.9},
    "dogeBuyer": {"DOGE": 9.9, "USDT": 10},
    "tonSeller": {"TON": 0, "USDT": 9.9},
    "tonBuyer": {"TON": 1.98, "USDT": 10},
    "concurrentSeller": {"BTC": 0, "USDT": 62.37},
    "concurrentBuyerOne": {"BTC": 0.00099, "USDT": 9},
    "concurrentBuyerTwo": {"BTC": 0.00099, "USDT": 9},
    "concurrentBuyerThree": {"BTC": 0.00099, "USDT": 9},
    "overfillSeller": {"BTC": 0, "USDT": 43.56},
    "overfillFilledBuyer": {"BTC": 0.00099, "USDT": 8},
    "overfillResidualBuyer": {"USDT": 30}
  }
}
EOF

  if (( KEEP_RUNNING == 0 )); then
    "${COMPOSE[@]}" stop matching-gateway matching-engine matching-engine-duo accountant wallet market api bc-gateway auth eventlog || true
  fi
}

main "$@"
