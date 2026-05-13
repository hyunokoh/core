# Travel Rule transport adapters

This package provides pluggable transports for posting IVMS 101 messages
to a counterparty VASP, plus a polling inbox for inbound messages from
the same providers. The transport is selected at boot by the
`TR_PROVIDER` environment variable.

## Architecture

```
                    +-----------------------------+
                    |   tools/travel_rule_server  |
                    |   (port 5630, HTTP API)     |
                    +--+---------------+----------+
                       |               |
       /screen (loopback)               /inbound (signed)
       /admin/retry/<id>                +-> ed25519_verify
       /admin/deliveries                    against tr_vasp_directory
                       |                    rate-limit 100/min/vasp
                       v
                +------+--------+
                |  TR_ADAPTER   |     <- get_adapter() via TR_PROVIDER
                +--+--+--+--+---+
                   |  |  |  |
        +----------+  |  |  +-------------+
        |             |  |                |
   +----+----+   +----+--+--+   +--------+----+   +-----+----+
   | SumsubAdapter | NotabeneAdapter | TrisaAdapter | StubAdapter |
   +-------+-------+--------+--------+------+-------+------+----+
           |                |               |              |
   https://api.sumsub.com   |               |              (no network)
   /resources/travelRule/   |               |
   transactions             |               |
                            |               |
                  https://api.notabene.id   |
                  /v1/transactions          |
                  (mTLS or Bearer+HMAC)     |
                                            |
                                  https://api.trisa.io
                                  /v1/transfer
                                  (HTTP gateway; gRPC for prod)

Retry loop:
  Every TR_RETRY_POLL_S (30s) the server scans tr_requests where the
  last delivery attempt was 'unreachable' or 'pending', and retries
  via the same adapter using the per-row exponential backoff schedule.
  After TR_RETRY_GIVE_UP_S (24h) the row is flipped to
  'delivery_failed' and surfaced for ops manual review.

Persistence:
  tr_requests       - per-screen decision
  tr_deliveries     - per-attempt audit row (one per adapter call)
  tr_vasp_directory - counterparty key + URL registry
  tr_inbound        - verified inbound IVMS messages
```

## Environment variables

### Universal

| Var | Default | Effect |
| --- | --- | --- |
| `TR_PROVIDER` | (unset → stub) | `sumsub`, `notabene`, `trisa`, or `stub`. |
| `TR_RETRY_MIN_DELAY_S` | 30 | First-retry backoff. |
| `TR_RETRY_MAX_DELAY_S` | 28800 (8h) | Cap on exponential backoff. |
| `TR_RETRY_GIVE_UP_S` | 86400 (24h) | When to mark `delivery_failed`. |
| `TR_RETRY_POLL_S` | 30 | Retry loop poll cadence. |
| `TR_INBOUND_RATE_LIMIT` | 100 | Max inbound msgs per sender VASP per minute. |

### Sumsub (`TR_PROVIDER=sumsub`)

| Var | Required | Effect |
| --- | --- | --- |
| `SUMSUB_TR_APP_TOKEN` | yes | App token sent in `X-App-Token`. |
| `SUMSUB_TR_APP_SECRET` | yes | HMAC-SHA256 key for `X-App-Access-Sig`. |
| `SUMSUB_TR_BASE_URL` | no  | Override base (default production). |
| `SUMSUB_TR_TIMEOUT` | no  | Request timeout in seconds. Default 8. |

Sample request body POSTed to `/resources/travelRule/transactions`:

```json
{
  "originatorVaspId": "zkcex",
  "beneficiaryVaspId": "sumsub-demo",
  "transaction": {
    "amount": "5000", "asset": "USDT",
    "originatorAccount": "zkcex:user-1",
    "beneficiaryAccount": "0xdeadbeef…",
    "timestamp": "2026-05-11T07:23:00Z"
  },
  "originatorParty": {"primary": "홍", "secondary": "길동"},
  "beneficiaryParty": {"primary": "Alice", "secondary": ""},
  "ivms101": { /* verbatim IVMS 101 dict */ },
  "originatorSignature": {"scheme": "Ed25519", "signature": "<hex>"},
  "originatorPublicKey": {"scheme": "Ed25519", "publicKey": "<hex>"}
}
```

Sample 2xx response:

```json
{"id":"tr-abc123","status":"ACCEPTED","inboxUrl":"https://api.sumsub.com/..."}
```

### Notabene (`TR_PROVIDER=notabene`)

| Var | Required | Effect |
| --- | --- | --- |
| `NOTABENE_API_KEY` | yes | `Authorization: Bearer <key>`. |
| `NOTABENE_API_SECRET` | yes | HMAC body signature. |
| `NOTABENE_BASE_URL` | no | Override base URL. |
| `NOTABENE_CLIENT_CERT_PATH` | no (mTLS opt-in) | PEM client cert path. |
| `NOTABENE_CLIENT_KEY_PATH` | no (mTLS opt-in) | PEM client key path. |
| `NOTABENE_CA_BUNDLE_PATH` | no | Pin server CA. |
| `NOTABENE_TIMEOUT` | no | Timeout seconds. Default 8. |

**mTLS setup (production):**

1. Onboard with Notabene; receive a TRP client cert + key in PEM.
2. Store them at, e.g., `/etc/zkcex/notabene/client.crt` and
   `/etc/zkcex/notabene/client.key` with mode `0600`, owner the
   `travel-rule` system user.
3. Set both `NOTABENE_CLIENT_CERT_PATH` and `NOTABENE_CLIENT_KEY_PATH`.
4. The adapter auto-detects both files and switches to mTLS mode.
5. Without those env vars, the adapter falls back to HTTPS-Bearer mode
   suitable for sandbox testing only — **production must use mTLS**.

Sample TRP envelope POSTed to `/v1/transactions`:

```json
{
  "asset":{"slip0044":60,"symbol":"USDT"},
  "amount":"5000",
  "originatorVASPdid":"zkcex",
  "beneficiaryVASPdid":"notabene-demo",
  "originator":{ /* IVMS originator block */ },
  "beneficiary":{ /* IVMS beneficiary block */ },
  "ivms101":{ /* verbatim */ },
  "originatorProof":{
    "scheme":"Ed25519","publicKey":"<hex>","signature":"<hex>"
  },
  "trpVersion":"3.1.0"
}
```

Notabene response shape (state ∈ `ACCEPTED|PENDING_REVIEW|ACTION_REQUIRED|REJECTED`):

```json
{"id":"trp-xyz","state":"ACCEPTED","createdAt":"..."}
```

### TRISA (`TR_PROVIDER=trisa`)

| Var | Required | Effect |
| --- | --- | --- |
| `TRISA_API_KEY` | yes | Bearer token for JSON gateway. |
| `TRISA_BASE_URL` | no | Override JSON gateway base URL. |
| `TRISA_DIRECTORY_URL` | no | GDS endpoint (future). |
| `TRISA_CLIENT_CERT_PATH` | no | TRISA cert (production gRPC). |
| `TRISA_CLIENT_KEY_PATH` | no | TRISA key (production gRPC). |
| `TRISA_CA_BUNDLE_PATH` | no | Pin GDS CA. |
| `TRISA_TIMEOUT` | no | Timeout seconds. Default 8. |

**gRPC setup (production):**

The stdlib has no gRPC runtime. To use the canonical TRISA wire format:

```bash
pip install grpcio trisa-protocol
```

Then, in `trisa.py`, replace the `urlopen()` call in `_post_json` with:

```python
import grpc
from trisa.api.v1beta1 import trisa_pb2_grpc
from trisa.envelope import secure_envelope_pb2

channel = grpc.secure_channel(
    counterparty_endpoint,
    grpc.ssl_channel_credentials(
        root_certificates=ca_bundle,
        private_key=client_key,
        certificate_chain=client_cert,
    ),
)
stub = trisa_pb2_grpc.TRISANetworkStub(channel)
response = stub.Transfer(envelope)
```

The IVMS-to-envelope translation in `_ivms_to_secure_envelope()`
already produces the right JSON shape and would just need a protobuf
encode wrapper.

The HTTP-gateway fallback shipping in this repo POSTs the JSON
encoding to `/v1/transfer` so the integration shape is correct.

## Failure mode matrix

| Failure | Adapter status | Server behavior |
| --- | --- | --- |
| Provider DNS / TCP fail | `unreachable` | Persisted; retry loop runs backoff. |
| Provider HTTP 5xx | `unreachable` | Persisted; retry. |
| Provider HTTP 4xx | `rejected` | Persisted; admin manual retry only. |
| Provider responds `PENDING` | `pending` | Persisted; retry loop polls. |
| Provider responds `REJECTED` | `rejected` | Persisted; admin manual retry only. |
| Provider responds 2xx unknown body | `unknown` | Persisted; ops triage. |
| Inbound signature mismatch | n/a | HTTP 401, NOT stored. |
| Inbound unknown VASP | n/a | HTTP 401, NOT stored. |
| Inbound rate limit | n/a | HTTP 429 with `retry_after_s`. |
| Retry after 24h | n/a | Row → `delivery_failed`, ops review. |

## Operator endpoints

- `GET  /travel-rule/adapter-info` — public; returns current provider,
  configured status, last 10 delivery attempts, retry schedule.
- `GET  /travel-rule/admin/deliveries?limit=50` — admin bearer; full
  delivery audit log.
- `POST /travel-rule/admin/retry/<tr_request_id>` — admin bearer; forces
  one retry against the current adapter regardless of backoff.

## Production checklist (out of scope for this commit)

1. Real Sumsub / Notabene / TRISA credentials in HashiCorp Vault.
2. Notabene mTLS client cert obtained, rotated every 90 days.
3. TRISA Global Directory Service registration + 60-day attestation
   rotation (30-day grace window).
4. `tr_vasp_directory` populated from the TRISA GDS / TRP directory
   API (today's hand-seeded list is for demo only).
5. PII redaction review (the RRN masking + name masking is already in
   the server, but ops needs to audit logs once per quarter).
6. Webhook receiver for push-mode inbound (Sumsub/Notabene both
   webhook to `/travel-rule/inbound`; today we only pull via
   `fetch_inbox`).
