"""Timestamp parsing and look-back window maths.

Reusable by ``fresh_watcher.py``: the window must be *longer* than one poll
interval + jitter + run time, otherwise a job posted just after one poll and
just before the next can fall between two windows and never be seen.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta, timezone

_REL = re.compile(r"(\d+)\s*(second|sec|minute|min|hour|hr|day|week|month)s?\s+ago")
_UNIT = {
    "second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600,
    "day": 86400, "week": 7 * 86400, "month": 30 * 86400,
}


def parse_posted_at(value, now: datetime, default_tz=timezone.utc) -> datetime | None:
    """Return an aware UTC datetime or None when the value cannot be verified.

    Accepts datetimes, ISO strings (naive strings are interpreted in
    ``default_tz``), and LinkedIn-style relative strings ("2 minutes ago").
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=default_tz)
        return dt.astimezone(timezone.utc)
    s = str(value).strip().lower()
    if s in {"just now", "now", "moments ago", "a moment ago"}:
        return now.astimezone(timezone.utc)
    if s == "yesterday":
        return now.astimezone(timezone.utc) - timedelta(days=1)
    m = _REL.search(s)
    if m:
        return now.astimezone(timezone.utc) - timedelta(seconds=int(m.group(1)) * _UNIT[m.group(2)])
    try:
        dt = datetime.fromisoformat(s.replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(timezone.utc)


def compute_lookback_seconds(poll_interval: float, max_jitter: float = 0.0,
                             max_run_seconds: float = 0.0, safety: float = 15.0) -> int:
    """Smallest window that guarantees consecutive polls overlap."""
    return int(math.ceil(poll_interval + max_jitter + max_run_seconds + safety))


def catchup_lookback_seconds(now: datetime, last_success: datetime | None, base_window: int, cap: int = 86400,
                             safety: int = 30) -> int:
    """Look-back to use for the NEXT poll.

    A fixed 120s window silently loses every job posted while polling was paused (HTTP 429 cool-down,
    crash, redeploy, Render sleep). After a gap, widen the window to reach back to the last *successful*
    poll (plus a safety margin), never above ``cap`` (LinkedIn's f_TPR accepts r<seconds>).
    The seen_jobs table de-duplicates, so a wider window cannot cause repeats.
    """
    if last_success is None:
        return min(cap, max(base_window, 3600))          # first run after a cold start: look back an hour
    gap = (now - last_success).total_seconds() + safety
    return int(min(cap, max(base_window, math.ceil(gap))))


def is_within_window(posted_at: datetime | None, now: datetime, window_seconds: float,
                     future_tolerance: float = 120.0) -> bool:
    """True if ``posted_at`` is inside [now - window, now + tolerance]."""
    if posted_at is None:
        return False
    delta = (now - posted_at).total_seconds()
    return -future_tolerance <= delta <= window_seconds


def format_relative(posted_at: datetime | None, now: datetime) -> str:
    if posted_at is None:
        return "not verified"
    secs = int((now - posted_at).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"
