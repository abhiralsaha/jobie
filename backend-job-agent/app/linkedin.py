import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger("linkedin")

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36 Edg/123.0",
]


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""


class RateLimited(Exception):
    pass


class LinkedIn:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=True)
        self.cooldown_until = 0.0
        self.strikes = 0

    async def close(self):
        await self.client.aclose()

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml",
        }

    def in_cooldown(self) -> bool:
        return time.time() < self.cooldown_until

    async def _get(self, url: str, params: dict | None = None) -> str:
        started = time.perf_counter()
        if self.in_cooldown():
            raise RateLimited(f"cooling down {int(self.cooldown_until - time.time())}s more")
        log.info("LinkedIn GET | url=%s params=%s", url, params)
        r = await self.client.get(url, params=params, headers=self._headers())
        elapsed = time.perf_counter() - started
        if r.status_code in (429, 999):
            self.strikes += 1
            wait = min(60 * (2 ** (self.strikes - 1)), 900)
            self.cooldown_until = time.time() + wait
            log.warning("LinkedIn rate-limited (%s). Cooling down %ss | url=%s duration=%.2fs", r.status_code, wait, url, elapsed)
            raise RateLimited(str(r.status_code))
        if r.status_code >= 400:
            log.warning("LinkedIn HTTP error | status=%s url=%s duration=%.2fs body=%s", r.status_code, url, elapsed, r.text[:300])
        r.raise_for_status()
        self.strikes = 0
        log.info("LinkedIn GET success | status=%s url=%s duration=%.2fs bytes=%d", r.status_code, url, elapsed, len(r.content))
        return r.text

    async def search(self, keywords: str, location: str, lookback: int, max_pages: int) -> list[Job]:
        jobs: dict[str, Job] = {}
        for page in range(max_pages):
            params = {
                "keywords": keywords,
                "location": location,
                "f_TPR": f"r{lookback}",   # posted within the last N seconds
                "sortBy": "DD",            # most recent first
                "start": page * 10,
            }
            page_started = time.perf_counter()
            log.info("LinkedIn search page %d | keyword=%s location=%s params=%s", page + 1, keywords, location, params)
            html = await self._get(SEARCH_URL, params)
            found = parse_search(html)
            for j in found:
                jobs.setdefault(j.id, j)
            log.info("LinkedIn search page %d complete | keyword=%s location=%s results=%d duration=%.2fs", page + 1, keywords, location, len(found), time.perf_counter() - page_started)
            if len(found) < 10:
                break
            await asyncio.sleep(random.uniform(0.5, 1.5))
        return list(jobs.values())

    async def description(self, job_id: str) -> str:
        started = time.perf_counter()
        log.info("Fetching LinkedIn job detail | job_id=%s", job_id)
        html = await self._get(DETAIL_URL.format(id=job_id))
        text = parse_description(html)
        log.info("LinkedIn job detail fetched | job_id=%s duration=%.2fs chars=%d", job_id, time.perf_counter() - started, len(text))
        return text


def parse_search(html: str) -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for card in soup.select("li"):
        base = card.select_one("div.base-card, div.job-search-card")
        if not base:
            continue
        urn = base.get("data-entity-urn", "")
        m = re.search(r"(\d{6,})$", urn)
        if not m:
            link = card.select_one("a.base-card__full-link")
            m = re.search(r"-(\d{6,})\?", (link.get("href", "") if link else "") + "?")
        if not m:
            continue
        jid = m.group(1)
        t = card.select_one(".base-search-card__title")
        c = card.select_one(".base-search-card__subtitle")
        l = card.select_one(".job-search-card__location")
        out.append(Job(
            id=jid,
            title=t.get_text(strip=True) if t else "Unknown title",
            company=c.get_text(strip=True) if c else "Unknown company",
            location=l.get_text(strip=True) if l else "",
            url=f"https://www.linkedin.com/jobs/view/{jid}",
        ))
    return out


def parse_description(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.select_one(".show-more-less-html__markup, .description__text")
    if not node:
        page_title = soup.title.get_text(" ", strip=True) if soup.title else "unknown"
        log.warning("LinkedIn job description missing from detail response | page_title=%s html_chars=%d",
                    page_title, len(html))
        return ""
    text = node.get_text("\n", strip=True)
    return text[:12000]
