from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class JobListing:
    job_id: str
    title: str
    company: str
    url: str
    posted_at: Optional[str] = None
    raw_time_text: Optional[str] = None
    source: str = "manual"
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class ApprovalRequest:
    job_id: str
    title: str
    company: str
    url: str
    request_id: str
    approved: bool = False
