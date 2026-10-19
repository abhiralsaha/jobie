import asyncio
import html
import logging

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
        for _ in range(retries):
            try:
                r = await self.client.post(self.url, json={
                    "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                    "disable_web_page_preview": False})
                if r.status_code == 429:
                    await asyncio.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
                    continue
                if r.status_code >= 400:
                    log.error("Telegram error %s: %s", r.status_code, r.text[:200])
                    return False
                return True
            except httpx.HTTPError as e:
                log.warning("Telegram network error: %s", e)
                await asyncio.sleep(2)
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
