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
        if self.in_cooldown():
            raise RateLimited(f"cooling down {int(self.cooldown_until - time.time())}s more")
        started = time.perf_counter()
        endpoint = url.split("?", 1)[0]
        try:
            r = await self.client.get(url, params=params, headers=self._headers())
            if r.status_code in (429, 999):
                self.strikes += 1
                wait = min(60 * (2 ** (self.strikes - 1)), 900)
                self.cooldown_until = time.time() + wait
                log.warning("LinkedIn rate-limited | status=%d endpoint=%s strikes=%d cooldown=%ss duration=%.2fs",
                            r.status_code, endpoint, self.strikes, wait, time.perf_counter() - started)
                raise RateLimited(str(r.status_code))
            r.raise_for_status()
            self.strikes = 0
            log.debug("LinkedIn request succeeded | status=%d endpoint=%s bytes=%d duration=%.2fs",
                      r.status_code, endpoint, len(r.content), time.perf_counter() - started)
            return r.text
        except RateLimited:
            raise
        except httpx.HTTPError:
            log.exception("LinkedIn request failed | endpoint=%s duration=%.2fs",
                          endpoint, time.perf_counter() - started)
            raise

    async def search(self, keywords: str, location: str, lookback: int, max_pages: int) -> list[Job]:
        search_started = time.perf_counter()
        jobs: dict[str, Job] = {}
        for page in range(max_pages):
            params = {
                "keywords": keywords,
                "location": location,
                "f_TPR": f"r{lookback}",   # posted within the last N seconds
                "sortBy": "DD",            # most recent first
                "start": page * 10,
            }
            html = await self._get(SEARCH_URL, params)
            found = parse_search(html)
            for j in found:
                jobs.setdefault(j.id, j)
            log.debug("LinkedIn search page %d: found %d cards (%d unique).",
                      page + 1, len(found), len(jobs))
            if len(found) < 10:
                break
            await asyncio.sleep(random.uniform(0.5, 1.5))
        results = list(jobs.values())
        log.debug("LinkedIn returned %d jobs for %r in %s (%.1fs).",
                  len(results), keywords, location, time.perf_counter() - search_started)
        return results

    async def description(self, job_id: str) -> str:
        started = time.perf_counter()
        html = await self._get(DETAIL_URL.format(id=job_id))
        description = parse_description(html)
        log.debug("Read LinkedIn job details | id=%s chars=%d duration=%.2fs",
                  job_id, len(description), time.perf_counter() - started)
        return description


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
    text = node.get_text("\n", strip=True) if node else soup.get_text("\n", strip=True)
    return text[:12000]
