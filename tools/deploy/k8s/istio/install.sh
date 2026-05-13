#!/usr/bin/env bash
# Install Istio onto the zkcex kind cluster.
#
# Prereqs:
#   - The kind cluster created by deploy/k8s/kind/up.sh must already exist.
#   - curl, kubectl in PATH.
#
# Idempotent: safe to re-run. Picks up where it left off.

set -euo pipefail

ISTIO_VERSION="${ISTIO_VERSION:-1.23.2}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADDONS_REF="${ADDONS_REF:-release-1.23}"

cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing $1" >&2; exit 1; }; }
cmd kubectl
cmd curl

# ---- 1. Ensure the kind cluster exists --------------------------------------
if command -v kind >/dev/null 2>&1; then
  if ! kind get clusters 2>/dev/null | grep -qx zkcex; then
    echo "ERROR: kind cluster 'zkcex' not found." >&2
    echo "       Bring it up first:  bash ${HERE}/../kind/up.sh" >&2
    exit 1
  fi
else
  echo "WARNING: 'kind' not on PATH. Assuming the current kubectl context already targets the zkcex cluster."
fi

kubectl cluster-info >/dev/null

# ---- 2. Download istioctl if missing ----------------------------------------
ISTIO_HOME=""
if ! command -v istioctl >/dev/null 2>&1; then
  echo ">> istioctl not found, downloading ${ISTIO_VERSION} to /tmp"
  (
    cd /tmp
    if [[ ! -d "istio-${ISTIO_VERSION}" ]]; then
      curl -fsSL "https://istio.io/downloadIstio" | ISTIO_VERSION="${ISTIO_VERSION}" sh -
    fi
  )
  ISTIO_HOME="/tmp/istio-${ISTIO_VERSION}"
  export PATH="${ISTIO_HOME}/bin:${PATH}"
fi
echo ">> istioctl: $(command -v istioctl)"
istioctl version --remote=false

# ---- 3. Install Istio control plane (default profile, lean sidecar) ---------
echo ">> installing istio control plane (profile=default)"
istioctl install --set profile=default --skip-confirmation \
  --set values.global.proxy.resources.requests.cpu=10m \
  --set values.global.proxy.resources.requests.memory=64Mi \
  --set values.global.proxy.resources.limits.cpu=200m \
  --set values.global.proxy.resources.limits.memory=256Mi

# ---- 4. Cluster-wide strict mTLS --------------------------------------------
echo ">> applying strict mTLS PeerAuthentication"
kubectl apply -f "${HERE}/peer-authentication-strict.yaml"

# ---- 5. Observability addons (Kiali + Jaeger + Prometheus exists already) ---
echo ">> installing addons: jaeger, kiali"
kubectl apply -f "https://raw.githubusercontent.com/istio/istio/${ADDONS_REF}/samples/addons/jaeger.yaml"
kubectl apply -f "https://raw.githubusercontent.com/istio/istio/${ADDONS_REF}/samples/addons/kiali.yaml"

# ---- 6. zkcex namespace + sidecar injection ---------------------------------
kubectl create namespace zkcex --dry-run=client -o yaml | kubectl apply -f -
kubectl label namespace zkcex istio-injection=enabled --overwrite

# ---- 7. Mesh-wide policies (authz + retries + canary) -----------------------
echo ">> applying retry policy + canary virtualservice + authorization policies"
kubectl apply -f "${HERE}/retry-policy.yaml"
kubectl apply -f "${HERE}/virtualservice-canary.yaml"
kubectl apply -f "${HERE}/authorization-policies/"

echo
echo "=== Istio installed ==="
istioctl version || true
kubectl -n istio-system get pods
echo
echo "Restart any pods that existed before sidecar injection was enabled:"
echo "   kubectl -n zkcex rollout restart deploy"
echo
echo "Dashboards (each runs in the foreground until you Ctrl-C):"
echo "   istioctl dashboard kiali"
echo "   istioctl dashboard jaeger"
echo "   istioctl dashboard prometheus"
