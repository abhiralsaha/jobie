import asyncio
import unittest
from unittest.mock import patch

import httpx

from app.keepalive import KeepAlive


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, stop):
        self.stop = stop
        self.calls = []
        self.closed = False

    async def get(self, url, headers):
        self.calls.append((url, headers))
        if len(self.calls) == 2:
            self.stop.set()
        return FakeResponse()

    async def aclose(self):
        self.closed = True


class KeepAliveTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_sends_two_requests_and_closes_client_on_stop(self):
        stop = asyncio.Event()
        keepalive = KeepAlive("https://jobie-2.onrender.com", 780)
        client = FakeClient(stop)
        keepalive.client = client

        with patch("app.keepalive.REQUEST_PAUSE_SECONDS", 0):
            await keepalive.run(stop)

        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call[0] == "https://jobie-2.onrender.com" for call in client.calls))
        self.assertTrue(client.closed)

    async def test_http_error_is_logged_and_does_not_stop_worker(self):
        keepalive = KeepAlive("https://jobie-2.onrender.com", 780)

        class ErrorClient:
            async def get(self, *args, **kwargs):
                request = httpx.Request("GET", "https://jobie-2.onrender.com")
                response = httpx.Response(503, request=request)
                response.raise_for_status()

            async def aclose(self):
                pass

        keepalive.client = ErrorClient()
        with self.assertLogs("keepalive", level="WARNING") as logs:
            await keepalive._request(1)
        self.assertIn("Keep-alive request failed", logs.output[0])


if __name__ == "__main__":
    unittest.main()
