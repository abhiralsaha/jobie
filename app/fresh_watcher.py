"""
fresh_watcher.py
================
24/7 bot that polls LinkedIn's guest job-search endpoint every ~60 seconds
looking for postings that appeared in the last 2 minutes.

Whenever a genuinely new job is found it fires an immediate Telegram alert.

Rate-limit protections
----------------------
* Realistic Chrome User-Agent via a persistent requests.Session
* Per-request exponential backoff with Retry-After support (up to MAX_RETRIES attempts)
* Circuit breaker: after exhausting all retries, all polls pause for
  CIRCUIT_COOLDOWN_SECONDS (default 30 min) before resuming
* Random +-jitter on every sleep so requests don't land on a fixed cadence
* Deduplication is persisted in the `seen_jobs` PostgreSQL table so a
  restart or crash never causes duplicate Telegram alerts

Usage
-----
  python -m app.fresh_watcher            # blocking, runs forever

Environment variables (add to .env or Render dashboard)
---------------------------------------------------------
  FRESH_WATCHER_BOT_TOKEN   Telegram bot token for this watcher (falls back to TELEGRAM_BOT_TOKEN)
  FRESH_WATCHER_CHAT_ID     Telegram chat / group ID for alerts (falls back to TELEGRAM_CHAT_ID)
  FRESH_WATCHER_INTERVAL    Poll interval in seconds (default 60)
  FRESH_WATCHER_LOOKBACK    Time-window in seconds to search (default 120 = 2 min)
  LINKEDIN_SEARCH_KEYWORDS  Shared with the main worker
  LINKEDIN_SEARCH_LOCATION  Shared with the main worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunsplit

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app import http_client
from app.config import settings
from app.db import get_connection, init_db
from app.messaging import TelegramNotifier

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
_log = logging.getLogger("fresh_watcher")
_log.setLevel(logging.INFO)
_log.propagate = False
if not _log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s [fresh_watcher] %(levelname)s %(message)s"))
    _log.addHandler(_h)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)

BOT_TOKEN: str = os.getenv("FRESH_WATCHER_BOT_TOKEN", "")
CHAT_ID: str = os.getenv("FRESH_WATCHER_CHAT_ID", "")
POLL_INTERVAL: int = int(os.getenv("FRESH_WATCHER_INTERVAL", "60"))
LOOKBACK_SECONDS: int = int(os.getenv("FRESH_WATCHER_LOOKBACK", "120"))  # 2 minutes

KEYWORDS: str = settings.LINKEDIN_SEARCH_KEYWORDS
LOCATION: str = settings.LINKEDIN_SEARCH_LOCATION

JITTER_RANGE = 15         # +/- seconds per poll


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------

def fetch_page() -> str:
    return http_client.fetch(
        GUEST_SEARCH_URL,
        {
            "keywords": KEYWORDS,
            "location": LOCATION,
            "f_TPR": f"r{LOOKBACK_SECONDS}",
            "sortBy": "DD",
            "start": 0,
        },
        log=_log,
    )


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

def _parse_posted_at(time_el) -> str | None:
    label = time_el.get_text(" ", strip=True).lower()
    if "just now" in label:
        return datetime.now(timezone.utc).isoformat()
    m = re.search(r"(?:(\d+)\s+)?(minute|hour|day|week)s?\s+ago", label)
    if m:
        amount = int(m.group(1) or 1)
        unit = m.group(2)
        ts = datetime.now(timezone.utc) - timedelta(**{f"{unit}s": amount})
        return ts.isoformat()
    return time_el.get("datetime")


def parse_jobs(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []
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
                "posted_at": _parse_posted_at(time_el) if time_el else None,
                "url": url,
            }
        )
    return jobs


# ---------------------------------------------------------------------------
# Deduplication via PostgreSQL
# ---------------------------------------------------------------------------

def is_new_job(job: dict) -> bool:
    """Return True only if this job_id is brand-new and was successfully recorded."""
    try:
        now_epoch = time.time()
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO seen_jobs (job_id, title, company, first_seen, url, posted_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id) DO NOTHING
                    """,
                    (
                        job["job_id"],
                        job.get("title"),
                        job.get("company"),
                        now_epoch,
                        job["url"],
                        job.get("posted_at"),
                    ),
                )
                return cur.rowcount == 1  # 1 = inserted (new); 0 = conflict (already seen)
    except Exception as exc:
        _log.warning("DB dedup error for job_id=%s: %s", job.get("job_id"), exc)
        return False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def _build_notifier() -> TelegramNotifier | None:
    token = os.getenv("FRESH_WATCHER_BOT_TOKEN", "").strip() or getattr(settings, "FRESH_WATCHER_BOT_TOKEN", "").strip()
    chat_id = os.getenv("FRESH_WATCHER_CHAT_ID", "").strip() or getattr(settings, "FRESH_WATCHER_CHAT_ID", "").strip()
    if token and chat_id:
        n = TelegramNotifier(token, chat_id)
        if n.is_configured:
            _log.info("Using dedicated FRESH_WATCHER Telegram bot (token=%s...)", token[:8])
            return n
        else:
            _log.warning("FRESH_WATCHER credentials provided but invalid format.")
    # Fall back to the main bot credentials
    n = TelegramNotifier()
    if n.is_configured:
        _log.info(
            "Dedicated FRESH_WATCHER_BOT_TOKEN / CHAT_ID not set — "
            "falling back to main TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
        )
        return n
    return None


def send_alert(notifier: TelegramNotifier, job: dict) -> None:
    title = job.get("title") or "Untitled role"
    company = job.get("company") or "Unknown company"
    try:
        notifier.send_alert(title, company, job["url"])
        _log.info(
            "Alert sent  job_id=%s  %r @ %r",
            job["job_id"],
            title,
            company,
        )
    except Exception as exc:
        _log.warning("Telegram alert failed for job_id=%s: %s", job["job_id"], exc)


# ---------------------------------------------------------------------------
# Background maintenance: prune seen_jobs older than 7 days
# ---------------------------------------------------------------------------

async def _prune_loop() -> None:
    """Delete seen_jobs rows older than 7 days every 6 hours."""
    PRUNE_INTERVAL = 6 * 3600  # seconds
    KEEP_SECONDS = 7 * 86400
    while True:
        await asyncio.sleep(PRUNE_INTERVAL)
        try:
            cutoff = time.time() - KEEP_SECONDS
            def _prune():
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "DELETE FROM seen_jobs WHERE first_seen < %s", (cutoff,)
                        )
                        return cur.rowcount
            deleted = await asyncio.to_thread(_prune)
            if deleted:
                _log.info("Pruned %d stale seen_jobs rows (>7 days old)", deleted)
        except Exception as exc:
            _log.warning("seen_jobs prune failed: %s", exc)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------

async def run() -> None:
    _log.info(
        "Starting fresh_watcher — keywords=%r  location=%r  "
        "lookback=%ds  poll_interval=~%ds",
        KEYWORDS,
        LOCATION,
        LOOKBACK_SECONDS,
        POLL_INTERVAL,
    )

    await asyncio.to_thread(init_db)

    notifier = _build_notifier()
    if notifier is None:
        # Don't exit — Render would see exit-code 0 and not restart.
        # Loop with a loud warning until the operator sets the env vars and redeploys.
        _log.error(
            "FATAL: No Telegram bot configured. "
            "Set FRESH_WATCHER_BOT_TOKEN + FRESH_WATCHER_CHAT_ID "
            "(or TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID) and redeploy. "
            "Sleeping forever until then."
        )
        while True:
            await asyncio.sleep(300)
            _log.error("Still waiting for Telegram credentials — bot is idle.")

    _log.info("Telegram OK. Entering 24/7 poll loop...")

    while True:
        # ── Circuit breaker ───────────────────────────────────────────────
        is_open, remaining = http_client.circuit_is_open()
        if is_open:
            _log.info("Circuit breaker OPEN — %ds remaining", remaining)
            await asyncio.sleep(min(60, remaining))
            continue

        # ── Fetch ─────────────────────────────────────────────────────────
        try:
            html = await asyncio.to_thread(fetch_page)
        except RuntimeError as exc:
            cooldown = http_client.trip_circuit()
            _log.error(
                "Circuit breaker TRIPPED (%s). Pausing %ds.", exc, cooldown
            )
            await asyncio.sleep(60)
            continue
        except Exception as exc:
            _log.warning("Fetch error — retrying next cycle: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)
            continue

        # ── Parse ─────────────────────────────────────────────────────────
        try:
            jobs = await asyncio.to_thread(parse_jobs, html)
        except Exception as exc:
            _log.warning("Parse error: %s", exc)
            jobs = []

        # ── Dedup & alert ─────────────────────────────────────────────────
        new_count = 0
        for job in jobs:
            try:
                if await asyncio.to_thread(is_new_job, job):
                    new_count += 1
                    await asyncio.to_thread(send_alert, notifier, job)
            except Exception as exc:
                _log.warning("Error handling job %s: %s", job.get("job_id"), exc)

        _log.info(
            "Poll done — %d on page, %d new alert(s) fired", len(jobs), new_count
        )

        # ── Sleep with jitter ─────────────────────────────────────────────
        sleep_for = max(10, POLL_INTERVAL + random.uniform(-JITTER_RANGE, JITTER_RANGE))
        await asyncio.sleep(sleep_for)


def main() -> None:
    async def _all() -> None:
        await asyncio.gather(
            run(),
            _prune_loop(),
        )
    asyncio.run(_all())


if __name__ == "__main__":
    main()
