import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import fresh_watcher


class FreshWatcherCatchupTests(unittest.TestCase):
    def test_lookback_reaches_jobs_posted_during_long_circuit_pause(self):
        now = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
        last_success = now - timedelta(minutes=30)

        lookback = fresh_watcher.poll_lookback_seconds(now, last_success)

        self.assertGreaterEqual(lookback, 30 * 60)

    def test_fetch_uses_catchup_window_in_linkedin_query(self):
        with patch.object(fresh_watcher.http_client, "fetch", return_value="html") as fetch:
            self.assertEqual(fresh_watcher.fetch_page(1830), "html")

        self.assertEqual(fetch.call_args.args[1]["f_TPR"], "r1830")


if __name__ == "__main__":
    unittest.main()
