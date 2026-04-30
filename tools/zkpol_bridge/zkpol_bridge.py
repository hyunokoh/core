#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional, Sequence, Set
from urllib.parse import parse_qs, unquote, urlparse

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError as exc:  # pragma: no cover - runtime dependency
    psycopg2 = None
    RealDictCursor = None
    POSTGRES_IMPORT_ERROR = exc
else:
    POSTGRES_IMPORT_ERROR = None

try:
    import pymysql
except ImportError as exc:  # pragma: no cover - runtime dependency
    pymysql = None
    MYSQL_IMPORT_ERROR = exc
else:
    MYSQL_IMPORT_ERROR = None


LOGGER = logging.getLogger("zkpol-bridge")
DEFAULT_EVENT_ID_OFFSET = 1_000_000_000_000
DEFAULT_EVENT_TYPE_MAP = {
    "withdraw": "withdrawal",
}


@dataclass(frozen=True)
class BridgeConfig:
    postgres_dsn: str
    mariadb_dsn: str
    bridge_name: str
    batch_size: int
    poll_interval_seconds: float
    ledger_event_id_offset: int
    token_allowlist: Optional[Set[str]]
    event_type_map: Dict[str, str]

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        postgres_dsn = required_env("OPEX_WALLET_POSTGRES_DSN")
        mariadb_dsn = required_env("ZKPOL_MARIADB_DSN")
        allowlist = parse_allowlist(os.getenv("ZKPOL_TOKEN_ALLOWLIST", "").strip())
        return cls(
            postgres_dsn=postgres_dsn,
            mariadb_dsn=mariadb_dsn,
            bridge_name=os.getenv("ZKPOL_BRIDGE_NAME", "default"),
            batch_size=int(os.getenv("ZKPOL_BRIDGE_BATCH_SIZE", "500")),
            poll_interval_seconds=float(os.getenv("ZKPOL_BRIDGE_POLL_INTERVAL_SECONDS", "5")),
            ledger_event_id_offset=int(os.getenv("ZKPOL_LEDGER_EVENT_ID_OFFSET", str(DEFAULT_EVENT_ID_OFFSET))),
            token_allowlist=allowlist,
            event_type_map=parse_event_type_map(os.getenv("ZKPOL_EVENT_TYPE_MAP", "")),
        )


@dataclass(frozen=True)
class OutboxEvent:
    outbox_id: int
    token_id: str
    account_id: str
    balance: Decimal
    delta: Decimal
    event_type: str
    occurred_at: datetime
    reference_id: str
    source_system: str
    precision: int


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"missing required environment variable: {name}")
    return value


def parse_allowlist(raw: str) -> Optional[Set[str]]:
    if not raw:
        return None
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def parse_event_type_map(raw: str) -> Dict[str, str]:
    mapping = dict(DEFAULT_EVENT_TYPE_MAP)
    if not raw.strip():
        return mapping
    for item in raw.split(","):
        if not item.strip():
            continue
        source, _, target = item.partition(":")
        if not source.strip() or not target.strip():
            raise ValueError(f"invalid ZKPOL_EVENT_TYPE_MAP entry: {item}")
        mapping[source.strip().lower()] = target.strip().lower()
    return mapping


def parse_mysql_dsn(dsn: str) -> Dict[str, object]:
    parsed = urlparse(dsn)
    if parsed.scheme not in {"mysql", "mariadb"}:
        raise ValueError(f"unsupported MariaDB DSN scheme: {parsed.scheme}")
    query = parse_qs(parsed.query)
    return {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": parsed.path.lstrip("/"),
        "charset": query.get("charset", ["utf8mb4"])[0],
        "autocommit": False,
    }


def normalize_event_type(event_type: str, event_type_map: Dict[str, str]) -> str:
    normalized = event_type.strip().lower()
    return event_type_map.get(normalized, normalized)


def decimal_to_scaled_int(value: Decimal, precision: int) -> int:
    if precision < 0:
        raise ValueError(f"precision must be >= 0, got {precision}")
    scaled = value * (Decimal(10) ** precision)
    integral = scaled.to_integral_exact()
    return int(integral)


def bridge_event_id(outbox_id: int, offset: int) -> int:
    return offset + outbox_id


def ensure_dependencies() -> None:
    errors = []
    if psycopg2 is None:
        errors.append(f"psycopg2-binary not available: {POSTGRES_IMPORT_ERROR}")
    if pymysql is None:
        errors.append(f"PyMySQL not available: {MYSQL_IMPORT_ERROR}")
    if errors:
        raise RuntimeError("; ".join(errors))


def pg_connect(dsn: str):
    return psycopg2.connect(dsn)


def mysql_connect(dsn: str):
    return pymysql.connect(**parse_mysql_dsn(dsn))


def ensure_bridge_state(pg_conn, bridge_name: str) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO zkpol_bridge_state (bridge_name, last_outbox_id)
            VALUES (%s, 0)
            ON CONFLICT (bridge_name) DO NOTHING
            """,
            (bridge_name,),
        )
        cur.execute(
            "SELECT last_outbox_id FROM zkpol_bridge_state WHERE bridge_name = %s",
            (bridge_name,),
        )
        row = cur.fetchone()
    pg_conn.commit()
    if row is None:
        raise RuntimeError(f"failed to initialize bridge state for {bridge_name}")
    return int(row[0])


def fetch_outbox_batch(pg_conn, cfg: BridgeConfig, last_outbox_id: int) -> List[OutboxEvent]:
    token_filter = ""
    params: List[object] = [last_outbox_id]
    if cfg.token_allowlist:
        token_filter = "AND o.token_id = ANY(%s)"
        params.append(sorted(cfg.token_allowlist))
    params.append(cfg.batch_size)

    with pg_conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                o.id,
                o.token_id,
                o.account_id,
                o.balance::text AS balance,
                o.delta::text AS delta,
                o.event_type,
                o.occurred_at,
                o.reference_id,
                o.source_system,
                c.precision::text AS precision
            FROM zkpol_liability_outbox o
            JOIN currency c ON c.symbol = o.token_id
            WHERE o.id > %s
              {token_filter}
            ORDER BY o.id ASC
            LIMIT %s
            """,
            params,
        )
        rows = cur.fetchall()

    events = []
    for row in rows:
        try:
            precision = int(Decimal(row["precision"]))
            balance = Decimal(row["balance"])
            delta = Decimal(row["delta"])
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"invalid decimal data in outbox row {row['id']}: {exc}") from exc

        occurred_at = row["occurred_at"]
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)

        events.append(
            OutboxEvent(
                outbox_id=int(row["id"]),
                token_id=row["token_id"],
                account_id=row["account_id"],
                balance=balance,
                delta=delta,
                event_type=row["event_type"],
                occurred_at=occurred_at.astimezone(timezone.utc),
                reference_id=row["reference_id"],
                source_system=row["source_system"],
                precision=precision,
            )
        )
    return events


def insert_events(mysql_conn, cfg: BridgeConfig, events: Sequence[OutboxEvent]) -> None:
    if not events:
        return

    rows = []
    for event in events:
        rows.append(
            (
                bridge_event_id(event.outbox_id, cfg.ledger_event_id_offset),
                event.account_id,
                event.token_id,
                decimal_to_scaled_int(event.balance, event.precision),
                decimal_to_scaled_int(event.delta, event.precision),
                normalize_event_type(event.event_type, cfg.event_type_map),
                event.occurred_at.replace(tzinfo=None),
            )
        )

    with mysql_conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ledger_change_event (
                id,
                account_id,
                token_id,
                balance,
                delta,
                event_type,
                occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                account_id = VALUES(account_id),
                token_id = VALUES(token_id),
                balance = VALUES(balance),
                delta = VALUES(delta),
                event_type = VALUES(event_type),
                occurred_at = VALUES(occurred_at)
            """,
            rows,
        )
    mysql_conn.commit()


def update_bridge_state(pg_conn, bridge_name: str, last_outbox_id: int) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            UPDATE zkpol_bridge_state
            SET last_outbox_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE bridge_name = %s
            """,
            (last_outbox_id, bridge_name),
        )
    pg_conn.commit()


def run_once(cfg: BridgeConfig) -> int:
    ensure_dependencies()
    pg_conn = pg_connect(cfg.postgres_dsn)
    mysql_conn = mysql_connect(cfg.mariadb_dsn)
    try:
        last_outbox_id = ensure_bridge_state(pg_conn, cfg.bridge_name)
        events = fetch_outbox_batch(pg_conn, cfg, last_outbox_id)
        if not events:
            LOGGER.info("no pending zkPoL outbox events after id=%s", last_outbox_id)
            return 0

        insert_events(mysql_conn, cfg, events)
        update_bridge_state(pg_conn, cfg.bridge_name, events[-1].outbox_id)
        LOGGER.info(
            "bridged %s events into zkPoL ledger_change_event ids=%s..%s outbox=%s..%s",
            len(events),
            bridge_event_id(events[0].outbox_id, cfg.ledger_event_id_offset),
            bridge_event_id(events[-1].outbox_id, cfg.ledger_event_id_offset),
            events[0].outbox_id,
            events[-1].outbox_id,
        )
        return len(events)
    except Exception:
        pg_conn.rollback()
        mysql_conn.rollback()
        raise
    finally:
        pg_conn.close()
        mysql_conn.close()


def run_daemon(cfg: BridgeConfig) -> None:
    while True:
        bridged = run_once(cfg)
        if bridged < cfg.batch_size:
            time.sleep(cfg.poll_interval_seconds)


def show_status(cfg: BridgeConfig) -> None:
    ensure_dependencies()
    pg_conn = pg_connect(cfg.postgres_dsn)
    try:
        last_outbox_id = ensure_bridge_state(pg_conn, cfg.bridge_name)
        with pg_conn.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(id), 0) FROM zkpol_liability_outbox")
            latest_outbox_id = int(cur.fetchone()[0])
        print(
            f"bridge_name={cfg.bridge_name}\n"
            f"last_outbox_id={last_outbox_id}\n"
            f"latest_outbox_id={latest_outbox_id}\n"
            f"pending={max(latest_outbox_id - last_outbox_id, 0)}"
        )
    finally:
        pg_conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bridge OPEX zkPoL outbox rows into zkPoL ledger_change_event")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("run-once")
    subparsers.add_parser("daemon")
    subparsers.add_parser("status")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        cfg = BridgeConfig.from_env()
        if args.command == "run-once":
            run_once(cfg)
        elif args.command == "daemon":
            run_daemon(cfg)
        elif args.command == "status":
            show_status(cfg)
        else:  # pragma: no cover - argparse prevents this
            parser.error(f"unknown command: {args.command}")
    except Exception as exc:
        LOGGER.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
