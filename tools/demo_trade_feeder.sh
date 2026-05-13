#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${FEED_BASE_URL:-http://127.0.0.1}"
SYMBOL="${FEED_SYMBOL:-ETH_USDT}"
PAIR="${FEED_PAIR:-ETH_USDT}"
INTERVAL_MODE="${FEED_INTERVAL_MODE:-jitter}"
PUBLIC_SYMBOL="${FEED_PUBLIC_SYMBOL:-ETHUSDT}"
COINBASE_PRODUCT="${FEED_COINBASE_PRODUCT:-ETH-USD}"
KRAKEN_PAIR="${FEED_KRAKEN_PAIR:-ETHUSDT}"
PRICE_REFRESH_SECONDS="${FEED_PRICE_REFRESH_SECONDS:-5}"
DEPOSIT_SETTLE_SECONDS="${FEED_DEPOSIT_SETTLE_SECONDS:-0.35}"
TRACKED_USERS_CSV="${FEED_TRACKED_USERS:-}"
TOP_UP_TRACKED_USERS="${FEED_TOP_UP_TRACKED_USERS:-1}"
PUBLIC_TRADES_URL="${FEED_PUBLIC_TRADES_URL:-${BASE_URL}:5500/v3/trades?symbol=${PUBLIC_SYMBOL}&limit=100}"
TRADE_PUBLISH_TIMEOUT_SECONDS="${FEED_TRADE_PUBLISH_TIMEOUT_SECONDS:-8}"

log() {
  printf '%s %s\n' "$(date -Is)" "$*"
}

post_json() {
  local owner="$1"
  local body="$2"
  local resp status
  resp="$(curl -sS -w $'\n%{http_code}' \
    -H "Content-Type: application/json" \
    -H "X-Opex-User: ${owner}" \
    -X POST \
    -d "$body" \
    "${BASE_URL}:8093/order" 2>&1 || true)"
  status="${resp##*$'\n'}"
  if [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
    return 0
  fi
  log "warn order rejected owner=${owner} status=${status} body=${resp%$'\n'*}"
  return 1
}

deposit() {
  local owner="$1"
  local asset="$2"
  local amount="$3"
  local ref="$4"
  local resp status
  resp="$(curl -sS -w $'\n%{http_code}' \
    -X POST \
    "${BASE_URL}:8091/deposit/${amount}_test-ethereum_${asset}/${owner}_MAIN?description=demo-feed&transferRef=${ref}" 2>&1 || true)"
  status="${resp##*$'\n'}"
  if [[ "$status" =~ ^2[0-9][0-9]$ ]]; then
    return 0
  fi
  log "warn deposit rejected owner=${owner} asset=${asset} amount=${amount} status=${status} body=${resp%$'\n'*}"
  return 1
}

latest_trade_id() {
  local body latest
  body="$(curl -fsS --max-time 2 "$PUBLIC_TRADES_URL" 2>/dev/null || true)"
  if [[ -z "$body" ]]; then
    printf '0\n'
    return
  fi
  latest="$(jq -r 'map(.id) | max // 0' <<<"$body" 2>/dev/null || true)"
  if [[ "$latest" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$latest"
  else
    printf '0\n'
  fi
}

wait_for_public_trade() {
  local before_id="$1"
  local latest_id="0"
  local deadline=$((SECONDS + TRADE_PUBLISH_TIMEOUT_SECONDS))

  while (( SECONDS < deadline )); do
    sleep 0.5
    latest_id="$(latest_trade_id)"
    if [[ "$latest_id" =~ ^[0-9]+$ ]] && (( latest_id > before_id )); then
      printf '%s\n' "$latest_id"
      return 0
    fi
  done

  printf '%s\n' "$latest_id"
  return 1
}

fetch_market() {
  local body price qty

  body="$(curl -fsS --max-time 4 "https://api.binance.com/api/v3/ticker/24hr?symbol=${PUBLIC_SYMBOL}" 2>/dev/null || true)"
  if [[ -n "$body" ]]; then
    price="$(jq -r '.lastPrice // empty' <<<"$body" 2>/dev/null || true)"
    qty="$(jq -r '.lastQty // empty' <<<"$body" 2>/dev/null || true)"
    if [[ -n "$price" && "$price" != "null" ]]; then
      printf '%s %s %s\n' "$price" "${qty:-0.05}" "binance"
      return 0
    fi
  fi

  body="$(curl -fsS --max-time 4 "https://api.exchange.coinbase.com/products/${COINBASE_PRODUCT}/ticker" 2>/dev/null || true)"
  if [[ -n "$body" ]]; then
    price="$(jq -r '.price // empty' <<<"$body" 2>/dev/null || true)"
    qty="$(jq -r '.size // empty' <<<"$body" 2>/dev/null || true)"
    if [[ -n "$price" && "$price" != "null" ]]; then
      printf '%s %s %s\n' "$price" "${qty:-0.05}" "coinbase"
      return 0
    fi
  fi

  body="$(curl -fsS --max-time 4 "https://api.kraken.com/0/public/Ticker?pair=${KRAKEN_PAIR}" 2>/dev/null || true)"
  if [[ -n "$body" ]]; then
    price="$(jq -r '.result | to_entries[0].value.c[0] // empty' <<<"$body" 2>/dev/null || true)"
    qty="$(jq -r '.result | to_entries[0].value.c[1] // empty' <<<"$body" 2>/dev/null || true)"
    if [[ -n "$price" && "$price" != "null" ]]; then
      printf '%s %s %s\n' "$price" "${qty:-0.05}" "kraken"
      return 0
    fi
  fi

  printf '%s %s %s\n' "${FEED_FALLBACK_PRICE:-2285.00}" "0.05" "fallback"
}

sleep_between_trades() {
  if [[ "$INTERVAL_MODE" == "fixed" ]]; then
    sleep "${FEED_INTERVAL_SECONDS:-0.75}"
    return
  fi

  case $((RANDOM % 3)) in
    0) sleep 0.50 ;;
    1) sleep 0.75 ;;
    *) sleep 1.00 ;;
  esac
}

build_trade_numbers() {
  local market_price="$1"
  local market_qty="$2"
  python3 - "$market_price" "$market_qty" <<'PY'
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
import random
import sys

price = Decimal(sys.argv[1])
try:
    last_qty = Decimal(sys.argv[2])
except Exception:
    last_qty = Decimal("0.05")

price_bps = Decimal(random.randint(-12, 12)) / Decimal(10000)
trade_price = (price * (Decimal(1) + price_bps)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
cross_bps = Decimal(random.randint(90, 180)) / Decimal(10000)
ask_price = (trade_price * (Decimal(1) - cross_bps)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
bid_price = (trade_price * (Decimal(1) + cross_bps)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

if last_qty <= 0 or last_qty > Decimal("2"):
    base_qty = Decimal(str(random.uniform(0.02, 0.24)))
else:
    base_qty = last_qty
qty = base_qty * Decimal(str(random.uniform(0.35, 2.10)))
if qty < Decimal("0.008"):
    qty = Decimal("0.008")
if qty > Decimal("0.75"):
    qty = Decimal("0.75")
qty = qty.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)

seller_deposit = (qty * Decimal("1.08")).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
buyer_deposit = (bid_price * qty * Decimal("1.08") + Decimal("5.0")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

print(f"{trade_price} {qty} {seller_deposit} {buyer_deposit} {ask_price} {bid_price}")
PY
}

run_id="$(date +%s)"
last_market_fetch=0
market_price="${FEED_FALLBACK_PRICE:-2285.00}"
market_qty="0.05"
market_source="fallback"
tracked_users=()
if [[ -n "$TRACKED_USERS_CSV" ]]; then
  IFS=',' read -ra tracked_users <<<"$TRACKED_USERS_CSV"
  for i in "${!tracked_users[@]}"; do
    item="${tracked_users[$i]}"
    item="${item#"${item%%[![:space:]]*}"}"
    item="${item%"${item##*[![:space:]]}"}"
    tracked_users[$i]="$item"
  done
  if (( ${#tracked_users[@]} < 2 )); then
    log "error FEED_TRACKED_USERS requires at least two comma-separated users"
    exit 1
  fi
fi

log "starting demo feeder run_id=${run_id} pair=${PAIR} public_symbol=${PUBLIC_SYMBOL} tracked_users=${#tracked_users[@]} public_trades_url=${PUBLIC_TRADES_URL}"

seq_no=0
while true; do
  seq_no=$((seq_no + 1))
  now="$(date +%s)"
  if (( now - last_market_fetch >= PRICE_REFRESH_SECONDS )); then
    read -r market_price market_qty market_source < <(fetch_market)
    last_market_fetch="$now"
    log "market source=${market_source} price=${market_price} last_qty=${market_qty}"
  fi

  if (( ${#tracked_users[@]} >= 2 )); then
    seller="${tracked_users[$(( (seq_no - 1) % ${#tracked_users[@]} ))]}"
    buyer="${tracked_users[$(( seq_no % ${#tracked_users[@]} ))]}"
    if [[ "$seller" == "$buyer" ]]; then
      buyer="${tracked_users[$(( (seq_no + 1) % ${#tracked_users[@]} ))]}"
    fi
  else
    seller="demo-feed-seller-${run_id}-${seq_no}"
    buyer="demo-feed-buyer-${run_id}-${seq_no}"
  fi
  read -r price quantity seller_eth buyer_usdt ask_price bid_price < <(build_trade_numbers "$market_price" "$market_qty")

  if (( ${#tracked_users[@]} == 0 )) || [[ "$TOP_UP_TRACKED_USERS" == "1" ]]; then
    deposit "$seller" ETH "$seller_eth" "demo-feed-${run_id}-${seq_no}-seller-eth"
    deposit "$buyer" USDT "$buyer_usdt" "demo-feed-${run_id}-${seq_no}-buyer-usdt"
    sleep "$DEPOSIT_SETTLE_SECONDS"
  fi

  ask_body="$(printf '{"uuid":null,"pair":"%s","price":%s,"quantity":%s,"direction":"ASK","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}' "$PAIR" "$ask_price" "$quantity")"
  bid_body="$(printf '{"uuid":null,"pair":"%s","price":%s,"quantity":%s,"direction":"BID","matchConstraint":"GTC","orderType":"LIMIT_ORDER","userLevel":"*"}' "$PAIR" "$bid_price" "$quantity")"
  before_trade_id="$(latest_trade_id)"

  if (( seq_no % 2 == 0 )); then
    if post_json "$seller" "$ask_body" && post_json "$buyer" "$bid_body"; then
      if after_trade_id="$(wait_for_public_trade "$before_trade_id")"; then
        log "trade seq=${seq_no} symbol=${SYMBOL} side=buyer-taker ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity} public_trade_id=${after_trade_id} source=${market_source}"
      else
        log "warn trade not published seq=${seq_no} before_id=${before_trade_id} latest_id=${after_trade_id} side=buyer-taker ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity}"
      fi
    else
      sleep 0.40
      if post_json "$seller" "$ask_body" && post_json "$buyer" "$bid_body"; then
        if after_trade_id="$(wait_for_public_trade "$before_trade_id")"; then
          log "trade seq=${seq_no} symbol=${SYMBOL} side=buyer-retry ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity} public_trade_id=${after_trade_id} source=${market_source}"
        else
          log "warn trade retry not published seq=${seq_no} before_id=${before_trade_id} latest_id=${after_trade_id} side=buyer-retry ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity}"
        fi
      else
        log "warn trade failed seq=${seq_no} side=buyer-taker ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity}"
      fi
    fi
  else
    if post_json "$buyer" "$bid_body" && post_json "$seller" "$ask_body"; then
      if after_trade_id="$(wait_for_public_trade "$before_trade_id")"; then
        log "trade seq=${seq_no} symbol=${SYMBOL} side=seller-taker ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity} public_trade_id=${after_trade_id} source=${market_source}"
      else
        log "warn trade not published seq=${seq_no} before_id=${before_trade_id} latest_id=${after_trade_id} side=seller-taker ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity}"
      fi
    else
      sleep 0.40
      if post_json "$buyer" "$bid_body" && post_json "$seller" "$ask_body"; then
        if after_trade_id="$(wait_for_public_trade "$before_trade_id")"; then
          log "trade seq=${seq_no} symbol=${SYMBOL} side=seller-retry ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity} public_trade_id=${after_trade_id} source=${market_source}"
        else
          log "warn trade retry not published seq=${seq_no} before_id=${before_trade_id} latest_id=${after_trade_id} side=seller-retry ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity}"
        fi
      else
        log "warn trade failed seq=${seq_no} side=seller-taker ref_price=${price} ask=${ask_price} bid=${bid_price} qty=${quantity}"
      fi
    fi
  fi

  sleep_between_trades
done
