"""Small shared helpers (no project-specific imports)."""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"jobie_ai.{name}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Fixed-width UTC ISO string (lexicographically sortable)."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def slugify(text: str, max_len: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return (text[:max_len].strip("_")) or "x"


_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_QUOTES = {ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201c"): '"', ord("\u201d"): '"',
           ord("\u00a0"): " ", ord("\u200b"): ""}


def norm_text(s: str) -> str:
    """Lower-case, unify dashes/quotes, collapse whitespace (for evidence matching)."""
    s = (s or "").translate(_DASHES).translate(_QUOTES).lower()
    return re.sub(r"\s+", " ", s).strip()


def mask(value: str | None) -> str:
    return "set" if value else "NOT SET"


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(s: str, max_len: int | None = None) -> str:
    s = _CTRL.sub("", s or "").strip()
    if max_len and len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "\u2026"
    return s
