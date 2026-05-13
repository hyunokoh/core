# 2. Stdlib-only Python services

Date: 2026-05-12

## Status
Accepted

## Context
We have ~25 Python microservices. Adding pip deps to each multiplies the supply-chain attack surface, container build time, and image size.

## Decision
Python services use only the standard library, with one exception: `pg8000` (pure-Python Postgres driver) for the auth backend. All other persistence is SQLite (stdlib) or HTTP-mediated.

## Consequences
- Slower local development (no convenient libraries like `requests`, `pydantic`).
- Some code is more verbose (HTTP signing, JSON-RPC parsing, base32 decoding).
- Far smaller attack surface; reproducible builds easier.
- Some demo features (e.g., real Web Push encryption, gRPC Travel Rule) are deferred because stdlib alone can't ship them cleanly. They have documented escape hatches.
