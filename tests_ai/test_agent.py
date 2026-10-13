import tempfile
import threading
import unittest
from pathlib import Path

from app.ai_pipeline.agent import TelegramAgent
from app.ai_pipeline.approval import ApprovalPoller
from app.ai_pipeline.fakes import ScriptedLLM
from tests_ai.helpers import AUTH_USER, CONTROL_CHAT, make_pipeline


class TelegramAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pipeline, self.tg, _, self.store, self.settings = make_pipeline(self.tmp)
        self.search_done = threading.Event()
        self.agent = TelegramAgent(
            self.pipeline,
            self.tg,
            search=lambda: (self.search_done.set() or {"matched": 2, "new": 1}),
        )

    def tearDown(self):
        self.store.close()

    def message(self, text, user=AUTH_USER, chat=CONTROL_CHAT):
        return {
            "message": {
                "from": {"id": user},
                "chat": {"id": int(chat), "type": "private"},
                "text": text,
            }
        }

    def callback(self, action, user=AUTH_USER, chat=CONTROL_CHAT):
        return {
            "callback_query": {
                "id": "agent-callback",
                "from": {"id": user},
                "message": {"chat": {"id": int(chat), "type": "private"}, "message_id": 12},
                "data": action,
            }
        }

    def test_start_sends_owner_menu(self):
        self.assertEqual(self.agent.handle_update(self.message("/start")), "start")
        sent = self.tg.sent("sendMessage", CONTROL_CHAT)
        self.assertIn("Jobie Agent", sent[-1]["payload"]["text"])
        self.assertIn("reply_markup", sent[-1]["payload"])

    def test_non_owner_cannot_read_or_control_agent(self):
        self.assertEqual(self.agent.handle_update(self.message("/status", user=999)), "refused_unauthorized")
        self.assertEqual(self.tg.sent("sendMessage", CONTROL_CHAT), [])
        self.assertEqual(self.agent.handle_update(self.callback("agent:status", user=999)), "refused_unauthorized")
        self.assertEqual(self.tg.sent("sendMessage", CONTROL_CHAT), [])

    def test_owner_can_view_status_and_ai_jobs(self):
        self.store.execute(
            "INSERT INTO ai_jobs (job_id, title, company, stage, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("test-1", "Backend Engineer", "Example", "review", "2026-01-01", "2026-01-01"),
        )
        self.assertEqual(self.agent.handle_update(self.message("/status")), "status")
        self.assertIn("Tracked AI jobs: 1", self.tg.sent("sendMessage", CONTROL_CHAT)[-1]["payload"]["text"])
        self.assertEqual(self.agent.handle_update(self.message("/jobs")), "jobs")
        self.assertIn("Backend Engineer", self.tg.sent("sendMessage", CONTROL_CHAT)[-1]["payload"]["text"])

    def test_retry_requires_a_separate_confirmation_tap(self):
        self.pipeline.tick = lambda: 2
        self.agent.handle_update(self.message("/retry"))
        self.assertIn(
            "agent:confirm_retry",
            str(self.tg.sent("sendMessage", CONTROL_CHAT)[-1]["payload"]["reply_markup"]),
        )
        self.agent.handle_update(self.callback("agent:confirm_retry"))
        self.assertIn("Queued 2 due AI job(s)", self.tg.sent("sendMessage", CONTROL_CHAT)[-1]["payload"]["text"])

    def test_manual_search_requires_confirmation_and_runs_in_background(self):
        self.agent.handle_update(self.callback("agent:search"))
        self.assertFalse(self.search_done.is_set())
        self.agent.handle_update(self.callback("agent:confirm_search"))
        self.assertTrue(self.search_done.wait(2))
        self.assertIn("Manual search complete: 2 matching jobs", self.tg.sent("sendMessage", CONTROL_CHAT)[-1]["payload"]["text"])

    def test_natural_language_action_cannot_skip_retry_confirmation(self):
        self.pipeline.classifier.llm = ScriptedLLM(
            lambda _system, _prompt: {"reply": "I can retry due jobs.", "action": "retry"}
        )
        self.pipeline.tick = lambda: self.fail("retry must wait for confirmation")
        self.agent.handle_update(self.message("Please retry work that is ready."))
        messages = self.tg.sent("sendMessage", CONTROL_CHAT)
        self.assertIn("retry due jobs", messages[-2]["payload"]["text"].lower())
        self.assertIn("agent:confirm_retry", str(messages[-1]["payload"]["reply_markup"]))

    def test_approval_poller_routes_owner_messages_to_agent(self):
        self.tg.updates = [self.message("/start") | {"update_id": 77}]
        poller = ApprovalPoller(self.pipeline.approvals, self.store, self.tg, self.agent)
        self.assertEqual(poller.poll_once(), 1)
        self.assertIn("Jobie Agent", self.tg.sent("sendMessage", CONTROL_CHAT)[-1]["payload"]["text"])
        self.assertEqual(self.store.kv_get(f"tg_offset:{self.pipeline.approvals.control.bot_id}"), "78")


if __name__ == "__main__":
    unittest.main()
