"""Full job-description retrieval.

The HTTP layer is injectable (``set_http_get``) so you can route requests
through the project's existing client / circuit breaker instead of opening a
second, uncoordinated stream of requests to LinkedIn. Default: one polite
``requests`` GET per *new* job; a 429 starts a shared, escalating cool-down.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable

import requests
from bs4 import BeautifulSoup

from .util import get_logger

log = get_logger("jd_fetch")

GUEST_POSTING_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
_TRUNC_TAIL = re.compile(r"(show more|see more|read more|\.\.\.|\u2026)\s*$", re.I)


class FetchRateLimited(Exception):
    def __init__(self, retry_after: float):
        super().__init__(f"rate limited; retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class FetchError(Exception):
    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


def extract_description_from_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    node = (soup.select_one("div.show-more-less-html__markup") or soup.select_one("div.description__text")
            or soup.select_one("section.description"))
    if node is None:
        return ""
    for br in node.find_all("br"):
        br.replace_with("\n")
    for li in node.find_all("li"):
        li.insert_before("\n- ")
    for tag in node.find_all(["p", "div", "ul", "ol", "h1", "h2", "h3", "h4"]):
        tag.insert_after("\n")
    text = node.get_text()
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def looks_truncated(text: str) -> bool:
    return bool(_TRUNC_TAIL.search((text or "").strip()))


def is_complete(text: str | None, min_chars: int) -> bool:
    t = (text or "").strip()
    return len(t) >= min_chars and not looks_truncated(t)


class DescriptionFetcher:
    def __init__(self, http_get: Callable[[str], str] | None = None, *, min_chars: int = 250,
                 clock: Callable[[], float] = time.monotonic):
        self._http_get = http_get or self._default_get
        self.min_chars = min_chars
        self._clock = clock
        self._lock = threading.Lock()
        self._cooldown_until = 0.0
        self._strikes = 0

    def set_http_get(self, fn: Callable[[str], str]) -> None:
        self._http_get = fn

    @staticmethod
    def _default_get(url: str) -> str:
        try:
            r = requests.get(url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (compatible; JobieAI/1.0)", "Accept-Language": "en-US,en;q=0.8"})
        except requests.RequestException as exc:
            raise FetchError(f"network error ({type(exc).__name__})") from None
        if r.status_code == 429:
            ra = r.headers.get("Retry-After", "")
            raise FetchRateLimited(float(ra) if ra.isdigit() else 0.0)
        if r.status_code != 200:
            retryable = not (400 <= r.status_code < 500)
            raise FetchError(f"HTTP {r.status_code}", retryable=retryable)
        return r.text

    def fetch(self, job_id: str) -> tuple[str, bool]:
        """Return (description_text, is_complete). Raises FetchRateLimited / FetchError."""
        with self._lock:
            remaining = self._cooldown_until - self._clock()
        if remaining > 0:
            raise FetchRateLimited(remaining)
        try:
            html = self._http_get(GUEST_POSTING_URL.format(job_id=job_id))
        except FetchRateLimited as rl:
            with self._lock:
                self._strikes += 1
                wait = max(rl.retry_after, min(900.0, 60.0 * (2 ** (self._strikes - 1))))
                self._cooldown_until = self._clock() + wait
            raise FetchRateLimited(wait) from None
        with self._lock:
            self._strikes = 0
        text = extract_description_from_html(html)
        return text, is_complete(text, self.min_chars)
