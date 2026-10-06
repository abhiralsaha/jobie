"""
db.py

Supabase / PostgreSQL database layer using psycopg2.
Replaces the former SQLite (sqlite3) implementation.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.extras
import psycopg2.pool

from app.config import settings

# ---------------------------------------------------------------------------
# Connection pool (thread-safe, min 1 / max 10 connections)
# ---------------------------------------------------------------------------

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            if not settings.SUPABASE_DB_URL:
                raise RuntimeError(
                    "SUPABASE_DB_URL is not set. "
                    "Add it to your .env file in the format:\n"
                    "  SUPABASE_DB_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres"
                )
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=1,
                maxconn=10,
                dsn=settings.SUPABASE_DB_URL,
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
    return _pool


@contextmanager
def get_connection() -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a pooled psycopg2 connection.  Returns it to the pool on exit."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all required tables in Supabase if they don't already exist."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            # ── jobs seen via the intake API ─────────────────────────────────
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs_seen (
                    id             BIGSERIAL PRIMARY KEY,
                    job_id         TEXT UNIQUE NOT NULL,
                    title          TEXT,
                    company        TEXT,
                    url            TEXT,
                    posted_at      TEXT,
                    first_seen_at  TIMESTAMPTZ DEFAULT NOW(),
                    status         TEXT DEFAULT 'new'
                )
                """
            )
            # ── jobs the user has applied to ─────────────────────────────────
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs_applied (
                    id          BIGSERIAL PRIMARY KEY,
                    job_id      TEXT UNIQUE NOT NULL,
                    title       TEXT,
                    company     TEXT,
                    url         TEXT,
                    applied_at  TIMESTAMPTZ DEFAULT NOW(),
                    result      TEXT,
                    notes       TEXT
                )
                """
            )
            # ── telegram alert log ────────────────────────────────────────────
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts_sent (
                    id       BIGSERIAL PRIMARY KEY,
                    job_id   TEXT,
                    message  TEXT,
                    sent_at  TIMESTAMPTZ DEFAULT NOW(),
                    status   TEXT DEFAULT 'sent'
                )
                """
            )
            # ── LinkedIn seen_jobs (used by linkedin_fresh_jobs.py) ───────────
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_jobs (
                    job_id      TEXT PRIMARY KEY,
                    title       TEXT,
                    company     TEXT,
                    first_seen  DOUBLE PRECISION,
                    url         TEXT,
                    posted_at   TEXT
                )
                """
            )
            # ── daily digest tracking ─────────────────────────────────────────
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS digest_runs (
                    digest_date TEXT PRIMARY KEY,
                    sent_at     TEXT NOT NULL
                )
                """
            )
            # ── manual digest rate-limiting ───────────────────────────────────
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS manual_digest_runs (
                    id            BIGSERIAL PRIMARY KEY,
                    local_date    TEXT NOT NULL,
                    requested_at  TEXT NOT NULL,
                    status        TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_manual_digest_runs_date
                    ON manual_digest_runs (local_date)
                """
            )
