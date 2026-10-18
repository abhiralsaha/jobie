import os
import logging
import sqlite3
import time

from supabase import Client, create_client
from postgrest.exceptions import APIError

log = logging.getLogger("storage")


class Store:
    """Persistent dedupe store so a job is never alerted twice."""

    def __init__(self, path: str):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            "job_id TEXT PRIMARY KEY, ts INTEGER NOT NULL, status TEXT NOT NULL)")
        self.db.commit()

    def seen(self, job_id: str) -> bool:
        started = time.perf_counter()
        seen = self.db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone() is not None
        log.debug("SQLite dedupe lookup | id=%s seen=%s duration=%.4fs", job_id, seen, time.perf_counter() - started)
        return seen

    def mark(self, job_id: str, status: str) -> None:
        started = time.perf_counter()
        self.db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (job_id, int(time.time()), status))
        self.db.commit()
        log.debug("SQLite job status saved | id=%s status=%s duration=%.4fs",
                  job_id, status, time.perf_counter() - started)

    def cleanup(self, days: int = 14) -> None:
        started = time.perf_counter()
        self.db.execute("DELETE FROM jobs WHERE ts < ?", (int(time.time()) - days * 86400,))
        self.db.commit()
        log.debug("SQLite cleanup finished | retention_days=%d duration=%.4fs",
                  days, time.perf_counter() - started)


class SupabaseStore:
    """Persistent dedupe store backed by a Supabase table."""

    def __init__(self, url: str, key: str):
        self.client: Client = create_client(url, key)
        try:
            self.client.table("jobs").select("job_id").limit(1).execute()
        except APIError as exc:
            if exc.code == "PGRST205":
                raise RuntimeError(
                    "Supabase table public.jobs was not found. Run supabase/schema.sql "
                    "in your project's SQL Editor, then restart the agent."
                ) from exc
            raise

    def seen(self, job_id: str) -> bool:
        started = time.perf_counter()
        result = (
            self.client.table("jobs")
            .select("job_id")
            .eq("job_id", job_id)
            .limit(1)
            .execute()
        )
        seen = bool(result.data)
        log.debug("Supabase dedupe lookup | id=%s seen=%s duration=%.4fs",
                  job_id, seen, time.perf_counter() - started)
        return seen

    def mark(self, job_id: str, status: str) -> None:
        started = time.perf_counter()
        self.client.table("jobs").upsert(
            {"job_id": job_id, "ts": int(time.time()), "status": status},
            on_conflict="job_id",
        ).execute()
        log.debug("Supabase job status saved | id=%s status=%s duration=%.4fs",
                  job_id, status, time.perf_counter() - started)

    def cleanup(self, days: int = 14) -> None:
        started = time.perf_counter()
        cutoff = int(time.time()) - days * 86400
        self.client.table("jobs").delete().lt("ts", cutoff).execute()
        log.debug("Supabase cleanup finished | retention_days=%d duration=%.4fs",
                  days, time.perf_counter() - started)
