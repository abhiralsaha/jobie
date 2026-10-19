import asyncio
import logging
import random
import signal
import time

import httpx

from . import config as cfgmod
from . import health
from .linkedin import LinkedIn, Job, RateLimited
from .llm import LLM, Verdict
from .storage import Store
from .telegram import Telegram

log = logging.getLogger("agent")
MAX_LLM_ATTEMPTS = 3


class Agent:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(cfg.supabase_url, cfg.supabase_key)
        self.li = LinkedIn()
        self.llm = LLM(cfg.llm_provider, cfg.llm_api_key, cfg.llm_model, cfg.llm_base_url)
        self.tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
        self.attempts: dict[str, int] = {}
        self.sem = asyncio.Semaphore(3)

    async def close(self):
        await asyncio.gather(self.li.close(), self.llm.close(), self.tg.close(), self.store.close(), return_exceptions=True)

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
        log.info("Processing job %s | title=%s company=%s location=%s", job.id, job.title, job.company, loc)
        async with self.sem:
            try:
                if self.title_blocked(job.title):
                    mark_started = time.perf_counter()
                    await self.store.mark(job, "title_blocked")
                    log.info("Skip title-blocked job %s | title=%s duration=%.2fs", job.id, job.title, time.perf_counter() - mark_started)
                    return

                desc_started = time.perf_counter()
                job.description = await self.li.description(job.id)
                log.info("Fetched job description for %s in %.2fs | chars=%d", job.id, time.perf_counter() - desc_started, len(job.description))

                if len(job.description) < 80:
                    raise ValueError("description too short / not loaded")

                llm_started = time.perf_counter()
                v = await self.llm.analyse(job.title, job.description)
                log.info("LLM analysis for %s completed in %.2fs | min_years=%s max_years=%s fresher_ok=%s reason=%s",
                         job.id, time.perf_counter() - llm_started, v.min_years, v.max_years, v.fresher_ok, v.reason)

                if not self.passes(v):
                    reject_started = time.perf_counter()
                    await self.store.mark(job, "rejected", {"min_years": v.min_years, "max_years": v.max_years, "reason": v.reason})
                    log.info("Reject job %s | title=%s duration=%.2fs reason=%s", job.id, job.title, time.perf_counter() - reject_started, v.reason)
                    return

                send_started = time.perf_counter()
                if await self.tg.send_job(job, v, loc):
                    await self.store.mark(job, "sent", {"min_years": v.min_years, "max_years": v.max_years, "reason": v.reason})
                    health.STATE["alerts"] += 1
                    log.info("SENT %s @ %s | duration=%.2fs", job.title, job.company, time.perf_counter() - send_started)
                else:
                    log.error("Telegram send failed; will retry next cycle: %s | duration=%.2fs", job.id, time.perf_counter() - send_started)
            except RateLimited:
                log.warning("rate limited while processing %s; retry next cycle | total_duration=%.2fs", job.id, time.perf_counter() - started)
            except Exception as e:
                n = self.attempts.get(job.id, 0) + 1
                self.attempts[job.id] = n
                log.warning("error processing %s (attempt %d): %s | total_duration=%.2fs", job.id, n, e, time.perf_counter() - started)
                if n >= MAX_LLM_ATTEMPTS:
                    failed_started = time.perf_counter()
                    await self.store.mark(job, "failed")
                    log.info("Marked job %s as failed for this attempt; it remains eligible for retry | duration=%.2fs",
                             job.id, time.perf_counter() - failed_started)
        log.info("Finished processing %s | total_duration=%.2fs", job.id, time.perf_counter() - started)

    async def cycle(self):
        cycle_started = time.perf_counter()
        queries = [(k, l) for k in self.cfg.keywords for l in self.cfg.locations]
        tasks = []
        for kw, loc in queries:
            if self.li.in_cooldown():
                log.warning("LinkedIn cooldown active, skipping remaining searches this cycle")
                break
            try:
                search_started = time.perf_counter()
                log.info("Searching LinkedIn | keyword=%s location=%s", kw, loc)
                found = await self.li.search(kw, loc, self.cfg.lookback, self.cfg.max_pages)
                log.info("LinkedIn search complete | keyword=%s location=%s found=%d duration=%.2fs", kw, loc, len(found), time.perf_counter() - search_started)
            except RateLimited:
                log.warning("LinkedIn rate limited during search | keyword=%s location=%s", kw, loc)
                break
            except Exception as e:
                log.warning("search failed (%s / %s): %s", kw, loc, e)
                continue
            seen = await self.store.seen_ids([j.id for j in found])
            new = [j for j in found if j.id not in seen]
            log.info("%s | %s -> %d in window, %d new | seen_check_duration=%.2fs", kw, loc, len(found), len(new), time.perf_counter() - search_started)
            tasks += [self.process(j, loc) for j in new]
            await asyncio.sleep(random.uniform(1, 3))  # be polite between locations
        if tasks:
            gather_started = time.perf_counter()
            log.info("Starting parallel job processing for %d jobs", len(tasks))
            await asyncio.gather(*tasks)
            log.info("Parallel job processing done | jobs=%d duration=%.2fs", len(tasks), time.perf_counter() - gather_started)
        health.STATE["last_cycle"] = time.time()
        health.STATE["cycles"] += 1
        log.info("Cycle complete | total_duration=%.2fs | alerts=%d", time.perf_counter() - cycle_started, health.STATE["alerts"])

    async def run(self, stop: asyncio.Event):
        log.info("Agent started | keywords=%s locations=%s interval=%ss lookback=%ss",
                 self.cfg.keywords, self.cfg.locations, self.cfg.poll_interval, self.cfg.lookback)
        check_started = time.perf_counter()
        await self.store.check()
        log.info("Supabase connectivity check completed | duration=%.2fs", time.perf_counter() - check_started)
        await self.llm.verify()
        tg_started = time.perf_counter()
        await self.tg.send("✅ Job agent started. Watching: "
                           f"{', '.join(self.cfg.keywords)} in {', '.join(self.cfg.locations)}")
        log.info("Startup Telegram notification sent | duration=%.2fs", time.perf_counter() - tg_started)
        last_cleanup = 0.0
        while not stop.is_set():
            t0 = time.time()
            try:
                cycle_started = time.perf_counter()
                log.info("Starting poll cycle at wall_clock=%s", time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))
                await self.cycle()
                if t0 - last_cleanup > 3600:
                    cleanup_started = time.perf_counter()
                    log.info("Starting cleanup cycle | retention_days=30")
                    try:
                        await self.store.cleanup()
                    except httpx.HTTPStatusError as e:
                        log.warning("Supabase cleanup failed | status=%d response=%s duration=%.2fs",
                                    e.response.status_code, e.response.text[:500], time.perf_counter() - cleanup_started)
                    except httpx.HTTPError:
                        log.exception("Supabase cleanup request failed | duration=%.2fs", time.perf_counter() - cleanup_started)
                    else:
                        log.info("Supabase cleanup completed | duration=%.2fs", time.perf_counter() - cleanup_started)
                    finally:
                        last_cleanup = t0
                log.info("Poll cycle finished | duration=%.2fs", time.perf_counter() - cycle_started)
            except Exception:
                log.exception("cycle crashed (continuing)")
            sleep_for = max(1.0, self.cfg.poll_interval - (time.time() - t0))
            log.info("Sleeping until next cycle | sleep_seconds=%.2f", sleep_for)
            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass


async def amain():
    cfg = cfgmod.load()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            signal.signal(sig, lambda *_: stop.set())
    server = await health.start(cfg.port)
    agent = Agent(cfg)
    try:
        await agent.run(stop)
    finally:
        server.close()
        await agent.close()
        log.info("Shut down cleanly")


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
