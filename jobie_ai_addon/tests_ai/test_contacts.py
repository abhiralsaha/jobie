import tempfile
import unittest
from pathlib import Path

from app.ai_pipeline.contacts import (ContactService, SearchProvider, classify_role, evaluate_candidate, mentions_company,
                                      parse_linkedin_result)
from app.ai_pipeline.fakes import StaticHunter, StaticSearch
from app.ai_pipeline.settings import Settings
from app.ai_pipeline.store import Store
from tests_ai.helpers import SEARCH_RESULTS, env_for, result

CO = "Acme Technologies Pvt Ltd"


def svc(search=None, hunter="default", **env):
    s = Settings.from_env(env_for(env))
    h = StaticHunter("acme.example", "valid") if hunter == "default" else hunter
    return ContactService(s, Store(None, ":memory:"), search if search is not None else StaticSearch(SEARCH_RESULTS), h,
                          base_dir=Path(tempfile.mkdtemp())), h


class ParsingTests(unittest.TestCase):
    def test_role_classification(self):
        for text, slot in (("Senior Software Engineer", "senior_swe"), ("Staff Software Engineer", "senior_swe"), ("Software Engineer II", "swe"),
                           ("Software Engineer", "swe"), ("Talent Acquisition Partner", "hr"), ("Technical Recruiter", "hr"),
                           ("Engineering Manager", None), ("Software Engineering Intern", None), ("Sales Executive", None)):
            self.assertEqual(classify_role(text), slot, text)

    def test_only_linkedin_profile_results_are_people(self):
        r = result("Riya Sharma", "Senior Software Engineer", "https://in.linkedin.com/in/riya-1")
        self.assertEqual(parse_linkedin_result(r)[0], "Riya Sharma")
        self.assertIsNone(parse_linkedin_result({**r, "url": "https://www.linkedin.com/company/acme"}))
        self.assertIsNone(parse_linkedin_result({**r, "url": "https://example.com/in/riya"}))
        self.assertIsNone(parse_linkedin_result({"title": "Acme careers | LinkedIn", "url": "https://linkedin.com/in/x", "snippet": ""}))

    def test_company_matching_ignores_legal_suffixes(self):
        self.assertTrue(mentions_company("Senior Engineer at Acme Technologies", CO))
        self.assertFalse(mentions_company("Senior Engineer at Globex", CO))

    def test_former_employees_are_rejected(self):
        r = result("Riya Sharma", "Senior Software Engineer", "https://in.linkedin.com/in/riya-1",
                   snippet="Former Senior Software Engineer at Acme Technologies. Now at Globex.")
        self.assertIsNone(evaluate_candidate(r, CO, "senior_swe"))
        ok = result("Riya Sharma", "Senior Software Engineer", "https://in.linkedin.com/in/riya-1")
        self.assertIsNotNone(evaluate_candidate(ok, CO, "senior_swe"))

    def test_wrong_slot_or_company_is_rejected(self):
        r = result("Riya Sharma", "Senior Software Engineer", "https://in.linkedin.com/in/riya-1")
        self.assertIsNone(evaluate_candidate(r, CO, "swe"))
        self.assertIsNone(evaluate_candidate(r, "Globex Corp", "senior_swe"))


class DiscoveryTests(unittest.TestCase):
    def test_three_contacts_with_evidence_sources_and_verified_emails(self):
        s, h = svc()
        found, problems = s.find("1", CO)
        self.assertEqual(set(found), {"hr", "senior_swe", "swe"})
        self.assertEqual(problems, [])
        for c in found.values():
            self.assertEqual(c.email_status, "verified_service")
            self.assertTrue(c.email.endswith("@acme.example"))
            self.assertTrue(c.source_urls and c.affiliation_evidence and c.profile_url.startswith("https://"))
            self.assertIn("not independently confirmed", c.affiliation_strength)
            self.assertTrue(c.reason)

    def test_nothing_is_fabricated_when_search_finds_nothing(self):
        s, h = svc(search=StaticSearch({}))
        found, problems = s.find("1", CO)
        self.assertEqual(found, {})
        self.assertEqual(len(problems), 3)
        self.assertEqual(h.calls, [])                              # no email lookups for people who do not exist

    def test_no_provider_configured_is_reported(self):
        s, _ = svc(search=SearchProvider())
        found, problems = s.find("1", CO)
        self.assertEqual(found, {})
        self.assertIn("no contact search provider", problems[0])

    def test_unfilled_roles_are_left_unfilled(self):
        s, _ = svc(search=StaticSearch({"hr": SEARCH_RESULTS["hr"]}))
        found, problems = s.find("1", CO)
        self.assertEqual(list(found), ["hr"])
        self.assertTrue(any("Senior Software Engineer" in p for p in problems))

    def test_same_person_is_not_used_for_two_slots(self):
        one = result("Neha Gupta", "Software Engineer", "https://in.linkedin.com/in/neha-gupta-3")
        s, _ = svc(search=StaticSearch({"swe": [one], "senior_swe": [one]}))
        found, _ = s.find("1", CO)
        self.assertEqual([c.profile_url for c in found.values()].count(one["url"]), 1)

    def test_search_failure_is_reported_not_swallowed(self):
        class Boom(SearchProvider):
            name = "boom"
            def search(self, q): raise RuntimeError("HTTP 500")
        found, problems = svc(search=Boom())[0].find("1", CO)
        self.assertEqual(found, {})
        self.assertTrue(any("search failed" in p for p in problems))


class EmailStatusTests(unittest.TestCase):
    def status(self, hunter_status):
        s, _ = svc(hunter=StaticHunter("acme.example", hunter_status))
        return s.find("1", CO)[0]["swe"]

    def test_statuses_are_reported_honestly(self):
        self.assertEqual(self.status("valid").email_status, "verified_service")
        self.assertEqual(self.status("accept_all").email_status, "accept_all_unverified")
        self.assertEqual(self.status("unknown").email_status, "unverified")
        inv = self.status("invalid")
        self.assertEqual((inv.email, inv.email_status), ("", "none"))

    def test_without_a_verification_service_no_address_is_guessed(self):
        s, _ = svc(hunter=None)
        c = s.find("1", CO)[0]["swe"]
        self.assertEqual((c.email, c.email_status), ("", "none"))

    def test_domain_mismatch_from_service_is_discarded(self):
        class Wrong(StaticHunter):
            def find_email(self, d, f, l): return {"email": "x@other.com", "verification": {"status": "valid"}}
        s, _ = svc(hunter=Wrong("acme.example"))
        self.assertEqual(s.find("1", CO)[0]["swe"].email_status, "none")

    def test_public_email_next_to_the_name_counts_as_public_evidence(self):
        r = result("Neha Gupta", "Software Engineer", "https://in.linkedin.com/in/neha-gupta-3",
                   snippet="Neha Gupta, Software Engineer at Acme Technologies. Contact: neha.gupta@acme.example")
        s, h = svc(search=StaticSearch({"swe": [r]}))
        c = s.find("1", CO)[0]["swe"]
        self.assertEqual((c.email, c.email_status), ("neha.gupta@acme.example", "public_evidence"))
        self.assertEqual(h.calls, ["domain"])                       # no finder call needed

    def test_quotas_are_enforced(self):
        s, h = svc(SEARCH_DAILY_CAP="1", HUNTER_DAILY_CAP="2")
        found, problems = s.find("1", CO)
        self.assertEqual(list(found), ["hr"])
        self.assertTrue(any("daily search quota" in p for p in problems))
        self.assertLessEqual(len(h.calls), 2)


if __name__ == "__main__":
    unittest.main()
