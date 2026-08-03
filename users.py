"""
users.py — per-user account storage for the multi-tenant cloud build.

Replaces google_service.py's single .google_cache/token.json (one shared
account) with one row per signed-in user, keyed by their Google account's
own id. SQLite via the stdlib — no new dependency, and Render/Railway/Fly
all support a small persistent-disk SQLite file for a project at this scale
(a real Postgres is a reasonable later upgrade, not a phase-1 requirement).
"""

from __future__ import annotations  # PEP 604 `X | None` hints, running on Python 3.9

import logging
import os
import sqlite3
import time
from contextlib import contextmanager

logger = logging.getLogger("jarvis.users")

DB_PATH = os.environ.get("JARVIS_DB_PATH", "users.db")


def _init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,       -- Google's own 'sub' (stable account id)
                email TEXT NOT NULL,
                name TEXT,
                google_token_json TEXT,    -- same JSON shape creds.to_json() already produces
                created_at REAL NOT NULL,
                last_login_at REAL NOT NULL
            )
            """
        )


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_user(user_id: str, email: str, name: str = "") -> None:
    """Called right after a successful Google sign-in. Creates the row on
    first login, just bumps last_login_at on every login after that."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO users (id, email, name, created_at, last_login_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                email = excluded.email,
                name = excluded.name,
                last_login_at = excluded.last_login_at
            """,
            (user_id, email, name, now, now),
        )


def get_user(user_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, email, name, google_token_json FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "email": row[1], "name": row[2], "google_token_json": row[3]}


def save_google_token(user_id: str, token_json: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET google_token_json = ? WHERE id = ?", (token_json, user_id))


def get_google_token(user_id: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT google_token_json FROM users WHERE id = ?", (user_id,)).fetchone()
    return row[0] if row else None


def clear_google_token(user_id: str) -> None:
    """Used by the per-user equivalent of the old disconnect() — drops the
    stored token without deleting the account row itself."""
    with _connect() as conn:
        conn.execute("UPDATE users SET google_token_json = NULL WHERE id = ?", (user_id,))


_init_db()
