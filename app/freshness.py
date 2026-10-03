from datetime import datetime, timezone
from typing import Optional


def parse_timestamp(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def posted_within_seconds(value: str, seconds: int = 60) -> bool:
    posted_at = parse_timestamp(value)
    if posted_at is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - posted_at).total_seconds()
    return 0 <= age_seconds <= seconds
