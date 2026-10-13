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

    def test_telegram_search_uses_existing_dedup_and_ai_pipeline(self):
        job = {
            "job_id": "123456",
            "title": "Software Engineer",
            "company": "Example",
            "location": "Remote",
            "url": "https://www.linkedin.com/jobs/view/123456",
            "posted_at": "2026-10-13T12:00:00+00:00",
        }
        with (
            patch.object(fresh_watcher, "fetch_page", return_value="html"),
            patch.object(fresh_watcher, "parse_jobs", return_value=[job]),
            patch.object(fresh_watcher, "is_new_job", return_value=True),
            patch.object(fresh_watcher, "_build_notifier") as build_notifier,
            patch.object(fresh_watcher.ai_hook, "enabled", return_value=True),
            patch.object(fresh_watcher.ai_hook, "on_new_job") as submit,
        ):
            result = fresh_watcher.run_agent_search()

        self.assertEqual(result, {"matched": 1, "new": 1})
        build_notifier.assert_called_once()
        submit.assert_called_once()
        self.assertEqual(submit.call_args.kwargs["source"], "telegram_agent_search")


if __name__ == "__main__":
    unittest.main()
