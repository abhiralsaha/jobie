import json
import sys
import time
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from app.config import settings
from app.job_intake import normalize_job

FEED_URL = settings.AUTHORIZED_JOB_FEED_URL
JOBIE_API_URL = settings.JOBIE_API_URL
POLL_SECONDS = settings.FEED_POLL_SECONDS
FEED_TOKEN = settings.AUTHORIZED_JOB_FEED_TOKEN
JOBIE_API_TOKEN = settings.JOBIE_API_TOKEN
MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_FEED_JOBS = 500


def validate_transport(url: str, name: str) -> None:
    parsed = urlparse(url)
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f"{name} must be a valid URL without embedded credentials or fragments")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{name} has an invalid port") from exc
    if parsed.scheme == "https":
        return
    if (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        return
    raise ValueError(f"{name} must use HTTPS unless it targets localhost")


def request_headers() -> Dict[str, str]:
    headers = {"Accept": "application/json", "User-Agent": "JobieAuthorizedFeed/1.0"}
    if FEED_TOKEN:
        headers["Authorization"] = f"Bearer {FEED_TOKEN}"
    return headers


def fetch_feed() -> List[Dict[str, Any]]:
    if not FEED_URL:
        raise ValueError("AUTHORIZED_JOB_FEED_URL is not configured")
    validate_transport(FEED_URL, "AUTHORIZED_JOB_FEED_URL")

    request = Request(FEED_URL, headers=request_headers())
    with urlopen(request, timeout=20) as response:
        raw_payload = response.read(MAX_FEED_BYTES + 1)
    if len(raw_payload) > MAX_FEED_BYTES:
        raise ValueError("Authorized feed response exceeds the 5 MB limit")
    payload = json.loads(raw_payload.decode("utf-8"))

    if isinstance(payload, dict):
        payload = payload.get("jobs")
    if not isinstance(payload, list):
        raise ValueError("Authorized feed must return a JSON list or an object with a jobs list")
    if len(payload) > MAX_FEED_JOBS or any(not isinstance(job, dict) for job in payload):
        raise ValueError("Authorized feed must contain at most 500 job objects")
    return payload


def send_to_jobie(job: Dict[str, Any]) -> Dict[str, Any]:
    if not JOBIE_API_TOKEN:
        raise ValueError("JOBIE_API_TOKEN is not configured")
    validate_transport(JOBIE_API_URL, "JOBIE_API_URL")
    normalized = normalize_job(job)
    normalized["source"] = str(job.get("source", "authorized_feed"))
    body = json.dumps(normalized).encode("utf-8")
    request = Request(
        JOBIE_API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {JOBIE_API_TOKEN}",
        },
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def poll_once() -> int:
    jobs = fetch_feed()
    sent = 0
    for raw_job in jobs:
        try:
            result = send_to_jobie(raw_job)
            print(f"Feed record submitted: imported={result.get('imported', False)}")
            sent += 1
        except (ValueError, KeyError, HTTPError, URLError, TimeoutError) as exc:
            print(f"Skipping feed record ({type(exc).__name__})", file=sys.stderr)
    return sent


def run_forever() -> None:
    print(f"Polling authorized feed every {POLL_SECONDS} seconds")
    while True:
        try:
            poll_once()
        except (ValueError, HTTPError, URLError, TimeoutError) as exc:
            print(f"Feed poll failed ({type(exc).__name__})", file=sys.stderr)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--once":
        poll_once()
    else:
        run_forever()
