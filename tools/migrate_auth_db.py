#!/usr/bin/env python3
"""Migrate the zkCEX auth + KYC tables between backends.

Both directions are supported:

    # SQLite -> Postgres
    python3 tools/migrate_auth_db.py \
        --from sqlite --from-path tools/.local/auth.db \
        --to postgres --to-dsn "postgresql://app:app-password@127.0.0.1:5433/zkcex_auth"

    # Postgres -> SQLite (for restore / rollback)
    python3 tools/migrate_auth_db.py \
        --from postgres --from-dsn "postgresql://app:app-password@127.0.0.1:5433/zkcex_auth" \
        --to sqlite --to-path tools/.local/auth.db

Strategy: read each table from source, TRUNCATE the destination table, INSERT
each row preserving primary keys (so existing sessions stay valid).

Tables are migrated in FK-safe order: parents before children. Each table is
wrapped in its own transaction; the script aborts cleanly on any failure
(typically a duplicate-key collision, e.g. you ran it twice without
``--truncate-dest``).

After all tables are migrated, the script:
1. Validates that source and destination row counts match.
2. Bumps Postgres IDENTITY sequences so the next INSERT does not collide
   with a migrated id (no-op for SQLite).

This tool only depends on the stdlib + ``pg8000`` (the same dep auth_server
already needs for the postgres backend).
"""

from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import auth_db  # noqa: E402

# FK-safe order. Parents (users) come before children (sessions, kyc_*).
TABLES = [
    "users",
    "sessions",
    "kyc_verifications",
    "sumsub_applicants",
    "sumsub_webhooks",
]


def _open(side: str, args: argparse.Namespace):
    backend = getattr(args, f"{side}")
    if backend == "sqlite":
        path = getattr(args, f"{side}_path")
        if not path:
            sys.exit(f"--{side}-path is required for sqlite")
        return auth_db.SqliteAuthDB(path=path)
    if backend == "postgres":
        dsn = getattr(args, f"{side}_dsn")
        if not dsn:
            sys.exit(f"--{side}-dsn is required for postgres")
        return auth_db.PostgresAuthDB(dsn=dsn)
    sys.exit(f"unknown backend for --{side}: {backend!r}")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--from", dest="from_", choices=["sqlite", "postgres"], required=True, help="source backend"
    )
    p.add_argument("--from-path", help="sqlite source path")
    p.add_argument("--from-dsn", help="postgres source DSN")
    p.add_argument(
        "--to", choices=["sqlite", "postgres"], required=True, help="destination backend"
    )
    p.add_argument("--to-path", help="sqlite destination path")
    p.add_argument("--to-dsn", help="postgres destination DSN")
    p.add_argument(
        "--truncate-dest",
        action="store_true",
        help="wipe destination tables before inserting (default: " "fail on first duplicate)",
    )
    p.add_argument("--dry-run", action="store_true", help="just report row counts; don't write")
    args = p.parse_args()

    # argparse can't have a hyphen in the dest, but we prefer the natural
    # CLI flags. Reshape the namespace so _open() finds 'from'/'from_path'.
    setattr(args, "from", args.from_)
    args.from_path = getattr(args, "from_path", None)
    args.from_dsn = getattr(args, "from_dsn", None)
    args.to_path = getattr(args, "to_path", None)
    args.to_dsn = getattr(args, "to_dsn", None)

    src = _open("from", args)
    dst = _open("to", args)

    print(f"[migrate] {src.backend} -> {dst.backend}")
    # Make sure destination has the schema in place.
    dst.init_schema()

    counts: dict[str, tuple[int, int]] = {}
    if args.truncate_dest and not args.dry_run:
        # Reverse order so children are wiped before parents.
        for t in reversed(TABLES):
            print(f"[migrate]   truncate dest.{t}")
            dst.truncate(t)

    for t in TABLES:
        src_rows = src.fetch_all(t)
        n_before = len(src_rows)
        if args.dry_run:
            print(f"[migrate]   {t}: {n_before} rows (dry-run, skipping insert)")
            counts[t] = (n_before, 0)
            continue

        for row in src_rows:
            try:
                dst.insert_raw(t, row)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(
                    f"[migrate] FAIL on {t}: {e!r}\n"
                    f"  hint: pass --truncate-dest if the dest table is not empty.\n"
                )
                return 2
        # Re-read destination to verify count.
        n_after = len(dst.fetch_all(t))
        counts[t] = (n_before, n_after)
        print(f"[migrate]   {t}: {n_before} -> {n_after}")

    if not args.dry_run:
        print("[migrate] resetting destination IDENTITY sequences (if any)")
        dst.reset_sequences()

    bad = [t for t, (a, b) in counts.items() if a != b]
    if bad:
        sys.stderr.write(f"[migrate] row-count mismatch: {bad}\n")
        return 3

    print("[migrate] OK. summary: " + ", ".join(f"{t} {a}->{b}" for t, (a, b) in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
