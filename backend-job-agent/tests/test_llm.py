import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.llm import LLM, Verdict, _parse


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    async def post(self, *args, **kwargs):
        self.calls += 1
        return next(self.responses)

    async def aclose(self):
        pass


def response(status_code, *, headers=None, body=None, text=""):
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.Response(
        status_code,
        headers=headers,
        json=body if body is not None else None,
        text=text if body is None else None,
        request=request,
    )


class LLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_429_using_retry_after(self):
        client = FakeClient([
            response(429, headers={"Retry-After": "10"}, text="rate limited"),
            response(200, body={"ok": True}),
        ])
        llm = LLM("openrouter", "key", "model", "https://openrouter.ai/api/v1")
        llm.client = client

        with patch("app.llm.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await llm._post_with_retry(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={},
                json={},
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(client.calls, 2)
        sleep.assert_awaited_once_with(10.0)

    async def test_parses_valid_experience_verdict(self):
        verdict = _parse(
            '{"min_years": 0, "max_years": 2, "fresher_ok": true, "reason": "Entry level"}'
        )
        self.assertEqual(verdict, Verdict(0, 2, True, "Entry level"))

    async def test_rejects_truncated_json(self):
        with self.assertRaisesRegex(ValueError, "Incomplete or invalid JSON"):
            _parse('{"min_years":')

    async def test_rejects_invalid_boolean_instead_of_treating_string_as_true(self):
        with self.assertRaisesRegex(ValueError, "fresher_ok"):
            _parse(
                '{"min_years": null, "max_years": null, "fresher_ok": "false", "reason": ""}'
            )

    async def test_retry_delay_is_capped(self):
        self.assertEqual(LLM._retry_delay(response(429, headers={"Retry-After": "999"}), 1), 60)
        self.assertEqual(LLM._retry_delay(response(503), 2), 2)

    async def test_reads_retry_after_from_openrouter_error_body(self):
        openrouter_response = response(
            429,
            body={
                "error": {
                    "metadata": {
                        "headers": {"Retry-After": "10"},
                    }
                }
            },
        )
        self.assertEqual(LLM._retry_delay(openrouter_response, 1), 10)


if __name__ == "__main__":
    unittest.main()
