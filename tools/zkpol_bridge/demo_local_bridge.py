#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import pymysql

import zkpol_bridge as bridge_module


ROOT = Path("/Users/hoh/Documents/Projects/zkCEX/core")
WALLET_SCHEMA = ROOT / "wallet/wallet-ports/wallet-persister-postgres/src/main/resources/schema.sql"
ZKPOL_SCHEMA = Path("/Users/hoh/Documents/Projects/zkPoL/docs/reference/deployment/zkpol_schema_v9.sql")


def apply_postgres_schema(dsn: str) -> None:
    schema = WALLET_SCHEMA.read_text()
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(schema)
    finally:
        conn.close()


def apply_mariadb_schema(dsn: str) -> None:
    schema = ZKPOL_SCHEMA.read_text()
    conn = pymysql.connect(**bridge_module.parse_mysql_dsn(dsn))
    try:
        with conn.cursor() as cur:
            for stmt in [item.strip() for item in schema.split(";") if item.strip()]:
                cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def seed_sample_event(dsn: str, token_id: str, account_id: str, balance: str, delta: str, reference_id: str) -> None:
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO currency(symbol, name, precision)
                VALUES (%s, %s, %s)
                ON CONFLICT (symbol) DO UPDATE
                SET name = EXCLUDED.name,
                    precision = EXCLUDED.precision
                """,
                (token_id, token_id, 2),
            )
            cur.execute(
                """
                INSERT INTO zkpol_liability_outbox(
                    token_id,
                    account_id,
                    balance,
                    delta,
                    event_type,
                    occurred_at,
                    reference_id,
                    source_system
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (reference_id) DO NOTHING
                """,
                (
                    token_id,
                    account_id,
                    balance,
                    delta,
                    "deposit",
                    datetime.now(timezone.utc),
                    reference_id,
                    "opex-wallet",
                ),
            )
    finally:
        conn.close()


def print_latest_mariadb_rows(dsn: str, limit: int) -> None:
    conn = pymysql.connect(**bridge_module.parse_mysql_dsn(dsn))
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, account_id, token_id, balance, delta, event_type
                FROM ledger_change_event
                ORDER BY id DESC
                LIMIT %s
                """,
                (limit,),
            )
            for row in cur.fetchall():
                print(row)
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bootstrap and demo the local zkPoL bridge")
    parser.add_argument("--token-id", default="USDT")
    parser.add_argument("--account-id", default="user-bridge-demo")
    parser.add_argument("--balance", default="12.34")
    parser.add_argument("--delta", default="12.34")
    parser.add_argument("--reference-id", default="bridge-demo-ref-001")
    parser.add_argument("--limit", type=int, default=5)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    cfg = bridge_module.BridgeConfig.from_env()
    apply_postgres_schema(cfg.postgres_dsn)
    apply_mariadb_schema(cfg.mariadb_dsn)
    seed_sample_event(
        cfg.postgres_dsn,
        token_id=args.token_id,
        account_id=args.account_id,
        balance=args.balance,
        delta=args.delta,
        reference_id=args.reference_id,
    )
    bridge_module.run_once(cfg)
    print_latest_mariadb_rows(cfg.mariadb_dsn, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
