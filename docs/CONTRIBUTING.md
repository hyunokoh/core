# Contributing to zkCEX

This document covers everything you need to know to land a change: branch
policy, the CI/CD pipeline, required secrets, and the release cadence.

## Branch policy

- `main` is always deployable to staging. Every merge to `main`
  auto-deploys to the staging cluster via `deploy-staging.yml`.
- Feature branches: `feat/<short-slug>`, `fix/<short-slug>`, or `chore/<slug>`.
- Open a PR to merge into `main`. PRs require:
  - All required checks green (lint, test, build).
  - One review from a `CODEOWNERS` entry that covers the touched paths.
  - The PR template fully filled in (Summary, Test plan, Security review,
    Migration steps, Rollback plan, Linked issue).

## The CI/CD pipeline

| Workflow              | When                            | What                                                  |
|-----------------------|---------------------------------|-------------------------------------------------------|
| `lint.yml`            | PR + push to main               | ruff, ruff format, shellcheck, yamllint, helm lint    |
| `test.yml`            | PR + push to main               | pytest unit + Postgres-backed integration             |
| `build.yml`           | PR + push to main + `v*` tags   | Build 24 images, scan with Trivy on PRs, push on main |
| `deploy-staging.yml`  | push to main                    | `helm upgrade --install` to the staging cluster       |
| `e2e.yml`             | PR + after deploy-staging       | PR smoke Playwright, full staging Playwright          |
| `release.yml`         | `v*` tag                        | Build changelog, publish GitHub release               |

## Required secrets

The pipeline references these — set them in
**Settings -> Secrets and variables -> Actions**:

| Secret                | Used by                | Notes                                          |
|-----------------------|------------------------|------------------------------------------------|
| `GITHUB_TOKEN`        | build, release         | Auto-provided. Used to push to `ghcr.io`.      |
| `CODECOV_TOKEN`       | test                   | Codecov upload token.                          |
| `STAGING_KUBECONFIG`  | deploy-staging         | Base64-encoded kubeconfig for staging cluster. |

Required repository variables:

| Variable            | Used by | Notes                                      |
|---------------------|---------|--------------------------------------------|
| `STAGING_BASE_URL`  | e2e     | Public base URL for post-deploy Playwright. |

Optional, only needed if you add the corresponding step:

| Secret                | Used by             | Notes                                              |
|-----------------------|---------------------|----------------------------------------------------|
| `PROD_KUBECONFIG`     | (future) deploy-prod | Base64-encoded kubeconfig for production cluster. |
| `SLACK_WEBHOOK_URL`   | (future) notify     | Pipeline notifications.                            |

## Local development

```bash
# Set up Python deps.
python3 -m venv .venv && source .venv/bin/activate
pip install ruff==0.6.9 mypy==1.11 pytest pytest-cov pg8000

# Lint locally exactly as CI does.
ruff check tools/
ruff format --check tools/

# Run unit tests.
pytest tools/ -v -m "not integration and not live"

# Run integration tests (needs Postgres on 5433).
docker run --rm -d --name pg-it \
  -e POSTGRES_USER=app -e POSTGRES_PASSWORD=app-password \
  -e POSTGRES_DB=zkcex_auth -p 5433:5432 postgres:16
bash tools/run_integration_tests.sh
docker stop pg-it

# Run live zkPoL E2E only against a provisioned zkPoL/BulletinBoard env.
ZKPOL_E2E_REQUIRED=1 PYTHONPATH=tools pytest tools/tests/test_zkpol_e2e.py -v -m live
```

## Release cadence

- **Staging:** continuous (every push to `main`).
- **Production:** weekly cut, tagged `vYYYY.MM.DD-N`. The `release.yml`
  workflow builds the changelog from the previous tag's `^` and publishes
  a GitHub release. Production rollout is gated on a manual approval
  (see Settings -> Environments -> production).

## Adding a new service

1. Add `core/tools/<service>_server.py` (Python) or equivalent.
2. Add a Dockerfile at `core/deploy/images/Dockerfile.<service>`.
3. Add the service slug to the build matrix in
   `.github/workflows/build.yml`.
4. Add a Helm template under
   `tools/deploy/k8s/charts/zkcex/templates/`.
5. Add a `CODEOWNERS` entry if the service touches a sensitive boundary
   (auth, custody, compliance, trading).
