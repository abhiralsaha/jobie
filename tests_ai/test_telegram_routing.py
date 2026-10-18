import copy
import tempfile
import unittest
from pathlib import Path

import requests

from app.ai_pipeline.classifier import Classification
from app.ai_pipeline.delivery import Deliverer, format_alert_blocks
from app.ai_pipeline.destinations import (AllowlistError, RESUME_DESTINATION_NAMES, UnauthorizedDestination, load_registry)
from app.ai_pipeline.fakes import RecordingTelegram
from app.ai_pipeline.models import Job
from app.ai_pipeline.store import Store
from app.ai_pipeline.telegram import TelegramClient, TelegramError, split_escaped
from app.ai_pipeline.util import utcnow
from tests_ai.helpers import CHATS, NAMES, TOKEN, dest_data, env_for

JOB = Job("77", "Software Engineer I", "Acme & Sons", "Gurugram", "https://example.com/jobs/77", "2 minutes ago")
CLS = Classification("77", "include", "0-2 years stated", min_years=0, max_years=2, summary="Build <backend> services & APIs.",
                     technologies=["Python"], fresher_eligible=True, evidence=["0-2 years"])


def registry(data=None, env=None):
    return load_registry(data=data or dest_data(), env=env or env_for())


class AllowlistTests(unittest.TestCase):
    def test_exactly_the_four_named_destinations_resolve(self):
        got = registry().resume_destinations()
        self.assertEqual([d.name for d in got], list(RESUME_DESTINATION_NAMES))
        self.assertEqual({d.chat_id for d in got},
                         {CHATS["one_min"], CHATS["fresher_bot"], CHATS["five_min"], CHATS["thirty_min"]})

    def test_fails_closed_when_a_name_is_missing(self):
        d = dest_data()
        d["destinations"] = [x for x in d["destinations"] if x["name"] != "Fresher bot"]
        with self.assertRaises(AllowlistError) as cm:
            registry(d).resume_destinations()
        self.assertIn("Fresher bot", str(cm.exception))

    def test_fails_closed_on_ambiguous_duplicate_names(self):
        d = dest_data()
        dup = copy.deepcopy(d["destinations"][0]); dup["key"] = "one_min_2"
        d["destinations"].append(dup)
        with self.assertRaises(AllowlistError):
            registry(d).resume_destinations()

    def test_fails_closed_when_an_extra_destination_is_flagged(self):
        flags = {k: r for k, _, r in NAMES}; flags["fifteen"] = True
        with self.assertRaises(AllowlistError) as cm:
            registry(dest_data(flags)).resume_destinations()
        self.assertIn("not an authorised resume destination", str(cm.exception))

    def test_fails_closed_when_a_destination_is_not_flagged(self):
        flags = {k: r for k, _, r in NAMES}; flags["one_min"] = False
        with self.assertRaises(AllowlistError):
            registry(dest_data(flags)).resume_destinations()

    def test_fails_closed_when_credentials_are_missing(self):
        env = env_for(); del env["TOK_thirty_min"]
        reg = registry(env=env)
        with self.assertRaises(AllowlistError):
            reg.resume_destinations()
        self.assertTrue(any("30 min" in u for u in reg.unresolved()))

    def test_fails_closed_when_two_names_map_to_the_same_chat(self):
        env = env_for(); env["CHAT_fresher_bot"] = env["CHAT_one_min"]; env["TOK_fresher_bot"] = env["TOK_one_min"]
        with self.assertRaises(AllowlistError):
            registry(env=env).resume_destinations()

    def test_tokens_in_the_file_are_refused(self):
        d = dest_data(); d["destinations"][0]["bot_token"] = TOKEN(1)
        reg = registry(d)
        self.assertTrue(any("do not put tokens" in u for u in reg.unresolved()))

    def test_malformed_ids_are_reported_without_echoing_secrets(self):
        env = env_for(); env["TOK_one_min"] = "not-a-token"; env["CHAT_fresher_bot"] = "abc"
        reg = registry(env=env)
        text = " ".join(reg.unresolved())
        self.assertIn("does not look like a Telegram bot token", text)
        self.assertNotIn("not-a-token", text)


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.tg, self.store = RecordingTelegram(), Store(None, ":memory:")
        self.reg = registry()
        self.dv = Deliverer(self.store, self.tg, self.reg)

    def test_alert_goes_to_every_job_alert_destination(self):
        rep = self.dv.send_alerts(JOB, CLS)
        self.assertEqual(len(rep.sent), 7)
        self.assertEqual({c["payload"]["chat_id"] for c in self.tg.sent("sendMessage")}, set(CHATS.values()))
        self.assertNotIn("555000111", {c["payload"]["chat_id"] for c in self.tg.sent("sendMessage")})   # control bot gets no alerts

    def test_alert_contains_required_fields_and_escapes_html(self):
        text = "\n".join(format_alert_blocks(JOB, CLS, utcnow()))
        for needle in ("Software Engineer I", "Acme &amp; Sons", "Gurugram", "0-2 years", "https://example.com/jobs/77", "77", "Python",
                       "Eligible", "Build &lt;backend&gt; services &amp; APIs.", "Posted"):
            self.assertIn(needle, text)

    def test_only_included_jobs_can_be_alerted(self):
        with self.assertRaises(ValueError):
            self.dv.send_alerts(JOB, Classification("77", "review", "unclear"))
        self.assertEqual(self.tg.calls, [])

    def test_alert_fails_explicitly_when_no_job_alert_destinations_are_usable(self):
        empty = load_registry(data={"destinations": []}, env={})
        report = Deliverer(self.store, self.tg, empty).send_alerts(JOB, CLS)
        self.assertIn("configuration", report.failed)
        self.assertFalse(report.complete)
        self.assertEqual(self.tg.calls, [])

    def test_pdf_goes_only_to_the_four_and_never_elsewhere(self):
        pdf = Path(tempfile.mkdtemp()) / "r.pdf"; pdf.write_bytes(b"%PDF-1.4 " + b"x" * 3000 + b"\n%%EOF")
        from app.ai_pipeline.util import sha256_file
        res = {"status": "ok", "pdf_path": str(pdf), "pdf_sha256": sha256_file(pdf), "filename": "r.pdf"}
        self.dv.send_resume(JOB, CLS, res)
        docs = {c["payload"]["chat_id"] for c in self.tg.sent("sendDocument")}
        self.assertEqual(docs,
                         {CHATS["one_min"], CHATS["fresher_bot"], CHATS["five_min"], CHATS["thirty_min"]})
        cap = self.tg.sent("sendDocument")[0]["payload"]["caption"]
        for needle in ("Software Engineer I", "Acme &amp; Sons", "https://example.com/jobs/77", "r.pdf"):
            self.assertIn(needle, cap)

    def test_client_level_guard_refuses_non_allowlisted_destinations(self):
        pdf = Path(tempfile.mkdtemp()) / "r.pdf"; pdf.write_bytes(b"%PDF")
        other = [d for d in self.reg.job_alert_destinations() if d.name == "15 min"][0]
        for d in (other, self.reg.control):
            with self.assertRaises(UnauthorizedDestination):
                self.tg.send_document(d, pdf, "x")
        self.assertEqual(self.tg.sent("sendDocument"), [])

    def test_resume_blocked_when_pdf_changed_after_validation(self):
        pdf = Path(tempfile.mkdtemp()) / "r.pdf"; pdf.write_bytes(b"%PDF-1" + b"a" * 3000)
        res = {"status": "ok", "pdf_path": str(pdf), "pdf_sha256": "0" * 64, "filename": "r.pdf"}
        with self.assertRaises(RuntimeError):
            self.dv.send_resume(JOB, CLS, res)
        self.assertEqual(self.tg.sent("sendDocument"), [])

    def test_resume_blocked_entirely_when_allowlist_is_ambiguous(self):
        d = dest_data(); d["destinations"] = d["destinations"][1:]
        dv = Deliverer(self.store, self.tg, registry(d))
        with self.assertRaises(AllowlistError):
            dv.send_resume(JOB, CLS, {"status": "ok", "pdf_path": "x", "pdf_sha256": "y", "filename": "x"})
        self.assertEqual(self.tg.calls, [])

    def test_delivery_failure_is_reported_precisely_and_retry_does_not_duplicate(self):
        self.tg.fail[CHATS["fifteen"]] = TelegramError("Forbidden: bot was blocked by the user", 403)
        self.tg.fail[CHATS["ten_hours"]] = TelegramError("Bad Request: chat not found", 400)
        rep = self.dv.send_alerts(JOB, CLS)
        self.assertEqual(sorted(rep.failed), ["fifteen", "ten_hours"])
        self.assertIn("blocked", rep.failed["fifteen"])
        self.assertIn("chat not found", rep.failed["ten_hours"])
        self.assertEqual(len(rep.sent), 5)
        rows = {r["dest_key"]: r for r in self.store.deliveries("77", "alert")}
        self.assertEqual(rows["fifteen"]["status"], "failed")
        self.assertEqual(rows["one_min"]["status"], "sent")
        self.tg.fail.clear()
        before = len(self.tg.sent("sendMessage"))
        rep2 = self.dv.send_alerts(JOB, CLS)
        self.assertEqual(sorted(rep2.sent), ["fifteen", "ten_hours"])
        self.assertEqual(len(rep2.already_sent), 5)
        self.assertEqual(len(self.tg.sent("sendMessage")) - before, 2)             # only the two failed ones were re-sent
        per_chat = {}
        for c in self.tg.sent("sendMessage"):
            per_chat[c["payload"]["chat_id"]] = per_chat.get(c["payload"]["chat_id"], 0) + 1
        for key in ("one_min", "fresher_bot", "five_min", "thirty_min", "one_hour"):  # one delivery per destination
            self.assertEqual(per_chat[CHATS[key]], 1, key)
        for key in ("fifteen", "ten_hours"):                                   # one failed attempt + one successful retry
            self.assertEqual(per_chat[CHATS[key]], 2, key)

    def test_concurrent_sends_do_not_duplicate(self):
        import threading
        ts = [threading.Thread(target=self.dv.send_alerts, args=(JOB, CLS)) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(self.tg.sent("sendMessage")), 7)


class TransportTests(unittest.TestCase):
    class Resp:
        def __init__(self, data, code=200): self._d, self.status_code = data, code
        def json(self): return self._d

    def test_long_text_is_split_under_the_limit(self):
        chunks = split_escaped(("word & <tag> " * 400 + "\n") * 5)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 4000 for c in chunks))
        self.assertEqual(split_escaped("x" * 20000)[0][:3], "xxx")
        self.assertTrue(all(len(c) <= 4000 for c in split_escaped("a&" * 9000)))

    def test_long_summary_becomes_several_messages_each_valid(self):
        tg, store = RecordingTelegram(), Store(None, ":memory:")
        big = Classification("77", "include", "ok", min_years=0, max_years=2, summary=("Line & text.\n" * 1200), fresher_eligible=True)
        Deliverer(store, tg, registry()).send_alerts(JOB, big)
        msgs = [c["payload"]["text"] for c in tg.sent("sendMessage")]
        self.assertGreater(len(msgs), 6)
        self.assertTrue(all(len(m) <= 4096 for m in msgs))

    def test_429_is_waited_out_then_retried(self):
        seq = [self.Resp({"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}}, 429),
               self.Resp({"ok": True, "result": {"message_id": 5}})]
        sleeps = []

        class S:
            def post(self, *a, **k): return seq.pop(0)
        tg = TelegramClient(session=S(), sleep=sleeps.append)
        self.assertEqual(tg.call("1:x", "sendMessage", {"chat_id": "1"}), {"message_id": 5})
        self.assertEqual(sleeps, [3.0])

    def test_network_errors_never_leak_the_bot_token(self):
        token = TOKEN(5)

        class S:
            def post(self, url, **k): raise requests.ConnectionError(f"Max retries exceeded with url: /bot{url.split('/bot')[1]}")
        tg = TelegramClient(session=S(), sleep=lambda s: None, max_retries=1)
        with self.assertRaises(TelegramError) as cm:
            tg.call(token, "sendMessage", {"chat_id": "1"})
        self.assertNotIn(token, str(cm.exception))
        self.assertNotIn("AAAA", str(cm.exception))

    def test_permanent_errors_are_not_retried(self):
        calls = []

        class S:
            def post(self, *a, **k):
                calls.append(1)
                return TransportTests.Resp({"ok": False, "error_code": 403, "description": "Forbidden: bot was kicked"}, 403)
        with self.assertRaises(TelegramError):
            TelegramClient(session=S(), sleep=lambda s: None).call("1:x", "sendMessage", {})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
