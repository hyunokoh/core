#!/usr/bin/env python3
"""Bootstrap a new operator account for the compliance console.

Usage:
    python3 tools/ops_bootstrap.py --email ops@zkcex.test --name "Demo" --role admin

On success the plaintext ``staff_token`` is printed to **stderr** (so it is
trivial to capture with ``2>&1`` or redirect, but doesn't get caught by a
``$(...)`` capture by accident). Only the scrypt hash is persisted in
``tools/.local/ops.db``; the plaintext cannot be recovered after this run.

The operator must ALSO have a customer-tier account (same email, with a
password) in auth_server. The console verifies both halves at login time.
Use ``--rotate`` against an existing operator to mint a fresh staff_token
without touching anything else.
"""

from __future__ import annotations

import argparse
import sys

from ops_server import (
    DB_PATH,
    create_operator,
    init_db,
    rotate_staff_token,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--email", required=True, help="operator email (must also exist in auth_server)"
    )
    ap.add_argument("--name", help="display name (required unless --rotate)")
    ap.add_argument("--role", default="admin", choices=("compliance", "support", "admin"))
    ap.add_argument(
        "--rotate", action="store_true", help="rotate the staff_token of an existing operator"
    )
    args = ap.parse_args()

    init_db()

    if args.rotate:
        try:
            tok = rotate_staff_token(args.email)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(2)
        # Plaintext token to stderr, structured confirmation to stdout.
        print(f"staff_token (save this — won't be shown again):\n  {tok}", file=sys.stderr)
        print(f"OK rotated staff_token for {args.email}")
        print(f"db: {DB_PATH}")
        return

    if not args.name:
        print("--name is required when creating a new operator", file=sys.stderr)
        sys.exit(2)
    try:
        op_id, tok = create_operator(
            email=args.email,
            display_name=args.name,
            role=args.role,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    print(f"staff_token (save this — won't be shown again):\n  {tok}", file=sys.stderr)
    print(f"OK operator created id={op_id} email={args.email} " f"role={args.role}")
    print(f"db: {DB_PATH}")


if __name__ == "__main__":
    main()
