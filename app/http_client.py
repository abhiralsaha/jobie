"""
http_client.py
==============
Shared HTTP client for all LinkedIn scraping.  Designed to minimise the
fingerprint that Render's shared IPs present to LinkedIn's guest endpoint.

Techniques used
---------------
1. **User-Agent rotation** — picks a different realistic UA each request.
2. **Header variation** — Accept-Language, Accept-Encoding, and Sec-Fetch-*
   headers vary slightly per request to avoid a static fingerprint.
3. **Session recycling** — a fresh requests.Session is created every
   SESSION_RECYCLE_AFTER requests, resetting cookies and TCP state.
4. **Optional rotating proxy** — set HTTP_PROXY / HTTPS_PROXY (or the
   dedicated LINKEDIN_PROXY_URL) in .env or the Render dashboard to route
   requests through any proxy (residential, rotating, or datacenter).
   Example providers with free tiers: Webshare, ProxyScrape, ScraperAPI.
5. **Exponential back-off on 429** — respects Retry-After header.
6. **Escalating circuit-breaker** — first trip: 5 min cooldown, second: 15 min,
   third+: 30 min. Resets on any successful response.

Environment variables
---------------------
  LINKEDIN_PROXY_URL         Full proxy URL, e.g. http://user:pass@host:port
                             (also honours standard HTTP_PROXY / HTTPS_PROXY)
  SESSION_RECYCLE_AFTER      Requests before a new session is created (default 10)
"""

from __future__ import annotations

import logging
import os
import random
import time
from typing import Any

import requests

_log = logging.getLogger("jobie.http_client")

# ---------------------------------------------------------------------------
# User-Agent pool — a representative spread of real browser UA strings.
# LinkedIn's detection keys heavily on this; using a single static string is
# the fastest route to being fingerprinted.
# ---------------------------------------------------------------------------
_USER_AGENTS: list[str] = [
    # Chrome 124 — Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    # Chrome 123 — Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    # Chrome 124 — macOS
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    # Firefox 125 — Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
    # Firefox 124 — Linux
    (
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    # Safari 17 — macOS
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4.1 Safari/605.1.15"
    ),
    # Edge 124 — Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
]

# ---------------------------------------------------------------------------
# Accept-Language pool — vary the locale header so all requests don't look
# like they come from a single automated account.
# ---------------------------------------------------------------------------
_ACCEPT_LANGUAGES: list[str] = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.8,hi;q=0.6",
    "en-IN,en;q=0.9",
    "en-US,en;q=0.9,en-IN;q=0.7",
]

# ---------------------------------------------------------------------------
# Proxy config — LINKEDIN_PROXY_URL takes precedence; falls back to the
# standard HTTP_PROXY / HTTPS_PROXY env vars (requests picks those up
# automatically, but we also set them explicitly for clarity).
# ---------------------------------------------------------------------------
_PROXY_URL: str = os.getenv("LINKEDIN_PROXY_URL", "")
_PROXIES: dict[str, str] | None = (
    {"http": _PROXY_URL, "https": _PROXY_URL} if _PROXY_URL else None
)
if _PROXY_URL:
    _log.info("LinkedIn HTTP client: routing through proxy %s", _PROXY_URL.split("@")[-1])

# How many requests to make before recycling the session (new cookies, new TCP)
SESSION_RECYCLE_AFTER: int = int(os.getenv("SESSION_RECYCLE_AFTER", "10"))

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------
_session: requests.Session | None = None
_request_count: int = 0


def _random_headers() -> dict[str, str]:
    """Return a varied but plausible set of browser-like headers."""
    ua = random.choice(_USER_AGENTS)
    is_firefox = "Firefox" in ua
    return {
        "User-Agent": ua,
        "Accept-Language": random.choice(_ACCEPT_LANGUAGES),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            if is_firefox
            else "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }


def _get_session(force_new: bool = False) -> requests.Session:
    """Return the current session, recycling it every SESSION_RECYCLE_AFTER requests."""
    global _session, _request_count
    if force_new or _session is None or _request_count >= SESSION_RECYCLE_AFTER:
        if _session is not None:
            try:
                _session.close()
            except Exception:
                pass
        _session = requests.Session()
        if _PROXIES:
            _session.proxies.update(_PROXIES)
        _request_count = 0
        _log.debug("HTTP session recycled (proxy=%s)", bool(_PROXY_URL))
    return _session


# ---------------------------------------------------------------------------
# Circuit breaker with escalating cooldown
# ---------------------------------------------------------------------------
_circuit_open_until: float = 0.0
_circuit_trip_count: int = 0
_COOLDOWN_STEPS: list[int] = [300, 900, 1800]  # 5 min → 15 min → 30 min


def _trip_circuit() -> float:
    """Open the circuit breaker and return how long (seconds) it will stay open."""
    global _circuit_open_until, _circuit_trip_count
    idx = min(_circuit_trip_count, len(_COOLDOWN_STEPS) - 1)
    cooldown = _COOLDOWN_STEPS[idx]
    _circuit_open_until = time.time() + cooldown
    _circuit_trip_count += 1
    return cooldown


# Public alias so callers don't need to reach into private names
trip_circuit = _trip_circuit


def reset_circuit() -> None:
    """Reset the circuit breaker after a successful request."""
    global _circuit_open_until, _circuit_trip_count
    _circuit_open_until = 0.0
    _circuit_trip_count = 0


def circuit_is_open() -> tuple[bool, int]:
    """Return (is_open, seconds_remaining)."""
    remaining = int(max(0, _circuit_open_until - time.time()))
    return remaining > 0, remaining


# ---------------------------------------------------------------------------
# Core fetch with backoff
# ---------------------------------------------------------------------------

def fetch(
    url: str,
    params: dict[str, Any],
    max_retries: int = 5,
    log: logging.Logger | None = None,
) -> str:
    """GET *url* with *params*, rotating headers and backing off on 429.

    Returns the response body as a string on success.
    Raises RuntimeError if all retries are exhausted (caller should trip the
    circuit breaker via _trip_circuit()).
    Raises requests.HTTPError for non-429 HTTP errors.
    """
    global _request_count
    log = log or _log
    delay = 5.0

    for attempt in range(max_retries):
        sess = _get_session()
        headers = _random_headers()
        try:
            resp = sess.get(url, params=params, headers=headers, timeout=(5, 20))
        except requests.exceptions.ProxyError as exc:
            log.warning("Proxy error on attempt %d: %s", attempt + 1, exc)
            _get_session(force_new=True)  # new session, maybe new proxy rotation
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        except requests.exceptions.ConnectionError as exc:
            log.warning("Connection error on attempt %d: %s", attempt + 1, exc)
            time.sleep(delay)
            delay = min(delay * 2, 60)
            continue

        _request_count += 1

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", delay))
            wait = retry_after + random.uniform(0, 5)
            log.warning(
                "429 rate-limited (attempt %d/%d) — sleeping %.0fs "
                "(Retry-After: %s)",
                attempt + 1,
                max_retries,
                wait,
                resp.headers.get("Retry-After", "absent"),
            )
            time.sleep(wait)
            delay = min(delay * 2, 120)
            # Recycle the session after a 429 — new cookies, new TCP connection
            _get_session(force_new=True)
            continue

        resp.raise_for_status()
        reset_circuit()
        return resp.text

    raise RuntimeError(
        f"Rate-limited: exhausted {max_retries} retries for {url}"
    )
