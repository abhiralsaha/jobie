"""The ONLY file you need to touch your existing code with.

    from app.ai_pipeline import hook as ai_hook
    ...
    # in fresh_watcher.py, right after a job was inserted into seen_jobs (rowcount == 1):
    if ai_hook.enabled():
        ai_hook.on_new_job(job_id=job_id, title=title, company=company, location=location, url=url, posted_at=posted_at)
    else:
        <your existing immediate Telegram alert>

    # once at process start (e.g. in the watcher's main or FastAPI startup):
    ai_hook.start_background()

Optional: ``ai_hook.set_http_get(fn)`` lets the description fetcher reuse your own HTTP
client / circuit breaker (fn(url) -> html text; raise FetchRateLimited on 429).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

from .approval import ApprovalPoller, ApprovalService
from .classifier import JobClassifier
from .contacts import ContactService
from .delivery import Deliverer
from .destinations import load_registry
from .jd_fetch import DescriptionFetcher
from .llm import LLMClient
from .mailer import SmtpMailer
from .models import Job
from .pipeline import Pipeline
from .referral import ReferralService
from .resume import ResumeService
from .scheduler import FixedRateScheduler
from .settings import Settings
from .store import Store
from .telegram import TelegramClient
from .util import get_logger

log = get_logger("hook")
_lock = threading.Lock()
_state: dict = {"pipeline": None, "threads": [], "stop": threading.Event(), "http_get": None, "poller": None}


def enabled() -> bool:
    return Settings.from_env().enabled


def build(settings: Settings | None = None, *, env: dict | None = None, base_dir: str | Path | None = None, store=None,
          llm=None, tg=None, mailer=None, search=None, hunter=None, fetcher=None, registry=None) -> Pipeline:
    """Assemble the real components (anything can be injected, which is how the tests/dry-run work)."""
    s = settings or Settings.from_env(env)
    base = Path(base_dir) if base_dir else Path.cwd()
    store = store or Store(s.db_url, str(s.path(s.sqlite_path, base)))
    store.init_schema()
    llm = llm or LLMClient(s)
    if s.dry_run:
        from .fakes import RecordingMailer, RecordingTelegram

        tg, mailer = tg or RecordingTelegram(), mailer or RecordingMailer()
        log.warning("AI_DRY_RUN=1: Telegram messages and e-mails are recorded, NOT sent")
    tg = tg or TelegramClient()
    mailer = mailer or SmtpMailer(s)
    registry = registry or load_registry(s.path(s.destinations_file, base), env)
    fetcher = fetcher or DescriptionFetcher(_state["http_get"], min_chars=s.min_desc_chars)
    classifier = JobClassifier(s, store, llm)
    return Pipeline(s, store, classifier, fetcher, Deliverer(store, tg, registry, dry_run=s.dry_run),
                    ResumeService(s, store, llm, base_dir=base), ContactService(s, store, search, hunter, base_dir=base),
                    ReferralService(s, llm, base_dir=base), ApprovalService(s, store, tg, registry, mailer), base_dir=base)


def get_pipeline() -> Pipeline:
    with _lock:
        if _state["pipeline"] is None:
            _state["pipeline"] = build()
        return _state["pipeline"]


def set_http_get(fn: Callable[[str], str]) -> None:
    _state["http_get"] = fn
    if _state["pipeline"] is not None:
        _state["pipeline"].fetcher.set_http_get(fn)


def set_project_http_client() -> None:
    """Route description requests through Jobie's shared client and circuit breaker."""
    import requests

    from app import http_client

    from .jd_fetch import FetchError, FetchRateLimited

    def http_get(url: str) -> str:
        is_open, remaining = http_client.circuit_is_open()
        if is_open:
            raise FetchRateLimited(remaining)
        try:
            return http_client.fetch(url, {}, log=log)
        except RuntimeError as exc:
            cooldown = http_client.trip_circuit()
            raise FetchRateLimited(cooldown) from exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            raise FetchError(f"HTTP {status}") from exc

    set_http_get(http_get)


def on_new_job(job_id=None, title="", company="", location="", url="", posted_at=None, description=None,
               source="fresh_watcher") -> bool:
    """Hand a freshly discovered job to the AI pipeline. Non-blocking; returns False if already known/invalid."""
    try:
        job = Job.from_dict({"job_id": job_id, "title": title, "company": company, "location": location, "url": url,
                             "posted_at": posted_at, "description": description, "source": source})
    except ValueError as exc:
        log.error("on_new_job rejected: %s", exc)
        return False
    return get_pipeline().submit(job)


def start_background(*, run_poller: bool | None = None, tick_interval: float = 30.0) -> None:
    """Start the retry ticker (+ the approval poller in exactly one process). Idempotent."""
    with _lock:
        if _state["threads"]:
            return
    p = get_pipeline()
    p.recover()
    stop = _state["stop"]
    sched = FixedRateScheduler(p.tick, tick_interval, name="ai-retry")
    t1 = threading.Thread(target=sched.run_forever, args=(stop,), daemon=True, name="ai-retry")
    t1.start()
    _state["threads"].append(t1)
    want = p.s.run_approval_poller if run_poller is None else run_poller
    if want and p.approvals.control:
        from .agent import TelegramAgent

        from app.fresh_watcher import run_agent_search

        control_tg = TelegramClient() if p.s.dry_run else p.approvals.tg
        agent = TelegramAgent(p, control_tg, search=run_agent_search)
        poller = ApprovalPoller(p.approvals, p.store, control_tg, agent=agent)
        t2 = threading.Thread(target=poller.run_forever, args=(stop,), daemon=True, name="ai-approvals")
        t2.start()
        _state["threads"].append(t2)
    elif want:
        log.error("approval poller not started: no control bot configured")


def stop_background() -> None:
    _state["stop"].set()
