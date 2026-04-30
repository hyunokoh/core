# zkCEX Exchange Test Hardening Implementation Plan

This plan is optimized for `zkcoder plan-sync` and `zkcoder run-next`.

## Phase 0
### Objective
Make zkCoder operate safely in this fork while prioritizing real exchange tests over zkPoL/zkAML integration work.

### Tasks
- Add repo-local zkCoder scaffolding: `TASK_BRIEF.md`, `.zkcoder/project.json`, `.zkcoder/scripts/run-agent.sh`, and `.zkcoder/scripts/verify.sh`.
- Keep verification focused on real OPEX exchange behavior: matching, accountant, wallet, market, Kafka, Postgres, and app-level integration.
- Preserve user and prior integration work; do not revert unrelated dirty files.

### Exit Criteria
- `zkcoder brief`, `zkcoder plan-sync`, and `zkcoder next` run from the repo root.
- The verifier runs the highest-value exchange test command available from the local environment.

## Phase 1
### Objective
Make app-level Testcontainers work with current Docker Desktop and remove in-memory Kafka test binder shortcuts.

### Tasks
- Upgrade stale Testcontainers dependencies in `accountant-app` and `wallet-app`.
- Remove `TestChannelBinderConfiguration` from app integration tests.
- Verify that app tests use real Kafka/Postgres Testcontainers rather than in-memory binders.

### Exit Criteria
- `accountant-app` context tests no longer fail at Docker environment discovery.
- `wallet-app` context tests no longer fail at Docker environment discovery.

## Phase 2
### Objective
Replace app-level mocks with real Spring beans and real container-backed dependencies.

### Tasks
- Remove `@MockBean` usage in wallet and accountant app integration tests where the real service can be wired.
- Replace Mockito setup with database fixtures and actual service calls.
- Replace MockServer wallet proxy tests with a real wallet app or local real HTTP service when feasible.

### Exit Criteria
- `rg -n "MockBean|Mockito|mockk|MockServer|TestChannelBinderConfiguration" wallet/wallet-app/src/test accountant/accountant-app/src/test` returns no app-level mock seams except explicitly justified external-boundary adapters.

## Phase 3
### Objective
Add a real exchange smoke test that proves the core CEX flow.

### Tasks
- Create deterministic fixtures for users, currencies, wallets, balances, and trading pair configuration.
- Submit matching bid/ask orders through the real gateway/engine/accountant path.
- Assert financial actions, wallet balances, order status, trade output, and market visibility.

### Exit Criteria
- A single verification command fails if wallet balance movement, order matching, accountant settlement, or market projection breaks.

## Phase 4
### Objective
Reduce lower-level mock-heavy tests by adding real repository/service coverage.

### Tasks
- Add container-backed repository tests for accountant persister behavior currently tested only with `mockk`.
- Add container-backed repository tests for wallet persister behavior currently tested only with `mockk`.
- Add container-backed repository tests for market persister/query behavior currently tested only with `mockk`.
- Keep unit tests only for pure calculations and impossible-to-container external edges.

### Exit Criteria
- Core/persister tests cover real DB interactions for the main exchange state transitions.
