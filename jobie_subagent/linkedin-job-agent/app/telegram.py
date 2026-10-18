import asyncio
import html
import logging
import time

import httpx

from .linkedin import Job
from .llm import Verdict

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.client = httpx.AsyncClient(timeout=20)

    async def close(self):
        await self.client.aclose()

    async def send(self, text: str, retries: int = 3) -> bool:
        started = time.perf_counter()
        for attempt in range(1, retries + 1):
            request_started = time.perf_counter()
            try:
                r = await self.client.post(self.url, json={
                    "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": False})
                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 5) + 1
                    log.warning("Telegram rate-limited | attempt=%d/%d retry_in=%.2fs request_duration=%.2fs",
                                attempt, retries, wait, time.perf_counter() - request_started)
                    await asyncio.sleep(wait)
                    continue
                if r.status_code >= 400:
                    if r.status_code == 400 and "chat not found" in r.text.lower():
                        log.error(
                            "Telegram cannot find the configured chat. For a private chat, open this "
                            "bot and send it any message (a slash command is not required), then verify "
                            "TELEGRAM_CHAT_ID belongs to this bot. For a group, add this bot and use "
                            "that group's chat ID."
                        )
                    log.error("Telegram error %s: %s", r.status_code, r.text[:200])
                    log.error("Telegram send failed | attempt=%d/%d duration=%.2fs",
                              attempt, retries, time.perf_counter() - started)
                    return False
                log.debug("Telegram message sent | attempt=%d/%d duration=%.2fs",
                          attempt, retries, time.perf_counter() - started)
                return True
            except httpx.HTTPError as e:
                log.warning("Telegram network error | attempt=%d/%d duration=%.2fs error=%s",
                            attempt, retries, time.perf_counter() - request_started, e)
                if attempt < retries:
                    await asyncio.sleep(2)
        log.error("Telegram send exhausted retries | attempts=%d total_duration=%.2fs",
                  retries, time.perf_counter() - started)
        return False

    async def send_job(self, job: Job, v: Verdict, location_query: str) -> bool:
        if v.min_years is None and v.max_years is None:
            exp = "Fresher / 0-2 yrs" if v.fresher_ok else "Not specified"
        else:
            lo = "?" if v.min_years is None else f"{v.min_years:g}"
            hi = "" if v.max_years is None else f"-{v.max_years:g}"
            exp = f"{lo}{hi} yrs"
        e = html.escape
        text = (
            f"🆕 <b>{e(job.title)}</b>\n"
            f"🏢 {e(job.company)}\n"
            f"📍 {e(job.location or location_query)}\n"
            f"🎯 Experience: {e(exp)}\n"
            f"💬 {e(v.reason)}\n\n"
            f"🔗 {job.url}"
        )
        return await self.send(text)
