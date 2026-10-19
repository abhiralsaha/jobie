import unittest

from app.storage import Store


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return [{"job_id": "terminal-job"}]


class FakeClient:
    def __init__(self):
        self.params = None

    async def get(self, endpoint, params):
        self.params = params
        return FakeResponse()


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_terminal_statuses_are_deduplicated(self):
        store = Store.__new__(Store)
        store.endpoint = "https://example.test/rest/v1/backend_dev"
        store.client = FakeClient()

        seen = await store.seen_ids(["terminal-job", "failed-job"])

        self.assertEqual(seen, {"terminal-job"})
        self.assertEqual(store.client.params["status"], "in.(sent,rejected,title_blocked)")


if __name__ == "__main__":
    unittest.main()
