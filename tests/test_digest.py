import asyncio
import json
import unittest
from datetime import datetime, timedelta, time, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from tests.db_fakes import FakeConnection, connection_context, install_psycopg2_import_stub

install_psycopg2_import_stub()
import app.linkedin_fresh_jobs as worker
from app.config import settings
from app.messaging import TelegramNotifier


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True, "result": {"message_id": 123}}


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.connection = FakeConnection()
        self.connection_patch = patch.object(
            worker, "get_connection", return_value=self.connection
        )
        self.connection_patch.start()
        self.original_max = settings.MANUAL_DIGEST_MAX_PER_DAY
        self.original_cooldown = settings.MANUAL_DIGEST_COOLDOWN_SECONDS
        self.original_rolling = {
            name: getattr(settings, name) for name in (
                "TELEGRAM_DIGEST_BOT_TOKEN", "TELEGRAM_DIGEST_CHAT_ID",
                "TELEGRAM_DIGEST_INTERVAL_HOURS", "TELEGRAM_DIGEST_5M_BOT_TOKEN",
                "TELEGRAM_DIGEST_5M_CHAT_ID", "TELEGRAM_DIGEST_15M_BOT_TOKEN",
                "TELEGRAM_DIGEST_15M_CHAT_ID", "TELEGRAM_DIGEST_30M_BOT_TOKEN",
                "TELEGRAM_DIGEST_30M_CHAT_ID", "TELEGRAM_DIGEST_1H_BOT_TOKEN",
                "TELEGRAM_DIGEST_1H_CHAT_ID",
            )
        }
        settings.TELEGRAM_DIGEST_BOT_TOKEN = f"654321:{'b' * 32}"
        settings.TELEGRAM_DIGEST_CHAT_ID = "987654321"
        settings.TELEGRAM_DIGEST_INTERVAL_HOURS = 10
        for index, suffix in enumerate(("5M", "15M", "30M", "1H"), start=1):
            setattr(settings, f"TELEGRAM_DIGEST_{suffix}_BOT_TOKEN", f"{654321 + index}:{chr(96 + index) * 32}")
            setattr(settings, f"TELEGRAM_DIGEST_{suffix}_CHAT_ID", str(987654320 + index))
        worker.app.state.last_rolling_digest_checked_at = {}
        worker.new_jobs_buffer.clear()

    def tearDown(self):
        self.connection_patch.stop()
        settings.MANUAL_DIGEST_MAX_PER_DAY = self.original_max
        settings.MANUAL_DIGEST_COOLDOWN_SECONDS = self.original_cooldown
        for name, value in self.original_rolling.items():
            setattr(settings, name, value)

    def add_job(self, job_id, posted_at):
        self.connection.tables["seen_jobs"][job_id] = {
            "job_id": job_id, "title": f"Role {job_id}", "company": "Example Co",
            "first_seen": datetime.now(timezone.utc).timestamp(),
            "url": f"https://www.linkedin.com/jobs/view/{job_id}",
            "posted_at": posted_at.isoformat(),
        }

    def test_digest_includes_only_jobs_in_lookback_window(self):
        now = datetime.now(timezone.utc)
        self.add_job("recent", now - timedelta(hours=2))
        self.add_job("old", now - timedelta(hours=30))
        jobs = worker.get_recent_digest_jobs(now, 24 * 3600)
        self.assertEqual([job["job_id"] for job in jobs], ["recent"])

    def test_empty_or_invalid_digest_sends_no_message(self):
        notifier = TelegramNotifier()
        with patch("app.messaging.requests.post") as send:
            self.assertEqual(notifier.send_digest([]), [])
            self.assertEqual(notifier.send_digest([{"title": "Invalid", "company": "Co", "url": "http://example.com"}]), [])
        send.assert_not_called()

    def test_scheduled_digest_is_sent_once_per_local_date(self):
        self.add_job("recent", datetime.now(timezone.utc) - timedelta(hours=1))
        with patch("app.messaging.requests.post", return_value=FakeResponse()) as send:
            asyncio.run(worker.maybe_send_daily_digest(ZoneInfo("UTC"), time(0, 0)))
            asyncio.run(worker.maybe_send_daily_digest(ZoneInfo("UTC"), time(0, 0)))
        self.assertEqual(send.call_count, 1)

    def test_rolling_digests_are_checked_without_sending_when_empty(self):
        with patch("app.messaging.requests.post") as send:
            asyncio.run(worker.maybe_send_rolling_digests())
        send.assert_not_called()
        self.assertEqual(set(worker.app.state.last_rolling_digest_checked_at), {"5m", "15m", "30m", "1h", "10h"})

    def test_manual_digest_quota_and_cooldown_use_postgres_cursor(self):
        settings.MANUAL_DIGEST_MAX_PER_DAY = 2
        settings.MANUAL_DIGEST_COOLDOWN_SECONDS = 0
        local_date = datetime.now(ZoneInfo("UTC")).date().isoformat()
        worker.reserve_manual_digest_slot(local_date)
        worker.reserve_manual_digest_slot(local_date)
        with self.assertRaises(HTTPException) as error:
            worker.reserve_manual_digest_slot(local_date)
        self.assertEqual(error.exception.status_code, 429)

    def test_manual_digest_cooldown(self):
        settings.MANUAL_DIGEST_MAX_PER_DAY = 3
        settings.MANUAL_DIGEST_COOLDOWN_SECONDS = 300
        local_date = datetime.now(ZoneInfo("UTC")).date().isoformat()
        worker.reserve_manual_digest_slot(local_date)
        with self.assertRaises(HTTPException) as error:
            worker.reserve_manual_digest_slot(local_date)
        self.assertEqual(error.exception.status_code, 429)

    def test_linkedin_parser_accepts_regional_links_and_relative_post_times(self):
        html = '<li><a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/role-1234567890"></a><h3>Recent</h3><time>7 hours ago</time></li>'
        jobs = worker.parse_jobs(html)
        self.assertEqual(jobs[0]["job_id"], "1234567890")
        self.assertTrue(worker.posted_within_seconds(jobs[0]["posted_at"], 8 * 3600))

    def test_poll_delay_shortens_for_next_rolling_digest_deadline(self):
        now = datetime.now(timezone.utc)
        worker.app.state.last_rolling_digest_checked_at = {"5m": (now - timedelta(minutes=4)).isoformat()}
        self.assertEqual(worker.rolling_poll_delay_seconds(now), 60)

    def test_poll_classifies_seen_job_without_duplicate_insert(self):
        now = datetime.now(timezone.utc).isoformat()
        self.add_job("seen", datetime.now(timezone.utc))
        jobs = [{"job_id": "seen", "title": "Updated", "company": "Co",
                 "url": "https://www.linkedin.com/jobs/view/seen", "posted_at": now}]
        with patch.object(worker, "fetch_recent_jobs", return_value=jobs), \
             patch.object(worker.http_client, "circuit_is_open", return_value=(False, 0)), \
             patch.object(worker, "record_search_run"), \
             patch.object(worker, "maybe_send_daily_digest", return_value=None), \
             patch.object(worker, "maybe_send_rolling_digests", return_value=None), \
             patch.object(worker.logger, "info") as log_info, \
             patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(worker.poll_loop(ZoneInfo("UTC"), time(0, 0)))
        inserts = [query for query, _ in self.connection.fake_cursor.executed
                   if "INSERT INTO seen_jobs" in query]
        self.assertEqual(inserts, [])
        self.assertEqual(self.connection.tables["seen_jobs"]["seen"]["title"], "Updated")
        self.assertIn("already tracked", " ".join(str(call) for call in log_info.call_args_list))

    def test_poll_inserts_new_job(self):
        now = datetime.now(timezone.utc).isoformat()
        job = {"job_id": "new", "title": "New role", "company": "Co",
               "url": "https://www.linkedin.com/jobs/view/new", "posted_at": now}
        with patch.object(worker, "fetch_recent_jobs", return_value=[job]), \
             patch.object(worker.http_client, "circuit_is_open", return_value=(False, 0)), \
             patch.object(worker, "record_search_run"), \
             patch.object(worker, "maybe_send_daily_digest", return_value=None), \
             patch.object(worker, "maybe_send_rolling_digests", return_value=None), \
             patch.object(worker.notifier, "send_alert"), \
             patch.object(worker.logger, "info") as log_info, \
             patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(worker.poll_loop(ZoneInfo("UTC"), time(0, 0)))
        inserts = [query for query, _ in self.connection.fake_cursor.executed
                   if "INSERT INTO seen_jobs" in query]
        self.assertEqual(len(inserts), 1)
        self.assertIn("new", self.connection.tables["seen_jobs"])
        self.assertIn("new", " ".join(str(call) for call in log_info.call_args_list))

    def test_health_reports_success_and_stale_poll(self):
        worker.app.state.worker_started_at = "started"
        worker.app.state.last_poll_success_at = datetime.now(timezone.utc).isoformat()
        worker.app.state.last_poll_job_count = 4
        worker.app.state.last_poll_error = None
        worker.app.state.last_digest_sent_at = None
        healthy = asyncio.run(worker.worker_health())
        self.assertEqual(healthy.status_code, 200)
        self.assertEqual(json.loads(healthy.body)["last_poll_job_count"], 4)

    def test_digest_messages_are_chunked_and_links_are_included(self):
        jobs = [{"title": f"Engineer {i}", "company": "Example Co", "url": f"https://www.linkedin.com/jobs/view/{1000000000 + i}"} for i in range(40)]
        with patch("app.messaging.requests.post", return_value=FakeResponse()) as send:
            responses = TelegramNotifier().send_digest(jobs)
        self.assertGreater(len(responses), 1)
        self.assertIn(jobs[0]["url"], "\n".join(call.kwargs["json"]["text"] for call in send.call_args_list))

    def test_notifier_rejects_malformed_telegram_credentials(self):
        self.assertFalse(TelegramNotifier("not-a-bot-token", "123456789").is_configured)


if __name__ == "__main__":
    unittest.main()
