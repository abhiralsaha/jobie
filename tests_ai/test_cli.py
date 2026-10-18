import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.ai_pipeline import cli


class CheckDestinationsTests(unittest.TestCase):
    def test_empty_preflight_does_not_report_success(self):
        registry = SimpleNamespace(
            errors=[],
            job_alert_destinations=lambda: [],
            unresolved=lambda: [],
            summary=lambda: {"resume_destinations": "BLOCKED: no destinations"},
        )
        settings = SimpleNamespace(destinations_file="config/telegram_destinations.json", path=lambda value: value)
        output = io.StringIO()
        with patch.object(cli.Settings, "from_env", return_value=settings), \
                patch.object(cli, "load_registry", return_value=registry), \
                patch("app.ai_pipeline.telegram.preflight", return_value=[]), \
                contextlib.redirect_stdout(output):
            result = cli.cmd_check_destinations(None)

        self.assertEqual(result, 1)
        self.assertIn("BLOCKED: no destinations", output.getvalue())
