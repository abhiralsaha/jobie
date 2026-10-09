import asyncio
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from tests.db_fakes import install_psycopg2_import_stub

install_psycopg2_import_stub()

import app.linkedin_fresh_jobs as worker
from app.config import settings


class FakeCursor:
    def __init__(self):
        self.query = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query):
        self.query = query

    def fetchone(self):
        return {"id": 42, "received_at": "2026-10-07T00:00:00+00:00"}


class FakeConnection:
    def __init__(self):
        self.fake_cursor = FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.fake_cursor


class KeepAliveTests(unittest.TestCase):
    def test_ping_is_authenticated_and_persisted(self):
        token = "a" * 32
        connection = FakeConnection()
        with (
            patch.object(settings, "JOBIE_API_TOKEN", token),
            patch.object(worker, "get_connection", return_value=connection),
        ):
            result = asyncio.run(worker.keep_alive(f"Bearer {token}"))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["ping"]["id"], 42)
        self.assertIn("INSERT INTO keep_alive_pings", connection.fake_cursor.query)

    def test_ping_rejects_missing_authentication(self):
        with patch.object(settings, "JOBIE_API_TOKEN", "a" * 32):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(worker.keep_alive(None))

        self.assertEqual(error.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
