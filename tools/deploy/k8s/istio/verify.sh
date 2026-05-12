#!/usr/bin/env bash
# Sanity-check that Istio is wired up the way the install.sh script intends.
#
# Each section prints what it's checking and the relevant kubectl/istioctl
# output, and is best-effort: a failure in one section won't stop the rest.

set -uo pipefail

NS="${NS:-zkcex}"

heading() { printf "\n=== %s ===\n" "$*"; }

heading "Istio control plane"
kubectl -n istio-system get deploy
istioctl version || true

heading "mTLS strict mode (cluster-wide + zkcex)"
kubectl get peerauthentication -A
# Expected: default/STRICT in istio-system AND in zkcex.

heading "Sidecars injected"
# Each pod should report two containers: the workload + istio-proxy.
kubectl -n "$NS" get pods -o jsonpath='{range .items[*]}{.metadata.name}{" -> "}{.spec.containers[*].name}{"\n"}{end}'
echo
echo "Pods missing istio-proxy:"
kubectl -n "$NS" get pods -o json | \
  python3 -c '
import json, sys
for p in json.load(sys.stdin)["items"]:
    cs = [c["name"] for c in p["spec"]["containers"]]
    if "istio-proxy" not in cs:
        print(" ", p["metadata"]["name"], cs)
' 2>/dev/null || true

heading "mTLS TLS check (proxy -> auth)"
PROXY_POD="$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=proxy \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
if [[ -n "${PROXY_POD}" ]]; then
  istioctl x authz tls-check "${PROXY_POD}.${NS}" \
    "auth.${NS}.svc.cluster.local" || true
else
  echo "(no proxy pod yet, skipping)"
fi

heading "Authorization policies in zkcex"
kubectl -n "$NS" get authorizationpolicy

heading "VirtualServices + DestinationRules"
kubectl -n "$NS" get virtualservice
kubectl -n "$NS" get destinationrule

heading "Canary routing distribution (100 requests, no x-canary header)"
if kubectl -n "$NS" get svc proxy >/dev/null 2>&1; then
  TMP=$(mktemp)
  for _ in $(seq 1 100); do
    kubectl -n "$NS" run curl-canary-test --rm -i --quiet --restart=Never \
      --image=curlimages/curl:8.10.1 --command -- \
      curl -s -o /dev/null -w "%{http_code} %header{x-served-by}\n" \
      "http://proxy.${NS}.svc.cluster.local:5500/healthz" 2>/dev/null
  done >"$TMP" || true
  sort "$TMP" | uniq -c | sort -nr
  rm -f "$TMP"
  echo "(expect ~95 stable / ~5 canary if both subsets have endpoints)"
else
  echo "(proxy service not present yet)"
fi

heading "Outlier ejection state (per-host stats)"
if [[ -n "${PROXY_POD:-}" ]]; then
  istioctl proxy-config cluster "${PROXY_POD}.${NS}" \
    --fqdn "auth.${NS}.svc.cluster.local" -o json | \
    python3 -c '
import json, sys
clusters = json.load(sys.stdin)
for c in clusters:
    od = c.get("outlierDetection") or {}
    if od:
        print(c["name"], "->", od)
' 2>/dev/null || true
fi

heading "Sample workload TLS identity"
if [[ -n "${PROXY_POD:-}" ]]; then
  istioctl proxy-config secret "${PROXY_POD}.${NS}" 2>/dev/null | head -40 || true
fi

heading "istioctl analyze (mesh-wide)"
istioctl analyze -A || true

echo
echo "verify.sh done."
