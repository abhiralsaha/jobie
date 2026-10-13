import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.ai_pipeline import resume as R
from app.ai_pipeline.fakes import DemoLLM, ScriptedLLM
from app.ai_pipeline.llm import LLMError
from app.ai_pipeline.models import Job
from app.ai_pipeline.util import sha256_file
from tests_ai.helpers import GOOD_DESC, PROFILE, make_pipeline

HAVE_TEX = bool(shutil.which("latexmk") or shutil.which("pdflatex"))
JOB = Job("111", "Software Engineer I", "Acme Technologies", "Gurugram", "https://example.com/jobs/111", None, GOOD_DESC)


def tailor_response(**over):
    base = {"summary": PROFILE["summary"], "skills": PROFILE["skills"], "experience": [],
            "projects": [{"id": "p2", "bullets": [{"fact_id": "p2.b1", "text": PROFILE["projects"][1]["bullets"][0]["text"]}]},
                         {"id": "p1", "bullets": [{"fact_id": "p1.b1", "text": PROFILE["projects"][0]["bullets"][0]["text"]}]}],
            "jd_keywords_matched": [], "jd_keywords_missing": ["Kubernetes"]}
    base.update(over)
    return base


class PureTests(unittest.TestCase):
    def setUp(self):
        d = Path(tempfile.mkdtemp()); (d / "p.json").write_text(json.dumps(PROFILE))
        self.profile, self.lex = R.Profile.load(d / "p.json"), R.load_lexicon()

    def v(self, out):
        return R.validate_tailoring(out, self.profile, GOOD_DESC, self.lex, 3)

    def test_latex_escape(self):
        self.assertEqual(R.latex_escape("R&D 100% #1 a_b {x} ~ ^ \\"), r"R\&D 100\% \#1 a\_b \{x\} \textasciitilde{} \textasciicircum{} \textbackslash{}")

    def test_invented_skills_and_categories_are_dropped(self):
        t = self.v(tailor_response(skills={"Languages": ["Python", "Kubernetes", "COBOL"], "Cloud": ["AWS"]}))
        flat = [s for v in t.skills.values() for s in v]
        self.assertNotIn("Kubernetes", flat); self.assertNotIn("COBOL", flat); self.assertNotIn("AWS", flat)
        self.assertEqual(t.skills["Languages"][0], "Python")
        self.assertTrue(any("not in profile" in n for n in t.notes))

    def test_rewrite_with_new_number_or_technology_falls_back_to_original(self):
        proj = lambda text: tailor_response(projects=[{"id": "p1", "bullets": [{"fact_id": "p1.b2", "text": text}]}])  # noqa: E731
        orig = "Added pytest tests for the core endpoints."
        self.assertEqual(self.v(proj("Added pytest tests, raising coverage to 95%."))  .projects[0]["bullets"][0]["text"], orig)
        self.assertEqual(self.v(proj("Added pytest tests and deployed on Kubernetes."))  .projects[0]["bullets"][0]["text"], orig)
        ok = "Wrote pytest tests covering the core endpoints."
        self.assertEqual(self.v(proj(ok)).projects[0]["bullets"][0]["text"], ok)           # legitimate rephrase accepted

    def test_unknown_or_mismatched_fact_ids_are_dropped(self):
        t = self.v(tailor_response(projects=[{"id": "p1", "bullets": [{"fact_id": "p2.b1", "text": "x" * 40}, {"fact_id": "zz", "text": "y" * 40}]},
                                             {"id": "ghost", "bullets": []}]))
        self.assertEqual([p["id"] for p in t.projects], ["p1", "p2"])                       # AI selection unusable -> bank defaults
        self.assertTrue(any("mismatched" in n for n in t.notes))

    def test_summary_cannot_invent_experience_numbers_or_technologies(self):
        for bad in ("Engineer with 5 years of experience in Python.", "Graduate skilled in Python and Kubernetes.", "Built 12 production systems."):
            self.assertEqual(self.v(tailor_response(summary=bad)).summary, PROFILE["summary"], bad)
        good = "Computer science graduate with project experience building Python backend services and APIs."
        self.assertEqual(self.v(tailor_response(summary=good)).summary, good)

    def test_render_replaces_only_marked_regions(self):
        tpl = (Path(__file__).parent.parent / "templates" / "resume_template.tex").read_text()
        out = R.render_template(tpl, R.render_sections(self.v(tailor_response()), self.profile))
        self.assertIn("Computer science graduate", out)
        self.assertIn(r"\section{Education}", out)
        self.assertNotIn("Placeholder", out)
        self.assertIn(r"\& orders (100\% test pass)", out)
        with self.assertRaises(R.ResumeError):
            R.render_template("\\begin{document}no markers\\end{document}", {})

    def test_placeholder_profile_is_refused(self):
        d = Path(tempfile.mkdtemp()); (d / "p.json").write_text(json.dumps({**PROFILE, "candidate": {"name": "Your Name"}}))
        with self.assertRaises(R.ResumeError):
            R.Profile.load(d / "p.json")

    def test_pdf_validation_flags_overflow_in_log(self):
        pdf = Path(tempfile.mkdtemp()) / "x.pdf"; pdf.write_bytes(b"short")
        self.assertIn("PDF is missing or implausibly small", R.validate_pdf(pdf, self.profile, R.default_tailoring(self.profile, 3), "", 1)[0])


@unittest.skipUnless(HAVE_TEX, "no LaTeX compiler installed")
class CompileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def build(self, llm, **kw):
        pipe, tg, mailer, store, s = make_pipeline(self.tmp, llm=llm, **kw)
        return pipe, pipe.resume_svc.build(JOB, GOOD_DESC)

    def test_tailored_latex_compiles_to_a_valid_associated_pdf(self):
        template = self.tmp / "templates" / "resume_template.tex"
        pipe, res = self.build(DemoLLM())
        before = sha256_file(template)
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.filename, "Acme_Technologies_Software_Engineer_I_111.pdf")
        self.assertTrue(Path(res.tex_path).exists() and res.tex_path.endswith("_111.tex"))
        pages, text = R.inspect_pdf(Path(res.pdf_path))
        self.assertEqual(pages, 1)
        flat = " ".join(text.split())
        for needle in ("Python", "Inventory API", "Education", "Technical Skills"):
            self.assertIn(needle, flat)
        self.assertNotIn("\ufffd", text)
        row = pipe.store.get_resume("111")                                               # tied to the job in the DB
        self.assertEqual((row["status"], row["pdf_sha256"]), ("ok", sha256_file(res.pdf_path)))
        self.assertEqual(sha256_file(template), before)                                 # original template untouched
        self.assertIn("% JOBIE:BEGIN SKILLS", template.read_text())                      # and still has its placeholders

    def test_unsupported_skills_never_reach_the_pdf(self):
        llm = ScriptedLLM(lambda s, u: tailor_response(skills={"Languages": ["Python", "Kubernetes"], "Tools": ["Terraform", "Git"]},
                                                       summary="Graduate with 7 years of experience in Kubernetes."))
        _, res = self.build(llm)
        self.assertTrue(res.ok, res.error)
        _, text = R.inspect_pdf(Path(res.pdf_path))
        for banned in ("Kubernetes", "Terraform", "7 years"):
            self.assertNotIn(banned, text)

    def test_second_build_reuses_the_validated_file_without_calling_the_ai(self):
        llm = ScriptedLLM(lambda s, u: tailor_response())
        pipe, res = self.build(llm)
        n = len(llm.calls)
        again = pipe.resume_svc.build(JOB, GOOD_DESC)
        self.assertTrue(again.reused)
        self.assertEqual(len(llm.calls), n)

    def test_compile_failure_falls_back_to_profile_defaults(self):
        real = R.compile_latex
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            return R.CompileResult(False, error="LaTeX error: Undefined control sequence") if len(calls) == 1 else real(*a, **k)
        with mock.patch.object(R, "compile_latex", flaky):
            _, res = self.build(DemoLLM())
        self.assertTrue(res.ok)
        self.assertTrue(any("fell back" in n for n in res.notes))

    def test_failed_validation_is_reported_never_delivered(self):
        with mock.patch.object(R, "validate_pdf", return_value=["page count 3 outside 1..1 (content overflow)"]):
            _, res = self.build(DemoLLM())
        self.assertFalse(res.ok)
        self.assertIn("overflow", res.error)
        self.assertEqual(list((self.tmp / "resumes").glob("*.pdf")) if (self.tmp / "resumes").exists() else [], [])

    def test_ai_outage_raises_unavailable(self):
        with self.assertRaises(R.ResumeUnavailable):
            self.build(ScriptedLLM(lambda s, u: LLMError("HTTP 503")))

    def test_page_limit_is_enforced(self):
        pipe, res = self.build(DemoLLM())
        profile = R.Profile.load(self.tmp / "config" / "resume_profile.json")
        problems = R.validate_pdf(Path(res.pdf_path), profile, R.default_tailoring(profile, 3), "", 0)
        self.assertTrue(any("page count" in p for p in problems))
        big = "Overfull \\hbox (42.5pt too wide) in paragraph"
        self.assertTrue(any("overflow" in p for p in R.validate_pdf(Path(res.pdf_path), profile, R.default_tailoring(profile, 3), big, 1)))


if __name__ == "__main__":
    unittest.main()
