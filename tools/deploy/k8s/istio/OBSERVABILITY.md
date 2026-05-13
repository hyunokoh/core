# Istio observability for zkCEX

This document is a quick map of what each addon shows and how to reach it from
a developer laptop talking to the kind cluster.

## Kiali — service graph + health

```bash
istioctl dashboard kiali
```

Opens Kiali in the browser. Things to look at:

- **Graph -> Namespace `zkcex`** — a live topology of which workloads talk to
  which. Healthy edges are green; eject events show up as red dashes.
- **Workloads -> auth -> Inbound metrics** — per-source latency / error rate.
  Useful when an `AuthorizationPolicy` is silently blocking traffic.
- **Istio Config -> AuthorizationPolicy / PeerAuthentication / VirtualService /
  DestinationRule** — Kiali statically validates these and flags conflicts
  (e.g. two VirtualServices both claiming the same host/path).

## Jaeger — distributed traces

```bash
istioctl dashboard jaeger
```

Istio populates traces at the proxy hop level even without application-layer
instrumentation. A request that enters the cluster at the ingress will produce
spans like:

```
istio-ingressgateway
 -> proxy.zkcex
    -> auth.zkcex                (e.g. /auth/login)
       -> postgres.zkcex         (database call, only visible if PG has a
                                   sidecar, which we don't enable in kind)
    -> api-key.zkcex             (/apikey/verify)
```

Once the application services emit OpenTelemetry spans (planned in a
separate work stream), those spans will nest underneath the Envoy spans and
give per-function timing, DB queries, push notifications, etc.

To force a sampled trace through the mesh (default sampling is 1%):

```bash
curl -H 'x-b3-flags: 1' http://app.zkcex.io/v3/exchangeInfo
```

The `x-b3-flags: 1` header sets the "debug" bit, which Envoy honours by
always recording the trace.

## Prometheus + Grafana

Prometheus already runs in the cluster from the monitoring stack. Istio
exposes the standard `istio_requests_total`, `istio_request_duration_*`,
`istio_tcp_connections_*` metric families. Useful queries:

```promql
# Per-service error rate (5xx) over the last 5 minutes
sum by (destination_service_name) (
  rate(istio_requests_total{response_code=~"5.."}[5m])
)
/
sum by (destination_service_name) (
  rate(istio_requests_total[5m])
)

# p99 latency from proxy -> auth
histogram_quantile(
  0.99,
  sum by (le) (
    rate(istio_request_duration_milliseconds_bucket{
      reporter="source",
      source_workload="proxy",
      destination_service_name="auth"
    }[5m])
  )
)

# Circuit-breaker ejections
sum by (cluster_name) (
  increase(envoy_cluster_outlier_detection_ejections_active[5m])
)
```

## Cost / overhead notes

Istio's "default" profile is convenient but not lean:

- Each sidecar adds ~50-150MB RSS and 10-50m CPU steady-state. With ~30
  workloads in zkcex that's roughly 1.5-4.5 GB and 300m-1.5 vCPU of pure
  overhead. We've capped requests to 10m / 64Mi but real load will need more.
- Tracing at 100% sampling drives a serious cost in Jaeger storage; production
  should run at 1-5%.
- Prometheus cardinality from `istio_*` metrics can explode if you have many
  distinct `source_app` / `destination_app` pairs. Drop unused labels via
  `Telemetry` resources.

If those costs bite, **Ambient Mesh** (sidecar-less, uses per-node ztunnels)
is a path forward — same APIs, much lower per-pod overhead. It's GA on Istio
1.22+. We don't switch to it here because Ambient changes some semantics
around `PeerAuthentication` PORT-level overrides and we'd want to re-validate
the AuthorizationPolicies first.
