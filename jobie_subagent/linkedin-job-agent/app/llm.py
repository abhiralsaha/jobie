import json
import logging
import re
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger("llm")

SYSTEM = (
    "You analyse job postings. Read the job description and extract the work-experience "
    "requirement for the candidate. Reply with ONLY a JSON object, no markdown:\n"
    '{"min_years": <number or null>, "max_years": <number or null>, '
    '"fresher_ok": <true|false>, "reason": "<max 20 words>"}\n'
    "Rules: min_years = the MINIMUM years of professional experience required "
    "(e.g. '0-2 years' -> 0, '2-4 yrs' -> 2, '3+ years' -> 3, 'freshers welcome' -> 0). "
    "If a range is given, min_years is the lower bound. Ignore years asked for a specific "
    "single tool only if the overall requirement is clearly lower. "
    "If the posting states no experience requirement at all, use null for both and fresher_ok=false."
)


@dataclass
class Verdict:
    min_years: float | None
    max_years: float | None
    fresher_ok: bool
    reason: str


class LLM:
    def __init__(self, provider: str, api_key: str, model: str, base_url: str):
        self.provider, self.key, self.model, self.base = provider, api_key, model, base_url
        self.client = httpx.AsyncClient(timeout=45)

    async def close(self):
        await self.client.aclose()

    async def analyse(self, title: str, description: str) -> Verdict:
        started = time.perf_counter()
        log.debug("AI experience check started | provider=%s model=%s title=%r",
                  self.provider, self.model, title)
        user = f"Job title: {title}\n\nJob description:\n{description}"
        try:
            text = await (self._anthropic(user) if self.provider == "anthropic" else self._openai(user))
            verdict = _parse(text)
            log.debug("AI experience check finished | min_years=%s max_years=%s fresher_ok=%s duration=%.2fs",
                      verdict.min_years, verdict.max_years, verdict.fresher_ok, time.perf_counter() - started)
            return verdict
        except Exception:
            log.exception("LLM analysis failed | provider=%s model=%s duration=%.2fs",
                          self.provider, self.model, time.perf_counter() - started)
            raise

    async def _anthropic(self, user: str) -> str:
        r = await self.client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 300, "temperature": 0,
                  "system": SYSTEM, "messages": [{"role": "user", "content": user}]},
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])

    async def _openai(self, user: str) -> str:
        r = await self.client.post(
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": self.model, "temperature": 0, "max_tokens": 300,
                  "messages": [{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": user}]},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _parse(text: str) -> Verdict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"No JSON in LLM reply: {text[:200]}")
    d = json.loads(m.group(0))
    return Verdict(_num(d.get("min_years")), _num(d.get("max_years")),
                   bool(d.get("fresher_ok", False)), str(d.get("reason", ""))[:200])
