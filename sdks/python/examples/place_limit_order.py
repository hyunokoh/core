#!/usr/bin/env python3
"""Five-line spirit example: signup, fetch depth, place a tiny limit order.

Run::

    PYTHONPATH=. python3 examples/place_limit_order.py
"""

from __future__ import annotations

import os
import time

from zkcex import ZkcexClient, ZkcexError


def main() -> None:
    base = os.environ.get("ZKCEX_URL", "http://localhost:5500")
    c = ZkcexClient(base_url=base)

    # 1) Sign up — gives us a bearer token + a freshly-seeded wallet.
    email = f"sdk-py-{int(time.time())}@example.com"
    me = c.signup(email=email, password="pass1234", name="SDK demo")
    print("signup user:", me["user"]["opex_user"])

    # 2) Public market data (no auth required).
    info = c.exchange_info()
    print("exchange has", len(info["symbols"]), "markets")
    print("depth ETHUSDT:", c.depth("ETHUSDT", limit=5))

    # 3) Issue an HMAC API key for the trade calls.
    key = c.create_api_key(label="sdk-demo", scopes=["read", "trade"])
    c.api_key = key["key_id"]
    c.api_secret = key["secret"]
    print("api key:", key["key_id"])

    # 4) Account snapshot through the signed path.
    print("account:", c.account())

    # 5) Place a tiny limit order.
    try:
        order = c.place_order(
            symbol="ETHUSDT", side="BUY", type="LIMIT",
            quantity="0.01", price="50",
        )
        print("order:", order)
    except ZkcexError as exc:
        print("place_order failed (expected if balance too small):",
              exc.status, exc.payload)


if __name__ == "__main__":
    main()
