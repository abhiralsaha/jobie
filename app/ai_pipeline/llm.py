"""Provider-agnostic JSON-returning LLM client (Anthropic, OpenAI-compatible, Gemini).

* Reuses whatever key the project already has (see ``Settings.from_env``).
* Client-side rate limit (AI_MAX_RPM), bounded retries with exponential
  backoff, honours ``Retry-After`` on 429.
* Secrets never appear in logs or exception text.
"""
from __future__ import annotations

import json
import random
import re
import threading
import time
from typing import Callable

import requests

from .settings import Settings
from .util import get_logger

log = get_logger("llm")


class LLMError(Exception):
    """Non-retryable or exhausted-retries failure."""


class LLMMalformed(LLMError):
    """The model answered, but not with a JSON object."""


def extract_json(text: str) -> dict:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise LLMMalformed("response contained no JSON object")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMMalformed(f"invalid JSON: {exc.msg}") from None
    if not isinstance(obj, dict):
        raise LLMMalformed("top-level JSON value is not an object")
    return obj


class LLMClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.s = settings
        self.session = session or requests.Session()
        self._sleep, self._clock = sleep, clock
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self.calls = 0

    # -- rate limiting -----------------------------------------------------
    def _throttle(self) -> None:
        gap = 60.0 / max(1, self.s.ai_max_rpm)
        with self._lock:
            now = self._clock()
            wait = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + gap
        if wait > 0:
            self._sleep(wait)

    # -- request building --------------------------------------------------
    def _build(self, system: str, user: str, max_tokens: int):
        p, key, model = self.s.ai_provider, self.s.ai_api_key, self.s.ai_model
        if p == "anthropic":
            return ("https://api.anthropic.com/v1/messages",
                    {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    {"model": model, "max_tokens": max_tokens, "temperature": 0, "system": system,
                     "messages": [{"role": "user", "content": user}]})
        if p == "gemini":
            return (f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    {"x-goog-api-key": key, "content-type": "application/json"},
                    {"systemInstruction": {"parts": [{"text": system}]},
                     "contents": [{"role": "user", "parts": [{"text": user}]}],
                     "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens,
                                          "responseMimeType": "application/json"}})
        if p in {"openai", "groq", "openrouter", "openai_compat"}:
            body = {"model": model, "temperature": 0, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
            if p in {"openai", "groq"}:
                body["response_format"] = {"type": "json_object"}
            return (self.s.ai_base_url.rstrip("/") + "/chat/completions",
                    {"Authorization": f"Bearer {key}", "content-type": "application/json"}, body)
        raise LLMError(f"unsupported AI_PROVIDER '{p}'")

    @staticmethod
    def _text(provider: str, data: dict) -> str:
        try:
            if provider == "anthropic":
                return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
            if provider == "gemini":
                return "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMMalformed("unexpected response shape") from None

    # -- public ------------------------------------------------------------
    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        if not self.s.ai_api_key:
            raise LLMError("no AI API key configured")
        url, headers, body = self._build(system, user, max_tokens)
        last = "unknown error"
        for attempt in range(self.s.ai_max_retries + 1):
            self._throttle()
            self.calls += 1
            try:
                resp = self.session.post(url, headers=headers, json=body, timeout=self.s.ai_timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last = f"network error ({type(exc).__name__})"   # exception text may embed the URL
            else:
                if resp.status_code == 200:
                    try:
                        return self._text(self.s.ai_provider, resp.json())
                    except ValueError:
                        raise LLMMalformed("response body was not JSON") from None
                last = f"HTTP {resp.status_code}: {resp.text[:160].replace(chr(10), ' ')}"
                if resp.status_code not in (408, 409, 429, 500, 502, 503, 504, 529):
                    raise LLMError(last)            # auth/400/etc: retrying cannot help
                retry_after = resp.headers.get("retry-after")
                if retry_after and retry_after.replace(".", "", 1).isdigit():
                    delay = min(60.0, float(retry_after))
                    if attempt < self.s.ai_max_retries:
                        log.warning("LLM %s; waiting %.0fs (Retry-After)", last[:40], delay)
                        self._sleep(delay)
                    continue
            if attempt < self.s.ai_max_retries:
                delay = min(60.0, 2.0 * (2 ** attempt)) + random.random()
                log.warning("LLM call failed (%s); retry %d in %.1fs", last[:60], attempt + 1, delay)
                self._sleep(delay)
        raise LLMError(f"gave up after {self.s.ai_max_retries + 1} attempts: {last}")

    def complete_json(self, system: str, user: str, max_tokens: int = 1500) -> dict:
        return extract_json(self.complete(system, user, max_tokens))
