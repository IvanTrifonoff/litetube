#!/usr/bin/env python3
"""Litetube init_operator: create / rotate the operator (admin) user.

Run BEFORE docker compose up so the operator exists at first boot.

Usage:
    sudo LITETUBE_DB_PATH=/srv/proxy-infra/db/litetube.db \
        python3 ./scripts/init_operator.py \
        --email admin@litetube.trfnv.ru \
        --random-password

Prints OPERATOR_PASSWORD=... exactly once. Operator is expected to save
it in their password manager immediately.

Note on SCHEMA_V5: users.password_hash became nullable in V5 (for
Google-only clients). The operator row always carries a bcrypt hash here
(init/rotate writes one unconditionally), so this script remains
schema-safe — no NOT NULL violation possible, and downgrade_to_v5_to_v4
(also in db.py) doesn't block on operator rows.
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

# Reuse Litube's bcrypt helper if importable from the deployed source tree.
# Otherwise fall back to system bcrypt (raising if missing).
try:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))
    from litetube.auth import hash_password
except Exception:
    import bcrypt  # type: ignore

    def hash_password(plain: str) -> str:
        return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(rounds=12)).decode()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", default="admin@litetube.trfnv.ru")
    parser.add_argument("--random-password", action="store_true",
                        help="generate a random password instead of prompting")
    parser.add_argument("--db",
                        default=os.environ.get(
                            "LITETUBE_DB_PATH", "/srv/proxy-infra/db/litetube.db"))
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[init_operator] DB {db_path} does not exist. "
              "Run setup.sh first to create /srv/proxy-infra/db/.")
        return 2

    # Schema preflight: confirm 'users' table exists. Without this check, the
    # subsequent INSERT fails with a cryptic 'no such table: users' traceback
    # when init_operator runs *before* the FastAPI container has applied its
    # migrations. In a normal setup.sh flow it's harmless because the FastAPI
    # container starts before init_operator runs.
    _check = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        hit = _check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='users' LIMIT 1"
        ).fetchone()
        if not hit:
            print(f"[init_operator] DB at {db_path} exists but has no 'users' table. "
                  "FastAPI container hasn't applied migrations yet. "
                  "Run setup.sh through 'docker compose up -d' first.")
            return 3
    finally:
        _check.close()

    email = args.email.lower()
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE email=? AND role='operator'",
            (email,)).fetchone()

        if args.random_password:
            pw = secrets.token_urlsafe(16)
        else:
            pw = getpass.getpass("new operator password (>=8 chars): ")
            if len(pw) < 8:
                print("password too short"); return 1
            pw2 = getpass.getpass("repeat: ")
            if pw != pw2:
                print("passwords do not match"); return 1

        h = hash_password(pw)
        now = _now()
        if existing:
            conn.execute(
                "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                (h, now, existing["id"]))
            print(f"[init_operator] password rotated for {email}")
        else:
            conn.execute(
                "INSERT INTO users(email,password_hash,role,status,trial_started_at,"
                "created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (email, h, "operator", "active", now, now, now))
            print(f"[init_operator] operator {email} created")
        conn.commit()

        print()
        print(f"OPERATOR_EMAIL={email}")
        print(f"OPERATOR_PASSWORD={pw}")
        print()
        print("Store this password NOW. It is not saved anywhere on disk.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
