"""Supabase (PostgREST) storage for dedupe + job history. Uses plain HTTPS, no extra SDK."""
import logging
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("storage")


class Store:
    def __init__(self, url: str, key: str, table: str = "backend_dev"):
        self.endpoint = f"{url.rstrip('/')}/rest/v1/{table}"
        self.client = httpx.AsyncClient(timeout=20, headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        })

    async def close(self):
        await self.client.aclose()

    async def check(self):
        """Fail fast on startup if URL/key/table are wrong."""
        started = datetime.now(timezone.utc)
        r = await self.client.get(self.endpoint, params={"select": "job_id", "limit": 1})
        duration_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        log.info("Supabase check | endpoint=%s duration_ms=%.0f status=%s", self.endpoint, duration_ms, r.status_code)
        if r.status_code >= 400:
            raise SystemExit(f"Supabase check failed ({r.status_code}): {r.text[:300]}\n"
                             "Did you run schema.sql and use the service_role key?")

    async def seen_ids(self, job_ids: list[str]) -> set[str]:
        if not job_ids:
            return set()
        started = datetime.now(timezone.utc)
        ids = ",".join(f'"{i}"' for i in job_ids)
        r = await self.client.get(
            self.endpoint,
            params={
                "select": "job_id",
                "job_id": f"in.({ids})",
                "status": "in.(sent,rejected,title_blocked)",
            },
        )
        duration_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        log.info("Supabase seen_ids check | count=%d duration_ms=%.0f status=%s", len(job_ids), duration_ms, r.status_code)
        r.raise_for_status()
        return {row["job_id"] for row in r.json()}

    async def mark(self, job, status: str, extra: dict | None = None):
        started = datetime.now(timezone.utc)
        row = {"job_id": job.id, "status": status, "title": job.title, "company": job.company,
               "location": job.location, "url": job.url}
        if extra:
            row.update(extra)
        r = await self.client.post(
            self.endpoint, params={"on_conflict": "job_id"}, json=row,
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
        duration_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        log.info("Supabase mark | job_id=%s status=%s duration_ms=%.0f status_code=%s", job.id, status, duration_ms, r.status_code)
        r.raise_for_status()

    async def cleanup(self, days: int = 30):
        started = datetime.now(timezone.utc)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        r = await self.client.delete(self.endpoint, params={"created_at": f"lt.{cutoff}"})
        duration_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        log.info("Supabase cleanup | cutoff=%s duration_ms=%.0f status=%s", cutoff, duration_ms, r.status_code)
        r.raise_for_status()
