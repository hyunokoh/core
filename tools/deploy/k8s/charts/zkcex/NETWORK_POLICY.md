# NetworkPolicy — zkCEX

Default posture: **deny all** ingress and egress within the `zkcex` namespace,
then re-enable specific edges.

## Allowed edges

```
                           +-----------------+
   internet ---ingress---> | ingress-nginx   |
                           +--------+--------+
                                    |
                                    v
                              +-----+-----+
                              |   proxy   |  (port 5500)
                              +-----+-----+
                                    |
        +--------+--------+---------+---------+---------+---------+--------+
        |        |        |         |         |         |         |        |
        v        v        v         v         v         v         v        v
     auth     chain     pol     zk-orderbk  ws-feed    perp    api-key   mcp ...
     5501     5502    5503       5660       6455      8091     5550     5560
        |        |        |         |         |         |         |
        +--------+--------+---------+---------+---------+---------+
                                    |
                          +---------+---------+
                          |        push        |  (auth + chain + ops)
                          +--------+-----------+
                                   |
                                   v
                          fan-out to clients (FCM/APNS via egress to public internet)

         Stateful tier
        +-------------+   +-----------+   +--------+   +---------+
        | postgresql  |   |  mariadb  |   | redis  |   |  kafka  |
        +------+------+   +-----+-----+   +---+----+   +----+----+
               ^               ^             ^             ^
               |               |             |             |
        auth,chain,api-key   wallet,kyc   ws-feed,proxy  order-engine,export
```

## Egress rules

| Service | Allowed egress |
|---------|----------------|
| `auth` | `postgresql:5432`, `push:5580`, DNS |
| `chain` | `auth:5501`, `postgresql:5432`, DNS, external RPC (constrained) |
| `zk-orderbook` | `ws-feed:6455`, `postgresql:5432`, `kafka:9092` |
| `ws-feed` | `redis:6379` |
| `perp`, `order-engine` | `ws-feed:6455`, `postgresql:5432`, `kafka:9092` |
| `mm-bot` | `proxy:5500` (places test orders), `ws-feed:6455` |
| `zkpol-bridge` | external `:443` to peer region (cross-cluster) |
| `pol-feed` | `postgresql:5432`, external chain RPC `:443` |
| `proxy` | every internal service |
| `*` | DNS to `kube-system/kube-dns` (always allowed) |

## Cross-namespace exceptions

- `ingress-nginx -> proxy` — only this edge is allowed from `ingress-nginx`
  namespace.
- `monitoring -> *:metrics` — when the Prometheus Operator chart is installed,
  add a NetworkPolicy that allows ingress on the metrics port from pods in the
  `monitoring` namespace. (Templated via `monitoring/servicemonitor.yaml`.)

## Verification

```bash
# Dry-run from one pod to another. If denied, NetworkPolicy is working.
kubectl -n zkcex run -it --rm probe --image=nicolaka/netshoot --restart=Never -- \
  sh -c "curl -sv http://auth:5501/auth/health"

# Expected: success for proxy -> auth, FAILURE for an unrelated pod -> postgres.
```
