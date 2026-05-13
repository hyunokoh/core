# zkcex — Python SDK

Minimal stdlib-only client for the zkCEX REST API.

## Install

```bash
pip install -e sdks/python
```

(No third-party dependencies. Works on Python 3.9+.)

## Five-line quickstart

```python
from zkcex import ZkcexClient

c = ZkcexClient(base_url="http://localhost:5500",
                api_key="YOUR_KEY", api_secret="YOUR_SECRET")
print(c.depth("ETHUSDT", limit=5))
print(c.place_order("ETHUSDT", "BUY", "LIMIT",
                    quantity="0.01", price="50", time_in_force="GTC"))
```

Get a key at `http://localhost:5500/app/api-keys.html`.

## Auth surfaces

| Routes                | Auth                                                  | SDK setup                                    |
| --------------------- | ----------------------------------------------------- | -------------------------------------------- |
| `/v3/*`, `/fapi/v1/*` | HMAC (`X-MBX-APIKEY` + signed query)                  | pass `api_key=`, `api_secret=`               |
| `/auth/me`, `/chain`, `/pol`, `/api-keys`, `/orders/conditional` | Bearer | call `signup()` / `login()` first, or pass `session_token=` |
| `/v3/exchangeInfo`, `/v3/depth`, `/pol/server-info`, …          | None   | no setup                                     |

## Examples

See `examples/place_limit_order.py`.
