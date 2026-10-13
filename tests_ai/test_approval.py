import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from app.ai_pipeline.approval import ApprovalPoller
from app.ai_pipeline.contacts import Contact
from app.ai_pipeline.fakes import DemoLLM, RecordingMailer
from app.ai_pipeline.mailer import ACCEPTED, NOT_SUBMITTED, UNKNOWN, SendOutcome
from app.ai_pipeline.models import Job
from app.ai_pipeline.resume import Profile
from app.ai_pipeline.util import iso, sha256_file, utcnow
from tests_ai.helpers import AUTH_USER, CONTROL_CHAT, make_pipeline

JOB = Job("111", "Software Engineer I", "Acme Technologies", "Gurugram", "https://example.com/jobs/111", None, "x")


def contact(status="verified_service", email="neha.gupta@acme.example", slot="swe", name="Neha Gupta"):
    return Contact(slot=slot, name=name, position="Software Engineer", company="Acme Technologies",
                   profile_url="https://in.linkedin.com/in/neha-gupta-3", email=email, email_status=status,
                   affiliation_evidence=["Neha Gupta - Software Engineer - Acme Technologies | LinkedIn"],
                   source_urls=["https://in.linkedin.com/in/neha-gupta-3"], reason="headline matches")


class Base(unittest.TestCase):
    def setUp(self, mailer=None):
        self.tmp = Path(tempfile.mkdtemp())
        self.pipe, self.tg, self.mailer, self.store, self.s = make_pipeline(self.tmp, llm=DemoLLM(), mailer=mailer or RecordingMailer())
        (self.tmp / "resumes").mkdir()
        self.pdf = self.tmp / "resumes" / "Acme_SE_111.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\n" + b"resume bytes " * 300 + b"\n%%EOF")
        self.resume = {"status": "ok", "pdf_path": str(self.pdf), "sha256": sha256_file(self.pdf), "pdf_sha256": sha256_file(self.pdf),
                       "filename": self.pdf.name}
        self.profile = Profile.load(self.tmp / "config" / "resume_profile.json")
        self.ap = self.pipe.approvals
        self.contact = contact()
        self.draft = self.pipe.referral_svc.draft_for(JOB, self.contact, self.profile, self.resume)
        self.state = self.ap.request(self.draft, JOB, self.contact)

    def runTest(self):  # lets FailureTests instantiate Base directly
        pass

    def row(self, draft_id=None):
        return self.store.one("SELECT * FROM ai_email_drafts WHERE draft_id = ?", (draft_id or self.draft.draft_id,))

    def tap(self, action="a", user=AUTH_USER, chat=CONTROL_CHAT, data=None, draft=None):
        d = draft or self.draft
        return self.ap.handle_update({"update_id": 1, "callback_query": {
            "id": "cb1", "from": {"id": user}, "message": {"chat": {"id": int(chat)}, "message_id": 5},
            "data": data if data is not None else f"{action}:{d.draft_id}:{d.content_hash[:12]}"}})

    def control_msgs(self):
        return self.tg.sent("sendMessage", CONTROL_CHAT)


class PresentationTests(Base):
    def test_draft_is_shown_in_full_with_buttons_before_anything_is_sent(self):
        self.assertEqual(self.state, "pending")
        msgs = self.control_msgs()
        text = "\n".join(m["payload"]["text"] for m in msgs)
        for needle in ("Neha Gupta", "neha.gupta@acme.example", "verified_service", "Software Engineer I", "https://example.com/jobs/111",
                       self.draft.subject.replace("&", "&amp;"), "Acme_SE_111.pdf", self.resume["sha256"][:12], "Hi Neha,"):
            self.assertIn(needle, text)
        kb = msgs[-1]["payload"]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual([b["text"][0] for b in kb], ["\u2705", "\u274C"])
        self.assertTrue(kb[0]["callback_data"].startswith("a:") and kb[1]["callback_data"].startswith("r:"))
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for b in kb))
        self.assertEqual(self.mailer.sent, [])
        self.assertEqual(self.row()["state"], "pending")

    def test_the_resume_pdf_is_not_sent_to_the_control_bot(self):
        self.assertEqual(self.tg.sent("sendDocument", CONTROL_CHAT), [])

    def test_request_is_idempotent(self):
        n = len(self.control_msgs())
        self.assertEqual(self.ap.request(self.draft, JOB, self.contact), "pending")
        self.assertEqual(len(self.control_msgs()), n)

    def test_unverified_email_is_never_offered_for_sending(self):
        c = contact(status="unverified", slot="hr", name="Priya Nair", email="priya.nair@acme.example")
        d = self.pipe.referral_svc.draft_for(JOB, c, self.profile, self.resume)
        self.assertEqual(self.ap.request(d, JOB, c), "blocked")
        last = self.control_msgs()[-1]["payload"]
        self.assertNotIn("reply_markup", last)
        self.assertIn("NOT SENDABLE", last["text"])
        self.assertEqual(self.tap(data=f"a:{d.draft_id}:{d.content_hash[:12]}", draft=d), "noop")
        self.assertEqual(self.mailer.sent, [])

    def test_drafts_are_personalised_per_recipient(self):
        texts = set()
        for slot, name in (("hr", "Priya Nair"), ("senior_swe", "Rahul Verma"), ("swe", "Neha Gupta")):
            c = contact(slot=slot, name=name, email=f"{name.split()[0].lower()}@acme.example")
            c.position = {"hr": "Talent Acquisition Specialist", "senior_swe": "Senior Software Engineer", "swe": "Software Engineer"}[slot]
            d = self.pipe.referral_svc.draft_for(JOB, c, self.profile, self.resume)
            self.assertIn(f"Hi {name.split()[0]},", d.body)
            self.assertIn(JOB.url, d.body)
            texts.add(d.body)
        self.assertEqual(len(texts), 3)


class DecisionTests(Base):
    def test_approve_sends_exactly_the_approved_email_with_the_approved_pdf(self):
        self.assertEqual(self.tap("a"), "sent")
        self.assertEqual(len(self.mailer.sent), 1)
        m = self.mailer.sent[0]
        self.assertEqual((m["to"], m["subject"], m["body"], m["attachment"]), (self.draft.to_email, self.draft.subject, self.draft.body, str(self.pdf)))
        r = self.row()
        self.assertEqual((r["state"], r["approver_id"]), ("sent", str(AUTH_USER)))
        self.assertIn("accepted", r["outcome"])
        self.assertIn("accepted it for delivery", self.control_msgs()[-1]["payload"]["text"])
        self.assertIn("not a guarantee", self.control_msgs()[-1]["payload"]["text"])

    def test_repeated_and_concurrent_approvals_send_once(self):
        results = []
        ts = [threading.Thread(target=lambda: results.append(self.tap("a"))) for _ in range(10)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(len(self.mailer.sent), 1)
        self.assertEqual(results.count("sent"), 1)
        self.tap("a"); self.tap("a")
        self.assertEqual(len(self.mailer.sent), 1)

    def test_replayed_telegram_update_does_not_resend(self):
        update = {"update_id": 9, "callback_query": {"id": "c", "from": {"id": AUTH_USER}, "message": {"chat": {"id": int(CONTROL_CHAT)}},
                                                  "data": f"a:{self.draft.draft_id}:{self.draft.content_hash[:12]}"}}
        for _ in range(3):
            self.ap.handle_update(update)
        self.assertEqual(len(self.mailer.sent), 1)

    def test_reject_prevents_sending_even_if_approve_is_tapped_later(self):
        self.assertEqual(self.tap("r"), "rejected")
        self.assertEqual(self.tap("a"), "noop")
        self.assertEqual(self.mailer.sent, [])
        self.assertEqual(self.row()["state"], "rejected")

    def test_no_response_never_sends_and_expiry_closes_the_door(self):
        self.assertEqual(self.mailer.sent, [])
        self.ap.now = lambda: utcnow() + timedelta(days=10)
        self.assertEqual(self.ap.expire_stale(), 1)
        self.assertEqual(self.row()["state"], "expired")
        self.assertEqual(self.tap("a"), "noop")
        self.assertEqual(self.mailer.sent, [])

    def test_only_the_authorised_user_can_approve(self):
        self.assertEqual(self.tap("a", user=999), "refused_unauthorised")
        self.assertEqual(self.tap("r", user=999), "refused_unauthorised")
        self.assertEqual(self.row()["state"], "pending")
        self.assertEqual(self.mailer.sent, [])
        self.assertEqual(self.tap("a", chat="123456789"), "refused_wrong_chat")
        self.assertEqual(self.mailer.sent, [])

    def test_no_configured_user_means_nobody_can_approve(self):
        self.ap.s = replace(self.s, authorized_user_id=None)
        self.assertEqual(self.tap("a"), "refused_unconfigured")
        self.assertEqual(self.mailer.sent, [])

    def test_malformed_or_foreign_callback_data_is_refused(self):
        for data in ("a:zzzz:123", "x:" + self.draft.draft_id + ":" + self.draft.content_hash[:12], "a:" + "0" * 16 + ":" + "0" * 12, "", "a"):
            self.assertIn(self.tap(data=data), ("refused_bad_data", "unknown_draft"))
        self.assertEqual(self.mailer.sent, [])

    def test_button_from_an_older_draft_version_is_refused(self):
        self.assertEqual(self.tap(data=f"a:{self.draft.draft_id}:{'0' * 12}"), "stale_button")
        self.assertEqual(self.mailer.sent, [])

    def test_changed_resume_after_request_requires_new_approval(self):
        self.pdf.write_bytes(b"%PDF-1.4 different " + b"z" * 3000)
        self.assertEqual(self.tap("a"), "stale")
        self.assertEqual(self.mailer.sent, [])
        self.assertEqual(self.row()["state"], "stale")

    def test_missing_resume_blocks_sending(self):
        self.pdf.unlink()
        self.assertEqual(self.tap("a"), "stale")
        self.assertEqual(self.mailer.sent, [])

    def test_edited_draft_after_request_requires_new_approval(self):
        self.store.execute("UPDATE ai_email_drafts SET body = ? WHERE draft_id = ?", (self.draft.body + "\nP.S. send me money", self.draft.draft_id))
        self.assertEqual(self.tap("a"), "stale")
        self.assertEqual(self.mailer.sent, [])

    def test_new_version_supersedes_old_buttons(self):
        self.store.execute("UPDATE ai_email_drafts SET state = 'pending' WHERE draft_id = ?", (self.draft.draft_id,))
        newer = self.pipe.referral_svc.draft_for(JOB, self.contact, self.profile, self.resume)
        from dataclasses import replace as rp
        from app.ai_pipeline.referral import content_hash
        body = newer.body + "\nPS."
        newer = rp(newer, body=body, content_hash=content_hash(JOB.job_id, newer.to_email, newer.subject, body, newer.resume_sha256))
        newer = rp(newer, draft_id="f" * 16)
        self.ap.request(newer, JOB, self.contact)
        self.assertEqual(self.row()["state"], "superseded")
        self.assertEqual(self.tap("a"), "noop")
        self.assertEqual(self.mailer.sent, [])
        self.assertEqual(self.tap("a", draft=newer), "sent")
        self.assertEqual(len(self.mailer.sent), 1)


class FailureTests(unittest.TestCase):
    def make(self, outcome):
        t = Base("runTest")
        t.setUp(mailer=RecordingMailer(outcome=outcome))
        return t

    def test_refused_by_server_is_reported_and_retry_works_once(self):
        outcomes = iter([SendOutcome(NOT_SUBMITTED, "SMTP login rejected"), SendOutcome(ACCEPTED, "accepted", "<id@x>")])
        t = self.make(lambda: next(outcomes))
        self.assertEqual(t.tap("a"), "failed")
        last = t.control_msgs()[-1]["payload"]
        self.assertIn("Not sent", last["text"]); self.assertIn("SMTP login rejected", last["text"])
        self.assertTrue(last["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("t:"))
        self.assertEqual(t.tap("t"), "sent")
        self.assertEqual(t.tap("t"), "noop")
        self.assertEqual(len(t.mailer.sent), 2)                              # two attempts, only one accepted
        self.assertEqual(t.row()["state"], "sent")

    def test_unknown_outcome_is_never_retried(self):
        t = self.make(SendOutcome(UNKNOWN, "connection reset during transfer"))
        self.assertEqual(t.tap("a"), "send_unknown")
        self.assertEqual(t.tap("t"), "noop")
        self.assertEqual(t.tap("a"), "noop")
        self.assertEqual(len(t.mailer.sent), 1)
        self.assertIn("Check your Sent folder", t.control_msgs()[-1]["payload"]["text"])

    def test_unexpected_mailer_exception_is_treated_as_unknown(self):
        def boom(): raise OSError("disk")
        t = self.make(boom)
        self.assertEqual(t.tap("a"), "send_unknown")
        self.assertEqual(t.tap("a"), "noop")

    def test_restart_mid_send_does_not_resend(self):
        t = self.make(None)
        t.store.execute("UPDATE ai_email_drafts SET state = 'sending', updated_at = ? WHERE draft_id = ?", (iso(utcnow() - timedelta(minutes=30)), t.draft.draft_id))
        t.ap.recover_after_restart()
        self.assertEqual(t.row()["state"], "send_unknown")
        self.assertEqual(t.tap("a"), "noop")
        self.assertEqual(t.mailer.sent, [])


class PollerTests(Base):
    def test_poller_processes_callbacks_and_saves_offset(self):
        self.tg.updates = [{"update_id": 41, "callback_query": {"id": "c", "from": {"id": AUTH_USER}, "message": {"chat": {"id": int(CONTROL_CHAT)}},
                                                                "data": f"r:{self.draft.draft_id}:{self.draft.content_hash[:12]}"}}]
        poller = ApprovalPoller(self.ap, self.store, self.tg)
        self.assertEqual(poller.poll_once(), 1)
        self.assertEqual(self.row()["state"], "rejected")
        self.assertEqual(self.store.kv_get(f"tg_offset:{self.pipe.approvals.control.bot_id}"), "42")
        self.assertEqual(poller.poll_once(), 0)
        offsets = [c["payload"].get("offset") for c in self.tg.sent("getUpdates")]
        self.assertEqual(offsets, [None, 42])

    def test_a_bad_update_does_not_wedge_the_queue(self):
        self.tg.updates = [{"update_id": 1, "callback_query": "garbage"}, {"update_id": 2, "message": {"text": "hi"}}]
        self.assertEqual(ApprovalPoller(self.ap, self.store, self.tg).poll_once(), 2)
        self.assertEqual(self.store.kv_get(f"tg_offset:{self.ap.control.bot_id}"), "3")

    def test_dry_run_poller_never_processes_an_email_approval(self):
        self.s = replace(self.s, dry_run=True)
        self.ap.s = self.s
        self.tg.updates = [{"update_id": 5, "callback_query": {
            "id": "cb-dry-run", "from": {"id": AUTH_USER},
            "message": {"chat": {"id": int(CONTROL_CHAT)}},
            "data": f"a:{self.draft.draft_id}:{self.draft.content_hash[:12]}",
        }}]

        self.assertEqual(ApprovalPoller(self.ap, self.store, self.tg).poll_once(), 1)

        self.assertEqual(self.row()["state"], "pending")
        self.assertEqual(self.mailer.sent, [])
        callback = [c for c in self.tg.calls if c["method"] == "answerCallbackQuery"][-1]
        self.assertIn("AI_DRY_RUN=true", callback["payload"]["text"])


if __name__ == "__main__":
    unittest.main()
