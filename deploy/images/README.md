# zkCEX container images

Reproducible OCI images for every Python service in `tools/`, hosted on a
local Docker registry so the kind cluster (and `docker run`) can pull them
without internet access.

## Layout

```
deploy/images/
  Dockerfile.base            distroless python3 (Debian 12) + pg8000, uid 65532
  Dockerfile.<service>       one per service, FROM zkcex-base:latest
  healthcheck.py             stdlib HTTP probe baked into the base image
  registry-compose.yml       local Docker registry on 127.0.0.1:5050
  init-registry.sh           start the registry, wait for health
  build_all.sh               build + push every image (multi-arch by default)
  Makefile                   make registry / base / all / push / multi-arch /
                             scan / sbom / catalog / clean
  DISTROLESS.md              trade-offs, debugging, adding a service
  sboms/                     generated SPDX JSON when `make sbom` is run
```

Build context is the repo `core/` directory (one level up from `deploy/`).
Every Dockerfile uses `COPY --chown=zkcex:zkcex tools/<svc>.py /app/tools/<svc>.py`
so the in-source `HERE = dirname(__file__)` + `sys.path.insert(0, HERE)` pattern
in each server keeps importing siblings (providers, asset_precisions, etc.).

## Quick start

```bash
# 1) stand up local registry
bash deploy/images/init-registry.sh
curl -s http://127.0.0.1:5050/v2/        # {}

# 2) build base + every service + push
make -C deploy/images push

# 3) check catalog
make -C deploy/images catalog

# 4) deploy to kind
bash tools/deploy/k8s/kind/up.sh
```

## Images built (24)

| Service             | Image                                | Port(s)     | Health probe path        |
|---------------------|--------------------------------------|-------------|--------------------------|
| auth                | zkcex-auth                           | 5501        | /auth/health             |
| chain               | zkcex-chain                          | 5502        | /chain/health            |
| pol-py              | zkcex-pol-py                         | 5503        | /pol/server-info *       |
| zkpol-bridge        | zkcex-zkpol-bridge                   | 5504        | /bridge/health           |
| pol-feed            | zkcex-pol-feed                       | 5505        | /health                  |
| ws-feed             | zkcex-ws-feed                        | 5510        | /health                  |
| custody-signer      | zkcex-custody-signer                 | 5520-5524   | /health                  |
| custody-coord       | zkcex-custody-coord                  | 5530        | /health                  |
| export              | zkcex-export                         | 5540        | /export/health           |
| api-key             | zkcex-api-key                        | 5550        | /api-keys/health         |
| mcp                 | zkcex-mcp                            | 5560        | /mcp/health              |
| order-engine        | zkcex-order-engine                   | 5570        | /orders/health           |
| push                | zkcex-push                           | 5580        | /push/health             |
| perp                | zkcex-perp                           | 5590        | /fapi/v1/ping *          |
| mm-bot              | zkcex-mm-bot                         | 5600        | /mm/health               |
| safu                | zkcex-safu                           | 5601        | /safu/health             |
| ops                 | zkcex-ops                            | 5620        | /health                  |
| travel-rule         | zkcex-travel-rule                    | 5630        | /travel-rule/health      |
| metrics-aggregator  | zkcex-metrics-aggregator             | 5640        | /health                  |
| pagerduty-webhook   | zkcex-pagerduty-webhook              | 5641        | /health                  |
| zk-orderbook        | zkcex-zk-orderbook                   | 5660        | /zk-trade/health         |
| backup              | zkcex-backup                         | 5670        | /health                  |
| waf                 | zkcex-waf                            | 5499        | /waf/health              |
| proxy               | zkcex-proxy                          | 5500        | /                        |

`*` = the service does not expose a dedicated `/health`; we probe the cheapest
public GET that returns 200 when the process is up.

## Image properties

- Base: `gcr.io/distroless/python3-debian12:nonroot` (Python 3.11).
- Non-root: uid 65532, name `nonroot`, no shell at all.
- PID-1: `python3` itself (no tini — distroless has no shell to spawn one).
- `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`,
  `PYTHONPATH=/app/site-packages:/app/tools`, `HOME=/home/nonroot`.
- One pip dependency total: `pg8000==1.31.5`, installed into
  `/app/site-packages` by the base image builder stage. Only the `auth`
  service actually imports it.
- Each service image is ~96 MB (distroless python3 itself is 92.4 MB; service
  code adds 1-4 MB). That's a ~56 % reduction from the old python-slim base.
- HEALTHCHECK fires every 15s with `python /app/healthcheck.py <port> <path>`
  (a stdlib-only HTTP probe; see `healthcheck.py`).
- Multi-arch: linux/amd64 + linux/arm64 manifest lists produced by buildx.
- See `DISTROLESS.md` for trade-offs, debug workflow, and how to add a new
  service.

## Helm chart wiring

`tools/deploy/k8s/charts/zkcex/values.yaml` defaults:

```yaml
image:
  repository: 127.0.0.1:5050
  tag: "0.1.0"
  pullPolicy: IfNotPresent
```

The helper `zkcex.image` renders `{{repository}}/zkcex-{{name}}:{{tag}}`,
with per-service `services.<name>.image.{repository,name,tag}` overrides.
Two such overrides are wired today:

- `services.pol.image.name: pol-py` (chart name `pol`, image `zkcex-pol-py`)
- `services.custody-coordinator.image.name: custody-coord`

Override the whole registry for prod:

```bash
helm upgrade --install zkcex tools/deploy/k8s/charts/zkcex \
  --set image.repository=registry.internal.zkcex.io/zkcex \
  --set image.tag=0.2.0
```

## kind cluster wiring

`tools/deploy/k8s/kind/cluster.yaml` includes a `containerdConfigPatches` block
that mirrors `127.0.0.1:5050` to `http://zkcex-registry:5000` so nodes resolve
the registry by its container name on the shared docker network. `up.sh` runs
`docker network connect kind zkcex-registry` (idempotent) before installing
the chart.

If you change the registry hostname, regenerate the kind cluster:

```bash
kind delete cluster --name zkcex
bash tools/deploy/k8s/kind/up.sh
```

## Image scanning + SBOM

`make scan` and `make sbom` run `trivy image` and `syft` against every pushed
image when those tools are installed. Install on macOS:

```bash
brew install trivy syft
```

Neither tool is invoked unless present. We do not fake the output.

## Known: 127.0.0.1 bind in some upstream services

Several services in `tools/` bind to `("127.0.0.1", port)` rather than `0.0.0.0`.
The in-container HEALTHCHECK still passes (it loops back to 127.0.0.1 inside
the container's network namespace), but `kubectl port-forward` / `Service`
traffic from outside the pod will NOT reach the process. Affected:
`chain_server.py`, `ws_feed.py`, `zkpol_bridge.py`, others use `("", port)`
which IS 0.0.0.0.

Resolution belongs upstream in each server (`BIND_HOST` env var honored
in `ThreadingServer((BIND_HOST, port), ...)`). Out of scope for this image
work — the images themselves run the code as written.

## Production differences

- **TLS + auth on the registry**: real prod runs `registry:2` behind nginx
  with `HTTPS=on` and either basic auth (htpasswd) or OIDC; the
  `--insecure-registry` knob in `/etc/docker/daemon.json` we'd need on every
  node here goes away.
- **CVE gate in CI**: `trivy image --exit-code 1 --severity HIGH,CRITICAL`
  is now wired into `.github/workflows/build.yml` for PRs.
- **Image signing**: `cosign sign --key cosign.key <registry>/zkcex-*:tag`
  and a Kyverno `verifyImages` policy that refuses unsigned/untrusted images
  in-cluster. Not wired here.
- **SBOM upload**: SPDX JSON gets attached as an OCI artifact via
  `cosign attach sbom`, then ingested by Dependency-Track for drift tracking.
- **Build attestation / provenance**: `docker buildx build --attest type=provenance`
  records SLSA-v1 build metadata. Disabled here to keep the local build fast.
- **Multi-arch**: enabled. `bash build_all.sh` with the default
  `PLATFORMS=linux/amd64,linux/arm64` produces fat manifests pushed to the
  registry. Set `PLATFORMS=linux/amd64` for single-arch local dev (faster,
  `--load`'s into the docker daemon).
