"""
linkedin_fresh_jobs.py

Background task polls LinkedIn's public guest job-search endpoint without a
login, filters recent postings, and sends new matches to Telegram.

GET /fresh-jobs returns and clears whatever's accumulated since the last read.
"""

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import http_client
from app.ai_pipeline import hook as ai_hook
from app.ai_pipeline.settings import Settings as AIScreeningSettings
from app.config import settings
from app.db import get_connection, init_db
from app.freshness import parse_timestamp, posted_within_seconds
from app.keepalive_agent import KeepAliveAgent
from app.logging_setup import setup_logger
from app.messaging import TelegramNotifier
from app.security import require_api_bearer_token

GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

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


def fetch_job_cards_html(keywords: str, location: str, tpr_seconds: int, start: int = 0) -> str:
    params = {
        "keywords": keywords,
        "location": location,
        "f_TPR": f"r{tpr_seconds}",
        "sortBy": "DD",  # most recent first
        "start": start,
    }
    return http_client.fetch(GUEST_SEARCH_URL, params, log=logger)


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
    """Fetch LinkedIn jobs page-by-page with per-page fault isolation.

    Strategy:
    - Always fetch page 0.
    - Fetch the next page only when *every* job on the current page was new
      (indicating there may be more unseen jobs just beyond it).
    - Catch errors per page; keep whatever has been collected so far.
    - After exhausting retries, trip the shared circuit breaker.
    """
    import random as _random

    jobs_by_id: dict[str, dict] = {}

    for page in range(MAX_SEARCH_PAGES):
        try:
            html = fetch_job_cards_html(
                SEARCH_KEYWORDS,
                SEARCH_LOCATION,
                TPR_SECONDS,
                start=page * SEARCH_PAGE_SIZE,
            )
        except RuntimeError as exc:
            # Retries exhausted — trip the circuit breaker and bail out
            cooldown = http_client.trip_circuit()
            logger.error(
                "LinkedIn circuit breaker tripped — pausing polls for %ds. Reason: %s",
                cooldown,
                exc,
            )
            break
        except Exception as exc:
            logger.warning("LinkedIn page %d fetch failed, keeping prior results: %s", page, exc)
            break

        page_jobs = parse_jobs(html)
        if not page_jobs:
            break

        new_on_page = [job for job in page_jobs if job["job_id"] not in jobs_by_id]
        for job in new_on_page:
            jobs_by_id[job["job_id"]] = job

        # Only go deeper if *every* job on this page was new; otherwise stop early.
        all_new = len(new_on_page) == len(page_jobs)
        if not all_new:
            break

        if page < MAX_SEARCH_PAGES - 1:
            # Space out page requests to reduce fingerprinting
            time.sleep(_random.uniform(2, 5))

    return list(jobs_by_id.values())


def record_search_run(
    started_at: datetime,
    completed_at: datetime,
    status: str,
    matched_count: int = 0,
    new_jobs: list[dict] | None = None,
    error: str | None = None,
) -> None:
    new_jobs = new_jobs or []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO search_runs
                    (started_at, completed_at, status, matched_count,
                     new_job_count, new_jobs, error)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    started_at,
                    completed_at,
                    status,
                    matched_count,
                    len(new_jobs),
                    json.dumps(new_jobs),
                    error,
                ),
            )


def get_recent_digest_jobs(
    now: datetime | None = None, lookback_seconds: int | None = None
) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    lookback_seconds = LOOKBACK_SECONDS if lookback_seconds is None else lookback_seconds
    jobs = []
    if ai_hook.enabled():
        rows = ai_hook.get_pipeline().store.query(
            "SELECT job_id, title, company, url, posted_at FROM ai_jobs "
            "WHERE stage IN ('alerts_pending', 'package_pending', 'done') "
            "AND EXISTS ("
            "SELECT 1 FROM ai_classifications c "
            "WHERE c.job_id = ai_jobs.job_id AND c.desc_hash = ai_jobs.desc_hash "
            "AND c.prompt_version = 'v2' AND c.decision = 'include')"
        )
        logger.info("AI-screened digest contains %d eligible job(s)", len(rows))
    else:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT job_id, title, company, url, posted_at FROM seen_jobs "
                    "WHERE url IS NOT NULL AND posted_at IS NOT NULL"
                )
                rows = cur.fetchall()
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
        jobs = await asyncio.to_thread(get_recent_digest_jobs, None, lookback_seconds)
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


async def maybe_send_rolling_digests() -> None:
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


def reserve_manual_digest_slot(local_date: str) -> int:
    """Reserve a manual digest slot — enforces rate limit in the DB."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Serialise slot reservation with an advisory lock
            cur.execute("SELECT pg_advisory_xact_lock(42)")
            cur.execute(
                "SELECT requested_at FROM manual_digest_runs "
                "WHERE local_date = %s ORDER BY id DESC",
                (local_date,),
            )
            rows = cur.fetchall()
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
            cur.execute(
                "INSERT INTO manual_digest_runs (local_date, requested_at, status) "
                "VALUES (%s, %s, 'pending') RETURNING id",
                (local_date, now_iso),
            )
            row = cur.fetchone()
            return row["id"]


async def maybe_send_daily_digest(digest_timezone: ZoneInfo, digest_time) -> None:
    local_now = datetime.now(digest_timezone)
    if local_now.timetz().replace(tzinfo=None) < digest_time:
        return

    digest_date = local_now.date().isoformat()
    async with digest_send_lock:
        async with buffer_lock:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM digest_runs WHERE digest_date = %s", (digest_date,)
                    )
                    already_sent = cur.fetchone()
        if already_sent:
            return

        jobs, message_ids = await deliver_digest()
        if not message_ids:
            return

        async with buffer_lock:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO digest_runs (digest_date, sent_at) VALUES (%s, %s) "
                        "ON CONFLICT (digest_date) DO NOTHING",
                        (digest_date, datetime.now(timezone.utc).isoformat()),
                    )
        logger.info(
            "Daily Telegram digest sent for %s: %d jobs in %d message(s)",
            digest_date,
            len(jobs),
            len(message_ids),
        )
        app.state.last_digest_sent_at = datetime.now(timezone.utc).isoformat()


async def poll_loop(digest_timezone: ZoneInfo, digest_time) -> None:
    while True:
        # Circuit breaker: if a recent 429 exhausted all retries, sit out
        is_open, remaining = http_client.circuit_is_open()
        if is_open:
            logger.info("LinkedIn circuit breaker open — skipping poll for %ds", remaining)
            await asyncio.sleep(60)
            continue

        poll_started_at = datetime.now(timezone.utc)
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
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        for job in jobs:
                            cur.execute(
                                "SELECT 1 FROM seen_jobs WHERE job_id = %s",
                                (job["job_id"],),
                            )
                            if cur.fetchone() is None:
                                cur.execute(
                                    "INSERT INTO seen_jobs "
                                    "(job_id, title, company, first_seen, url, posted_at) "
                                    "VALUES (%s, %s, %s, %s, %s, %s) "
                                    "ON CONFLICT (job_id) DO NOTHING",
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
                                cur.execute(
                                    "UPDATE seen_jobs "
                                    "SET title = %s, company = %s, url = %s, posted_at = %s "
                                    "WHERE job_id = %s",
                                    (
                                        job.get("title"),
                                        job.get("company"),
                                        job["url"],
                                        job.get("posted_at"),
                                        job["job_id"],
                                    ),
                                )
                        # Prune records older than 7 days
                        cur.execute(
                            "DELETE FROM seen_jobs WHERE first_seen < %s",
                            (now - 7 * 86400,),
                        )
            app.state.last_poll_success_at = datetime.now(timezone.utc).isoformat()
            app.state.last_poll_job_count = len(jobs)
            app.state.last_poll_error = None
            await asyncio.to_thread(
                record_search_run,
                poll_started_at,
                datetime.now(timezone.utc),
                "success",
                len(jobs),
                new_jobs,
            )
            logger.info(
                "LinkedIn search completed: %d matching jobs, %d newly discovered",
                len(jobs),
                len(new_jobs),
            )
            new_job_ids = {job["job_id"] for job in new_jobs}
            for job in jobs:
                logger.info(
                    "LinkedIn search result [%s]: job_id=%s title=%r company=%r "
                    "posted_at=%r url=%r",
                    "new" if job["job_id"] in new_job_ids else "already tracked",
                    job["job_id"],
                    job.get("title"),
                    job.get("company"),
                    job.get("posted_at"),
                    job.get("url"),
                )
            for job in new_jobs:
                if not posted_within_seconds(
                    job.get("posted_at") or "", settings.RECENT_JOB_WINDOW_SECONDS
                ):
                    continue
                if ai_hook.enabled():
                    ai_hook.set_project_http_client()
                    ai_hook.on_new_job(
                        job_id=job["job_id"],
                        title=job.get("title") or "",
                        company=job.get("company") or "",
                        location=job.get("location") or "",
                        url=job["url"],
                        posted_at=job.get("posted_at"),
                        source="linkedin_search",
                    )
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
                await asyncio.to_thread(
                    record_search_run,
                    poll_started_at,
                    datetime.now(timezone.utc),
                    "failed",
                    error=type(e).__name__,
                )
            except Exception:
                logger.exception("Failed to save LinkedIn search failure history")

        try:
            await maybe_send_daily_digest(digest_timezone, digest_time)
        except Exception as e:
            logger.exception("Daily Telegram digest failed: %s", e)

        try:
            await maybe_send_rolling_digests()
        except Exception as e:
            logger.exception("Rolling Telegram digest failed: %s", e)

        # Jitter: spread requests across the interval to avoid burst patterns
        import random as _random
        base_delay = rolling_poll_delay_seconds()
        jitter = _random.uniform(0, min(120, base_delay * 0.2))
        await asyncio.sleep(base_delay + jitter)


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

    # Initialise Supabase schema
    await asyncio.to_thread(init_db)
    ai_settings = AIScreeningSettings.from_env()
    if ai_settings.enabled:
        logger.info(
            "AI screening enabled for new jobs: full descriptions, experience limit 0-%g years",
            ai_settings.max_required_max_years,
        )

    app.state.digest_timezone = digest_timezone
    app.state.worker_started_at = datetime.now(timezone.utc).isoformat()
    app.state.last_poll_success_at = None
    app.state.last_poll_job_count = 0
    app.state.last_poll_error = None
    app.state.last_digest_sent_at = None
    app.state.last_rolling_digest_checked_at = {}
    task = asyncio.create_task(poll_loop(digest_timezone, digest_time))
    watcher_tasks = []
    if settings.FRESH_WATCHER_IN_MAIN:
        from app import fresh_watcher

        app.state.fresh_watcher_in_main = True
        watcher_tasks = [
            asyncio.create_task(fresh_watcher.run(), name="fresh-watcher"),
            asyncio.create_task(fresh_watcher._prune_loop(), name="fresh-watcher-prune"),
        ]
        logger.info("Fresh watcher is running inside the main server process")
    else:
        app.state.fresh_watcher_in_main = False

    # Keep-alive agent: pings this service every 14 min so Render free-tier
    # never sees it as idle and spins it down.
    if not settings.RENDER_EXTERNAL_URL:
        raise RuntimeError("Set RENDER_EXTERNAL_URL in your .env file (e.g. https://jobie-gu7t.onrender.com/)")
    keepalive = KeepAliveAgent(base_url=settings.RENDER_EXTERNAL_URL)
    keepalive.start()

    try:
        yield
    finally:
        keepalive.stop()
        task.cancel()
        for watcher_task in watcher_tasks:
            watcher_task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        if watcher_tasks:
            await asyncio.gather(*watcher_tasks, return_exceptions=True)
            if ai_hook.enabled():
                ai_hook.stop_background()


app = FastAPI(title="LinkedIn Fresh Jobs API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def record_keep_alive_ping() -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO keep_alive_pings DEFAULT VALUES RETURNING id, received_at"
            )
            return dict(cur.fetchone())


@app.post("/keep-alive")
async def keep_alive(authorization: str | None = Header(default=None)) -> dict:
    require_api_bearer_token(authorization, settings.JOBIE_API_TOKEN)
    ping = await asyncio.to_thread(record_keep_alive_ping)
    return {"status": "ok", "ping": ping}


@app.get("/api/jobs")
async def get_all_jobs(limit: int = Query(300, ge=1, le=1000)) -> dict:
    """Returns stored seen_jobs directly from the Supabase PostgreSQL database."""
    def _query_jobs():
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT job_id, title, company, first_seen, url, posted_at FROM seen_jobs ORDER BY first_seen DESC LIMIT %s",
                    (limit,),
                )
                return [dict(row) for row in cur.fetchall()]

    jobs = await asyncio.to_thread(_query_jobs)
    return {"count": len(jobs), "jobs": jobs}


@app.get("/api/search-logs")
async def get_search_logs(
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    """Returns paginated poll history, newest first."""

    def _query_logs():
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, started_at, completed_at, status, matched_count,
                           new_job_count, new_jobs, error
                    FROM search_runs
                    ORDER BY started_at DESC, id DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit + 1, offset),
                )
                return [dict(row) for row in cur.fetchall()]

    rows = await asyncio.to_thread(_query_logs)
    return {
        "logs": rows[:limit],
        "has_more": len(rows) > limit,
    }


@app.get("/health")
async def worker_health() -> JSONResponse:
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

    digest_timezone = getattr(app.state, "digest_timezone", None)
    if digest_timezone is None:
        digest_timezone = ZoneInfo(settings.DAILY_DIGEST_TIMEZONE)

    async with digest_send_lock:
        try:
            async with buffer_lock:
                request_id = await asyncio.to_thread(
                    reserve_manual_digest_slot,
                    datetime.now(digest_timezone).date().isoformat(),
                )
        except HTTPException:
            raise
        try:
            jobs, message_ids = await deliver_digest()
        except Exception as exc:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE manual_digest_runs SET status = 'failed' WHERE id = %s",
                        (request_id,),
                    )
            logger.warning("Manual Telegram digest failed (%s)", type(exc).__name__)
            raise HTTPException(status_code=502, detail="Telegram digest delivery failed") from exc

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE manual_digest_runs SET status = 'sent' WHERE id = %s",
                    (request_id,),
                )
        app.state.last_digest_sent_at = datetime.now(timezone.utc).isoformat()
    return {
        "status": "sent",
        "job_count": len(jobs),
        "message_count": len(message_ids),
    }


# Keep the authorized import API available on this same server process.
from app.main import app as import_api

app.mount("/", import_api, name="job-import-api")