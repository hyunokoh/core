#!/usr/bin/env python3
"""Generate the canonical zkCEX OpenAPI 3.1 specification.

This walks a hard-coded inventory of public endpoints across every backend
service exposed through the proxy at ``:5500`` and emits a well-formed
OpenAPI 3.1 JSON to ``homepage/openapi.json`` (plus a YAML twin).

Hard rules:
  * stdlib only — no PyYAML, no requests, no third-party deps.
  * static descriptor — we do not try to introspect the Python regex
    routers blindly. Each endpoint has its own row, copied from the
    handler signatures in the corresponding source files under tools/.
  * loopback-only / admin-only endpoints are intentionally skipped.

Run::

    python3 tools/openapi_gen.py

Outputs::

    homepage / openapi.json
    homepage / openapi.yaml
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_DIR = os.path.join(ROOT, "homepage")

VERSION = "1.0.0"
TITLE = "zkCEX REST API"
DESCRIPTION = (
    "Public REST + WebSocket surface of zkCEX. The /v3/* and /fapi/v1/* "
    "namespaces are wire-compatible with the Binance Spot and USDT-M "
    "Futures schemas, so existing Binance SDKs work side-by-side. "
    "Bearer-authenticated routes (/auth/*, /chain/*, /pol/*, /api-keys/*) "
    "use a session token from /auth/login or /auth/signup."
)

# ---------------------------------------------------------------------------
# Shared parameter / response shapes used by many endpoints. The full
# component dict is assembled at the bottom of this file from this map.
# ---------------------------------------------------------------------------

SCHEMAS: dict[str, dict[str, Any]] = {
    "Error": {
        "type": "object",
        "properties": {
            "error": {"type": "string", "example": "not_found"},
            "message": {"type": "string"},
            "code": {"type": "integer"},
        },
        "additionalProperties": True,
    },
    "BinanceError": {
        "type": "object",
        "description": "Binance-compatible error envelope (used by /v3/* and /fapi/v1/*).",
        "properties": {
            "code": {"type": "integer", "example": -1100},
            "msg": {"type": "string"},
        },
    },
    # ---- Auth -----------------------------------------------------------
    "User": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "email": {"type": "string", "format": "email"},
            "name": {"type": "string"},
            "opex_user": {"type": "string", "description": "Internal user UUID."},
            "kyc_status": {"type": "string", "enum": ["none", "pending", "verified"]},
        },
        "required": ["id", "email", "opex_user"],
    },
    "SignupRequest": {
        "type": "object",
        "required": ["email", "password", "name"],
        "properties": {
            "email": {"type": "string", "format": "email"},
            "password": {
                "type": "string",
                "minLength": 8,
                "description": "8+ chars, must contain letters and digits.",
            },
            "name": {"type": "string"},
        },
    },
    "LoginRequest": {
        "type": "object",
        "required": ["email", "password"],
        "properties": {
            "email": {"type": "string", "format": "email"},
            "password": {"type": "string"},
        },
    },
    "AuthResponse": {
        "type": "object",
        "properties": {
            "token": {"type": "string", "description": "Bearer session token."},
            "user": {"$ref": "#/components/schemas/User"},
        },
    },
    "AuthHealth": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "backend": {"type": "string", "enum": ["sqlite", "postgres"]},
            "db_latency_ms": {"type": "number"},
            "version": {"type": "string"},
            "n_users": {"type": "integer"},
            "n_active_sessions": {"type": "integer"},
        },
    },
    # ---- Spot market data ------------------------------------------------
    "SpotSymbol": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "example": "ETHUSDT"},
            "status": {"type": "string", "example": "TRADING"},
            "baseAsset": {"type": "string"},
            "baseAssetPrecision": {"type": "integer"},
            "quoteAsset": {"type": "string"},
            "quoteAssetPrecision": {"type": "integer"},
            "orderTypes": {"type": "array", "items": {"type": "string"}},
            "icebergAllowed": {"type": "boolean"},
            "ocoAllowed": {"type": "boolean"},
            "isSpotTradingAllowed": {"type": "boolean"},
            "isMarginTradingAllowed": {"type": "boolean"},
            "filters": {"type": "array", "items": {"type": "object"}},
            "permissions": {"type": "array", "items": {"type": "string"}},
        },
    },
    "SpotExchangeInfo": {
        "type": "object",
        "properties": {
            "timezone": {"type": "string", "example": "UTC"},
            "serverTime": {"type": "integer", "format": "int64"},
            "rateLimits": {"type": "array", "items": {"type": "object"}},
            "exchangeFilters": {"type": "array", "items": {"type": "object"}},
            "fees": {"type": "array", "items": {"type": "object"}},
            "symbols": {"type": "array", "items": {"$ref": "#/components/schemas/SpotSymbol"}},
        },
    },
    "DepthLevel": {
        "type": "array",
        "minItems": 2,
        "maxItems": 2,
        "items": {"type": "number"},
        "description": "Tuple of [price, quantity].",
    },
    "Depth": {
        "type": "object",
        "properties": {
            "lastUpdateId": {"type": "integer", "format": "int64"},
            "bids": {"type": "array", "items": {"$ref": "#/components/schemas/DepthLevel"}},
            "asks": {"type": "array", "items": {"$ref": "#/components/schemas/DepthLevel"}},
        },
    },
    "Trade": {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "format": "int64"},
            "price": {"type": "string"},
            "qty": {"type": "string"},
            "quoteQty": {"type": "string"},
            "time": {"type": "integer", "format": "int64"},
            "isBuyerMaker": {"type": "boolean"},
            "isBestMatch": {"type": "boolean"},
        },
    },
    "Kline": {
        "type": "array",
        "description": (
            "Tuple: [openTime, open, high, low, close, volume, closeTime, "
            "quoteAssetVolume, numberOfTrades, takerBuyBaseAssetVolume, "
            "takerBuyQuoteAssetVolume, ignore]."
        ),
        "items": {},
    },
    "Ticker24h": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "priceChange": {"type": "string"},
            "priceChangePercent": {"type": "string"},
            "lastPrice": {"type": "string"},
            "highPrice": {"type": "string"},
            "lowPrice": {"type": "string"},
            "volume": {"type": "string"},
            "quoteVolume": {"type": "string"},
            "openTime": {"type": "integer", "format": "int64"},
            "closeTime": {"type": "integer", "format": "int64"},
        },
    },
    "Order": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "orderId": {"type": "integer", "format": "int64"},
            "clientOrderId": {"type": "string"},
            "transactTime": {"type": "integer", "format": "int64"},
            "price": {"type": "string"},
            "origQty": {"type": "string"},
            "executedQty": {"type": "string"},
            "cummulativeQuoteQty": {"type": "string"},
            "status": {"type": "string", "example": "NEW"},
            "timeInForce": {"type": "string", "example": "GTC"},
            "type": {"type": "string", "example": "LIMIT"},
            "side": {"type": "string", "enum": ["BUY", "SELL"]},
            "fills": {"type": "array", "items": {"type": "object"}},
        },
    },
    "AccountBalance": {
        "type": "object",
        "properties": {
            "asset": {"type": "string"},
            "free": {"type": "string"},
            "locked": {"type": "string"},
        },
    },
    "Account": {
        "type": "object",
        "properties": {
            "makerCommission": {"type": "integer"},
            "takerCommission": {"type": "integer"},
            "buyerCommission": {"type": "integer"},
            "sellerCommission": {"type": "integer"},
            "canTrade": {"type": "boolean"},
            "canWithdraw": {"type": "boolean"},
            "canDeposit": {"type": "boolean"},
            "updateTime": {"type": "integer", "format": "int64"},
            "accountType": {"type": "string", "example": "SPOT"},
            "balances": {"type": "array", "items": {"$ref": "#/components/schemas/AccountBalance"}},
            "permissions": {"type": "array", "items": {"type": "string"}},
        },
    },
    # ---- Futures ---------------------------------------------------------
    "FuturesSymbol": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "example": "BTCUSDT_PERP"},
            "baseAsset": {"type": "string"},
            "quoteAsset": {"type": "string"},
            "marginAsset": {"type": "string"},
            "contractType": {"type": "string", "example": "PERPETUAL"},
            "status": {"type": "string"},
            "contractSize": {"type": "string"},
            "tickSize": {"type": "string"},
            "stepSize": {"type": "string"},
            "maxLeverage": {"type": "integer"},
            "maintenanceMarginRate": {"type": "string"},
            "fundingIntervalSeconds": {"type": "integer"},
            "fundingClamp": {"type": "string"},
            "indexSymbol": {"type": "string"},
            "filters": {"type": "array", "items": {"type": "object"}},
        },
    },
    "FuturesExchangeInfo": {
        "type": "object",
        "properties": {
            "timezone": {"type": "string"},
            "serverTime": {"type": "integer", "format": "int64"},
            "symbols": {"type": "array", "items": {"$ref": "#/components/schemas/FuturesSymbol"}},
        },
    },
    "PremiumIndex": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "markPrice": {"type": "string"},
            "indexPrice": {"type": "string"},
            "lastFundingRate": {"type": "string"},
            "nextFundingTime": {"type": "integer", "format": "int64"},
            "time": {"type": "integer", "format": "int64"},
        },
    },
    "FundingRateEntry": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "fundingRate": {"type": "string"},
            "fundingTime": {"type": "integer", "format": "int64"},
        },
    },
    "Position": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "positionAmt": {"type": "string"},
            "entryPrice": {"type": "string"},
            "markPrice": {"type": "string"},
            "unRealizedProfit": {"type": "string"},
            "leverage": {"type": "string"},
            "marginType": {"type": "string", "enum": ["isolated", "cross"]},
            "isolatedMargin": {"type": "string"},
            "positionSide": {"type": "string", "example": "BOTH"},
        },
    },
    "FuturesAccount": {
        "type": "object",
        "properties": {
            "totalWalletBalance": {"type": "string"},
            "totalUnrealizedProfit": {"type": "string"},
            "totalMarginBalance": {"type": "string"},
            "availableBalance": {"type": "string"},
            "assets": {"type": "array", "items": {"type": "object"}},
            "positions": {"type": "array", "items": {"$ref": "#/components/schemas/Position"}},
        },
    },
    "LeverageResponse": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "leverage": {"type": "integer"},
            "maxNotionalValue": {"type": "string"},
        },
    },
    "FuturesTransferResponse": {
        "type": "object",
        "properties": {
            "tranId": {"type": "integer", "format": "int64"},
        },
    },
    # ---- KYC -------------------------------------------------------------
    "KycStartRequest": {
        "type": "object",
        "required": ["full_name", "national_id", "phone"],
        "properties": {
            "full_name": {"type": "string"},
            "national_id": {"type": "string", "description": "RRN-style identifier."},
            "phone": {"type": "string"},
            "carrier": {"type": "string"},
        },
    },
    "KycVerifyRequest": {
        "type": "object",
        "required": ["session_id", "otp"],
        "properties": {
            "session_id": {"type": "string"},
            "otp": {"type": "string", "minLength": 6, "maxLength": 6},
        },
    },
    "KycStatus": {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["none", "pending", "verified", "failed"]},
            "provider": {"type": "string", "example": "pass-demo"},
            "verified_at": {"type": "integer", "format": "int64"},
        },
    },
    # ---- Chain -----------------------------------------------------------
    "ChainInfo": {
        "type": "object",
        "properties": {
            "chains": {"type": "array", "items": {"type": "object"}},
            "default": {"type": "string", "example": "hardhat"},
        },
    },
    "ChainWallet": {
        "type": "object",
        "properties": {
            "address": {"type": "string", "example": "0x..."},
            "chain": {"type": "string"},
            "balances": {"type": "array", "items": {"type": "object"}},
        },
    },
    "ChainWithdrawRequest": {
        "type": "object",
        "required": ["asset", "amount", "to_address"],
        "properties": {
            "asset": {"type": "string", "example": "USDT"},
            "amount": {"type": "string"},
            "to_address": {"type": "string"},
            "chain": {"type": "string"},
        },
    },
    "ChainAirdropRequest": {
        "type": "object",
        "properties": {
            "asset": {"type": "string"},
            "amount": {"type": "string"},
            "chain": {"type": "string"},
        },
    },
    "ChainTx": {
        "type": "object",
        "properties": {
            "tx_hash": {"type": "string"},
            "from": {"type": "string"},
            "to": {"type": "string"},
            "amount": {"type": "string"},
            "asset": {"type": "string"},
            "block_number": {"type": "integer"},
            "status": {"type": "string"},
        },
    },
    # ---- PoL -------------------------------------------------------------
    "PolServerInfo": {
        "type": "object",
        "properties": {
            "scheme_name": {"type": "string", "example": "zkcex-pol-v1"},
            "hash": {"type": "string", "example": "SHA-256"},
            "sig_scheme": {"type": "string", "example": "Ed25519"},
            "pubkey": {"type": "string", "description": "hex-encoded public key"},
            "epoch_seconds": {"type": "integer"},
        },
    },
    "PolEpoch": {
        "type": "object",
        "properties": {
            "epoch": {"type": "integer", "format": "int64"},
            "merkle_root": {"type": "string"},
            "total_liabilities": {"type": "object"},
            "signature": {"type": "string"},
            "timestamp": {"type": "integer", "format": "int64"},
        },
    },
    "PolMyProof": {
        "type": "object",
        "properties": {
            "epoch": {"type": "integer"},
            "balance_commitment": {"type": "string"},
            "merkle_path": {"type": "array", "items": {"type": "string"}},
            "merkle_root": {"type": "string"},
            "signature": {"type": "string"},
        },
    },
    "ReservesVsLiabilities": {
        "type": "object",
        "properties": {
            "epoch": {"type": "integer"},
            "assets": {"type": "array", "items": {"type": "object"}},
        },
    },
    "PolSnapshotCertificate": {
        "type": "object",
        "properties": {
            "certificate": {"type": "object"},
            "signature": {"type": "string"},
            "pubkey": {"type": "string"},
        },
    },
    # ---- API keys --------------------------------------------------------
    "ApiKey": {
        "type": "object",
        "properties": {
            "key_id": {"type": "string"},
            "label": {"type": "string"},
            "scopes": {
                "type": "array",
                "items": {"type": "string", "enum": ["read", "trade", "withdraw"]},
            },
            "ip_allowlist": {"type": "string"},
            "daily_quote_cap_usdt": {"type": "string"},
            "hourly_request_cap": {"type": "integer"},
            "created_at": {"type": "integer", "format": "int64"},
            "expires_at": {"type": "integer", "format": "int64", "nullable": True},
        },
    },
    "ApiKeyCreateRequest": {
        "type": "object",
        "required": ["label"],
        "properties": {
            "label": {"type": "string", "maxLength": 64},
            "scopes": {"type": "array", "items": {"type": "string"}, "default": ["read"]},
            "ip_allowlist": {"type": "string", "description": "Comma-separated CIDR list."},
            "daily_quote_cap_usdt": {"type": "string"},
            "hourly_request_cap": {"type": "integer"},
            "expires_in_days": {"type": "integer", "default": 90},
            "confirm_phrase": {"type": "string", "description": "Required when scope=withdraw."},
        },
    },
    "ApiKeyCreateResponse": {
        "type": "object",
        "properties": {
            "key_id": {"type": "string"},
            "secret": {"type": "string", "description": "Shown ONCE — store it."},
            "scopes": {"type": "array", "items": {"type": "string"}},
            "expires_at": {"type": "integer", "format": "int64", "nullable": True},
            "label": {"type": "string"},
            "warning": {"type": "string"},
        },
    },
    # ---- Conditional orders ---------------------------------------------
    "ConditionalOrderRequest": {
        "type": "object",
        "required": ["symbol", "side", "type", "quantity"],
        "properties": {
            "symbol": {"type": "string"},
            "side": {"type": "string", "enum": ["BUY", "SELL"]},
            "type": {"type": "string", "enum": ["STOP_LIMIT", "STOP_MARKET", "OCO"]},
            "quantity": {"type": "string"},
            "trigger_price": {"type": "string"},
            "limit_price": {"type": "string"},
            "take_profit": {"type": "object", "properties": {"limit_price": {"type": "string"}}},
            "stop_loss": {
                "type": "object",
                "properties": {
                    "trigger_price": {"type": "string"},
                    "limit_price": {"type": "string"},
                },
            },
        },
    },
    "ConditionalOrder": {
        "type": "object",
        "properties": {
            "client_order_id": {"type": "string"},
            "conditional_id": {"type": "integer"},
            "status": {"type": "string", "example": "pending"},
            "created_at": {"type": "integer", "format": "int64"},
            "oco_group_id": {"type": "string"},
            "legs": {"type": "array", "items": {"type": "object"}},
        },
    },
    # ---- Export ----------------------------------------------------------
    "ExportIndex": {
        "type": "object",
        "properties": {
            "files": {"type": "array", "items": {"type": "string"}},
        },
    },
    # ---- MCP -------------------------------------------------------------
    "JsonRpcRequest": {
        "type": "object",
        "required": ["jsonrpc", "method", "id"],
        "properties": {
            "jsonrpc": {"type": "string", "example": "2.0"},
            "method": {"type": "string"},
            "params": {"type": "object"},
            "id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
        },
    },
    "JsonRpcResponse": {
        "type": "object",
        "properties": {
            "jsonrpc": {"type": "string"},
            "result": {},
            "error": {"type": "object"},
            "id": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
        },
    },
    # ---- Push ------------------------------------------------------------
    "VapidKey": {
        "type": "object",
        "properties": {
            "publicKey": {"type": "string"},
        },
    },
    "PushSubscription": {
        "type": "object",
        "required": ["endpoint", "keys"],
        "properties": {
            "endpoint": {"type": "string", "format": "uri"},
            "keys": {
                "type": "object",
                "properties": {
                    "p256dh": {"type": "string"},
                    "auth": {"type": "string"},
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Reusable parameter objects
# ---------------------------------------------------------------------------

P_SYMBOL = {
    "name": "symbol",
    "in": "query",
    "required": True,
    "schema": {"type": "string", "example": "ETHUSDT"},
}
P_SYMBOL_OPTIONAL = {
    "name": "symbol",
    "in": "query",
    "required": False,
    "schema": {"type": "string"},
}
P_LIMIT = {
    "name": "limit",
    "in": "query",
    "required": False,
    "schema": {
        "type": "integer",
        "default": 100,
        "description": "Valid: 5,10,20,50,100,500,1000,5000.",
    },
}
P_INTERVAL = {
    "name": "interval",
    "in": "query",
    "required": True,
    "schema": {
        "type": "string",
        "example": "1m",
        "enum": [
            "1m",
            "3m",
            "5m",
            "15m",
            "30m",
            "1h",
            "2h",
            "4h",
            "6h",
            "8h",
            "12h",
            "1d",
            "3d",
            "1w",
            "1M",
        ],
    },
}
P_TIMESTAMP = {
    "name": "timestamp",
    "in": "query",
    "required": True,
    "schema": {"type": "integer", "format": "int64", "description": "Millis since epoch."},
}
P_RECV_WINDOW = {
    "name": "recvWindow",
    "in": "query",
    "required": False,
    "schema": {"type": "integer", "default": 5000},
}
P_SIGNATURE = {
    "name": "signature",
    "in": "query",
    "required": True,
    "schema": {"type": "string", "description": "HMAC-SHA256 of the query string."},
}

SIGNED_QUERY = [P_TIMESTAMP, P_RECV_WINDOW, P_SIGNATURE]


# ---------------------------------------------------------------------------
# Endpoint descriptor. One row per (path, method) — public-only.
# ---------------------------------------------------------------------------

# Each row:
#   path, method, summary, tags, auth, params, body, response, error_responses
# auth: "none" | "bearer" | "hmac"
# body: None or {"schema": "<name>"}
# response: schema name (single 200 response) OR full responses dict
# extra_responses: dict of status -> response object (optional)
ENDPOINTS: list[dict[str, Any]] = [
    # ====================== Auth ===========================================
    {
        "path": "/auth/signup",
        "method": "POST",
        "tags": ["auth"],
        "summary": "Create a new account",
        "auth": "none",
        "body": "SignupRequest",
        "response": "AuthResponse",
        "status": 201,
    },
    {
        "path": "/auth/login",
        "method": "POST",
        "tags": ["auth"],
        "summary": "Exchange email+password for a bearer token",
        "auth": "none",
        "body": "LoginRequest",
        "response": "AuthResponse",
    },
    {
        "path": "/auth/me",
        "method": "GET",
        "tags": ["auth"],
        "summary": "Get the current bearer token's user",
        "auth": "bearer",
        "response_inline": {
            "type": "object",
            "properties": {"user": {"$ref": "#/components/schemas/User"}},
        },
    },
    {
        "path": "/auth/logout",
        "method": "POST",
        "tags": ["auth"],
        "summary": "Revoke the current bearer token",
        "auth": "bearer",
        "status": 204,
        "response_inline": None,
    },
    {
        "path": "/auth/health",
        "method": "GET",
        "tags": ["auth"],
        "summary": "Backend health + version",
        "auth": "none",
        "response": "AuthHealth",
    },
    # ====================== KYC ===========================================
    {
        "path": "/kyc/start",
        "method": "POST",
        "tags": ["kyc"],
        "summary": "Begin a PASS-style KYC session",
        "auth": "bearer",
        "body": "KycStartRequest",
        "response_inline": {"type": "object", "properties": {"session_id": {"type": "string"}}},
    },
    {
        "path": "/kyc/verify",
        "method": "POST",
        "tags": ["kyc"],
        "summary": "Confirm a KYC OTP",
        "auth": "bearer",
        "body": "KycVerifyRequest",
        "response": "KycStatus",
    },
    {
        "path": "/kyc/status",
        "method": "GET",
        "tags": ["kyc"],
        "summary": "Current KYC state for the bearer's user",
        "auth": "bearer",
        "response": "KycStatus",
    },
    {
        "path": "/kyc/cancel",
        "method": "POST",
        "tags": ["kyc"],
        "summary": "Cancel an in-flight KYC session",
        "auth": "bearer",
        "response": "KycStatus",
    },
    {
        "path": "/kyc/sumsub/start",
        "method": "POST",
        "tags": ["kyc"],
        "summary": "Issue a Sumsub WebSDK access token",
        "auth": "bearer",
        "response_inline": {
            "type": "object",
            "properties": {"token": {"type": "string"}, "userId": {"type": "string"}},
        },
    },
    {
        "path": "/kyc/sumsub/status",
        "method": "GET",
        "tags": ["kyc"],
        "summary": "Current Sumsub applicant review status",
        "auth": "bearer",
        "response": "KycStatus",
    },
    {
        "path": "/kyc/sumsub/config",
        "method": "GET",
        "tags": ["kyc"],
        "summary": "Public Sumsub WebSDK config",
        "auth": "none",
        "response_inline": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}, "level_name": {"type": "string"}},
        },
    },
    # ====================== Spot market ==================================
    {
        "path": "/v3/exchangeInfo",
        "method": "GET",
        "tags": ["spot-market"],
        "summary": "Spot trading rules and symbol metadata",
        "auth": "none",
        "response": "SpotExchangeInfo",
    },
    {
        "path": "/v3/depth",
        "method": "GET",
        "tags": ["spot-market"],
        "summary": "Order book snapshot",
        "auth": "none",
        "params": [P_SYMBOL, P_LIMIT],
        "response": "Depth",
    },
    {
        "path": "/v3/klines",
        "method": "GET",
        "tags": ["spot-market"],
        "summary": "Candlestick / kline data",
        "auth": "none",
        "params": [P_SYMBOL, P_INTERVAL, P_LIMIT],
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/Kline"}},
    },
    {
        "path": "/v3/trades",
        "method": "GET",
        "tags": ["spot-market"],
        "summary": "Recent trades",
        "auth": "none",
        "params": [P_SYMBOL, P_LIMIT],
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/Trade"}},
    },
    {
        "path": "/v3/ticker/24hr",
        "method": "GET",
        "tags": ["spot-market"],
        "summary": "24-hour rolling price stats",
        "auth": "none",
        "params": [P_SYMBOL_OPTIONAL],
        "response_inline": {
            "oneOf": [
                {"$ref": "#/components/schemas/Ticker24h"},
                {"type": "array", "items": {"$ref": "#/components/schemas/Ticker24h"}},
            ]
        },
    },
    # ====================== Spot trade (signed) ==========================
    {
        "path": "/v3/order",
        "method": "POST",
        "tags": ["spot-trade"],
        "summary": "Place a new spot order",
        "auth": "hmac",
        "params": [
            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "side",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "enum": ["BUY", "SELL"]},
            },
            {
                "name": "type",
                "in": "query",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "LIMIT",
                        "MARKET",
                        "STOP_LOSS",
                        "STOP_LOSS_LIMIT",
                        "TAKE_PROFIT",
                        "TAKE_PROFIT_LIMIT",
                        "LIMIT_MAKER",
                    ],
                },
            },
            {"name": "quantity", "in": "query", "required": False, "schema": {"type": "string"}},
            {
                "name": "quoteOrderQty",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            },
            {"name": "price", "in": "query", "required": False, "schema": {"type": "string"}},
            {
                "name": "timeInForce",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "enum": ["GTC", "IOC", "FOK"]},
            },
            {
                "name": "newClientOrderId",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            },
        ]
        + SIGNED_QUERY,
        "response": "Order",
    },
    {
        "path": "/v3/order",
        "method": "DELETE",
        "tags": ["spot-trade"],
        "summary": "Cancel an open spot order",
        "auth": "hmac",
        "params": [
            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "orderId",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "format": "int64"},
            },
            {
                "name": "origClientOrderId",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            },
        ]
        + SIGNED_QUERY,
        "response": "Order",
    },
    {
        "path": "/v3/openOrders",
        "method": "GET",
        "tags": ["spot-trade"],
        "summary": "List all currently-open orders for the API key",
        "auth": "hmac",
        "params": [P_SYMBOL_OPTIONAL] + SIGNED_QUERY,
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}},
    },
    {
        "path": "/v3/account",
        "method": "GET",
        "tags": ["spot-account"],
        "summary": "Spot account balances + permissions",
        "auth": "hmac",
        "params": SIGNED_QUERY,
        "response": "Account",
    },
    {
        "path": "/v3/myTrades",
        "method": "GET",
        "tags": ["spot-trade"],
        "summary": "Trade history for the bearer/api-key",
        "auth": "hmac",
        "params": [P_SYMBOL, P_LIMIT] + SIGNED_QUERY,
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/Trade"}},
    },
    {
        "path": "/v3/withdraw",
        "method": "POST",
        "tags": ["spot-account"],
        "summary": "Withdraw an asset to an external address (requires withdraw scope)",
        "auth": "hmac",
        "params": [
            {"name": "asset", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "amount", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "address", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "network", "in": "query", "required": False, "schema": {"type": "string"}},
        ]
        + SIGNED_QUERY,
        "response_inline": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "status": {"type": "string"}},
        },
    },
    # ====================== Futures (signed) =============================
    {
        "path": "/fapi/v1/ping",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Liveness check",
        "auth": "none",
        "response_inline": {"type": "object"},
    },
    {
        "path": "/fapi/v1/time",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Server time",
        "auth": "none",
        "response_inline": {
            "type": "object",
            "properties": {"serverTime": {"type": "integer", "format": "int64"}},
        },
    },
    {
        "path": "/fapi/v1/exchangeInfo",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Futures trading rules and symbol metadata",
        "auth": "none",
        "response": "FuturesExchangeInfo",
    },
    {
        "path": "/fapi/v1/premiumIndex",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Mark / index price + last funding rate",
        "auth": "none",
        "params": [P_SYMBOL_OPTIONAL],
        "response_inline": {
            "oneOf": [
                {"$ref": "#/components/schemas/PremiumIndex"},
                {"type": "array", "items": {"$ref": "#/components/schemas/PremiumIndex"}},
            ]
        },
    },
    {
        "path": "/fapi/v1/fundingRate",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Historical funding rates",
        "auth": "none",
        "params": [
            P_SYMBOL_OPTIONAL,
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "default": 100},
            },
        ],
        "response_inline": {
            "type": "array",
            "items": {"$ref": "#/components/schemas/FundingRateEntry"},
        },
    },
    {
        "path": "/fapi/v1/depth",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Futures order book snapshot",
        "auth": "none",
        "params": [P_SYMBOL, P_LIMIT],
        "response": "Depth",
    },
    {
        "path": "/fapi/v1/ticker/24hr",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Futures 24h rolling stats",
        "auth": "none",
        "params": [P_SYMBOL_OPTIONAL],
        "response_inline": {
            "oneOf": [
                {"$ref": "#/components/schemas/Ticker24h"},
                {"type": "array", "items": {"$ref": "#/components/schemas/Ticker24h"}},
            ]
        },
    },
    {
        "path": "/fapi/v1/order",
        "method": "POST",
        "tags": ["futures"],
        "summary": "Place a futures order",
        "auth": "hmac",
        "params": [
            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "side",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "enum": ["BUY", "SELL"]},
            },
            {
                "name": "type",
                "in": "query",
                "required": True,
                "schema": {
                    "type": "string",
                    "enum": [
                        "LIMIT",
                        "MARKET",
                        "STOP",
                        "STOP_MARKET",
                        "TAKE_PROFIT",
                        "TAKE_PROFIT_MARKET",
                    ],
                },
            },
            {"name": "quantity", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "price", "in": "query", "required": False, "schema": {"type": "string"}},
            {
                "name": "timeInForce",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "enum": ["GTC", "IOC", "FOK"]},
            },
            {"name": "reduceOnly", "in": "query", "required": False, "schema": {"type": "boolean"}},
            {
                "name": "positionSide",
                "in": "query",
                "required": False,
                "schema": {"type": "string", "enum": ["BOTH", "LONG", "SHORT"]},
            },
            {
                "name": "newClientOrderId",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            },
        ]
        + SIGNED_QUERY,
        "response": "Order",
    },
    {
        "path": "/fapi/v1/order",
        "method": "DELETE",
        "tags": ["futures"],
        "summary": "Cancel a futures order",
        "auth": "hmac",
        "params": [
            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "orderId",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "format": "int64"},
            },
            {
                "name": "origClientOrderId",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            },
        ]
        + SIGNED_QUERY,
        "response": "Order",
    },
    {
        "path": "/fapi/v1/order",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Fetch a single futures order",
        "auth": "hmac",
        "params": [
            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "orderId",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "format": "int64"},
            },
            {
                "name": "origClientOrderId",
                "in": "query",
                "required": False,
                "schema": {"type": "string"},
            },
        ]
        + SIGNED_QUERY,
        "response": "Order",
    },
    {
        "path": "/fapi/v1/openOrders",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Open futures orders",
        "auth": "hmac",
        "params": [P_SYMBOL_OPTIONAL] + SIGNED_QUERY,
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/Order"}},
    },
    {
        "path": "/fapi/v1/userTrades",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Futures trade history",
        "auth": "hmac",
        "params": [P_SYMBOL, P_LIMIT] + SIGNED_QUERY,
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/Trade"}},
    },
    {
        "path": "/fapi/v1/positionRisk",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Current futures positions",
        "auth": "hmac",
        "params": [P_SYMBOL_OPTIONAL] + SIGNED_QUERY,
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/Position"}},
    },
    {
        "path": "/fapi/v1/leverage",
        "method": "POST",
        "tags": ["futures"],
        "summary": "Set initial leverage for a symbol",
        "auth": "hmac",
        "params": [
            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "leverage",
                "in": "query",
                "required": True,
                "schema": {"type": "integer", "minimum": 1},
            },
        ]
        + SIGNED_QUERY,
        "response": "LeverageResponse",
    },
    {
        "path": "/fapi/v1/marginType",
        "method": "POST",
        "tags": ["futures"],
        "summary": "Switch isolated/cross margin for a symbol",
        "auth": "hmac",
        "params": [
            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "marginType",
                "in": "query",
                "required": True,
                "schema": {"type": "string", "enum": ["ISOLATED", "CROSSED"]},
            },
        ]
        + SIGNED_QUERY,
        "response_inline": {
            "type": "object",
            "properties": {"code": {"type": "integer"}, "msg": {"type": "string"}},
        },
    },
    {
        "path": "/fapi/v1/account",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Futures account balances + positions",
        "auth": "hmac",
        "params": SIGNED_QUERY,
        "response": "FuturesAccount",
    },
    {
        "path": "/fapi/v1/transfer",
        "method": "POST",
        "tags": ["futures"],
        "summary": "Transfer between spot and futures wallets",
        "auth": "hmac",
        "params": [
            {"name": "asset", "in": "query", "required": True, "schema": {"type": "string"}},
            {"name": "amount", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "type",
                "in": "query",
                "required": True,
                "schema": {
                    "type": "integer",
                    "enum": [1, 2],
                    "description": "1 = spot->futures, 2 = futures->spot",
                },
            },
        ]
        + SIGNED_QUERY,
        "response": "FuturesTransferResponse",
    },
    {
        "path": "/fapi/v1/income",
        "method": "GET",
        "tags": ["futures"],
        "summary": "Funding + realized PnL history",
        "auth": "hmac",
        "params": [P_SYMBOL_OPTIONAL] + SIGNED_QUERY,
        "response_inline": {"type": "array", "items": {"type": "object"}},
    },
    {
        "path": "/fapi/v1/closePosition",
        "method": "POST",
        "tags": ["futures"],
        "summary": "Close an open futures position at market",
        "auth": "hmac",
        "params": [
            {"name": "symbol", "in": "query", "required": True, "schema": {"type": "string"}},
        ]
        + SIGNED_QUERY,
        "response": "Order",
    },
    # ====================== Chain ========================================
    {
        "path": "/chain/info",
        "method": "GET",
        "tags": ["chain"],
        "summary": "Configured chains + RPC liveness",
        "auth": "none",
        "response": "ChainInfo",
    },
    {
        "path": "/chain/wallet",
        "method": "GET",
        "tags": ["chain"],
        "summary": "User's on-chain deposit address + balances",
        "auth": "bearer",
        "params": [
            {"name": "chain", "in": "query", "required": False, "schema": {"type": "string"}}
        ],
        "response": "ChainWallet",
    },
    {
        "path": "/chain/deposits",
        "method": "GET",
        "tags": ["chain"],
        "summary": "Deposit history",
        "auth": "bearer",
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/ChainTx"}},
    },
    {
        "path": "/chain/withdraws",
        "method": "GET",
        "tags": ["chain"],
        "summary": "Withdrawal history",
        "auth": "bearer",
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/ChainTx"}},
    },
    {
        "path": "/chain/custody",
        "method": "GET",
        "tags": ["chain"],
        "summary": "Custodial vault status",
        "auth": "none",
        "response_inline": {"type": "object"},
    },
    {
        "path": "/chain/airdrop",
        "method": "POST",
        "tags": ["chain"],
        "summary": "Send demo airdrop to the bearer's user wallet (testnet only)",
        "auth": "bearer",
        "body": "ChainAirdropRequest",
        "response": "ChainTx",
    },
    {
        "path": "/chain/deposit-detect",
        "method": "POST",
        "tags": ["chain"],
        "summary": "Manually trigger deposit detection for the bearer",
        "auth": "bearer",
        "response_inline": {"type": "object", "properties": {"detected": {"type": "integer"}}},
    },
    {
        "path": "/chain/withdraw",
        "method": "POST",
        "tags": ["chain"],
        "summary": "Withdraw an on-chain asset (KYC + 2FA gated)",
        "auth": "bearer",
        "body": "ChainWithdrawRequest",
        "response": "ChainTx",
    },
    # ====================== PoL ==========================================
    {
        "path": "/pol/server-info",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Hash scheme + Ed25519 public key for snapshot verification",
        "auth": "none",
        "response": "PolServerInfo",
    },
    {
        "path": "/pol/latest-epoch",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Most recent PoL Merkle-sum commitment",
        "auth": "none",
        "response": "PolEpoch",
    },
    {
        "path": "/pol/my-proof",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Self-contained Merkle proof for the bearer's balance",
        "auth": "bearer",
        "response": "PolMyProof",
    },
    {
        "path": "/pol/refresh",
        "method": "POST",
        "tags": ["pol"],
        "summary": "Force a new PoL epoch capture",
        "auth": "bearer",
        "response": "PolEpoch",
    },
    {
        "path": "/pol/reserves-vs-liabilities",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Per-asset reserves vs aggregate liabilities",
        "auth": "none",
        "response": "ReservesVsLiabilities",
    },
    {
        "path": "/pol/reserves-history",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Historical reserves-vs-liabilities snapshots",
        "auth": "none",
        "params": [
            {
                "name": "limit",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "default": 100},
            }
        ],
        "response_inline": {"type": "array", "items": {"type": "object"}},
    },
    # ====================== zkPoL upstream (token-scoped reads) ==========
    {
        "path": "/zkpol/tokens/{token_id}/summary",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Live zkPoL aggregate summary for a token",
        "auth": "none",
        "params": [
            {"name": "token_id", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
        "response_inline": {"type": "object"},
    },
    {
        "path": "/zkpol/tokens/{token_id}/pipeline",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Live zkPoL pipeline status",
        "auth": "none",
        "params": [
            {"name": "token_id", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
        "response_inline": {"type": "object"},
    },
    {
        "path": "/zkpol/tokens/{token_id}/batches",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Recent zkPoL Merkle batches",
        "auth": "none",
        "params": [
            {"name": "token_id", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
        "response_inline": {"type": "array", "items": {"type": "object"}},
    },
    {
        "path": "/zkpol/tokens/{token_id}/accounts/{address}",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Per-account zkPoL inclusion proof",
        "auth": "none",
        "params": [
            {"name": "token_id", "in": "path", "required": True, "schema": {"type": "string"}},
            {"name": "address", "in": "path", "required": True, "schema": {"type": "string"}},
        ],
        "response_inline": {"type": "object"},
    },
    # ====================== PoL snapshot upstream =========================
    {
        "path": "/pol-snapshot/api/v1/certificate/latest",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Latest 5-minute Merkle-sum certificate",
        "auth": "none",
        "response": "PolSnapshotCertificate",
    },
    {
        "path": "/pol-snapshot/api/v1/certificate/{epoch}",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Certificate for a specific epoch",
        "auth": "none",
        "params": [
            {
                "name": "epoch",
                "in": "path",
                "required": True,
                "schema": {"type": "integer", "format": "int64"},
            }
        ],
        "response": "PolSnapshotCertificate",
    },
    {
        "path": "/pol-snapshot/api/v1/certificate/pubkey",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Ed25519 public key used to sign certificates",
        "auth": "none",
        "response_inline": {"type": "object", "properties": {"pubkey": {"type": "string"}}},
    },
    {
        "path": "/pol-snapshot/api/v1/audit/user-proof",
        "method": "GET",
        "tags": ["pol"],
        "summary": "Per-user audit proof against a certificate",
        "auth": "bearer",
        "params": [
            {"name": "user", "in": "query", "required": True, "schema": {"type": "string"}},
            {
                "name": "epoch",
                "in": "query",
                "required": False,
                "schema": {"type": "integer", "format": "int64"},
            },
        ],
        "response_inline": {"type": "object"},
    },
    # ====================== API keys =====================================
    {
        "path": "/api-keys/create",
        "method": "POST",
        "tags": ["api-keys"],
        "summary": "Issue an HMAC API key (Binance-compatible)",
        "auth": "bearer",
        "body": "ApiKeyCreateRequest",
        "response": "ApiKeyCreateResponse",
        "status": 201,
    },
    {
        "path": "/api-keys/list",
        "method": "GET",
        "tags": ["api-keys"],
        "summary": "List the bearer's API keys",
        "auth": "bearer",
        "response_inline": {"type": "array", "items": {"$ref": "#/components/schemas/ApiKey"}},
    },
    {
        "path": "/api-keys/{key_id}",
        "method": "PATCH",
        "tags": ["api-keys"],
        "summary": "Update label / scopes / caps for an API key",
        "auth": "bearer",
        "params": [
            {"name": "key_id", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
        "response": "ApiKey",
    },
    {
        "path": "/api-keys/{key_id}",
        "method": "DELETE",
        "tags": ["api-keys"],
        "summary": "Revoke an API key",
        "auth": "bearer",
        "params": [
            {"name": "key_id", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
        "status": 204,
        "response_inline": None,
    },
    {
        "path": "/api-keys/{key_id}/usage",
        "method": "GET",
        "tags": ["api-keys"],
        "summary": "Per-key usage counters (signed requests, withdraw notional, …)",
        "auth": "bearer",
        "params": [
            {"name": "key_id", "in": "path", "required": True, "schema": {"type": "string"}}
        ],
        "response_inline": {"type": "object"},
    },
    # ====================== Conditional orders ===========================
    {
        "path": "/orders/conditional",
        "method": "POST",
        "tags": ["conditional-orders"],
        "summary": "Create a Stop-Limit, Stop-Market, or OCO order",
        "auth": "bearer",
        "body": "ConditionalOrderRequest",
        "response": "ConditionalOrder",
    },
    {
        "path": "/orders/conditional",
        "method": "GET",
        "tags": ["conditional-orders"],
        "summary": "List pending conditional orders",
        "auth": "bearer",
        "response_inline": {
            "type": "array",
            "items": {"$ref": "#/components/schemas/ConditionalOrder"},
        },
    },
    {
        "path": "/orders/conditional/{client_order_id}",
        "method": "DELETE",
        "tags": ["conditional-orders"],
        "summary": "Cancel a pending conditional order",
        "auth": "bearer",
        "params": [
            {
                "name": "client_order_id",
                "in": "path",
                "required": True,
                "schema": {"type": "string"},
            }
        ],
        "response_inline": {"type": "object", "properties": {"cancelled": {"type": "boolean"}}},
    },
    # ====================== Exports =====================================
    {
        "path": "/export/index.json",
        "method": "GET",
        "tags": ["exports"],
        "summary": "List downloadable CSV reports",
        "auth": "bearer",
        "response": "ExportIndex",
    },
    {
        "path": "/export/trades.csv",
        "method": "GET",
        "tags": ["exports"],
        "summary": "CSV of fills (spot + futures)",
        "auth": "bearer",
        "response_inline": {"type": "string", "format": "binary"},
        "produces": "text/csv",
    },
    {
        "path": "/export/deposits.csv",
        "method": "GET",
        "tags": ["exports"],
        "summary": "CSV of on-chain deposits",
        "auth": "bearer",
        "response_inline": {"type": "string", "format": "binary"},
        "produces": "text/csv",
    },
    {
        "path": "/export/withdraws.csv",
        "method": "GET",
        "tags": ["exports"],
        "summary": "CSV of on-chain withdrawals",
        "auth": "bearer",
        "response_inline": {"type": "string", "format": "binary"},
        "produces": "text/csv",
    },
    {
        "path": "/export/orders.csv",
        "method": "GET",
        "tags": ["exports"],
        "summary": "CSV of all placed orders",
        "auth": "bearer",
        "response_inline": {"type": "string", "format": "binary"},
        "produces": "text/csv",
    },
    {
        "path": "/export/balance-history.csv",
        "method": "GET",
        "tags": ["exports"],
        "summary": "CSV of daily balance history",
        "auth": "bearer",
        "response_inline": {"type": "string", "format": "binary"},
        "produces": "text/csv",
    },
    {
        "path": "/export/tax.csv",
        "method": "GET",
        "tags": ["exports"],
        "summary": "CSV of taxable events",
        "auth": "bearer",
        "response_inline": {"type": "string", "format": "binary"},
        "produces": "text/csv",
    },
    # ====================== WebSocket ====================================
    {
        "path": "/ws/stream",
        "method": "GET",
        "tags": ["websocket"],
        "summary": (
            "WebSocket market-data feed. Send a JSON SUBSCRIBE message of the "
            'form {"method":"SUBSCRIBE","params":["ethusdt@trade",'
            '"ethusdt@depth"]}. Server pushes Binance-shaped trade, depth, '
            "and ticker events."
        ),
        "auth": "none",
        "response_inline": {"type": "string", "description": "Upgrade: websocket"},
    },
    # ====================== MCP ==========================================
    {
        "path": "/mcp",
        "method": "POST",
        "tags": ["mcp"],
        "summary": "Model Context Protocol JSON-RPC endpoint",
        "auth": "bearer",
        "body": "JsonRpcRequest",
        "response": "JsonRpcResponse",
    },
    {
        "path": "/mcp/stream",
        "method": "GET",
        "tags": ["mcp"],
        "summary": "MCP server-sent events stream",
        "auth": "bearer",
        "response_inline": {"type": "string", "description": "text/event-stream"},
        "produces": "text/event-stream",
    },
    # ====================== Push =========================================
    {
        "path": "/push/vapid-public-key",
        "method": "GET",
        "tags": ["push"],
        "summary": "VAPID public key for Web Push subscriptions",
        "auth": "none",
        "response": "VapidKey",
    },
    {
        "path": "/push/subscribe",
        "method": "POST",
        "tags": ["push"],
        "summary": "Register a Web Push subscription",
        "auth": "bearer",
        "body": "PushSubscription",
        "response_inline": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    },
    {
        "path": "/push/unsubscribe",
        "method": "POST",
        "tags": ["push"],
        "summary": "Remove a Web Push subscription",
        "auth": "bearer",
        "response_inline": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    },
    {
        "path": "/push/test",
        "method": "POST",
        "tags": ["push"],
        "summary": "Send a test push notification to the bearer",
        "auth": "bearer",
        "response_inline": {"type": "object", "properties": {"sent": {"type": "integer"}}},
    },
]


TAGS = [
    {"name": "auth", "description": "Email/password sessions and bearer tokens."},
    {"name": "kyc", "description": "Korean PASS-style + Sumsub identity verification."},
    {"name": "spot-market", "description": "Public spot market data (Binance-compatible)."},
    {"name": "spot-trade", "description": "Signed spot trading."},
    {"name": "spot-account", "description": "Signed spot wallet operations."},
    {"name": "futures", "description": "USDT-margined perpetual futures."},
    {"name": "chain", "description": "On-chain deposit detection + withdrawals."},
    {"name": "pol", "description": "Proof-of-Liabilities (epoch snapshots, certificates, zkPoL)."},
    {"name": "api-keys", "description": "HMAC API key issuance and revocation."},
    {"name": "conditional-orders", "description": "Stop-Limit, Stop-Market, and OCO orders."},
    {"name": "exports", "description": "Downloadable CSV reports (trades, tax, balance history)."},
    {"name": "websocket", "description": "Streaming market-data WebSocket."},
    {"name": "mcp", "description": "Model Context Protocol JSON-RPC."},
    {"name": "push", "description": "PWA Web Push subscriptions."},
]


# ---------------------------------------------------------------------------
# Spec assembly
# ---------------------------------------------------------------------------


def _resp(ep: dict) -> dict:
    status = ep.get("status", 200)
    if status == 204:
        return {"204": {"description": "No content."}}
    produces = ep.get("produces", "application/json")
    if "response" in ep:
        schema_ref = {"$ref": f"#/components/schemas/{ep['response']}"}
    elif "response_inline" in ep and ep["response_inline"] is not None:
        schema_ref = ep["response_inline"]
    else:
        schema_ref = {"type": "object"}
    resp = {
        str(status): {
            "description": ep.get("summary", "OK"),
            "content": {produces: {"schema": schema_ref}},
        }
    }
    # Default error response — always present for documentation completeness.
    err_schema = (
        "BinanceError" if ep["path"].startswith(("/v3/", "/fapi/v1/", "/sapi/")) else "Error"
    )
    resp["default"] = {
        "description": "Error response",
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{err_schema}"}}},
    }
    return resp


def _request_body(ep: dict) -> dict | None:
    if "body" not in ep or ep["body"] is None:
        return None
    return {
        "required": True,
        "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{ep['body']}"}}},
    }


def _security_for(auth: str) -> list[dict]:
    if auth == "none":
        return []
    if auth == "bearer":
        return [{"BearerAuth": []}]
    if auth == "hmac":
        return [{"BinanceHmacAuth": []}]
    return []


def build_spec() -> dict:
    paths: dict[str, dict[str, Any]] = {}
    for ep in ENDPOINTS:
        p = ep["path"]
        op = {
            "summary": ep.get("summary", ""),
            "tags": ep.get("tags", []),
            "operationId": _operation_id(ep),
            "responses": _resp(ep),
        }
        params = ep.get("params") or []
        if params:
            op["parameters"] = params
        body = _request_body(ep)
        if body:
            op["requestBody"] = body
        sec = _security_for(ep.get("auth", "none"))
        if sec:
            op["security"] = sec
        paths.setdefault(p, {})[ep["method"].lower()] = op

    spec = {
        "openapi": "3.1.0",
        "info": {
            "title": TITLE,
            "version": VERSION,
            "description": DESCRIPTION,
            "license": {"name": "See repo LICENSE-THIRD-PARTY"},
        },
        "servers": [
            {"url": "http://localhost:5500", "description": "Local zkCEX proxy"},
        ],
        "tags": TAGS,
        "components": {
            "securitySchemes": {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": (
                        "Session token issued by /auth/login or /auth/signup. "
                        "Send as `Authorization: Bearer <token>`."
                    ),
                },
                "BinanceHmacAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-MBX-APIKEY",
                    "description": (
                        "Binance-compatible HMAC scheme. Set the API key id "
                        "in the `X-MBX-APIKEY` header. The full query "
                        "string (including timestamp and recvWindow) is "
                        "signed with HMAC-SHA256(secret, queryString) and "
                        "appended as `&signature=<hex>`."
                    ),
                },
            },
            "schemas": SCHEMAS,
        },
        "paths": paths,
    }
    return spec


def _operation_id(ep: dict) -> str:
    method = ep["method"].lower()
    p = ep["path"]
    cleaned = (
        p.replace("/", "_").replace("{", "").replace("}", "").replace(".", "_").replace("-", "_")
    )
    if cleaned.startswith("_"):
        cleaned = cleaned[1:]
    return f"{method}_{cleaned}"


# ---------------------------------------------------------------------------
# Minimal YAML writer (no PyYAML dep). Handles the subset we emit: dicts,
# lists, strings, numbers, bools, None. Keys are emitted unquoted when safe.
# ---------------------------------------------------------------------------


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    # Always quote strings to avoid YAML parser surprises (e.g. "true",
    # "1.0", "null", strings with ':'). Use JSON-style double quotes —
    # those are valid YAML 1.2 flow scalars too.
    return json.dumps(s, ensure_ascii=False)


def _yaml_key(k: str) -> str:
    if (
        isinstance(k, str)
        and all(c.isalnum() or c in "_-/" for c in k)
        and k
        and not k[0].isdigit()
    ):
        return k
    return json.dumps(k, ensure_ascii=False)


def dict_to_yaml(obj: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        lines = []
        for k, v in obj.items():
            key = _yaml_key(k)
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{key}:")
                lines.append(dict_to_yaml(v, indent + 1))
            elif isinstance(v, (dict, list)):
                lines.append(f"{pad}{key}: {'{}' if isinstance(v, dict) else '[]'}")
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(v)}")
        return "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[]"
        lines = []
        for item in obj:
            if isinstance(item, (dict, list)) and item:
                # Block form for nested structures.
                sub = dict_to_yaml(item, indent + 1)
                # Replace first indent of sub with `- ` marker.
                first_pad = "  " * (indent + 1)
                if sub.startswith(first_pad):
                    sub = f"{pad}- " + sub[len(first_pad) :]
                    lines.append(sub)
                else:
                    lines.append(f"{pad}- {sub.lstrip()}")
            elif isinstance(item, (dict, list)):
                lines.append(f"{pad}- {'{}' if isinstance(item, dict) else '[]'}")
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_yaml_scalar(obj)}"


def main(argv: list[str]) -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    spec = build_spec()

    json_path = os.path.join(OUT_DIR, "openapi.json")
    yaml_path = os.path.join(OUT_DIR, "openapi.yaml")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(spec, f, indent=2, ensure_ascii=False)
        f.write("\n")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write("# zkCEX OpenAPI 3.1 — auto-generated by tools/openapi_gen.py\n")
        f.write(dict_to_yaml(spec))
        f.write("\n")

    print(
        f"wrote {json_path} ({os.path.getsize(json_path)} bytes, "
        f"{len(spec['paths'])} paths, "
        f"{len(spec['components']['schemas'])} schemas, "
        f"{len(spec['tags'])} tags)"
    )
    print(f"wrote {yaml_path} ({os.path.getsize(yaml_path)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
