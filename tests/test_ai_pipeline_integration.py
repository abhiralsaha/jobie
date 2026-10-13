import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.main import JobSubmission, import_job


def make_job() -> JobSubmission:
    return JobSubmission(
        job_id="integration-test",
        title="Software Engineer",
        company="Example",
        url="https://example.com/jobs/1",
        posted_at=datetime.now(timezone.utc).isoformat(),
        source="authorized_feed",
        description="Experience: 0-2 years. " + ("Relevant description. " * 20),
    )


class ImportApiAiIntegrationTests(unittest.TestCase):
    def test_disabled_pipeline_preserves_recent_telegram_alert(self):
        with (
            patch("app.main.require_api_bearer_token"),
            patch("app.main.import_jobs", return_value=[{"job_id": "integration-test"}]),
            patch("app.main.ai_hook.enabled", return_value=False),
            patch("app.main.notifier.send_alert") as send_alert,
        ):
            response = asyncio.run(import_job(make_job(), authorization="Bearer token"))

        send_alert.assert_called_once()
        self.assertTrue(response["telegram_alert_sent"])
        self.assertFalse(response["ai_pipeline_submitted"])

    def test_enabled_pipeline_receives_imported_description(self):
        job = make_job()
        with (
            patch("app.main.require_api_bearer_token"),
            patch("app.main.import_jobs", return_value=[{"job_id": job.job_id}]),
            patch("app.main.ai_hook.enabled", return_value=True),
            patch("app.main.ai_hook.set_project_http_client") as configure_http,
            patch("app.main.ai_hook.on_new_job", return_value=True) as submit,
            patch("app.main.notifier.send_alert") as send_alert,
        ):
            response = asyncio.run(import_job(job, authorization="Bearer token"))

        configure_http.assert_called_once()
        send_alert.assert_not_called()
        self.assertEqual(submit.call_args.kwargs["description"], job.description)
        self.assertEqual(submit.call_args.kwargs["source"], "authorized_feed")
        self.assertTrue(response["ai_pipeline_submitted"])
        self.assertFalse(response["telegram_alert_sent"])

    def test_ai_pipeline_dry_run_defaults_on(self):
        from app.ai_pipeline.settings import Settings

        self.assertTrue(Settings.from_env({}).dry_run)
        self.assertFalse(Settings.from_env({"AI_DRY_RUN": "false"}).dry_run)


class SharedHttpClientTests(unittest.TestCase):
    def test_description_fetch_respects_shared_circuit_breaker(self):
        from app.ai_pipeline import hook
        from app.ai_pipeline.jd_fetch import FetchRateLimited
        from app import http_client

        captured = {}
        with (
            patch.object(hook, "set_http_get", side_effect=lambda fn: captured.update(fn=fn)),
            patch.object(http_client, "circuit_is_open", return_value=(True, 120)),
            patch.object(http_client, "fetch") as fetch,
        ):
            hook.set_project_http_client()
            with self.assertRaises(FetchRateLimited) as error:
                captured["fn"]("https://example.com/job")

        self.assertEqual(error.exception.retry_after, 120)
        fetch.assert_not_called()

    def test_description_fetch_uses_project_http_client(self):
        from app.ai_pipeline import hook
        from app import http_client

        captured = {}
        with (
            patch.object(hook, "set_http_get", side_effect=lambda fn: captured.update(fn=fn)),
            patch.object(http_client, "circuit_is_open", return_value=(False, 0)),
            patch.object(http_client, "fetch", return_value="description") as fetch,
        ):
            hook.set_project_http_client()
            self.assertEqual(captured["fn"]("https://example.com/job"), "description")

        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
