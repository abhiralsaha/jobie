import re
from typing import Dict, Iterable, List
from urllib.parse import urlsplit

import requests

from app.config import settings


class TelegramNotifier:
    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str | None = None, chat_id: str | None = None):
        self.bot_token = settings.TELEGRAM_BOT_TOKEN if bot_token is None else bot_token
        self.chat_id = settings.TELEGRAM_CHAT_ID if chat_id is None else chat_id

    @property
    def is_configured(self) -> bool:
        return bool(
            re.fullmatch(
                r"\d+:[A-Za-z0-9_-]{20,}", self.bot_token
            )
            and re.fullmatch(r"-?\d+", self.chat_id)
        )

    def _send_text(self, text: str) -> str:
        if not self.is_configured:
            raise ValueError("Telegram bot token or chat ID is not configured")
        if not text or len(text) > 4096:
            raise ValueError("Telegram message text must contain 1 to 4096 characters")
        response = requests.post(
            self.API_URL.format(token=self.bot_token),
            json={
                "chat_id": self.chat_id,
                "text": text,
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        result = response.json()
        if result.get("ok") is not True:
            raise RuntimeError("Telegram Bot API rejected the message")
        return str(result["result"]["message_id"])

    def send_alert(self, job_title: str, company: str, job_url: str) -> str:
        parsed_url = urlsplit(job_url)
        if parsed_url.scheme != "https" or not parsed_url.hostname:
            raise ValueError("Job links must be valid HTTPS URLs")

        safe_title = " ".join(job_title.split())[:300]
        safe_company = " ".join(company.split())[:200]
        body = f"New job to review: {safe_title} at {safe_company}\nReview it manually: {job_url}"
        return self._send_text(body)

    def send_digest(
        self, jobs: Iterable[Dict[str, str]], lookback_period: str = "24 hours"
    ) -> List[str]:
        listings = list(jobs)
        if not listings:
            return []

        prefix = f"Jobie: {len(listings)} job(s) posted in the last {lookback_period}."
        chunks = []
        current = ""
        for index, job in enumerate(listings, start=1):
            title = " ".join(str(job.get("title", "Untitled role")).split())[:180]
            company = " ".join(str(job.get("company", "Unknown company")).split())[:120]
            url = str(job.get("url", ""))
            parsed_url = urlsplit(url)
            if parsed_url.scheme != "https" or not parsed_url.hostname or len(url) > 900:
                continue

            item = f"{index}. {title} at {company}\n{url}"
            if current and len(current) + len(item) + 2 > 1250:
                chunks.append(current)
                current = item
            else:
                current = f"{current}\n\n{item}" if current else item
        if current:
            chunks.append(current)
        if not chunks:
            return []

        responses = []
        total_chunks = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            body = f"{prefix} (part {index}/{total_chunks})\n\n{chunk}"
            responses.append(self._send_text(body))
        return responses
