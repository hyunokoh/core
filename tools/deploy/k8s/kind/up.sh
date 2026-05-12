#!/usr/bin/env bash
# Bring up a local kind cluster and install the zkcex chart.
#
# Prereqs (install with your package manager):
#   - docker  (https://docs.docker.com/get-docker/)
#   - kind    (https://kind.sigs.k8s.io/docs/user/quick-start/)
#   - kubectl (https://kubernetes.io/docs/tasks/tools/)
#   - helm    (https://helm.sh/docs/intro/install/)
#
# Idempotent: safe to re-run.

set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CHART_DIR="${SCRIPT_DIR}/../charts/zkcex"
CLUSTER_NAME="zkcex"

cmd() { command -v "$1" >/dev/null 2>&1 || { echo "ERROR: missing $1"; exit 1; }; }
cmd docker
cmd kind
cmd kubectl
cmd helm

if ! kind get clusters | grep -qx "${CLUSTER_NAME}"; then
  echo ">> creating kind cluster ${CLUSTER_NAME}"
  kind create cluster --name "${CLUSTER_NAME}" --config "${SCRIPT_DIR}/cluster.yaml" --wait 120s
else
  echo ">> kind cluster ${CLUSTER_NAME} already exists"
fi

# Make the local registry reachable from inside the kind network so node
# containerd can pull images that resolve to 127.0.0.1:5050 from the host.
# Idempotent: docker network connect is a no-op if already joined.
if docker ps --format '{{.Names}}' | grep -qx zkcex-registry; then
  docker network connect kind zkcex-registry 2>/dev/null || true
  echo ">> joined zkcex-registry to kind network"
else
  echo "!! zkcex-registry not running. Start it with:"
  echo "   bash ${SCRIPT_DIR}/../../images/init-registry.sh"
fi

kubectl config use-context "kind-${CLUSTER_NAME}"
kubectl cluster-info --context "kind-${CLUSTER_NAME}"

# Ingress controller (nginx)
echo ">> installing ingress-nginx"
kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.10.1/deploy/static/provider/kind/deploy.yaml || true
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=180s || echo "(ingress-nginx may still be coming up)"

# Namespace
kubectl create namespace zkcex --dry-run=client -o yaml | kubectl apply -f -

# Pull chart dependencies (Bitnami sub-charts). They are gated off by default.
echo ">> helm dependency update"
helm dependency update "${CHART_DIR}" || true

# Install / upgrade chart.
echo ">> helm upgrade --install"
helm upgrade --install zkcex "${CHART_DIR}" \
  --namespace zkcex \
  -f "${CHART_DIR}/values.yaml" \
  --set image.pullPolicy=IfNotPresent \
  --set monitoring.serviceMonitor.enabled=false \
  --set monitoring.podMonitor.enabled=false \
  --wait --timeout 5m || true

echo ">> rollout status (best effort; will fail if images don't exist in the kind node)"
for d in auth chain proxy ws-feed; do
  kubectl -n zkcex rollout status "deployment/${d}" --timeout=60s || true
done

echo ">> resources:"
kubectl -n zkcex get deploy,sts,svc,pdb,hpa,networkpolicy,ingress
