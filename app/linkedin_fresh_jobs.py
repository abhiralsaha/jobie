"""
linkedin_fresh_jobs.py

Background task polls LinkedIn's public guest job-search endpoint without a
login, filters recent postings, and sends new matches to Telegram.

GET /fresh-jobs returns and clears whatever's accumulated since the last read.
"""

import asyncio
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.config import settings
from app.freshness import parse_timestamp, posted_within_seconds
from app.logging_setup import setup_logger
from app.messaging import TelegramNotifier
from app.security import require_api_bearer_token

GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

HEADERS = {
    "User-Agent": "JobieJobMonitor/1.0",
    "Accept-Language": "en-US,en;q=0.9",
}

DB_PATH = settings.DATABASE_PATH
POLL_INTERVAL_SECONDS = settings.LINKEDIN_POLL_INTERVAL_SECONDS
LOOKBACK_SECONDS = settings.LINKEDIN_LOOKBACK_HOURS * 60 * 60
TPR_SECONDS = LOOKBACK_SECONDS
MAX_SEARCH_PAGES = min(max(settings.LINKEDIN_MAX_SEARCH_PAGES, 1), 10)
SEARCH_PAGE_SIZE = 25
MAX_BUFFERED_JOBS = 1000
SEARCH_KEYWORDS = settings.LINKEDIN_SEARCH_KEYWORDS
SEARCH_LOCATION = settings.LINKEDIN_SEARCH_LOCATION

new_jobs_buffer: list[dict] = []
buffer_lock = asyncio.Lock()
digest_send_lock = asyncio.Lock()
logger = setup_logger()
notifier = TelegramNotifier()


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            first_seen REAL,
            url TEXT,
            posted_at TEXT
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    if "url" not in columns:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN url TEXT")
    if "posted_at" not in columns:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN posted_at TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS digest_runs (
            digest_date TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS manual_digest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_date TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            status TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_manual_digest_runs_date ON manual_digest_runs(local_date)"
    )
    conn.row_factory = sqlite3.Row
    conn.commit()
    return conn


def fetch_job_cards_html(keywords: str, location: str, tpr_seconds: int, start: int = 0) -> str:
    params = {
        "keywords": keywords,
        "location": location,
        "f_TPR": f"r{tpr_seconds}",
        "sortBy": "DD",  # most recent first
        "start": start,
    }
    with requests.get(
        GUEST_SEARCH_URL, params=params, headers=HEADERS, timeout=(5, 10), stream=True
    ) as resp:
        resp.raise_for_status()
        content = bytearray()
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            content.extend(chunk)
            if len(content) > 3 * 1024 * 1024:
                raise ValueError("LinkedIn search response exceeds the 3 MB limit")
        return content.decode(resp.encoding or "utf-8", errors="replace")


def parse_linkedin_posted_at(time_el) -> str | None:
    label = time_el.get_text(" ", strip=True).lower()
    if "just now" in label:
        return datetime.now(timezone.utc).isoformat()

    relative_time = re.search(
        r"(?:(\d+)\s+)?(minute|hour|day|week)s?\s+ago", label
    )
    if relative_time:
        amount = int(relative_time.group(1) or 1)
        unit = relative_time.group(2)
        posted_at = datetime.now(timezone.utc) - timedelta(
            **{f"{unit}s": amount}
        )
        return posted_at.isoformat()

    return time_el.get("datetime")


def parse_jobs(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for card in soup.select("li"):
        link_el = card.select_one("a.base-card__full-link")
        if not link_el or not link_el.get("href"):
            continue
        parsed_url = urlparse(urljoin(GUEST_SEARCH_URL, link_el["href"]))
        if (
            parsed_url.scheme != "https"
            or parsed_url.hostname is None
            or not (
                parsed_url.hostname == "linkedin.com"
                or parsed_url.hostname.endswith(".linkedin.com")
            )
            or not parsed_url.path.startswith("/jobs/view/")
        ):
            continue
        try:
            if parsed_url.port not in (None, 443):
                continue
        except ValueError:
            continue
        url = urlunsplit((parsed_url.scheme, parsed_url.netloc, parsed_url.path, "", ""))
        job_id = parsed_url.path.rstrip("/").split("-")[-1]
        if not job_id.isdigit():
            continue

        title_el = card.select_one("h3.base-search-card__title")
        company_el = card.select_one("h4.base-search-card__subtitle")
        location_el = card.select_one("span.job-search-card__location")
        time_el = card.select_one("time")

        jobs.append(
            {
                "job_id": job_id,
                "title": title_el.get_text(strip=True) if title_el else None,
                "company": company_el.get_text(strip=True) if company_el else None,
                "location": location_el.get_text(strip=True) if location_el else None,
                "posted_at": parse_linkedin_posted_at(time_el) if time_el else None,
                "url": url,
            }
        )
    return jobs


def fetch_recent_jobs() -> list[dict]:
    jobs_by_id = {}
    for page in range(MAX_SEARCH_PAGES):
        html = fetch_job_cards_html(
            SEARCH_KEYWORDS,
            SEARCH_LOCATION,
            TPR_SECONDS,
            start=page * SEARCH_PAGE_SIZE,
        )
        page_jobs = parse_jobs(html)
        new_page_jobs = [job for job in page_jobs if job["job_id"] not in jobs_by_id]
        if page > 0 and not new_page_jobs:
            break
        for job in new_page_jobs:
            jobs_by_id[job["job_id"]] = job
        if not page_jobs:
            break
    return list(jobs_by_id.values())


def get_recent_digest_jobs(
    conn, now: datetime | None = None, lookback_seconds: int | None = None
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    lookback_seconds = LOOKBACK_SECONDS if lookback_seconds is None else lookback_seconds
    jobs = []
    rows = conn.execute(
        "SELECT job_id, title, company, url, posted_at FROM seen_jobs WHERE url IS NOT NULL AND posted_at IS NOT NULL"
    ).fetchall()
    for row in rows:
        posted_at = parse_timestamp(row["posted_at"])
        if posted_at is None:
            continue
        age_seconds = (now - posted_at).total_seconds()
        if 0 <= age_seconds <= lookback_seconds:
            jobs.append(dict(row))
    jobs.sort(key=lambda job: parse_timestamp(job["posted_at"]), reverse=True)
    return jobs


async def deliver_digest(
    conn,
    message_notifier: TelegramNotifier | None = None,
    lookback_seconds: int | None = None,
    lookback_period: str | None = None,
) -> tuple[list[dict], list[str]]:
    lookback_seconds = LOOKBACK_SECONDS if lookback_seconds is None else lookback_seconds
    lookback_period = (
        f"{settings.LINKEDIN_LOOKBACK_HOURS} hours"
        if lookback_period is None
        else lookback_period
    )
    message_notifier = message_notifier or notifier
    async with buffer_lock:
        jobs = get_recent_digest_jobs(conn, lookback_seconds=lookback_seconds)
    message_ids = await asyncio.to_thread(
        message_notifier.send_digest, jobs, lookback_period
    )
    return jobs, message_ids


def rolling_digest_specs() -> list[tuple[str, str, str, int, str]]:
    return [
        (
            "5m",
            settings.TELEGRAM_DIGEST_5M_BOT_TOKEN,
            settings.TELEGRAM_DIGEST_5M_CHAT_ID,
            5 * 60,
            "5 minutes",
        ),
        (
            "15m",
            settings.TELEGRAM_DIGEST_15M_BOT_TOKEN,
            settings.TELEGRAM_DIGEST_15M_CHAT_ID,
            15 * 60,
            "15 minutes",
        ),
        (
            "30m",
            settings.TELEGRAM_DIGEST_30M_BOT_TOKEN,
            settings.TELEGRAM_DIGEST_30M_CHAT_ID,
            30 * 60,
            "30 minutes",
        ),
        (
            "1h",
            settings.TELEGRAM_DIGEST_1H_BOT_TOKEN,
            settings.TELEGRAM_DIGEST_1H_CHAT_ID,
            60 * 60,
            "1 hour",
        ),
        (
            "10h",
            settings.TELEGRAM_DIGEST_BOT_TOKEN,
            settings.TELEGRAM_DIGEST_CHAT_ID,
            settings.TELEGRAM_DIGEST_INTERVAL_HOURS * 3600,
            f"{settings.TELEGRAM_DIGEST_INTERVAL_HOURS} hours",
        ),
    ]


async def maybe_send_rolling_digests(conn) -> None:
    checked_times = getattr(app.state, "last_rolling_digest_checked_at", {})
    for name, bot_token, chat_id, interval_seconds, lookback_period in rolling_digest_specs():
        if not bot_token and not chat_id:
            continue
        digest_notifier = TelegramNotifier(bot_token, chat_id)
        if not digest_notifier.is_configured:
            continue

        interval = timedelta(seconds=interval_seconds)
        try:
            async with digest_send_lock:
                checked_times = getattr(app.state, "last_rolling_digest_checked_at", {})
                last_checked = checked_times.get(name)
                now = datetime.now(timezone.utc)
                if last_checked and (now - datetime.fromisoformat(last_checked)) < interval:
                    continue

                jobs, message_ids = await deliver_digest(
                    conn,
                    digest_notifier,
                    interval_seconds,
                    lookback_period,
                )
                checked_times[name] = datetime.now(timezone.utc).isoformat()
                app.state.last_rolling_digest_checked_at = checked_times
                if not message_ids:
                    logger.info(
                        "Rolling Telegram digest %s skipped: no deliverable jobs",
                        name,
                    )
                    continue
                logger.info(
                    "Rolling Telegram digest %s sent: %d jobs in %d message(s)",
                    name,
                    len(jobs),
                    len(message_ids),
                )
        except Exception as exc:
            logger.exception("Rolling Telegram digest %s failed: %s", name, exc)


def rolling_poll_delay_seconds(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    checked_times = getattr(app.state, "last_rolling_digest_checked_at", {})
    due_delays = []
    for name, bot_token, chat_id, interval_seconds, _ in rolling_digest_specs():
        if not bot_token and not chat_id:
            continue
        last_checked = checked_times.get(name)
        if not last_checked:
            continue
        remaining = interval_seconds - (now - datetime.fromisoformat(last_checked)).total_seconds()
        if remaining <= 0:
            return POLL_INTERVAL_SECONDS
        due_delays.append(remaining)

    if not due_delays:
        return POLL_INTERVAL_SECONDS
    return max(1, min(POLL_INTERVAL_SECONDS, int(min(due_delays))))


def reserve_manual_digest_slot(conn, local_date: str) -> int:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows = conn.execute(
            "SELECT requested_at FROM manual_digest_runs WHERE local_date = ? ORDER BY id DESC",
            (local_date,),
        ).fetchall()
        if len(rows) >= settings.MANUAL_DIGEST_MAX_PER_DAY:
            raise HTTPException(
                status_code=429,
                detail="Daily manual digest limit reached",
                headers={"Retry-After": "86400"},
            )
        if rows:
            last_request = datetime.fromisoformat(rows[0]["requested_at"])
            elapsed = (now - last_request).total_seconds()
            remaining = settings.MANUAL_DIGEST_COOLDOWN_SECONDS - int(elapsed)
            if remaining > 0:
                raise HTTPException(
                    status_code=429,
                    detail="Manual digest cooldown is active",
                    headers={"Retry-After": str(remaining)},
                )
        cursor = conn.execute(
            "INSERT INTO manual_digest_runs (local_date, requested_at, status) VALUES (?, ?, 'pending')",
            (local_date, now_iso),
        )
        conn.commit()
        return cursor.lastrowid
    except Exception:
        conn.rollback()
        raise


async def maybe_send_daily_digest(conn, digest_timezone: ZoneInfo, digest_time) -> None:
    local_now = datetime.now(digest_timezone)
    if local_now.timetz().replace(tzinfo=None) < digest_time:
        return

    digest_date = local_now.date().isoformat()
    async with digest_send_lock:
        async with buffer_lock:
            already_sent = conn.execute(
                "SELECT 1 FROM digest_runs WHERE digest_date = ?", (digest_date,)
            ).fetchone()
        if already_sent:
            return

        jobs, message_ids = await deliver_digest(conn)
        if not message_ids:
            return

        async with buffer_lock:
            conn.execute(
                "INSERT OR IGNORE INTO digest_runs (digest_date, sent_at) VALUES (?, ?)",
                (digest_date, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        logger.info(
            "Daily Telegram digest sent for %s: %d jobs in %d message(s)",
            digest_date,
            len(jobs),
            len(message_ids),
        )
        app.state.last_digest_sent_at = datetime.now(timezone.utc).isoformat()


async def poll_loop(conn, digest_timezone: ZoneInfo, digest_time) -> None:
    while True:
        try:
            fetched_jobs = await asyncio.to_thread(fetch_recent_jobs)
            jobs = [
                job
                for job in fetched_jobs
                if posted_within_seconds(
                    job.get("posted_at") or "", LOOKBACK_SECONDS
                )
            ]
            now = time.time()
            new_jobs = []
            async with buffer_lock:
                for job in jobs:
                    cur = conn.execute("SELECT 1 FROM seen_jobs WHERE job_id = ?", (job["job_id"],))
                    if cur.fetchone() is None:
                        conn.execute(
                            "INSERT INTO seen_jobs (job_id, title, company, first_seen, url, posted_at) VALUES (?, ?, ?, ?, ?, ?)",
                            (
                                job["job_id"],
                                job.get("title"),
                                job.get("company"),
                                now,
                                job["url"],
                                job.get("posted_at"),
                            ),
                        )
                        new_jobs_buffer.append(job)
                        if len(new_jobs_buffer) > MAX_BUFFERED_JOBS:
                            del new_jobs_buffer[: len(new_jobs_buffer) - MAX_BUFFERED_JOBS]
                        new_jobs.append(job)
                    else:
                        conn.execute(
                            "UPDATE seen_jobs SET title = ?, company = ?, url = ?, posted_at = ? WHERE job_id = ?",
                            (
                                job.get("title"),
                                job.get("company"),
                                job["url"],
                                job.get("posted_at"),
                                job["job_id"],
                            ),
                        )
                conn.execute("DELETE FROM seen_jobs WHERE first_seen < ?", (now - 7 * 86400,))
                conn.commit()
            app.state.last_poll_success_at = datetime.now(timezone.utc).isoformat()
            app.state.last_poll_job_count = len(jobs)
            app.state.last_poll_error = None
            for job in new_jobs:
                if not posted_within_seconds(
                    job.get("posted_at") or "", settings.RECENT_JOB_WINDOW_SECONDS
                ):
                    continue
                try:
                    await asyncio.to_thread(
                        notifier.send_alert,
                        job.get("title") or "Untitled role",
                        job.get("company") or "Unknown company",
                        job["url"],
                    )
                    logger.info("Telegram alert sent for LinkedIn job %s", job["job_id"])
                except Exception as exc:
                    logger.warning(
                        "New LinkedIn job %s found but Telegram alert failed: %s",
                        job["job_id"],
                        exc,
                    )
        except Exception as e:
            app.state.last_poll_error = type(e).__name__
            logger.exception("LinkedIn guest search poll failed: %s", e)

        try:
            await maybe_send_daily_digest(conn, digest_timezone, digest_time)
        except Exception as e:
            logger.exception("Daily Telegram digest failed: %s", e)

        try:
            await maybe_send_rolling_digests(conn)
        except Exception as e:
            logger.exception("Rolling Telegram digest failed: %s", e)

        await asyncio.sleep(rolling_poll_delay_seconds())


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(settings.JOBIE_API_TOKEN) < 32:
        raise RuntimeError("Set a JOBIE_API_TOKEN of at least 32 characters before starting the worker")
    if not notifier.is_configured:
        raise RuntimeError(
            "Configure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env before starting the LinkedIn worker"
        )
    if settings.TELEGRAM_DIGEST_INTERVAL_HOURS < 1:
        raise RuntimeError("TELEGRAM_DIGEST_INTERVAL_HOURS must be at least 1")
    for name, bot_token, chat_id, _, _ in rolling_digest_specs():
        if (bot_token or chat_id) and not TelegramNotifier(bot_token, chat_id).is_configured:
            logger.warning(
                "Rolling Telegram digest %s disabled: set a valid token and chat ID pair",
                name,
            )
    if settings.MANUAL_DIGEST_MAX_PER_DAY < 1 or settings.MANUAL_DIGEST_COOLDOWN_SECONDS < 0:
        raise RuntimeError("Manual digest limits must be positive and cooldown cannot be negative")
    try:
        digest_timezone = ZoneInfo(settings.DAILY_DIGEST_TIMEZONE)
        digest_time = datetime.strptime(settings.DAILY_DIGEST_TIME, "%H:%M").time()
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise RuntimeError("Set a valid DAILY_DIGEST_TIME (HH:MM) and DAILY_DIGEST_TIMEZONE in .env") from exc

    conn = init_db()
    app.state.db_conn = conn
    app.state.digest_timezone = digest_timezone
    app.state.worker_started_at = datetime.now(timezone.utc).isoformat()
    app.state.last_poll_success_at = None
    app.state.last_poll_job_count = 0
    app.state.last_poll_error = None
    app.state.last_digest_sent_at = None
    app.state.last_rolling_digest_checked_at = {}
    task = asyncio.create_task(poll_loop(conn, digest_timezone, digest_time))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        app.state.db_conn = None
        conn.close()


app = FastAPI(title="LinkedIn Fresh Jobs API", lifespan=lifespan)


@app.get("/health")
async def worker_health() -> JSONResponse:
    if getattr(app.state, "db_conn", None) is None:
        return JSONResponse(status_code=503, content={"status": "not_started"})

    last_success = getattr(app.state, "last_poll_success_at", None)
    stale_after = max(POLL_INTERVAL_SECONDS * 3, 300)
    stale = last_success is None
    if last_success is not None:
        last_success_at = datetime.fromisoformat(last_success)
        stale = (datetime.now(timezone.utc) - last_success_at).total_seconds() > stale_after

    content = {
        "status": "degraded" if stale else "ok",
        "worker_started_at": app.state.worker_started_at,
        "last_poll_success_at": last_success,
        "last_poll_job_count": app.state.last_poll_job_count,
        "last_poll_error": app.state.last_poll_error,
        "last_digest_sent_at": app.state.last_digest_sent_at,
        "last_rolling_digest_checked_at": getattr(
            app.state, "last_rolling_digest_checked_at", {}
        ),
    }
    return JSONResponse(status_code=503 if stale else 200, content=content)


@app.get("/fresh-jobs")
async def fresh_jobs(
    peek: bool = Query(False, description="If true, don't clear the buffer after reading"),
    authorization: str | None = Header(default=None),
):
    require_api_bearer_token(authorization, settings.JOBIE_API_TOKEN)
    async with buffer_lock:
        jobs = list(new_jobs_buffer)
        if not peek:
            new_jobs_buffer.clear()
    return {"new_job_count": len(jobs), "jobs": jobs}


@app.post("/digest/send")
async def send_digest_now(authorization: str | None = Header(default=None)) -> dict:
    require_api_bearer_token(authorization, settings.JOBIE_API_TOKEN)
    conn = getattr(app.state, "db_conn", None)
    if conn is None:
        raise HTTPException(status_code=503, detail="Job search worker is not running")

    digest_timezone = getattr(app.state, "digest_timezone", None)
    if digest_timezone is None:
        digest_timezone = ZoneInfo(settings.DAILY_DIGEST_TIMEZONE)

    async with digest_send_lock:
        try:
            async with buffer_lock:
                request_id = reserve_manual_digest_slot(
                    conn, datetime.now(digest_timezone).date().isoformat()
                )
        except HTTPException:
            raise
        try:
            jobs, message_ids = await deliver_digest(conn)
        except Exception as exc:
            async with buffer_lock:
                conn.execute(
                    "UPDATE manual_digest_runs SET status = 'failed' WHERE id = ?",
                    (request_id,),
                )
                conn.commit()
            logger.warning("Manual Telegram digest failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Telegram digest delivery failed") from exc
        async with buffer_lock:
            conn.execute(
                "UPDATE manual_digest_runs SET status = 'sent' WHERE id = ?",
                (request_id,),
            )
            conn.commit()
        app.state.last_digest_sent_at = datetime.now(timezone.utc).isoformat()
    return {
        "status": "sent",
        "job_count": len(jobs),
        "message_count": len(message_ids),
    }