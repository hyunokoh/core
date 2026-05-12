# Production Readiness Audit

Status: not production-complete.

Current completion decision: the local codebase has been hardened and the
feasible local gates pass, but this worktree has not yet been pushed and
verified by hosted GitHub Actions. Final production sign-off requires green
hosted `exchange-e2e.yml`, green `build -> deploy-staging -> e2e`, and one
green `zkpol-live-e2e.yml` run against provisioned live zkPoL/BulletinBoard,
wallet, bridge, and anchor services.

This audit maps the production-readiness goal to concrete repo artifacts and
verification evidence. It is intentionally strict: a light-profile or local
smoke pass is demo/MVP evidence, not a final production sign-off.

## Success Criteria

1. Real exchange E2E path exists and exercises the production service path.
2. Demo/MVP path can run on the constrained local environment.
3. CI gates are present, parse, and point at real repo paths.
4. Blocking lint/test gates pass locally where feasible.
5. Known production gaps are explicit instead of hidden behind green smoke tests.

## Prompt-To-Artifact Checklist

| Requirement | Evidence | Current Result |
| --- | --- | --- |
| Real Docker-backed Exchange E2E | `tools/e2e/run_exchange_e2e.sh`, `tools/e2e/docker-compose.e2e.yml`, `docs/exchange-e2e.md` | Full profile is implemented and wired into CI. Kafka/Zookeeper have been moved to multi-arch Confluent Platform 7.6.1 images so Apple Silicon no longer needs amd64 emulation for those services after rebuild. The 2026-05-12 local full run passed with `--package --build --reset`. |
| Kafka/Zookeeper image portability | `docker-images/kafka/Dockerfile`, `docker-compose.yml`, `tools/e2e/docker-compose.e2e.yml` | `confluentinc/cp-kafka:7.6.1` and `confluentinc/cp-zookeeper:7.6.1` publish amd64 and arm64 manifests. The custom Kafka image builds locally on arm64; the temporary verification image was removed after the check. |
| Demo/MVP validation | `tools/e2e/run_exchange_e2e.sh --light-profile --reset` | Previously passed with `ready: light profile signed API core checks`. Light profile is allowed on arm64 as demo/MVP evidence and has a Docker memory preflight, but it is not production sign-off. |
| Production E2E gate | `.github/workflows/exchange-e2e.yml` | Runs `tools/e2e/run_exchange_e2e.sh --package --build --reset` on Ubuntu for relevant PR/main changes and uploads logs/artifacts on failure. A local full run on 2026-05-12 passed after building the stack and exercising restart recovery, Kafka outage/recovery, Postgres recovery, replay/idempotency, DLT, multi-symbol, and concurrency paths. Hosted CI still needs to be observed green before final release sign-off. |
| CI path correctness | `.github/workflows/*.yml`, `.github/dependabot.yml`, `.github/CODEOWNERS`, `docs/CI_CD.md` | Workflow YAML parses and `actionlint` passes. Root-relative paths were corrected from stale `/core/...` values where applicable. |
| Docker Compose config | `docker-compose*.yml`, `tools/e2e/docker-compose.e2e.yml` | Compose `version:` warnings were removed. Default E2E compose config renders successfully; OTC compose config renders successfully when its required deployment `.env` is present. |
| Legacy CI compatibility | `.github/workflows/pr.yml`, `main.yml`, `dev.yml`, `opex-test.yml`, `main-otc.yml`, `dev-otc.yml` | Updated to current checkout/setup-java/login actions and Temurin JDK 21. |
| Release tag guard | `.github/workflows/release.yml` | `v*` release tags must point at commits contained in `origin/main`; tags on unmerged commits fail before release publication. |
| Python blocking lint | `pyproject.toml`, `.github/workflows/lint.yml` | `python3 -m ruff check tools/` passes. `python3 -m ruff format --check tools/` passes. |
| Python tests | `tools/tests`, `pyproject.toml`, `.github/workflows/test.yml` | Unit CI excludes live-service tests with `pytest tools/ -m "not integration and not live"`; local verification passed with `74 passed, 3 deselected`. |
| Auth integration CI | `.github/workflows/test.yml`, `tools/run_integration_tests.sh` | CI starts Postgres and `auth_server.py`; the harness now fails if `auth_server` never becomes healthy, preventing false-green skipped integration runs. |
| zkPoL live E2E gate | `.github/workflows/zkpol-live-e2e.yml`, `tools/tests/test_zkpol_e2e.py` | Live zkPoL/BulletinBoard/wallet/anchor E2E is separated from unit CI. The workflow requires live endpoint variables and bearer secret, sets `ZKPOL_E2E_REQUIRED=1`, and fails if prerequisites are absent instead of recording a skipped green. |
| Staging deploy ordering | `.github/workflows/build.yml`, `.github/workflows/deploy-staging.yml` | Staging deploy now runs after successful `build.yml` completion on `main` and deploys the build workflow head SHA, preventing Helm from referencing images before they are pushed. |
| Post-deploy browser E2E alignment | `.github/workflows/e2e.yml` | Workflow-run Playwright checks checkout the `deploy-staging` head SHA, keeping test code aligned with the deployed image tag. |
| Helm chart lint | `tools/deploy/k8s/charts/zkcex`, `.github/workflows/lint.yml` | `helm lint tools/deploy/k8s/charts/zkcex` passed via a disposable `alpine/helm:3.16.1` container. CI also installs Helm 3.16.1 and runs the same lint. |
| Shell syntax | `tools/e2e/run_exchange_e2e.sh`, `tools/run_integration_tests.sh`, `tools/auth_server_pg_bootstrap.sh` | `bash -n` passes. |
| Diff hygiene | repo worktree | `git diff --check` passes. |
| Security audit gate | `.github/workflows/lint.yml`, `pyproject.toml` | Bandit/Ruff security findings are now blocking in CI via `ruff check tools/ --select S --statistics`. The repo-wide `tools/` security baseline is clean under `--select S`. |

## Remaining Production Blockers

- Hosted CI must show a green production E2E run on the target Ubuntu runner before final release sign-off. The equivalent local full run passed on 2026-05-12.
- The zkPoL live E2E workflow now exists, but final production sign-off still needs one green run against a provisioned zkPoL/BulletinBoard/wallet/anchor environment with repository variables/secrets configured.

## Closed Hardening Items

- Ruff Bandit security findings are triaged to zero for `tools/` and the CI lint workflow treats them as blocking.
- E2E Kafka/Zookeeper images are multi-arch (`confluentinc/cp-*:7.6.1`), removing the previous forced amd64 emulation path after rebuild.
- E2E Postgres services now inherit the `TAG` image selection from the shared compose anchor, so `--package --build --reset` uses the locally built `postgres-opex:e2e` image instead of an untagged registry image.
- E2E full-profile arm64 runs still fail fast if a local override reintroduces amd64 platform pins; light profile remains available for constrained demo/MVP validation.
- E2E Docker memory preflight now rejects obviously under-provisioned runs before the stack starts.
- Exchange E2E CI failure logging and cleanup now enable the full-stack compose profile, so profiled services such as `api`, `bc-gateway`, and `auth` are included in diagnostics and `down --volumes --remove-orphans`.
- GitHub Actions workflows are now covered by a blocking `actionlint` CI job; existing workflow shell fragments were cleaned up to pass it.
- Release workflow now rejects tags that do not point at `origin/main` history.
- Auth integration CI now fails on `auth_server` readiness failure instead of passing through pytest skips.
- Unit CI no longer treats live-service skips as unit success; integration and live tests are marker-gated.
- zkPoL live E2E now has a manual workflow with required-mode failure semantics instead of being only executable documentation.
- Staging deploy waits for successful image build workflow completion before using the commit SHA as the Helm image tag.
- Post-deploy Playwright checks use the deployed workflow head SHA instead of an unrelated default checkout.
- Helm chart lint passed locally through a disposable Helm container, then the pulled image was removed.

## Latest Local Verification

```bash
python3 -m ruff check tools/
python3 -m ruff format --check tools/
python3 -m pytest tools/ -q
python3 -m pytest tools/ -q -m "not integration and not live"
python3 tools/backup/crypto.py
bash -n tools/e2e/run_exchange_e2e.sh tools/run_integration_tests.sh tools/auth_server_pg_bootstrap.sh
git diff --check
python3 -m ruff check tools/ --select S --statistics
docker run --rm -v "$PWD:/work" -w /work alpine/helm:3.16.1 lint tools/deploy/k8s/charts/zkcex
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7
docker build -t zkcex-kafka-e2e-verify:tmp docker-images/kafka
tools/e2e/run_exchange_e2e.sh --package --build --reset
python3 -m ruff check tools/auth_server.py tools/auth_db.py tools/chain_server.py tools/serve_homepage.py tools/waf.py tools/api_key_server.py tools/order_engine.py tools/fee_engine.py tools/perp_engine.py tools/staking.py tools/lending.py tools/referral.py tools/nft_marketplace.py tools/zkpol_bridge.py tools/export_server.py tools/mcp_server.py tools/pol_server.py tools/safu_server.py tools/notification_center.py tools/ws_feed.py tools/metrics_aggregator.py tools/dr/game_day.py tools/ops_server.py tools/custody tools/travel_rule tools/travel_rule_server.py tools/backup/crypto.py tools/backup/restore.py tools/backup/postgres_backup.py --select S
```

Results:

- Blocking Ruff check: passed.
- Ruff format check: passed.
- Pytest: `74 passed, 3 skipped`.
- Unit-only pytest marker gate: `74 passed, 3 deselected`.
- zkPoL live E2E required-mode preflight: verified to fail when wallet/anchor prerequisites are absent, preventing false-green skips in the live workflow.
- Backup crypto self-test: passed.
- Bash syntax: passed.
- Diff whitespace check: passed.
- Critical-path security audit: passed.
- Repo-wide security audit: passed (`0` findings under `--select S`).
- Helm lint: passed (`1 chart(s) linted, 0 chart(s) failed`; chart icon recommended).
- Actionlint: passed across `.github/workflows`.
- Kafka image portability: Confluent 7.6.1 Kafka/Zookeeper manifests include amd64 and arm64; custom Kafka image build passed on arm64 and the temporary image was removed.
- Full Exchange E2E local arm64 run: passed with `tools/e2e/run_exchange_e2e.sh --package --build --reset`. It built and started the stack, passed broker readiness, topic creation, Vault/wallet/core readiness, consumer group checks, matching-engine/wallet/accountant/matching-gateway/core restart recovery, Kafka-down rejection/restart recovery, Postgres restart recovery, negative overreserve rejection checks, replay/idempotency checks, DLT poison-record handling, secondary-symbol routing, concurrency checks, and overfill protection. Temporary E2E containers and `:e2e` images were removed after verification.
