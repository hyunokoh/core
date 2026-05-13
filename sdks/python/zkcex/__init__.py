"""zkcex — official minimal Python SDK for the zkCEX REST API.

Install (editable, no external deps)::

    pip install -e sdks/python

Five-line example::

    from zkcex import ZkcexClient
    c = ZkcexClient(base_url="http://localhost:5500", api_key="...", api_secret="...")
    print(c.depth("ETHUSDT", limit=5))
    print(c.place_order("ETHUSDT", "BUY", "LIMIT",
                        quantity="0.01", price="50", time_in_force="GTC"))
"""

from .client import ZkcexClient, ZkcexError

__all__ = ["ZkcexClient", "ZkcexError"]
__version__ = "1.0.0"
