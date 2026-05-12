# AI Handoff: zkCEX Real Exchange E2E

## Repository

Working directory:

```bash
/Users/hoh/Documents/Projects/zkCEX/core
```

Current user priority:

- Build toward a fully working CEX.
- Real exchange correctness and real service e2e are more important than zkAML/zkPoL.
- Minimize mocks. Prefer Docker/local service paths with real DB, Kafka, APIs, wallet, matching, accountant, market, and bc-gateway services.
- Continue in small testable units, verify, then commit.
- If stopping, leave a clear handoff for another AI.

## Current Stop Point

The user explicitly asked to stop, leave a handoff, and end the session.

No further code changes should be made unless the user resumes work.

At stop time:

- latest committed work already includes:
  - `8c5f4d12 Assert Binance trade fee responses`
  - `6c963915 Reject stale Binance wallet history requests`
- current uncommitted changes are:
  - `M docs/exchange-e2e.md`
  - `M tools/e2e/run_exchange_e2e.sh`
  - `?? docs/AI_HANDOFF_CEX_E2E.md`

The current uncommitted work adds Binance-compatible deposit address assignment e2e coverage through the real bc-gateway path. It is not committed yet.

## Latest Verified Commits

Recent commits in the current branch:

```text
6c963915 Reject stale Binance wallet history requests
8c5f4d12 Assert Binance trade fee responses
b59a5d46 Validate signed asset valuation requests
7fda140e Handle quote-only asset valuation
3778926b Return terminal IOC order responses
1d463459 Assert Binance client cancel response IDs
```

Important earlier user-mentioned commits:

```text
87ed3bd4 Prevent self-trading order edits
2a46c8be Cover self-trading edit rejection
```

## What Was Completed Before Stop

### `8c5f4d12 Assert Binance trade fee responses`

Completed:

- Added `WalletControllerTest` coverage for `/v1/asset/tradeFee`:
  - symbol-specific path,
  - all-symbol path with deduplication,
  - stale timestamp rejection before accountant call.
- Added real Docker e2e assertions in `tools/e2e/run_exchange_e2e.sh` for:
  - stale timestamp rejection on `/v1/asset/tradeFee`,
  - accountant-configured maker/taker fee values for `ETHUSDT`,
  - all-symbol fee response including `ETHUSDT` and `BTCUSDT`.
- Updated `docs/exchange-e2e.md`.

Verified at the time:

- `WalletControllerTest`: 32 passed
- `bash -n tools/e2e/run_exchange_e2e.sh`: passed
- `git diff --check`: passed
- `tools/e2e/run_exchange_e2e.sh --reset`: passed

### `6c963915 Reject stale Binance wallet history requests`

Completed:

- Added `WalletControllerTest` coverage for stale signed timestamp rejection on:
  - GET `/v1/capital/deposit/hisrec`
  - GET `/v1/capital/withdraw/history`
- Added real Docker e2e assertions for stale signed timestamp rejection on those wallet history endpoints.
- Updated `docs/exchange-e2e.md`.

Verified at the time:

- `WalletControllerTest`: 34 passed
- `bash -n tools/e2e/run_exchange_e2e.sh`: passed
- `git diff --check`: passed
- `tools/e2e/run_exchange_e2e.sh --reset`: passed

## Current Uncommitted Work

Files changed:

```text
M docs/exchange-e2e.md
M tools/e2e/run_exchange_e2e.sh
?? docs/AI_HANDOFF_CEX_E2E.md
```

### Intent of the current uncommitted change

Strengthen Binance-compatible deposit address coverage so the API path is verified against the real bc-gateway address assignment flow, not just direct bc-gateway deposit helpers.

### Exact code changes made

#### `tools/e2e/run_exchange_e2e.sh`

1. Added usage text entry:

```text
36r4. Verify Binance-compatible deposit address assignment uses the real bc-gateway path.
```

2. Refactored the existing bc-gateway deposit helper:

- extracted:

```bash
upload_reserved_eth_address() {
  local address="$1"
  local csv_file
  csv_file="$(mktemp)"
  printf '%s,,ethereum\n' "$address" > "$csv_file"
  curl -fsS -X PUT -F "file=@${csv_file}" "http://127.0.0.1:8095/v1/address" >/dev/null
  rm -f "$csv_file"
}
```

- `deposit_via_bc_gateway()` now reuses `upload_reserved_eth_address` instead of duplicating the CSV upload logic.

3. Added a new real API verification block immediately after `wait_binance_exchange_info_symbol "ETHUSDT" "ETH" "USDT"` and before the signed timestamp block:

```bash
  local api_address_owner="e2e-api-address-$(date +%s)"
  local api_address="0xe2e${api_address_owner//[^[:alnum:]]/}"
  api_address="${api_address:0:42}"
  upload_reserved_eth_address "$api_address"
  expect_2xx "Binance API deposit address assignment" "$(binance_private_get_status "$api_address_owner" "/v1/capital/deposit/address" "coin=USDT&network=test-ethereum")" >/tmp/opex-e2e-binance-api-deposit-address.json
  jq -e \
    --arg address "$api_address" '
      .address == $address and
      .coin == "USDT" and
      .network == "test-ethereum" and
      .tag == "" and
      .url == ""
    ' /tmp/opex-e2e-binance-api-deposit-address.json >/dev/null
  expect_2xx "Binance API deposit address idempotent lookup" "$(binance_private_get_status "$api_address_owner" "/v1/capital/deposit/address" "coin=USDT&network=test-ethereum")" >/tmp/opex-e2e-binance-api-deposit-address-repeat.json
  jq -e \
    --arg address "$api_address" '
      .address == $address and
      .coin == "USDT" and
      .network == "test-ethereum"
    ' /tmp/opex-e2e-binance-api-deposit-address-repeat.json >/dev/null
```

This verifies:

- `/v1/capital/deposit/address` calls through the real API stack,
- bc-gateway reserved address assignment is reflected back to the Binance-compatible REST response,
- repeated lookup returns the same existing assigned address for the same user.

#### `docs/exchange-e2e.md`

Added:

```text
- Binance-compatible deposit address assignment uses the real bc-gateway path and repeated lookups return the user's existing assigned address.
```

## Verification State Of The Current Uncommitted Work

Completed checks:

```bash
bash -n tools/e2e/run_exchange_e2e.sh
git diff --check
```

Results:

- both passed for the current uncommitted deposit-address work.

### Important partial runtime evidence

A full `tools/e2e/run_exchange_e2e.sh --reset` rerun was started for the current uncommitted change, then later the user explicitly asked to stop.

Before stopping, the rerun had already reached:

- Kafka topics ready
- Vault secrets loaded
- wallet/accountant/matching-engine/matching-gateway/market ready
- bc-gateway ready
- api ready

This matters because the new deposit-address assertion block executes immediately after API readiness. There was no failure before the run reached `ready: api`, and no deposit-address-related error was emitted during that phase.

However:

- there is **no final captured proof** in this handoff that the full rerun completed with `E2E exchange flow passed`.
- therefore the current uncommitted deposit-address change must be treated as **partially verified only**.

The rerun process was explicitly stopped after the user requested handoff. It is no longer running.

## Next Action For Another AI

Resume from the current uncommitted deposit-address work and finish verification:

1. Start with:

```bash
git status --short
git diff --stat
```

2. Re-run the full e2e:

```bash
tools/e2e/run_exchange_e2e.sh --reset
```

3. If it passes:

```bash
git diff --check
git add tools/e2e/run_exchange_e2e.sh docs/exchange-e2e.md
git commit -m "Verify Binance deposit address assignment"
```

4. If it fails:

- treat the failure as a real product or e2e harness issue,
- inspect the failing tmp files and relevant service logs,
- do not replace the scenario with a mock,
- re-run full e2e after fixing.

## Suggested Follow-Up After This Uncommitted Work

If the deposit-address change is verified and committed, the next likely correctness gap is still Binance REST self-trade prevention on `/v3/order` through the real API path.

Recommended scenario:

1. create a fresh owner
2. deposit `1 ETH` and `100 USDT`
3. place a same-owner resting ask through Binance REST
4. place a crossing same-owner bid through Binance REST
5. verify:
   - `RejectOrderEvent`
   - reason `SELF_TRADE_PREVENTION`
   - operation `PLACE_ORDER`
   - direction `BID`
   - no trade inserted into `postgres-market`
   - resting ask remains open
   - rejected bid does not remain open
   - reserved quote funds are released
6. cancel the resting ask and confirm balances return to the original state

## Commands

Useful commands:

```bash
git status --short
git log --oneline -6
git diff --stat
bash -n tools/e2e/run_exchange_e2e.sh
git diff --check
tools/e2e/run_exchange_e2e.sh --reset
```

If production jars change later:

```bash
tools/e2e/run_exchange_e2e.sh --package --build --reset
```

Searches:

```bash
grep -n "deposit address assignment" tools/e2e/run_exchange_e2e.sh docs/exchange-e2e.md
grep -n "upload_reserved_eth_address" tools/e2e/run_exchange_e2e.sh
grep -n "SELF_TRADE_PREVENTION" tools/e2e/run_exchange_e2e.sh
```

## Prompt For The Next AI

```text
작업 위치: /Users/hoh/Documents/Projects/zkCEX/core

목표:
완벽하게 동작하는 CEX를 향해 실제 거래소 e2e와 exchange correctness를 계속 강화한다. zkAML/zkPoL보다 실제 거래소가 제대로 동작하는지 검증하는 것이 우선이다. mock은 최소화하고 가능한 실제 Docker/local service path, DB, Kafka, API, wallet, matching, accountant, market, bc-gateway 경로를 사용한다. 변경 후 검증하고 통과하면 커밋한다.

현재 상태:
- 최신 커밋:
  - 6c963915 Reject stale Binance wallet history requests
  - 8c5f4d12 Assert Binance trade fee responses
  - b59a5d46 Validate signed asset valuation requests
  - 7fda140e Handle quote-only asset valuation
  - 3778926b Return terminal IOC order responses
- 현재 미커밋 변경:
  - M docs/exchange-e2e.md
  - M tools/e2e/run_exchange_e2e.sh
  - ?? docs/AI_HANDOFF_CEX_E2E.md
- handoff 문서는 커밋 대상이 아니다. 필요하면 유지하고, 사용자 지시 없으면 커밋하지 않는다.

현재 미커밋 작업 목표:
Binance-compatible `/v1/capital/deposit/address`를 실제 bc-gateway address assignment 경로로 검증하는 e2e를 마무리한다.

이미 반영된 코드:
- `tools/e2e/run_exchange_e2e.sh`
  - `upload_reserved_eth_address()` helper 추가
  - API ready 직후 `/v1/capital/deposit/address?coin=USDT&network=test-ethereum` 호출
  - 첫 응답이 bc-gateway reserved address와 일치하는지 검증
  - 같은 user로 다시 호출했을 때 같은 address가 반환되는지 검증
- `docs/exchange-e2e.md`
  - deposit address assignment e2e coverage 설명 추가

이미 끝난 빠른 검증:
- `bash -n tools/e2e/run_exchange_e2e.sh` 통과
- `git diff --check` 통과

주의:
- full `tools/e2e/run_exchange_e2e.sh --reset`는 다시 끝까지 돌려야 한다.
- 이전 rerun은 user stop 요청 때문에 중간 중단되었고, 최종 `E2E exchange flow passed` 증거는 handoff에 없다.
- 중단 전에는 최소한 `ready: api`까지 도달했고, 새 deposit-address 검증 구간에서 실패 메시지는 없었다.
- 반드시 `git status --short`로 시작한다.
- 기존 사용자 변경을 절대 revert하지 않는다.
- e2e 실패 시 mock으로 우회하지 말고 실제 API/production path 기준으로 수정한다.

할 일:
1. `git status --short`
2. `tools/e2e/run_exchange_e2e.sh --reset`
3. 통과 시 `git diff --check`
4. `git add tools/e2e/run_exchange_e2e.sh docs/exchange-e2e.md`
5. `git commit -m "Verify Binance deposit address assignment"`

그 다음 추천:
Binance REST `/v3/order` same-owner crossing order self-trade prevention e2e 추가.
```
