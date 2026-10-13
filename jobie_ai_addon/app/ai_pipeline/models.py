from __future__ import annotations

import re
from dataclasses import dataclass, field

from .util import clean_text, sha256_hex


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    location: str = ""
    url: str = ""
    posted_at: str | None = None          # ISO string or relative text; verified later
    description: str | None = None
    source: str = "linkedin"

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        jid = str(d.get("job_id") or "").strip()
        if not jid:
            m = re.search(r"(\d{6,})", str(d.get("url") or ""))
            jid = m.group(1) if m else ""
        if not jid:
            raise ValueError("job has no job_id and none could be derived from the URL")
        return cls(job_id=jid, title=clean_text(d.get("title") or "", 200),
                   company=clean_text(d.get("company") or "", 200), location=clean_text(d.get("location") or "", 200),
                   url=(d.get("url") or "").strip(), posted_at=d.get("posted_at"),
                   description=d.get("description"), source=d.get("source") or "linkedin")

    @classmethod
    def from_row(cls, r: dict) -> "Job":
        return cls(job_id=r["job_id"], title=r.get("title") or "", company=r.get("company") or "",
                   location=r.get("location") or "", url=r.get("url") or "", posted_at=r.get("posted_at"),
                   description=r.get("description"), source=r.get("source") or "linkedin")


def desc_hash(description: str | None) -> str:
    return sha256_hex(description or "")
