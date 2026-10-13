"""Fixed-rate, non-overlapping scheduler.

* The callable runs in its own thread, so a slow run never blocks the loop.
* If the previous run is still going when the next beat arrives, that beat is
  skipped (never two overlapping runs).
* Missed beats are skipped, not replayed in a burst after a stall.
* Exceptions are logged and counted; the schedule keeps going.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Callable

from .util import get_logger

log = get_logger("scheduler")


class FixedRateScheduler:
    def __init__(self, fn: Callable[[], None], interval: float = 60.0, *,
                 clock: Callable[[], float] = time.monotonic, jitter: float = 0.0,
                 rng: Callable[[], float] = random.random, name: str = "job"):
        self.fn, self.interval, self.jitter, self.name = fn, float(interval), float(jitter), name
        self._clock, self._rng = clock, rng
        self._next_due = clock()
        self._running = threading.Lock()
        self._thread: threading.Thread | None = None
        self.runs = self.skipped_overlap = self.failures = 0
        self.last_error: str | None = None

    def _schedule_next(self, now: float) -> None:
        nxt = self._next_due + self.interval
        if nxt <= now:                       # stalled: skip missed beats, no burst
            nxt = now + self.interval
        if self.jitter:
            nxt += (self._rng() * 2 - 1) * self.jitter
        self._next_due = nxt

    def tick(self) -> str:
        """Call frequently. Returns 'not_due' | 'ran' | 'skipped_overlap'."""
        now = self._clock()
        if now < self._next_due:
            return "not_due"
        self._schedule_next(now)
        if not self._running.acquire(blocking=False):
            self.skipped_overlap += 1
            log.warning("%s: previous run still active, skipping this beat", self.name)
            return "skipped_overlap"
        self._thread = threading.Thread(target=self._run, name=f"sched-{self.name}", daemon=True)
        self._thread.start()
        return "ran"

    def _run(self) -> None:
        try:
            self.fn()
            self.runs += 1
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("%s: run failed", self.name)
        finally:
            self._running.release()

    def wait_idle(self, timeout: float = 5.0) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)

    def run_forever(self, stop: threading.Event, poll: float = 0.5) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(poll)
