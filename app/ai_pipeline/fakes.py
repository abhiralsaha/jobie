"""Offline stand-ins used by the tests, the dry-run mode and ``cli demo``.

Nothing here talks to the network. ``RecordingTelegram`` overrides only the
lowest-level ``call`` so the real routing / allow-list / splitting code runs.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from .classifier import FRESHER_RE, extract_experience_statements
from .contacts import SearchProvider
from .llm import LLMError
from .mailer import ACCEPTED, SendOutcome
from .telegram import TelegramClient, TelegramError


class RecordingTelegram(TelegramClient):
    """Records every API call; can be told to fail for given chat ids."""

    def __init__(self, fail: dict | None = None):
        super().__init__()
        self.calls: list[dict] = []
        self.fail = fail or {}                 # chat_id(str) -> TelegramError
        self.updates: list[dict] = []
        self._n = 1000

    def call(self, token, method, payload=None, files=None, timeout=None):
        payload = dict(payload or {})
        self.calls.append({"token_id": token.split(":")[0], "method": method, "payload": payload,
                           "file": files["document"][1] if files else None})
        chat = str(payload.get("chat_id", ""))
        if method in {"sendMessage", "sendDocument"} and chat in self.fail:
            raise self.fail[chat]
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        self._n += 1
        return {"message_id": self._n} if method.startswith("send") else True

    def sent(self, method: str | None = None, chat_id: str | None = None) -> list[dict]:
        return [c for c in self.calls if (method is None or c["method"] == method)
                and (chat_id is None or str(c["payload"].get("chat_id")) == str(chat_id))]


class RecordingMailer:
    def __init__(self, outcome: SendOutcome | Callable | None = None):
        self.sent: list[dict] = []
        self.outcome = outcome

    def send(self, to_email, to_name, subject, body, attachment):
        self.sent.append({"to": to_email, "name": to_name, "subject": subject, "body": body, "attachment": str(attachment)})
        if callable(self.outcome):
            return self.outcome()
        return self.outcome or SendOutcome(ACCEPTED, "accepted (recording mailer)", "<dryrun@local>")


class ScriptedLLM:
    """fn(system, user) -> dict | str. Raise LLMError to simulate an outage."""

    def __init__(self, fn: Callable[[str, str], object]):
        self.fn, self.calls = fn, []

    def complete_json(self, system: str, user: str, max_tokens: int = 0) -> dict:
        self.calls.append((system, user))
        out = self.fn(system, user)
        if isinstance(out, Exception):
            raise out
        if isinstance(out, str):
            from .llm import extract_json

            return extract_json(out)
        return out


class DemoLLM:
    """Rule-based stand-in that follows the real prompts closely enough to exercise every validator."""

    def __init__(self):
        self.calls = {"CLASSIFY_JOB": 0, "TAILOR_RESUME": 0, "REFERRAL_EMAIL": 0}

    def complete_json(self, system: str, user: str, max_tokens: int = 0) -> dict:
        task = system.split("\n", 1)[0].replace("TASK:", "").strip()
        self.calls[task] = self.calls.get(task, 0) + 1
        return getattr(self, "_" + task.lower())(user)

    def _classify_job(self, user: str) -> dict:
        jid = re.search(r"^job_id: (.+)$", user, re.M).group(1).strip()
        title = re.search(r"^title: (.*)$", user, re.M).group(1)
        company = re.search(r"^company: (.*)$", user, re.M).group(1)
        desc = user.split("<description>\n", 1)[1].rsplit("\n</description>", 1)[0]
        stmts = extract_experience_statements(desc)
        fm = FRESHER_RE.search(desc)
        evidence, mn, mx = [], None, None
        if stmts:
            st = min(stmts, key=lambda s: s.min_years)
            mn, mx = st.min_years, st.max_years
            m = re.search(re.escape(st.text).replace(r"\ ", r"\s*"), desc, re.I)
            evidence = [m.group(0)] if m else []
        if fm:
            evidence.append(fm.group(0))
            mn = 0 if mn is None else mn
        if mn is None:
            decision, reason = "review", "no experience requirement stated in the description"
        elif mn > 2 or (mx is not None and mx > 2):
            decision, reason = "exclude", f"requires {mn:g}+ years"
        else:
            decision, reason = "include", "description supports 0-2 years of experience"
        techs = [t for t in ("Python", "Java", "Node.js", "TypeScript", "SQL", "Docker", "AWS", "React") if t.lower() in desc.lower()]
        return {"job_id": jid, "title": title, "company": company, "description_fully_retrieved": True,
                "experience_min_years": mn, "experience_max_years": mx, "experience_mandatory": None if mn is None else mn > 0,
                "fresher_eligible": decision == "include", "requirements_evidence": evidence, "decision": decision,
                "reason": reason, "summary": desc[:380].replace("\n", " "), "key_technologies": techs}

    def _tailor_resume(self, user: str) -> dict:
        bank = json.loads(user.split("FACT BANK (the only source of truth):\n", 1)[1])
        jd = user.lower()
        proj = sorted(bank["projects"], key=lambda p: -sum(t.lower() in jd for t in p.get("tech", [])))
        return {"summary": bank["summary"], "skills": bank["skills"], "experience": [],
                "projects": [{"id": p["id"], "bullets": [{"fact_id": b["id"], "text": b["text"]} for b in p["bullets"][:3]]} for p in proj],
                "jd_keywords_matched": [], "jd_keywords_missing": []}

    def _referral_email(self, user: str) -> dict:
        subj = re.search(r"TEMPLATE SUBJECT: (.*)", user).group(1)
        body = user.split("TEMPLATE BODY:\n", 1)[1].split("\n\nRECIPIENT:", 1)[0]
        vals = json.loads(re.search(r"PLACEHOLDER VALUES \(use these literally\): (.*)", user).group(1))
        role = re.search(r"\((HR / Recruiter|Senior Software Engineer|Software Engineer)\)", user)
        sub = lambda t: re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(vals.get(m.group(1), m.group(0))), t)  # noqa: E731
        extra = {"HR / Recruiter": "Could you let me know how I can best be considered for this opening?",
                 "Senior Software Engineer": "I would value any guidance, and a referral if you feel comfortable.",
                 "Software Engineer": "If you are open to it, I would be grateful for a referral or a few pointers."}[role.group(1) if role else "Software Engineer"]
        return {"subject": sub(subj), "body": sub(body).replace("Thank you", extra + "\n\nThank you", 1)}


class StaticSearch(SearchProvider):
    name = "static"

    def __init__(self, results_by_slot: dict[str, list[dict]]):
        self.by_slot, self.queries = results_by_slot, []

    def search(self, query: str) -> list[dict]:
        self.queries.append(query)
        if "recruiter" in query:
            return self.by_slot.get("hr", [])
        if '"Senior Software Engineer"' in query:
            return self.by_slot.get("senior_swe", [])
        return self.by_slot.get("swe", [])


class StaticHunter:
    def __init__(self, domain: str = "acme.example", status: str = "valid"):
        self.domain, self.status, self.calls = domain, status, []

    def domain_for_company(self, company: str):
        self.calls.append("domain")
        return self.domain

    def find_email(self, domain, first, last):
        self.calls.append("find")
        return {"email": f"{first}.{last}@{domain}".lower(), "verification": {"status": self.status}}

    def verify(self, email):
        self.calls.append("verify")
        return {"status": self.status}
