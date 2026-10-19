import asyncio
import logging
import os
import random
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from . import config as cfgmod
from . import health
from .keepalive import KeepAlive
from .linkedin import LinkedIn, Job, RateLimited
from .llm import LLM, Verdict
from .storage import Store, SupabaseStore
from .telegram import Telegram

log = logging.getLogger("agent")
MAX_LLM_ATTEMPTS = 3


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        if cfg.supabase_url and cfg.supabase_key:
            self.store = SupabaseStore(cfg.supabase_url, cfg.supabase_key)
            storage_backend = "Supabase"
        else:
            self.store = Store(cfg.db_path)
            storage_backend = f"SQLite ({cfg.db_path})"
        self.li = LinkedIn()
        self.llm = LLM(cfg.llm_provider, cfg.llm_api_key, cfg.llm_model, cfg.llm_base_url)
        self.tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        self.attempts: dict[str, int] = {}
        self.sem = asyncio.Semaphore(3)
        log.info("Ready to search. Database: %s. AI provider: %s.", storage_backend, cfg.llm_provider)

    async def close(self):
        started = time.perf_counter()
        names = ("LinkedIn", "LLM", "Telegram")
        results = await asyncio.gather(
            self.li.close(), self.llm.close(), self.tg.close(), return_exceptions=True
        )
        for name, result in zip(names, results):
            if isinstance(result, BaseException):
                log.error("%s client close failed: %s", name, result,
                          exc_info=(type(result), result, result.__traceback__))
        log.info("Network client shutdown finished | clients=%d duration=%.2fs",
                 len(names), time.perf_counter() - started)

    def title_blocked(self, title: str) -> bool:
        t = title.lower()
        return any(w in t for w in self.cfg.title_blocklist)

    def passes(self, v: Verdict) -> bool:
        if v.min_years is not None:
            return v.min_years <= self.cfg.max_min_years
        if v.fresher_ok:
            return True
        return self.cfg.unspecified == "send"

    async def process(self, job: Job, loc: str):
        started = time.perf_counter()
        async with self.sem:
            try:
                log.info("Checking job: %s at %s (%s).", job.title, job.company, loc)
                if self.title_blocked(job.title):
                    self.store.mark(job.id, "title_blocked")
                    log.info("Skipped blocked title: %s (%.1fs).",
                             job.title, time.perf_counter() - started)
                    return
                step_started = time.perf_counter()
                job.description = await self.li.description(job.id)
                log.info("Read job details in %.1fs.", time.perf_counter() - step_started)
                if len(job.description) < 80:
                    raise ValueError("description too short / not loaded")
                step_started = time.perf_counter()
                v = await self.llm.analyse(job.title, job.description)
                log.info("Checked experience requirement in %.1fs.", time.perf_counter() - step_started)
                if not self.passes(v):
                    self.store.mark(job.id, "rejected")
                    log.info("Skipped job (experience does not match): %s. Total: %.1fs.",
                             job.title, time.perf_counter() - started)
                    return
                step_started = time.perf_counter()
                if await self.tg.send_job(job, v, loc):
                    self.store.mark(job.id, "sent")
                    health.STATE["alerts"] += 1
                    log.info("Sent job alert: %s at %s (%.1fs total).",
                             job.title, job.company, time.perf_counter() - started)
                else:
                    log.error("Could not send Telegram alert for %s. Will retry next search.", job.title)
            except RateLimited:
                log.warning("Request was rate-limited while checking %s. Will retry next search.", job.title)
            except Exception as e:
                n = self.attempts.get(job.id, 0) + 1
                self.attempts[job.id] = n
                log.exception("Job processing failed | id=%s attempt=%d/%d duration=%.2fs error=%s",
                              job.id, n, MAX_LLM_ATTEMPTS, time.perf_counter() - started, e)
                if n >= MAX_LLM_ATTEMPTS:
                    self.store.mark(job.id, "failed")
                    log.error("Job marked failed after maximum attempts | id=%s", job.id)

    async def cycle(self):
        cycle_started = time.perf_counter()
        queries = [(k, l) for k in self.cfg.keywords for l in self.cfg.locations]
        tasks = []
        total_found = 0
        total_new = 0
        completed_queries = 0
        log.info("Starting a search for %d keyword/location combinations.", len(queries))
        for kw, loc in queries:
            if self.li.in_cooldown():
                log.warning("LinkedIn asked us to wait. Skipping the remaining searches this round.")
                break
            search_started = time.perf_counter()
            log.info("Searching for %r in %s...", kw, loc)
            try:
                found = await self.li.search(kw, loc, self.cfg.lookback, self.cfg.max_pages)
            except RateLimited:
                log.warning("Search rate-limited for %s. Search took %.1fs.",
                            loc, time.perf_counter() - search_started)
                break
            except Exception as e:
                log.exception("Search failed for %r in %s after %.1fs: %s",
                              kw, loc, time.perf_counter() - search_started, e)
                continue
            new = [j for j in found if not self.store.seen(j.id)]
            completed_queries += 1
            total_found += len(found)
            total_new += len(new)
            log.info("Found %d jobs (%d new) in %s. Search took %.1fs.",
                     len(found), len(new), loc, time.perf_counter() - search_started)
            tasks += [self.process(j, loc) for j in new]
            await asyncio.sleep(random.uniform(1, 3))  # be polite between locations
        if tasks:
            processing_started = time.perf_counter()
            log.info("Checking %d new jobs...", len(tasks))
            await asyncio.gather(*tasks)
            log.info("Finished checking new jobs in %.1fs.", time.perf_counter() - processing_started)
        health.STATE["last_cycle"] = time.time()
        health.STATE["cycles"] += 1
        log.info(
            "Search round %d complete: checked %d/%d searches, found %d jobs (%d new), sent %d alerts. Took %.1fs.",
            health.STATE["cycles"], completed_queries, len(queries), total_found, total_new,
            health.STATE["alerts"], time.perf_counter() - cycle_started,
        )

    async def run(self, stop: asyncio.Event):
        log.info("Job agent started. Searching %s in %s every %d seconds.",
                 ", ".join(self.cfg.keywords), ", ".join(self.cfg.locations), self.cfg.poll_interval)
        log.info("Checking Telegram connection...")
        telegram_started = time.perf_counter()
        if not await self.tg.send(
            "✅ Job agent started. Watching: "
            f"{', '.join(self.cfg.keywords)} in {', '.join(self.cfg.locations)}"
        ):
            raise RuntimeError(
                "Telegram startup message could not be delivered. Check TELEGRAM_BOT_TOKEN "
                "and TELEGRAM_CHAT_ID; the bot must have received a message from a private chat "
                "before it can send messages there."
            )
        log.info("Telegram is connected (%.1fs).", time.perf_counter() - telegram_started)
        last_cleanup = 0.0
        while not stop.is_set():
            t0 = time.perf_counter()
            wall_started = time.time()
            try:
                await self.cycle()
                if wall_started - last_cleanup > 3600:
                    cleanup_started = time.perf_counter()
                    log.debug("Removing old job records...")
                    self.store.cleanup()
                    last_cleanup = wall_started
                    log.debug("Database cleanup finished in %.1fs.", time.perf_counter() - cleanup_started)
            except Exception:
                log.exception("Search cycle crashed; continuing | elapsed=%.2fs", time.perf_counter() - t0)
            elapsed = time.perf_counter() - t0
            sleep_for = max(1.0, self.cfg.poll_interval - elapsed)
            log.info("Next search starts in %.1f seconds (this round took %.1fs).", sleep_for, elapsed)
            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass
        log.info("Shutdown requested. Stopping the agent.")


def configure_logging(cfg):
    log_path = os.path.abspath(cfg.log_file)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=cfg.log_level, handlers=[console, file_handler], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("postgrest").setLevel(logging.WARNING)
    logging.getLogger("supabase").setLevel(logging.WARNING)
    logging.getLogger("agent").info("Logging configured | level=%s file=%s", cfg.log_level, log_path)


async def amain():
    cfg = cfgmod.load()
    configure_logging(cfg)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: stop.set())
    server = await health.start(cfg.port)
    agent = Agent(cfg)
    keepalive = KeepAlive(cfg.keepalive_url, cfg.keepalive_interval)
    keepalive_task = asyncio.create_task(keepalive.run(stop), name="keepalive")
    try:
        await agent.run(stop)
    finally:
        stop.set()
        try:
            await keepalive_task
        finally:
            server.close()
            await agent.close()
            log.info("Shut down cleanly")


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
