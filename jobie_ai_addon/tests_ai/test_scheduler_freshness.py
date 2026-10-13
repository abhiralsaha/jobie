import random
import threading
import unittest
from datetime import datetime, timedelta, timezone

from app.ai_pipeline.freshness import (catchup_lookback_seconds, compute_lookback_seconds, format_relative, is_within_window, parse_posted_at)
from app.ai_pipeline.scheduler import FixedRateScheduler


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class SchedulerTests(unittest.TestCase):
    def test_runs_every_60_seconds_and_not_between(self):
        clk, count = Clock(), []
        s = FixedRateScheduler(lambda: count.append(1), 60, clock=clk)
        outcomes = []
        for t in (0, 30, 59, 60, 90, 120, 179, 180):
            clk.t = t
            outcomes.append(s.tick())
            s.wait_idle()
        self.assertEqual(outcomes, ["ran", "not_due", "not_due", "ran", "not_due", "ran", "not_due", "ran"])
        self.assertEqual(len(count), 4)

    def test_overlapping_runs_are_skipped_not_stacked(self):
        clk, gate, started = Clock(), threading.Event(), []

        def slow():
            started.append(1)
            gate.wait(5)

        s = FixedRateScheduler(slow, 60, clock=clk)
        self.assertEqual(s.tick(), "ran")
        clk.t = 60
        self.assertEqual(s.tick(), "skipped_overlap")
        clk.t = 120
        self.assertEqual(s.tick(), "skipped_overlap")
        self.assertEqual(len(started), 1)                       # never two at once
        gate.set()
        s.wait_idle()
        clk.t = 180
        self.assertEqual(s.tick(), "ran")
        s.wait_idle()
        self.assertEqual(s.skipped_overlap, 2)

    def test_failures_do_not_stop_the_schedule(self):
        clk, n = Clock(), [0]

        def flaky():
            n[0] += 1
            raise TimeoutError("api timeout")

        s = FixedRateScheduler(flaky, 60, clock=clk)
        for t in (0, 60, 120):
            clk.t = t
            self.assertEqual(s.tick(), "ran")
            s.wait_idle()
        self.assertEqual((n[0], s.failures, s.runs), (3, 3, 0))
        self.assertIn("TimeoutError", s.last_error)

    def test_no_burst_after_a_stall(self):
        clk, n = Clock(), [0]
        s = FixedRateScheduler(lambda: n.__setitem__(0, n[0] + 1), 60, clock=clk)
        clk.t = 0
        s.tick(); s.wait_idle()
        clk.t = 1000                                            # process was frozen ~16 minutes
        self.assertEqual(s.tick(), "ran"); s.wait_idle()
        clk.t = 1001
        self.assertEqual(s.tick(), "not_due")
        self.assertEqual(n[0], 2)

    def test_jitter_stays_within_bounds(self):
        clk = Clock()
        s = FixedRateScheduler(lambda: None, 60, clock=clk, jitter=15, rng=random.Random(3).random)
        for _ in range(200):
            before = s._next_due
            clk.t = before
            s.tick(); s.wait_idle()
            self.assertTrue(60 - 15 - 1e-9 <= s._next_due - before <= 60 + 15 + 1e-9)


class FreshnessTests(unittest.TestCase):
    NOW = datetime(2026, 10, 13, 12, 0, 0, tzinfo=timezone.utc)

    def test_relative_and_iso_parsing(self):
        p = lambda v, **k: parse_posted_at(v, self.NOW, **k)  # noqa: E731
        self.assertEqual(p("2 minutes ago"), self.NOW - timedelta(minutes=2))
        self.assertEqual(p("Just now"), self.NOW)
        self.assertEqual(p("1 hour ago"), self.NOW - timedelta(hours=1))
        self.assertEqual(p("2026-10-13T12:00:00Z"), self.NOW)
        self.assertIsNone(p("recently"))
        self.assertIsNone(p(None))

    def test_timezone_handling(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        naive = parse_posted_at("2026-10-13T17:30:00", self.NOW, default_tz=ist)   # 17:30 IST == 12:00 UTC
        self.assertEqual(naive, self.NOW)
        aware = parse_posted_at("2026-10-13T17:30:00+05:30", self.NOW)
        self.assertEqual(aware, self.NOW)
        self.assertEqual(aware.utcoffset(), timedelta(0))

    def test_window_membership_and_label(self):
        self.assertTrue(is_within_window(self.NOW - timedelta(seconds=100), self.NOW, 120))
        self.assertFalse(is_within_window(self.NOW - timedelta(seconds=300), self.NOW, 120))
        self.assertFalse(is_within_window(None, self.NOW, 120))
        self.assertEqual(format_relative(None, self.NOW), "not verified")

    def test_overlapping_window_catches_jobs_across_poll_boundaries(self):
        rng = random.Random(7)
        polls, t = [], 0.0
        while t < 6 * 3600:                                       # 6h of polls at 60s +/- 15s jitter
            polls.append(t)
            t += 60 + rng.uniform(-15, 15)

        def missed(window):
            lost = 0
            for _ in range(3000):
                p = rng.uniform(100, 6 * 3600 - 200)
                if not any(pt - window <= p <= pt for pt in polls):
                    lost += 1
            return lost

        good = compute_lookback_seconds(60, 15, 0, 15)
        self.assertGreaterEqual(good, 90)
        self.assertEqual(missed(good), 0)
        self.assertGreater(missed(50), 0)                         # a too-short window DOES lose jobs

    def test_catchup_window_recovers_jobs_posted_during_a_429_cooldown(self):
        now, base = self.NOW, 120
        # healthy: last success 60s ago -> normal window
        self.assertEqual(catchup_lookback_seconds(now, now - timedelta(seconds=60), base), 120)
        # 30-minute circuit-breaker pause: window must reach back past the pause
        w = catchup_lookback_seconds(now, now - timedelta(minutes=30), base)
        self.assertGreaterEqual(w, 30 * 60)
        posted_during_pause = now - timedelta(minutes=20)
        self.assertTrue(is_within_window(posted_during_pause, now, w))
        self.assertFalse(is_within_window(posted_during_pause, now, base))      # the old fixed window loses it
        self.assertEqual(catchup_lookback_seconds(now, now - timedelta(days=3), base, cap=86400), 86400)
        self.assertGreaterEqual(catchup_lookback_seconds(now, None, base), 3600)


if __name__ == "__main__":
    unittest.main()
