import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.ai_pipeline import hook
from app.ai_pipeline.fakes import DemoLLM, RecordingMailer, RecordingTelegram, ScriptedLLM, StaticSearch
from app.ai_pipeline.jd_fetch import FetchError, FetchRateLimited, DescriptionFetcher, extract_description_from_html, is_complete
from app.ai_pipeline.llm import LLMError
from app.ai_pipeline.models import Job
from app.ai_pipeline.util import iso, utcnow
from tests_ai.helpers import CHATS, CONTROL_CHAT, GOOD_DESC, dest_data, make_pipeline

HAVE_TEX = bool(shutil.which("latexmk") or shutil.which("pdflatex"))
SENIOR_DESC = "Backend role. Requires 5+ years of experience in distributed systems and Java." + " Filler text for length." * 8
AMBIG_DESC = "Developer at Initech. Join a friendly team and ship features every week with designers and PMs." + " More text." * 12


def job(jid, title="Software Engineer I", desc=GOOD_DESC, company="Acme Technologies"):
    return Job(jid, title, company, "Gurugram", f"https://example.com/jobs/{jid}", "just now", desc)


def pdf_chats(tg):
    return {c["payload"]["chat_id"] for c in tg.sent("sendDocument")}


@unittest.skipUnless(HAVE_TEX, "no LaTeX compiler installed")
class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.pipe, self.tg, self.mailer, self.store, self.s = make_pipeline(self.tmp, llm=DemoLLM())

    def run_job(self, j):
        self.assertTrue(self.pipe.submit(j))
        self.pipe.wait_idle(60)

    def test_eligible_job_full_flow(self):
        self.run_job(job("1001"))
        self.assertEqual(self.store.get_job("1001")["stage"], "done")
        alert_chats = {c["payload"]["chat_id"] for c in self.tg.sent("sendMessage")} - {CONTROL_CHAT}
        self.assertEqual(alert_chats, set(CHATS.values()))                          # all seven job-alert destinations
        self.assertEqual(pdf_chats(self.tg), {CHATS["one_min"], CHATS["fresher_bot"], CHATS["five_min"], CHATS["thirty_min"]})
        self.assertEqual(len(self.tg.sent("sendMessage", CONTROL_CHAT)), 4)         # package summary + 3 approval requests
        drafts = self.store.query("SELECT slot, state, to_email FROM ai_email_drafts ORDER BY slot")
        self.assertEqual([(d["slot"], d["state"]) for d in drafts], [("hr", "pending"), ("senior_swe", "pending"), ("swe", "pending")])
        self.assertEqual(self.mailer.sent, [])                                      # NOTHING e-mailed without approval
        row = self.store.get_resume("1001")
        self.assertEqual(row["status"], "ok")
        self.assertTrue(Path(row["pdf_path"]).exists() and Path(row["tex_path"]).exists())
        docs = self.tg.sent("sendDocument")
        self.assertTrue(all("Acme_Technologies_Software_Engineer_I_1001.pdf" in c["payload"]["caption"] for c in docs))
        self.assertTrue(all(d["to_email"].endswith("@acme.example") for d in drafts))

    def test_resubmitting_or_reprocessing_never_duplicates_anything(self):
        self.run_job(job("1001"))
        snap = len(self.tg.calls)
        self.assertFalse(self.pipe.submit(job("1001")))                             # already known
        self.pipe.process("1001"); self.pipe.tick(); self.pipe.recover(); self.pipe.wait_idle(10)
        self.assertEqual(len(self.tg.calls), snap)

    def test_restart_after_partial_progress_only_does_what_is_missing(self):
        self.tg.fail[CHATS["fifteen"]] = __import__("app.ai_pipeline.telegram", fromlist=["x"]).TelegramError("Forbidden: bot was blocked by the user", 403)
        self.run_job(job("1001"))
        row = self.store.get_job("1001")
        self.assertEqual((row["stage"], row["attempts"]), ("alerts_pending", 1))
        self.assertIn("blocked", row["last_error"])
        self.assertEqual(pdf_chats(self.tg), set())                                 # no resume work before alerts complete
        self.tg.fail.clear()
        self.store.update_job("1001", utcnow(), next_attempt_at=iso(utcnow()))      # backoff elapsed
        self.assertEqual(self.pipe.tick(), 1); self.pipe.wait_idle(60)
        self.assertEqual(self.store.get_job("1001")["stage"], "done")
        per = {}
        for c in self.tg.sent("sendMessage"):
            per[c["payload"]["chat_id"]] = per.get(c["payload"]["chat_id"], 0) + 1
        self.assertEqual(per[CHATS["one_min"]], 1)                                  # others were not re-sent

    def test_ineligible_jobs_cause_no_outbound_messages(self):
        for j in (job("2", "Backend Engineer", SENIOR_DESC), job("3", "Developer", AMBIG_DESC), job("4", "Senior Software Engineer", None)):
            self.run_job(j)
        self.assertEqual(self.tg.calls, [])
        self.assertEqual([self.store.get_job(i)["stage"] for i in "234"], ["excluded", "review", "excluded"])
        self.assertEqual(self.store.get_resume("2"), None)

    def test_ai_outage_never_alerts_and_recovers_later(self):
        state = {"down": True}
        real = DemoLLM()
        llm = ScriptedLLM(lambda s, u: LLMError("HTTP 503") if state["down"] else real.complete_json(s, u))
        pipe, tg, mailer, store, s = make_pipeline(Path(tempfile.mkdtemp()), llm=llm)
        pipe.submit(job("1001")); pipe.wait_idle(30)
        row = store.get_job("1001")
        self.assertEqual((row["stage"], row["attempts"]), ("discovered", 1))
        self.assertIn("unavailable", row["last_error"])
        self.assertEqual(tg.calls, [])
        self.assertEqual(pipe.tick(), 0)                                            # backoff not elapsed: no hammering
        state["down"] = False
        store.update_job("1001", utcnow(), next_attempt_at=iso(utcnow()))
        self.assertEqual(pipe.tick(), 1); pipe.wait_idle(60)
        self.assertEqual(store.get_job("1001")["stage"], "done")

    def test_resume_outage_keeps_alerts_and_retries_without_duplicates(self):
        real = DemoLLM()
        down = {"v": True}

        def fn(system, user):
            if "TAILOR_RESUME" in system and down["v"]:
                return LLMError("HTTP 529")
            return real.complete_json(system, user)
        pipe, tg, mailer, store, s = make_pipeline(Path(tempfile.mkdtemp()), llm=ScriptedLLM(fn))
        pipe.submit(job("1001")); pipe.wait_idle(60)
        self.assertEqual(store.get_job("1001")["stage"], "package_pending")
        self.assertEqual(len(tg.sent("sendMessage")) , 7)                           # alerts went out
        self.assertEqual(tg.sent("sendDocument"), [])                               # no stale/fake resume sent
        down["v"] = False
        store.update_job("1001", utcnow(), next_attempt_at=iso(utcnow()))
        pipe.tick(); pipe.wait_idle(60)
        self.assertEqual(store.get_job("1001")["stage"], "done")
        alert_msgs = [c for c in tg.sent("sendMessage") if c["payload"]["chat_id"] != CONTROL_CHAT]
        self.assertEqual(len(alert_msgs), 7)                                        # alerts not repeated

    def test_ambiguous_resume_allowlist_sends_no_pdf_and_reports_it(self):
        d = dest_data(); d["destinations"] = [x for x in d["destinations"] if x["name"] != "1 min"]
        from app.ai_pipeline.destinations import load_registry
        from tests_ai.helpers import env_for
        reg = load_registry(data=d, env=env_for())
        pipe, tg, mailer, store, s = make_pipeline(Path(tempfile.mkdtemp()), llm=DemoLLM(), registry=reg)
        pipe.submit(job("1001")); pipe.wait_idle(60)
        self.assertEqual(tg.sent("sendDocument"), [])
        row = store.get_job("1001")
        self.assertNotEqual(row["stage"], "done")
        self.assertIn("fail-closed", row["last_error"])
        self.assertGreater(len([c for c in tg.sent("sendMessage")]), 0)             # job alerts still delivered

    def test_no_contact_sources_configured_yields_empty_contacts_not_invented_ones(self):
        from app.ai_pipeline.contacts import SearchProvider
        pipe, tg, mailer, store, s = make_pipeline(Path(tempfile.mkdtemp()), llm=DemoLLM(), search=SearchProvider())
        pipe.submit(job("1001")); pipe.wait_idle(60)
        self.assertEqual(store.get_job("1001")["stage"], "done")
        self.assertEqual(store.query("SELECT * FROM ai_email_drafts"), [])
        summary = tg.sent("sendMessage", CONTROL_CHAT)[-1]["payload"]["text"]
        self.assertIn("not found (left unfilled)", summary)

    def test_daily_referral_cap_skips_contact_search_but_not_the_resume(self):
        pipe, tg, mailer, store, s = make_pipeline(Path(tempfile.mkdtemp()), llm=DemoLLM(), env_extra={"REFERRAL_DAILY_JOB_CAP": "1"})
        pipe.submit(job("1")); pipe.wait_idle(60)
        pipe.submit(job("2")); pipe.wait_idle(60)
        self.assertEqual(len(store.query("SELECT DISTINCT job_id FROM ai_email_drafts")), 1)
        self.assertEqual(store.get_job("2")["stage"], "done")
        self.assertEqual(len(tg.sent("sendDocument")), 8)

    def test_concurrent_submissions_of_the_same_job_are_processed_once(self):
        results = []
        ts = [threading.Thread(target=lambda: results.append(self.pipe.submit(job("1001")))) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.pipe.wait_idle(60)
        self.assertEqual(results.count(True), 1)
        self.assertEqual(len([c for c in self.tg.sent("sendMessage") if c["payload"]["chat_id"] != CONTROL_CHAT]), 7)


class FetchBehaviour(unittest.TestCase):
    class Fetcher:
        def __init__(self, behaviour): self.b, self.calls = behaviour, []
        def fetch(self, job_id):
            self.calls.append(job_id)
            if isinstance(self.b, Exception): raise self.b
            return self.b

    def pipe(self, behaviour):
        f = self.Fetcher(behaviour)
        return make_pipeline(Path(tempfile.mkdtemp()), llm=ScriptedLLM(lambda s, u: LLMError("should not be called")), fetcher=f) + (f,)

    def test_senior_title_still_fetches_the_full_description_for_ai(self):
        pipe, tg, m, store, s, f = self.pipe(("x" * 120, True))
        pipe.submit(job("9", "Senior Software Engineer", None)); pipe.wait_idle(10)
        self.assertEqual(f.calls, ["9"])
        self.assertEqual(store.get_job("9")["stage"], "discovered")

    def test_rate_limited_fetch_defers_the_job_without_alerting(self):
        pipe, tg, m, store, s, f = self.pipe(FetchRateLimited(120))
        pipe.submit(job("9", desc=None)); pipe.wait_idle(10)
        row = store.get_job("9")
        self.assertEqual((row["stage"], row["attempts"]), ("discovered", 1))
        self.assertIn("rate-limited", row["last_error"])
        self.assertEqual(tg.calls, [])

    def test_truncated_description_is_never_classified_as_verified(self):
        pipe, tg, m, store, s, f = self.pipe(("Software Engineer I. 0-2 years of experience. We are hiring and the role involves... Show more", False))
        pipe.submit(job("9", desc=None)); pipe.wait_idle(10)
        self.assertEqual(store.get_job("9")["stage"], "discovered")
        self.assertEqual(tg.calls, [])

    def test_exhausted_attempts_with_incomplete_description_end_in_review_not_alert(self):
        pipe, tg, m, store, s, f = self.pipe(("short", False))
        pipe.submit(job("9", desc=None)); pipe.wait_idle(10)
        store.update_job("9", utcnow(), attempts=s.max_job_attempts - 1, next_attempt_at=None)
        pipe.process("9")
        self.assertEqual(store.get_job("9")["stage"], "review")
        self.assertEqual(tg.calls, [])

    def test_hard_fetch_error_is_recorded(self):
        pipe, tg, m, store, s, f = self.pipe(FetchError("HTTP 404", retryable=False))
        pipe.submit(job("9", desc=None)); pipe.wait_idle(10)
        self.assertIn("HTTP 404", store.get_job("9")["last_error"])
        self.assertEqual(store.get_job("9")["stage"], "review")
        self.assertEqual(store.get_job("9")["attempts"], 0)

    def test_non_retryable_bad_request_moves_job_to_review_without_retries(self):
        pipe, tg, m, store, s, f = self.pipe(FetchError("HTTP 400", retryable=False))
        pipe.submit(job("10", desc=None)); pipe.wait_idle(10)
        row = store.get_job("10")
        self.assertEqual(row["stage"], "review")
        self.assertEqual(row["attempts"], 0)
        self.assertIn("HTTP 400", row["last_error"])
        self.assertEqual(tg.calls, [])


class DescriptionFetcherTests(unittest.TestCase):
    HTML = ('<html><div class="show-more-less-html__markup"><p>Requirements:</p><ul><li>0-2 years of experience</li>'
            '<li>Python &amp; SQL</li></ul><br>Freshers welcome.</div></html>')

    def test_html_is_converted_to_readable_text(self):
        t = extract_description_from_html(self.HTML)
        self.assertIn("- 0-2 years of experience", t)
        self.assertIn("Python & SQL", t)

    def test_completeness_rules(self):
        self.assertFalse(is_complete("short", 100))
        self.assertFalse(is_complete("x" * 200 + " Show more", 100))
        self.assertTrue(is_complete("x" * 200, 100))

    def test_rate_limit_starts_a_shared_escalating_cooldown(self):
        now = [0.0]
        calls = []

        def get(url):
            calls.append(url)
            raise FetchRateLimited(0)
        f = DescriptionFetcher(get, min_chars=10, clock=lambda: now[0])
        with self.assertRaises(FetchRateLimited): f.fetch("1")
        with self.assertRaises(FetchRateLimited): f.fetch("2")                      # still cooling: no request made
        self.assertEqual(len(calls), 1)
        now[0] = 61
        with self.assertRaises(FetchRateLimited): f.fetch("3")
        self.assertEqual(len(calls), 2)
        self.assertGreaterEqual(f._cooldown_until - now[0], 120)                    # escalated


class HookAndLatency(unittest.TestCase):
    def test_submit_does_not_block_on_a_slow_ai(self):
        slow = ScriptedLLM(lambda s, u: (time.sleep(0.4), LLMError("late"))[1])
        pipe, *_ = make_pipeline(Path(tempfile.mkdtemp()), llm=slow)
        t0 = time.monotonic()
        pipe.submit(job("1001"))
        self.assertLess(time.monotonic() - t0, 0.2)
        pipe.wait_idle(10)

    def test_on_new_job_rejects_garbage_and_dry_run_never_touches_the_network(self):
        self.assertFalse(hook.on_new_job(job_id=None, title="x", company="y", url="https://example.com/no-id"))
        tmp = Path(tempfile.mkdtemp())
        from tests_ai.helpers import env_for, make_workspace
        make_workspace(tmp)
        pipe = hook.build(env=env_for({"AI_DRY_RUN": "1"}), base_dir=tmp, llm=ScriptedLLM(lambda s, u: LLMError("x")))
        self.assertIsInstance(pipe.deliverer.tg, RecordingTelegram)
        self.assertIsInstance(pipe.approvals.mailer, RecordingMailer)


if __name__ == "__main__":
    unittest.main()
