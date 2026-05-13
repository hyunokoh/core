# zkCEX end-to-end test suite

Playwright-based browser tests covering the customer signup → KYC → trade →
withdraw arc plus the privacy-trading, futures, PoL-verify, and operator
review flows.

## Prerequisites

* Node 20+ (the suite is type-checked against `@types/node` v20)
* The zkCEX stack running and reachable at `http://localhost:5500`
  (see `tools/run_*` scripts and `docker-compose.local.yml`).
* Chromium / WebKit browsers — installed automatically by
  `npx playwright install --with-deps`.

## Quick start

```bash
cd core/tools/tests/e2e
npm ci
npx playwright install --with-deps

# All projects, all specs:
npm test

# Just the smoke layer:
npm run test:smoke

# Just customer flows:
npm run test:customer

# Pretty HTML report from the last run:
npm run report
```

## Project layout

```
fixtures/      Reusable test-setup helpers (auth, chain seeding, constants)
helpers/       Lower-level API + page-object code shared across specs
tests/
  smoke/       Page-load + JS-error checks for every static route
  customer/    Buyer-side flows (signup, KYC, deposit, trade, withdraw, ...)
  operator/    Ops-console flows (KYC approval, withdraw approval)
```

## Configuration

The base URL is read from `BASE_URL` (default `http://localhost:5500`).
For an alternate stack:

```bash
BASE_URL=https://stg.example.invalid npm test
```

CI sets `CI=1`, which raises retries to 2 and bumps workers to 2.

## Execution model

`fullyParallel: false` is intentional. Many specs mutate live state
(create users, seed deposits, flip KYC). They are isolated *across* specs
because each spec invents its own user with a `e2e+<tag>-<ts>-<rand>@`
email prefix, but a single spec is multi-step and should not interleave
with itself. Projects (chromium / webkit / mobile-chromium) still run
in parallel.

## Skip semantics

Several specs guard against environment differences and **skip** rather
than fail when an optional service isn't wired up:

* `verify-balance` skips when `/pol/latest-epoch` 404s (PoL snapshot
  feed not running).
* `install-prompt` skips on WebKit / Firefox — `beforeinstallprompt` is
  Chromium-only.
* `operator/*` skips when the ops console requires a login we don't
  have credentials for (typical for fresh local stacks; operators are
  minted out-of-band via `ops_bootstrap.py`).

## Adding a new test

1. Decide the tier: `smoke/`, `customer/`, or `operator/`.
2. Use a fixture for user setup; do **not** click through signup +
   KYC manually from a new spec — use `createTestUserAndLogin()` with
   `verifyKyc: true`.
3. Prefer page objects in `helpers/page-objects/`. Inline selectors are
   fine for one-off assertions but anything reused belongs in an object.
4. Use unique e2e emails from `makeE2EEmail(tag)` so two suites can
   safely run against the same stack.

## Debugging

* `npm run test:ui` opens the Playwright UI runner (time-travel + DOM
  snapshots per step).
* `npx playwright test path/to/spec.ts --debug` opens the Playwright
  inspector against a single spec.
* HTML report: `playwright-report/index.html`.
* JUnit results: `reports/junit.xml` (CI consumes this).

## What this suite does **not** cover

* Visual regression — see issue tracker for the snapshot suite that
  belongs alongside this one.
* Performance budgets — Lighthouse runs separately.
* Per-test data isolation in shared stacks — every spec uses a unique
  email but they all share the same matching engine / wallet state.
  For a production-grade environment, run this suite against a
  per-PR ephemeral stack.
* Synthetic prod monitoring — these tests are CI-gates, not uptime
  checks.
