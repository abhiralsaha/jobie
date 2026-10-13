"""Persistence: SQLite (default/dev/tests) or Postgres (set DATABASE_URL).

All SQL is written with ``?`` placeholders and the portable subset shared by
both engines (``ON CONFLICT`` upserts, single-statement compare-and-set).
Timestamps are fixed-width UTC ISO strings so string comparison is correct.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .util import iso, utcnow

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS ai_jobs (
        job_id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, url TEXT, posted_at TEXT,
        description TEXT, desc_hash TEXT, desc_complete INTEGER NOT NULL DEFAULT 0, source TEXT,
        stage TEXT NOT NULL DEFAULT 'discovered', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at TEXT, last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ai_classifications (
        job_id TEXT NOT NULL, desc_hash TEXT NOT NULL, prompt_version TEXT NOT NULL,
        decision TEXT NOT NULL, source TEXT, model TEXT, raw_json TEXT, final_json TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (job_id, desc_hash, prompt_version))""",
    """CREATE TABLE IF NOT EXISTS ai_deliveries (
        job_id TEXT NOT NULL, kind TEXT NOT NULL, dest_key TEXT NOT NULL, status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, detail TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY (job_id, kind, dest_key))""",
    """CREATE TABLE IF NOT EXISTS ai_resumes (
        job_id TEXT PRIMARY KEY, tex_path TEXT, pdf_path TEXT, pdf_sha256 TEXT, filename TEXT,
        status TEXT NOT NULL, detail_json TEXT, created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ai_contacts (
        job_id TEXT NOT NULL, slot TEXT NOT NULL, data_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY (job_id, slot))""",
    """CREATE TABLE IF NOT EXISTS ai_email_drafts (
        draft_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, slot TEXT NOT NULL, to_email TEXT, to_name TEXT,
        subject TEXT, body TEXT, resume_path TEXT, resume_sha256 TEXT, content_hash TEXT NOT NULL,
        state TEXT NOT NULL, sendable INTEGER NOT NULL DEFAULT 0, block_reason TEXT,
        tg_chat_id TEXT, tg_message_id INTEGER, approver_id TEXT, decided_at TEXT, sent_at TEXT,
        outcome TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS ai_kv (k TEXT PRIMARY KEY, v TEXT, updated_at TEXT)""",
]


class Store:
    def __init__(self, db_url: str | None = None, sqlite_path: str = "data/ai_pipeline.sqlite3"):
        self._lock = threading.RLock()
        self.kind = "pg" if db_url else "sqlite"
        if db_url:
            import psycopg2.pool  # imported lazily: only needed for Postgres

            self._pool = psycopg2.pool.ThreadedConnectionPool(1, 4, dsn=db_url)
        else:
            if sqlite_path != ":memory:":
                Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(sqlite_path, check_same_thread=False, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout=5000")
            if sqlite_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()                      # idempotent (CREATE TABLE IF NOT EXISTS)

    # -- low level ---------------------------------------------------------
    def execute(self, sql: str, params: tuple = ()) -> int:
        if self.kind == "sqlite":
            with self._lock:
                return self._conn.execute(sql, params).rowcount
        conn = self._pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute(sql.replace("?", "%s"), params)
            rc = cur.rowcount
            conn.commit()
            return rc
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        if self.kind == "sqlite":
            with self._lock:
                return [dict(r) for r in self._conn.execute(sql, params).fetchall()]
        import psycopg2.extras

        conn = self._pool.getconn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), params)
            rows = [dict(r) for r in cur.fetchall()]
            conn.commit()
            return rows
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def init_schema(self) -> None:
        for stmt in SCHEMA:
            self.execute(stmt)

    def close(self) -> None:
        if self.kind == "sqlite":
            self._conn.close()
        else:
            self._pool.closeall()

    # -- kv ----------------------------------------------------------------
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT v FROM ai_kv WHERE k = ?", (key,))
        return row["v"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO ai_kv (k, v, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (k) DO UPDATE SET v = excluded.v, updated_at = excluded.updated_at",
            (key, value, iso(utcnow())))

    def quota_try_consume(self, name: str, cap: int, now: datetime | None = None) -> bool:
        """Per-UTC-day counter. Returns False when the cap is reached."""
        day = (now or utcnow()).strftime("%Y-%m-%d")
        key = f"quota:{name}:{day}"
        used = int(self.kv_get(key, "0") or 0)
        if used >= cap:
            return False
        self.kv_set(key, str(used + 1))
        return True

    # -- jobs --------------------------------------------------------------
    def insert_job_if_new(self, j: dict, now: datetime) -> bool:
        ts = iso(now)
        rc = self.execute(
            "INSERT INTO ai_jobs (job_id, title, company, location, url, posted_at, description, desc_hash, "
            "desc_complete, source, stage, attempts, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', 0, ?, ?) ON CONFLICT (job_id) DO NOTHING",
            (j["job_id"], j.get("title"), j.get("company"), j.get("location"), j.get("url"), j.get("posted_at"),
             j.get("description"), j.get("desc_hash"), 1 if j.get("desc_complete") else 0, j.get("source"), ts, ts))
        return rc == 1

    def get_job(self, job_id: str) -> dict | None:
        return self.one("SELECT * FROM ai_jobs WHERE job_id = ?", (job_id,))

    def update_job(self, job_id: str, now: datetime, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.execute(f"UPDATE ai_jobs SET {cols}, updated_at = ? WHERE job_id = ?",
                     tuple(fields.values()) + (iso(now), job_id))

    def jobs_due(self, now: datetime, stages: tuple, max_attempts: int, limit: int = 50) -> list[dict]:
        marks = ",".join("?" for _ in stages)
        return self.query(
            f"SELECT * FROM ai_jobs WHERE stage IN ({marks}) AND attempts < ? "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY created_at LIMIT ?",
            tuple(stages) + (max_attempts, iso(now), limit))

    # -- classifications ---------------------------------------------------
    def get_classification(self, job_id: str, desc_hash: str, prompt_version: str) -> dict | None:
        return self.one("SELECT * FROM ai_classifications WHERE job_id = ? AND desc_hash = ? AND prompt_version = ?",
                        (job_id, desc_hash, prompt_version))

    def save_classification(self, job_id, desc_hash, prompt_version, decision, source, model, raw, final, now) -> None:
        ts = iso(now)
        self.execute(
            "INSERT INTO ai_classifications (job_id, desc_hash, prompt_version, decision, source, model, raw_json, "
            "final_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (job_id, desc_hash, prompt_version) DO UPDATE SET decision = excluded.decision, "
            "final_json = excluded.final_json, updated_at = excluded.updated_at",
            (job_id, desc_hash, prompt_version, decision, source, model,
             json.dumps(raw, ensure_ascii=False), json.dumps(final, ensure_ascii=False), ts, ts))

    def latest_classification(self, job_id: str) -> dict | None:
        return self.one("SELECT * FROM ai_classifications WHERE job_id = ? ORDER BY updated_at DESC LIMIT 1", (job_id,))

    def jobs_in_stages(self, stages: tuple) -> list[dict]:
        marks = ",".join("?" for _ in stages)
        return self.query(f"SELECT * FROM ai_jobs WHERE stage IN ({marks}) ORDER BY created_at", tuple(stages))

    # -- deliveries (idempotency) -----------------------------------------
    def claim_delivery(self, job_id: str, kind: str, dest_key: str, now: datetime,
                       stale_after: timedelta = timedelta(minutes=10)) -> bool:
        """True if the caller should perform the send now. Never True for 'sent'."""
        ts = iso(now)
        if self.execute(
                "INSERT INTO ai_deliveries (job_id, kind, dest_key, status, attempts, updated_at) "
                "VALUES (?, ?, ?, 'sending', 1, ?) ON CONFLICT (job_id, kind, dest_key) DO NOTHING",
                (job_id, kind, dest_key, ts)) == 1:
            return True
        if self.execute(
                "UPDATE ai_deliveries SET status = 'sending', attempts = attempts + 1, updated_at = ? "
                "WHERE job_id = ? AND kind = ? AND dest_key = ? AND status = 'failed'",
                (ts, job_id, kind, dest_key)) == 1:
            return True
        # crash recovery: a 'sending' row that never finished may be reclaimed (at-least-once)
        return self.execute(
            "UPDATE ai_deliveries SET attempts = attempts + 1, updated_at = ? "
            "WHERE job_id = ? AND kind = ? AND dest_key = ? AND status = 'sending' AND updated_at < ?",
            (ts, job_id, kind, dest_key, iso(now - stale_after))) == 1

    def finish_delivery(self, job_id: str, kind: str, dest_key: str, ok: bool, detail: str, now: datetime) -> None:
        self.execute("UPDATE ai_deliveries SET status = ?, detail = ?, updated_at = ? "
                     "WHERE job_id = ? AND kind = ? AND dest_key = ?",
                     ("sent" if ok else "failed", detail[:500], iso(now), job_id, kind, dest_key))

    def deliveries(self, job_id: str, kind: str | None = None) -> list[dict]:
        if kind:
            return self.query("SELECT * FROM ai_deliveries WHERE job_id = ? AND kind = ? ORDER BY dest_key", (job_id, kind))
        return self.query("SELECT * FROM ai_deliveries WHERE job_id = ? ORDER BY kind, dest_key", (job_id,))

    # -- resumes -----------------------------------------------------------
    def save_resume(self, job_id, tex_path, pdf_path, sha, filename, status, detail: dict, now) -> None:
        self.execute(
            "INSERT INTO ai_resumes (job_id, tex_path, pdf_path, pdf_sha256, filename, status, detail_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (job_id) DO UPDATE SET tex_path = excluded.tex_path, "
            "pdf_path = excluded.pdf_path, pdf_sha256 = excluded.pdf_sha256, filename = excluded.filename, "
            "status = excluded.status, detail_json = excluded.detail_json",
            (job_id, tex_path, pdf_path, sha, filename, status, json.dumps(detail, ensure_ascii=False), iso(now)))

    def get_resume(self, job_id: str) -> dict | None:
        return self.one("SELECT * FROM ai_resumes WHERE job_id = ?", (job_id,))

    # -- contacts ----------------------------------------------------------
    def save_contact(self, job_id: str, slot: str, data: dict, now) -> None:
        self.execute(
            "INSERT INTO ai_contacts (job_id, slot, data_json, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (job_id, slot) DO UPDATE SET data_json = excluded.data_json",
            (job_id, slot, json.dumps(data, ensure_ascii=False), iso(now)))

    def get_contacts(self, job_id: str) -> dict[str, dict]:
        return {r["slot"]: json.loads(r["data_json"])
                for r in self.query("SELECT slot, data_json FROM ai_contacts WHERE job_id = ?", (job_id,))}
