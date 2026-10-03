import asyncio
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException
import requests

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
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_db_path = worker.DB_PATH
        self.original_max_manual = settings.MANUAL_DIGEST_MAX_PER_DAY
        self.original_cooldown = settings.MANUAL_DIGEST_COOLDOWN_SECONDS
        self.original_telegram_bot_token = settings.TELEGRAM_BOT_TOKEN
        self.original_telegram_chat_id = settings.TELEGRAM_CHAT_ID
        self.original_rolling_settings = {
            name: getattr(settings, name)
            for name in (
                "TELEGRAM_DIGEST_BOT_TOKEN",
                "TELEGRAM_DIGEST_CHAT_ID",
                "TELEGRAM_DIGEST_INTERVAL_HOURS",
                "TELEGRAM_DIGEST_5M_BOT_TOKEN",
                "TELEGRAM_DIGEST_5M_CHAT_ID",
                "TELEGRAM_DIGEST_15M_BOT_TOKEN",
                "TELEGRAM_DIGEST_15M_CHAT_ID",
                "TELEGRAM_DIGEST_30M_BOT_TOKEN",
                "TELEGRAM_DIGEST_30M_CHAT_ID",
                "TELEGRAM_DIGEST_1H_BOT_TOKEN",
                "TELEGRAM_DIGEST_1H_CHAT_ID",
            )
        }
        self.original_rolling_digest_time = getattr(
            worker.app.state, "last_rolling_digest_checked_at", None
        )
        settings.TELEGRAM_BOT_TOKEN = f"123456:{'a' * 32}"
        settings.TELEGRAM_CHAT_ID = "123456789"
        settings.TELEGRAM_DIGEST_BOT_TOKEN = f"654321:{'b' * 32}"
        settings.TELEGRAM_DIGEST_CHAT_ID = "987654321"
        settings.TELEGRAM_DIGEST_INTERVAL_HOURS = 10
        for index, suffix in enumerate(("5M", "15M", "30M", "1H"), start=1):
            setattr(
                settings,
                f"TELEGRAM_DIGEST_{suffix}_BOT_TOKEN",
                f"{654321 + index}:{chr(96 + index) * 32}",
            )
            setattr(
                settings,
                f"TELEGRAM_DIGEST_{suffix}_CHAT_ID",
                str(987654320 + index),
            )
        worker.app.state.last_rolling_digest_checked_at = {}
        worker.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        worker.new_jobs_buffer.clear()
        self.connection = worker.init_db()

    def tearDown(self):
        self.connection.close()
        worker.DB_PATH = self.original_db_path
        settings.MANUAL_DIGEST_MAX_PER_DAY = self.original_max_manual
        settings.MANUAL_DIGEST_COOLDOWN_SECONDS = self.original_cooldown
        settings.TELEGRAM_BOT_TOKEN = self.original_telegram_bot_token
        settings.TELEGRAM_CHAT_ID = self.original_telegram_chat_id
        for name, value in self.original_rolling_settings.items():
            setattr(settings, name, value)
        worker.app.state.last_rolling_digest_checked_at = self.original_rolling_digest_time
        self.temp_dir.cleanup()

    def add_job(self, job_id, posted_at):
        self.connection.execute(
            "INSERT INTO seen_jobs (job_id, title, company, first_seen, url, posted_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id,
                f"Role {job_id}",
                "Example Co",
                datetime.now(timezone.utc).timestamp(),
                f"https://www.linkedin.com/jobs/view/{job_id}",
                posted_at.isoformat(),
            ),
        )
        self.connection.commit()

    def test_digest_includes_only_jobs_in_lookback_window(self):
        now = datetime.now(timezone.utc)
        self.add_job("recent", now - timedelta(hours=2))
        self.add_job("old", now - timedelta(hours=30))

        jobs = worker.get_recent_digest_jobs(self.connection, now)

        self.assertEqual([job["job_id"] for job in jobs], ["recent"])

    def test_empty_or_invalid_digest_sends_no_message(self):
        notifier = TelegramNotifier()
        invalid_job = {
            "title": "Invalid link",
            "company": "Example Co",
            "url": "http://example.com/job",
        }

        with patch("app.messaging.requests.post") as send:
            self.assertEqual(notifier.send_digest([]), [])
            self.assertEqual(notifier.send_digest([invalid_job]), [])

        send.assert_not_called()

    def test_empty_daily_digest_is_not_sent_or_marked_complete(self):
        with patch("app.messaging.requests.post", return_value=FakeResponse()) as send:
            async def send_empty_then_populated():
                await worker.maybe_send_daily_digest(
                    self.connection, ZoneInfo("UTC"), time(0, 0)
                )
                await worker.maybe_send_daily_digest(
                    self.connection, ZoneInfo("UTC"), time(0, 0)
                )
                self.add_job("added-later", datetime.now(timezone.utc))
                await worker.maybe_send_daily_digest(
                    self.connection, ZoneInfo("UTC"), time(0, 0)
                )

            asyncio.run(send_empty_then_populated())

        self.assertEqual(send.call_count, 1)
        self.assertIn("Role added-later", send.call_args.kwargs["json"]["text"])

    def test_empty_rolling_digests_are_checked_without_sending(self):
        with patch("app.messaging.requests.post") as send:
            asyncio.run(worker.maybe_send_rolling_digests(self.connection))
            asyncio.run(worker.maybe_send_rolling_digests(self.connection))

        send.assert_not_called()
        self.assertEqual(
            set(worker.app.state.last_rolling_digest_checked_at),
            {"5m", "15m", "30m", "1h", "10h"},
        )

    def test_linkedin_parser_accepts_regional_links_and_relative_post_times(self):
        html = """
        <li>
            <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/recent-role-1234567890"></a>
            <h3 class="base-search-card__title">Recent role</h3>
            <h4 class="base-search-card__subtitle">Example Co</h4>
            <time datetime="2026-10-02">7 hours ago</time>
        </li>
        <li>
            <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/older-role-1234567891"></a>
            <h3 class="base-search-card__title">Older role</h3>
            <h4 class="base-search-card__subtitle">Example Co</h4>
            <time datetime="2026-10-02">9 hours ago</time>
        </li>
        """

        jobs = worker.parse_jobs(html)
        recent_job = next(job for job in jobs if job["job_id"] == "1234567890")
        older_job = next(job for job in jobs if job["job_id"] == "1234567891")

        self.assertTrue(worker.posted_within_seconds(recent_job["posted_at"], 8 * 3600))
        self.assertFalse(worker.posted_within_seconds(older_job["posted_at"], 8 * 3600))

    def test_legacy_database_migrates_digest_columns(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        legacy = sqlite3.connect(legacy_path)
        legacy.execute(
            "CREATE TABLE seen_jobs (job_id TEXT PRIMARY KEY, title TEXT, company TEXT, first_seen REAL)"
        )
        legacy.commit()
        legacy.close()

        worker.DB_PATH = str(legacy_path)
        migrated = worker.init_db()
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(seen_jobs)")}

        self.assertTrue({"url", "posted_at"}.issubset(columns))
        self.assertIsNotNone(
            migrated.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='digest_runs'"
            ).fetchone()
        )
        migrated.close()
        worker.DB_PATH = str(Path(self.temp_dir.name) / "test.db")

    def test_scheduled_digest_is_sent_once_per_local_date(self):
        now = datetime.now(timezone.utc)
        self.add_job("recent", now - timedelta(hours=1))
        with patch("app.messaging.requests.post", return_value=FakeResponse()) as send:
            async def send_twice():
                await worker.maybe_send_daily_digest(
                    self.connection, ZoneInfo("UTC"), time(0, 0)
                )
                await worker.maybe_send_daily_digest(
                    self.connection, ZoneInfo("UTC"), time(0, 0)
                )

            asyncio.run(send_twice())

        self.assertEqual(send.call_count, 1)
        self.assertIn("Role recent", send.call_args.kwargs["json"]["text"])

    def test_rolling_digests_filter_each_window_and_respect_each_interval(self):
        now = datetime.now(timezone.utc)
        self.add_job("five-minute", now - timedelta(minutes=3))
        self.add_job("fifteen-minute", now - timedelta(minutes=10))
        self.add_job("thirty-minute", now - timedelta(minutes=25))
        self.add_job("one-hour", now - timedelta(minutes=45))
        self.add_job("ten-hour", now - timedelta(hours=9))
        self.add_job("outside-all", now - timedelta(hours=11))

        with patch("app.messaging.requests.post", return_value=FakeResponse()) as send:
            asyncio.run(worker.maybe_send_rolling_digests(self.connection))
            asyncio.run(worker.maybe_send_rolling_digests(self.connection))

            self.assertEqual(send.call_count, 5)
            payloads = [call.kwargs["json"] for call in send.call_args_list]
            expected_chats = [
                settings.TELEGRAM_DIGEST_5M_CHAT_ID,
                settings.TELEGRAM_DIGEST_15M_CHAT_ID,
                settings.TELEGRAM_DIGEST_30M_CHAT_ID,
                settings.TELEGRAM_DIGEST_1H_CHAT_ID,
                settings.TELEGRAM_DIGEST_CHAT_ID,
            ]
            expected_messages = [
                ("5 minutes", "Role five-minute", "Role fifteen-minute"),
                ("15 minutes", "Role fifteen-minute", "Role thirty-minute"),
                ("30 minutes", "Role thirty-minute", "Role one-hour"),
                ("1 hour", "Role one-hour", "Role ten-hour"),
                ("10 hours", "Role ten-hour", "Role outside-all"),
            ]
            for payload, chat_id, expected in zip(payloads, expected_chats, expected_messages):
                label, included_title, excluded_title = expected
                self.assertEqual(payload["chat_id"], chat_id)
                self.assertIn(f"last {label}", payload["text"])
                self.assertIn(included_title, payload["text"])
                self.assertNotIn(excluded_title, payload["text"])

            sent_times = worker.app.state.last_rolling_digest_checked_at
            for name, _, _, interval_seconds, _ in worker.rolling_digest_specs():
                sent_times[name] = (
                    datetime.now(timezone.utc) - timedelta(seconds=interval_seconds + 1)
                ).isoformat()
            asyncio.run(worker.maybe_send_rolling_digests(self.connection))

        self.assertEqual(send.call_count, 10)

    def test_poll_delay_shortens_for_next_rolling_digest_deadline(self):
        now = datetime.now(timezone.utc)
        worker.app.state.last_rolling_digest_checked_at = {
            "5m": (now - timedelta(minutes=4)).isoformat(),
            "15m": (now - timedelta(minutes=1)).isoformat(),
            "30m": (now - timedelta(minutes=1)).isoformat(),
            "1h": (now - timedelta(minutes=1)).isoformat(),
            "10h": (now - timedelta(minutes=1)).isoformat(),
        }

        self.assertEqual(worker.rolling_poll_delay_seconds(now), 60)

    def test_incomplete_or_failed_rolling_bot_does_not_block_other_bots(self):
        settings.TELEGRAM_DIGEST_15M_BOT_TOKEN = ""
        self.add_job("recent-for-available-bots", datetime.now(timezone.utc) - timedelta(minutes=2))

        def send_for_chat(url, *, json, timeout):
            if json["chat_id"] == settings.TELEGRAM_DIGEST_30M_CHAT_ID:
                raise requests.ConnectionError("simulated bot outage")
            return FakeResponse()

        with patch("app.messaging.requests.post", side_effect=send_for_chat) as send:
            asyncio.run(worker.maybe_send_rolling_digests(self.connection))

        self.assertEqual(send.call_count, 4)
        sent_times = worker.app.state.last_rolling_digest_checked_at
        self.assertIn("5m", sent_times)
        self.assertNotIn("15m", sent_times)
        self.assertNotIn("30m", sent_times)
        self.assertIn("1h", sent_times)
        self.assertIn("10h", sent_times)

    def test_manual_digest_quota_persists_in_sqlite(self):
        settings.MANUAL_DIGEST_MAX_PER_DAY = 2
        settings.MANUAL_DIGEST_COOLDOWN_SECONDS = 0
        local_date = datetime.now(ZoneInfo("UTC")).date().isoformat()

        worker.reserve_manual_digest_slot(self.connection, local_date)
        worker.reserve_manual_digest_slot(self.connection, local_date)
        with self.assertRaises(HTTPException) as error:
            worker.reserve_manual_digest_slot(self.connection, local_date)

        self.assertEqual(error.exception.status_code, 429)

    def test_manual_digest_cooldown(self):
        settings.MANUAL_DIGEST_MAX_PER_DAY = 3
        settings.MANUAL_DIGEST_COOLDOWN_SECONDS = 300
        local_date = datetime.now(ZoneInfo("UTC")).date().isoformat()

        worker.reserve_manual_digest_slot(self.connection, local_date)
        with self.assertRaises(HTTPException) as error:
            worker.reserve_manual_digest_slot(self.connection, local_date)

        self.assertEqual(error.exception.status_code, 429)

    def test_manual_digest_endpoint_requires_token_and_enforces_quota(self):
        original_token = settings.JOBIE_API_TOKEN
        original_max = settings.MANUAL_DIGEST_MAX_PER_DAY
        original_cooldown = settings.MANUAL_DIGEST_COOLDOWN_SECONDS
        original_delivery = worker.deliver_digest
        original_db = getattr(worker.app.state, "db_conn", None)
        original_timezone = getattr(worker.app.state, "digest_timezone", None)
        settings.JOBIE_API_TOKEN = "x" * 40
        settings.MANUAL_DIGEST_MAX_PER_DAY = 1
        settings.MANUAL_DIGEST_COOLDOWN_SECONDS = 0
        worker.app.state.db_conn = self.connection
        worker.app.state.digest_timezone = ZoneInfo("UTC")

        async def fake_delivery(conn):
            return ([{"job_id": "test"}], ["fake-sid"])

        worker.deliver_digest = fake_delivery
        try:
            with self.assertRaises(HTTPException) as unauthorized:
                asyncio.run(worker.send_digest_now(authorization=None))
            self.assertEqual(unauthorized.exception.status_code, 401)

            result = asyncio.run(
                worker.send_digest_now(authorization=f"Bearer {settings.JOBIE_API_TOKEN}")
            )
            self.assertEqual(result["job_count"], 1)
            with self.assertRaises(HTTPException) as limited:
                asyncio.run(
                    worker.send_digest_now(
                        authorization=f"Bearer {settings.JOBIE_API_TOKEN}"
                    )
                )
            self.assertEqual(limited.exception.status_code, 429)
        finally:
            settings.JOBIE_API_TOKEN = original_token
            settings.MANUAL_DIGEST_MAX_PER_DAY = original_max
            settings.MANUAL_DIGEST_COOLDOWN_SECONDS = original_cooldown
            worker.deliver_digest = original_delivery
            worker.app.state.db_conn = original_db
            worker.app.state.digest_timezone = original_timezone

    def test_health_reports_success_and_stale_poll(self):
        original_state = {
            name: getattr(worker.app.state, name, None)
            for name in (
                "db_conn",
                "worker_started_at",
                "last_poll_success_at",
                "last_poll_job_count",
                "last_poll_error",
                "last_digest_sent_at",
            )
        }
        worker.app.state.db_conn = self.connection
        worker.app.state.worker_started_at = "started"
        worker.app.state.last_poll_success_at = datetime.now(timezone.utc).isoformat()
        worker.app.state.last_poll_job_count = 4
        worker.app.state.last_poll_error = None
        worker.app.state.last_digest_sent_at = None
        try:
            healthy = asyncio.run(worker.worker_health())
            self.assertEqual(healthy.status_code, 200)
            self.assertEqual(json.loads(healthy.body)["last_poll_job_count"], 4)

            worker.app.state.last_poll_success_at = (
                datetime.now(timezone.utc) - timedelta(hours=1)
            ).isoformat()
            degraded = asyncio.run(worker.worker_health())
            self.assertEqual(degraded.status_code, 503)
        finally:
            for name, value in original_state.items():
                setattr(worker.app.state, name, value)

    def test_digest_messages_are_chunked_and_links_are_included(self):
        notifier = TelegramNotifier()
        jobs = [
            {
                "title": f"Engineer {index}",
                "company": "Example Co",
                "url": f"https://www.linkedin.com/jobs/view/{1000000000 + index}",
            }
            for index in range(40)
        ]

        with patch("app.messaging.requests.post", return_value=FakeResponse()) as send:
            responses = notifier.send_digest(jobs)

        self.assertGreater(len(responses), 1)
        bodies = [call.kwargs["json"]["text"] for call in send.call_args_list]
        self.assertTrue(all(len(body) <= 1400 for body in bodies))
        self.assertIn(jobs[0]["url"], "\n".join(bodies))
        self.assertTrue(
            all(call.args[0].endswith("/sendMessage") for call in send.call_args_list)
        )

    def test_alert_uses_telegram_bot_and_chat_and_job_link(self):
        notifier = TelegramNotifier()
        job_url = "https://www.linkedin.com/jobs/view/1234567890"

        with patch("app.messaging.requests.post", return_value=FakeResponse()) as send:
            notifier.send_alert("Engineer", "Example Co", job_url)

        self.assertIn(settings.TELEGRAM_BOT_TOKEN, send.call_args.args[0])
        self.assertEqual(send.call_args.kwargs["json"]["chat_id"], settings.TELEGRAM_CHAT_ID)
        self.assertIn(job_url, send.call_args.kwargs["json"]["text"])

    def test_notifier_rejects_malformed_telegram_credentials(self):
        notifier = TelegramNotifier("not-a-bot-token", "123456789")

        self.assertFalse(notifier.is_configured)


if __name__ == "__main__":
    unittest.main()