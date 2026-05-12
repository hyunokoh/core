#!/usr/bin/env bash
# Build every zkCEX service image and push to the configured registry.
#
#   bash build_all.sh                        # default registry 127.0.0.1:5050, tag 0.1.0
#   REGISTRY=registry.zkcex.io TAG=0.2.0 bash build_all.sh
#   PLATFORMS=linux/amd64 bash build_all.sh  # single-arch (faster local dev)
#
# Behaviour:
#   - PLATFORMS=linux/amd64,linux/arm64  -> multi-arch manifest, requires --push
#     (the docker engine cannot --load a fat manifest into the local image store).
#   - PLATFORMS=linux/<single>           -> single-arch, image is also --load'ed
#     so subsequent `docker run` works without pulling.
#
# The build context is the repo "core/" directory (one level up from deploy/)
# so each Dockerfile can COPY tools/<service>.py and deploy/images/healthcheck.py.
#
# Pre-flight (one-time, on x86 hosts that need to cross-build for arm64):
#   docker run --privileged --rm tonistiigi/binfmt --install all
# This installs QEMU user-mode emulators so buildx can run arm64 builds.
set -euo pipefail
cd "$(dirname "$0")"

REGISTRY="${REGISTRY:-127.0.0.1:5050}"
TAG="${TAG:-0.1.0}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
BUILDER_NAME="${BUILDER_NAME:-zkcex-builder}"
CONTEXT="$(cd ../.. && pwd)"

# Single-arch builds can use the host's default builder + --load. Multi-arch
# manifests have to go through buildx + --push (this is a docker limitation).
PLATFORM_COUNT=$(awk -F, '{print NF}' <<<"$PLATFORMS")
MULTI_ARCH=0
if [ "$PLATFORM_COUNT" -gt 1 ]; then
  MULTI_ARCH=1
fi

echo "==> Build context: ${CONTEXT}"
echo "==> Target:        ${REGISTRY} tag=${TAG}"
echo "==> Platforms:     ${PLATFORMS} (multi-arch=${MULTI_ARCH})"

# Ensure a buildx builder exists that supports every requested platform.
if ! docker buildx ls | grep -q "^${BUILDER_NAME}"; then
  echo "==> Creating buildx builder ${BUILDER_NAME}"
  docker buildx create --name "${BUILDER_NAME}" --use --platform "${PLATFORMS}"
else
  docker buildx use "${BUILDER_NAME}"
fi
docker buildx inspect "${BUILDER_NAME}" --bootstrap >/dev/null

# Base image. Single-arch builds --load into the docker daemon so service
# images can FROM zkcex-base:latest by name. Multi-arch builds emit a manifest
# list to the registry; service builds then FROM ${REGISTRY}/zkcex-base:${TAG}.
echo "==> Building base"
if [ "$MULTI_ARCH" -eq 1 ]; then
  docker buildx build --builder "${BUILDER_NAME}" \
    --platform "${PLATFORMS}" \
    -t "${REGISTRY}/zkcex-base:${TAG}" \
    --build-arg SERVICE=base \
    -f Dockerfile.base \
    --push \
    "${CONTEXT}"
  BASE_REF="${REGISTRY}/zkcex-base:${TAG}"
else
  docker buildx build --builder "${BUILDER_NAME}" \
    --platform "${PLATFORMS}" \
    -t zkcex-base:latest \
    -t "${REGISTRY}/zkcex-base:${TAG}" \
    --build-arg SERVICE=base \
    -f Dockerfile.base \
    --load \
    "${CONTEXT}"
  BASE_REF="zkcex-base:latest"
fi

# For multi-arch, service Dockerfiles must FROM the registry-qualified base
# (the buildx builder runs in its own image store). We sed the FROM line on
# the fly into a temp file.
build_service() {
  local f="$1"
  local svc="${f#Dockerfile.}"
  local tmpfile

  if [ "$MULTI_ARCH" -eq 1 ] && [ "$BASE_REF" != "zkcex-base:latest" ]; then
    tmpfile="$(mktemp)"
    # Replace the FROM line for this build only; original file is untouched.
    sed "s|FROM zkcex-base:latest|FROM ${BASE_REF}|" "$f" > "$tmpfile"
    docker buildx build --builder "${BUILDER_NAME}" \
      --platform "${PLATFORMS}" \
      -t "${REGISTRY}/zkcex-${svc}:${TAG}" \
      --build-arg "SERVICE=${svc}" \
      -f "$tmpfile" \
      --push \
      "${CONTEXT}"
    rm -f "$tmpfile"
  else
    docker buildx build --builder "${BUILDER_NAME}" \
      --platform "${PLATFORMS}" \
      -t "${REGISTRY}/zkcex-${svc}:${TAG}" \
      -t "zkcex-${svc}:${TAG}" \
      --build-arg "SERVICE=${svc}" \
      -f "$f" \
      --load \
      "${CONTEXT}"
  fi
}

count=0
for f in Dockerfile.*; do
  [ "$f" = "Dockerfile.base" ] && continue
  svc="${f#Dockerfile.}"
  echo "==> ${svc}"
  build_service "$f"
  count=$((count + 1))
done

# For single-arch, --load already put images in the local daemon; push to
# registry as a separate step so kind / docker compose can pull.
if [ "$MULTI_ARCH" -eq 0 ]; then
  echo "==> Pushing ${count} single-arch images to ${REGISTRY}"
  for f in Dockerfile.*; do
    [ "$f" = "Dockerfile.base" ] && continue
    svc="${f#Dockerfile.}"
    docker push --quiet "${REGISTRY}/zkcex-${svc}:${TAG}"
  done
fi

echo "==> Built ${count} services for ${PLATFORMS} (registry ${REGISTRY}, tag ${TAG})"
if [ "$MULTI_ARCH" -eq 1 ]; then
  echo "==> Inspect a manifest with: docker buildx imagetools inspect ${REGISTRY}/zkcex-pol-py:${TAG}"
fi
