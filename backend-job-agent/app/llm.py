import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger("llm")
MAX_REQUEST_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 60

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
        self.request_sem = asyncio.Semaphore(1)

    async def close(self):
        await self.client.aclose()

    async def _post_with_retry(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                async with self.request_sem:
                    response = await self.client.post(url, headers=headers, json=json)
            except httpx.TransportError:
                if attempt == MAX_REQUEST_ATTEMPTS:
                    log.exception("LLM transport request failed | provider=%s model=%s attempt=%d/%d",
                                  self.provider, self.model, attempt, MAX_REQUEST_ATTEMPTS)
                    raise
                delay = min(2 ** (attempt - 1), MAX_RETRY_DELAY_SECONDS)
                log.warning("LLM transport request failed; retrying | provider=%s model=%s attempt=%d/%d "
                            "retry_in_seconds=%d", self.provider, self.model, attempt, MAX_REQUEST_ATTEMPTS, delay)
                await asyncio.sleep(delay)
                continue

            elapsed = time.perf_counter() - started
            if response.status_code < 500 and response.status_code != 429:
                return response

            if attempt == MAX_REQUEST_ATTEMPTS:
                log.error("LLM transient request failure; retries exhausted | provider=%s model=%s "
                          "status=%d attempt=%d/%d duration=%.2fs response=%s",
                          self.provider, self.model, response.status_code, attempt,
                          MAX_REQUEST_ATTEMPTS, elapsed, response.text[:400])
                return response

            delay = self._retry_delay(response, attempt)
            log.warning("LLM transient request failure; retrying | provider=%s model=%s status=%d "
                        "attempt=%d/%d retry_in_seconds=%.1f response=%s",
                        self.provider, self.model, response.status_code, attempt,
                        MAX_REQUEST_ATTEMPTS, delay, response.text[:400])
            await asyncio.sleep(delay)
        raise RuntimeError("LLM retry loop exited unexpectedly")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            try:
                payload = response.json()
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict):
                error = payload.get("error", {})
                metadata = error.get("metadata", {}) if isinstance(error, dict) else {}
                provider_headers = metadata.get("headers", {}) if isinstance(metadata, dict) else {}
                if isinstance(provider_headers, dict):
                    retry_after = provider_headers.get("Retry-After")
        if retry_after:
            try:
                return min(max(0.0, float(retry_after)), MAX_RETRY_DELAY_SECONDS)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(str(retry_after))
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                    return min(max(0.0, delay), MAX_RETRY_DELAY_SECONDS)
                except (TypeError, ValueError, OverflowError):
                    pass
        return float(min(2 ** (attempt - 1), MAX_RETRY_DELAY_SECONDS))

    async def analyse(self, title: str, description: str) -> Verdict:
        started = time.perf_counter()
        user = f"Job title: {title}\n\nJob description:\n{description}"
        log.info("LLM analyse started | provider=%s model=%s title=%s description_chars=%d", self.provider, self.model, title, len(description))
        text = await (self._anthropic(user) if self.provider == "anthropic" else self._openai(user))
        parsed = _parse(text)
        log.info("LLM analyse complete | provider=%s model=%s duration=%.2fs verdict=%s", self.provider, self.model, time.perf_counter() - started, parsed)
        return parsed

    async def verify(self) -> None:
        started = time.perf_counter()
        log.info("LLM startup verification started | provider=%s model=%s", self.provider, self.model)
        try:
            verdict = await self.analyse(
                "LLM startup verification",
                "This test role requires exactly 2 years of professional experience.",
            )
        except Exception:
            log.exception("LLM startup verification failed | provider=%s model=%s duration=%.2fs",
                          self.provider, self.model, time.perf_counter() - started)
            return
        log.info("LLM startup verification succeeded | provider=%s model=%s response_parsed=true "
                 "duration=%.2fs verdict=%s",
                 self.provider, self.model, time.perf_counter() - started, verdict)

    async def _anthropic(self, user: str) -> str:
        started = time.perf_counter()
        r = await self._post_with_retry(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": self.model, "max_tokens": 1000, "temperature": 0,
                  "system": SYSTEM, "messages": [{"role": "user", "content": user}]},
        )
        elapsed = time.perf_counter() - started
        if r.status_code >= 400:
            log.warning("Anthropic LLM request failed | status=%s duration=%.2fs response=%s", r.status_code, elapsed, r.text[:400])
        r.raise_for_status()
        out = "".join(b.get("text", "") for b in r.json()["content"])
        log.info("Anthropic LLM request complete | duration=%.2fs chars=%d", elapsed, len(out))
        return out

    async def _openai(self, user: str) -> str:
        started = time.perf_counter()
        r = await self._post_with_retry(
            f"{self.base}/chat/completions",
            headers={"Authorization": f"Bearer {self.key}"},
            json={"model": self.model, "temperature": 0, "max_tokens": 1000,
                  "messages": [{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": user}]},
        )
        elapsed = time.perf_counter() - started
        if r.status_code >= 400:
            log.warning("OpenAI-compatible LLM request failed | status=%s model=%s duration=%.2fs response=%s",
                        r.status_code, self.model, elapsed, r.text[:400])
        r.raise_for_status()
        choice = r.json()["choices"][0]
        out = choice.get("message", {}).get("content")
        if not isinstance(out, str) or not out.strip():
            raise ValueError(f"LLM returned empty or non-text content (finish_reason={choice.get('finish_reason')})")
        finish_reason = choice.get("finish_reason", "unknown")
        if finish_reason == "length":
            log.error("OpenAI-compatible LLM response truncated at token limit | model=%s duration=%.2fs chars=%d",
                      self.model, elapsed, len(out))
            raise ValueError(f"LLM response truncated at max_tokens=1000 (finish_reason={finish_reason})")
        log.info("OpenAI-compatible LLM request complete | model=%s duration=%.2fs chars=%d finish_reason=%s",
                 self.model, elapsed, len(out), finish_reason)
        return out


def _num(v):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("LLM JSON experience values must be numbers or null")
    number = float(v)
    if number < 0:
        raise ValueError("LLM JSON experience values cannot be negative")
    return number


def _parse(text: str) -> Verdict:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate, flags=re.I)
    start = candidate.find("{")
    if start < 0:
        raise ValueError(f"No JSON in LLM reply: {text[:200]}")
    try:
        d, _ = json.JSONDecoder().raw_decode(candidate[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"Incomplete or invalid JSON in LLM reply: {text[:200]}") from exc
    if not isinstance(d, dict):
        raise ValueError("LLM JSON reply must be an object")
    required = {"min_years", "max_years", "fresher_ok", "reason"}
    if not required.issubset(d):
        raise ValueError("LLM JSON reply is missing required fields")
    fresher_ok = d["fresher_ok"]
    if not isinstance(fresher_ok, bool):
        raise ValueError("LLM JSON field fresher_ok must be a boolean")
    reason = d["reason"]
    if not isinstance(reason, str):
        raise ValueError("LLM JSON field reason must be a string")
    min_years = _num(d["min_years"])
    max_years = _num(d["max_years"])
    if min_years is not None and max_years is not None and min_years > max_years:
        raise ValueError("LLM JSON min_years cannot exceed max_years")
    return Verdict(min_years, max_years, fresher_ok, reason[:200])
