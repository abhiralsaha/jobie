from typing import Any, Dict, Iterable, List

from app.db import get_connection, init_db


def normalize_job(job: Dict[str, Any]) -> Dict[str, str]:
    required = ("job_id", "title", "company", "url")
    missing = [field for field in required if not str(job.get(field, "")).strip()]
    if missing:
        raise ValueError(f"Missing required job fields: {', '.join(missing)}")

    return {
        "job_id": str(job["job_id"]).strip(),
        "title": str(job["title"]).strip(),
        "company": str(job["company"]).strip(),
        "url": str(job["url"]).strip(),
        "posted_at": str(job.get("posted_at", "")).strip(),
        "source": str(job.get("source", "manual")).strip(),
    }


def import_jobs(jobs: Iterable[Dict[str, Any]]) -> List[Dict[str, str]]:
    init_db()
    imported: List[Dict[str, str]] = []

    with get_connection() as conn:
        with conn.cursor() as cur:
            for raw_job in jobs:
                job = normalize_job(raw_job)
                cur.execute(
                    """
                    INSERT INTO jobs_seen (job_id, title, company, url, posted_at, status)
                    VALUES (%s, %s, %s, %s, %s, 'new')
                    ON CONFLICT (job_id) DO NOTHING
                    """,
                    (job["job_id"], job["title"], job["company"], job["url"], job["posted_at"]),
                )
                if cur.rowcount:
                    imported.append(job)

    return imported
