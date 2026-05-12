# CI/CD reference

End-to-end documentation for the GitHub Actions pipeline.

## Files

```
.github/
  workflows/
    lint.yml             # ruff / shellcheck / actionlint / yamllint / helm lint
    test.yml             # pytest unit + integration
    exchange-e2e.yml     # Docker-backed exchange E2E
    zkpol-live-e2e.yml   # manual zkPoL live E2E gate
    build.yml            # 24-service matrix image build + Trivy
    deploy-staging.yml   # helm upgrade --install after successful main build
    e2e.yml              # Playwright suite (PR + post-deploy)
    release.yml          # build changelog, publish GitHub release on v* tag
  dependabot.yml         # weekly dep upgrades (actions, pip, docker)
  CODEOWNERS             # mandatory reviewers per path
  pull_request_template.md
```

## Workflow details

### `lint.yml`

Runs five independent jobs (parallel). Failures gate the merge:

- **ruff**: blocking `ruff check tools/` + `ruff format --check tools/`.
  Pinned to `ruff==0.6.9`. The job also runs blocking Ruff security checks
  with `ruff check tools/ --select S --statistics`.
- **shellcheck**: scans every `*.sh` file. `SC2086` is suppressed because
  several of the deploy scripts intentionally word-split.
- **actionlint**: validates GitHub Actions workflow syntax, expressions, and
  embedded shell fragments with `rhysd/actionlint:1.7.7`.
- **yaml-lint**: lints `.github/`, `tools/observability/`, and `deploy/`.
- **helm-lint**: `helm lint tools/deploy/k8s/charts/zkcex`.

### `test.yml`

Two jobs:

- **unit**: runs `pytest tools/ -m "not integration and not live"` with
  coverage; uploads to Codecov.
- **integration**: spins up Postgres 16 as a service container on
  `localhost:5433`, then runs `tools/run_integration_tests.sh`, which
  starts `auth_server.py` on a known port and drives it via HTTP. The
  harness fails if `auth_server` does not become healthy, so the job cannot
  pass through pytest skips alone.

### `zkpol-live-e2e.yml`

Manual production-readiness gate for the zkCEX -> zkPoL -> BulletinBoard
path. It requires these repository variables/secrets:

- `ZKPOL_E2E_WALLET_BASE`
- `ZKPOL_E2E_BRIDGE_BASE`
- `ZKPOL_E2E_ANCHOR_BASE`
- `ZKPOL_E2E_USER`
- `BULLETIN_BOARD_ADDRESS`
- `ZKCEX_TEST_BEARER` secret

The workflow sets `ZKPOL_E2E_REQUIRED=1`, so missing live services fail the
job instead of producing skipped tests.

### `exchange-e2e.yml`

Docker-backed exchange production gate. It runs
`tools/e2e/run_exchange_e2e.sh --package --build --reset` on Ubuntu, building
the Java apps and local E2E images before exercising the real wallet,
accountant, matching, market, eventlog, API, bc-gateway, Kafka, Postgres, Vault,
and Consul path.

Failure diagnostics and cleanup use the `full-stack` compose profile so profiled
services are included in `ps`, logs, and `down --volumes --remove-orphans`.

### `build.yml`

A 24-service matrix that builds each Dockerfile in
`deploy/images/`. On PRs, Trivy scans the built image for
HIGH/CRITICAL findings (fails the job on any non-ignored hit). On
`main` pushes the image gets pushed to `ghcr.io/<owner>/zkcex-<svc>:<sha>`.

To add a service to the matrix:

1. Add the slug under `jobs.build.strategy.matrix.service`.
2. Make sure `deploy/images/Dockerfile.<slug>` exists.

### `deploy-staging.yml`

Triggered after the `build.yml` workflow completes successfully on `main`, or
manually through `workflow_dispatch`. This avoids deploying a Git SHA before
the matching images have been pushed. Steps:

1. Install helm 3.16.1 and kubectl v1.30.0.
2. Decode `STAGING_KUBECONFIG` into `~/.kube/config`.
3. `helm upgrade --install` the chart with `image.tag=<build head SHA>`.
4. Wait for `auth`, `chain`, `proxy` rollouts.
5. Run an in-cluster `curlimages/curl` smoke against
   `/auth/health` through the proxy service.

### `e2e.yml`

Runs a cheap Playwright smoke pass on PRs by starting the local static
homepage proxy and testing `tools/tests/e2e/tests/smoke` against
`http://127.0.0.1:5500`. After `deploy-staging.yml` completes, it runs the
full `tools/tests/e2e` suite against `STAGING_BASE_URL`. Post-deploy runs
checkout the `deploy-staging` head SHA so tests and deployed code stay aligned.

Set repository variable `STAGING_BASE_URL` in
**Settings -> Secrets and variables -> Actions -> Variables** before relying
on post-deploy browser E2E as a staging gate. Reports are uploaded as
artifacts on every run, including failures.

### `release.yml`

Triggered on `v*` tags. Builds a markdown changelog from
`git log` since the previous tag and publishes a GitHub release that
attaches `CHANGELOG.md` and `Chart.yaml`. The workflow rejects a release tag
whose target commit is not contained in `origin/main`.

## Bumping the K8s deployment image tag

Two paths:

- **Automatic (staging):** every successful `build.yml` run on `main` triggers
  `deploy-staging.yml`, which sets `--set image.tag=<build head SHA>`.
- **Manual (any env):** from the cluster:
  ```bash
  helm upgrade --install zkcex \
    tools/deploy/k8s/charts/zkcex \
    --namespace zkcex-staging \
    -f tools/deploy/k8s/charts/zkcex/values-region-a.yaml \
    --set image.tag=<sha-or-tag> \
    --wait --timeout 10m
  ```

## Caching strategy

- **pip** is cached automatically by `actions/setup-python@v5` keyed on
  `setup.py` / `requirements*.txt` hashes.
- **Docker buildx** uses GitHub Actions cache backend
  (`cache-from: type=gha`, `cache-to: type=gha,mode=max`).
- **Playwright browsers** are cached at `~/.cache/ms-playwright`, keyed
  on `tools/tests/e2e/package-lock.json`.
- **npm** modules are cached by `actions/setup-node@v4`.

## What this pipeline deliberately does not do

- **Sign images.** A production pipeline would sign every pushed image
  (cosign + Fulcio) and verify signatures at deploy. Out of scope here.
- **Rotate secrets.** `STAGING_KUBECONFIG` is read straight from the
  secret store; production should pull short-lived OIDC-issued
  credentials instead.
- **Promote across multiple environments.** Today: only staging.
  Production deploy is intentionally manual.
- **Track pipeline SLOs.** A real pipeline should publish job duration
  metrics and alert on regressions; this one does not.
