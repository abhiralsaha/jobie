import unittest

from app.ai_pipeline.llm import LLMClient
from app.ai_pipeline.settings import Settings


class LLMRequestTests(unittest.TestCase):
    def test_openrouter_request_uses_bearer_api_key(self):
        settings = Settings.from_env({
            "AI_PROVIDER": "openrouter",
            "AI_API_KEY": "test-secret",
            "AI_MODEL": "test-model",
            "AI_BASE_URL": "https://openrouter.ai/api/v1",
        })

        url, headers, body = LLMClient(settings)._build("system", "user", 20)

        self.assertEqual(url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer test-secret")
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["response_format"], {"type": "json_object"})
