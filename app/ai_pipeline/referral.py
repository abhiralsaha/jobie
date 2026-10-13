"""Personalised referral e-mail drafts from the user's own template.

The AI adapts the template per recipient; deterministic validation then checks
the result (job link present, greeting, recipient/company named, no leftover
placeholders, no numbers or technologies that are not in the user's profile).
If the AI is unavailable or fails validation, a plain template fill is used, so
a safe draft always exists. Drafts are NOT sent: approval is a separate step.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .contacts import SLOT_LABEL, Contact
from .llm import LLMError
from .models import Job
from .resume import Profile, load_lexicon, numbers_in, terms_in
from .util import clean_text, get_logger, sha256_hex

log = get_logger("referral")

EMAIL_SYSTEM = """TASK: REFERRAL_EMAIL
You personalise the candidate's referral-request e-mail template for ONE recipient.
Rules:
- Keep the intent, tone and structure of the template. Address the recipient by first name if known.
- Mention the recipient's company and the exact job title, and include the job link verbatim.
- Adapt the ask to the recipient's role: recruiter -> ask how to be considered / whether they can route the
  application; engineers -> politely ask whether they would be open to referring or sharing advice.
- Mention only candidate facts present in CANDIDATE FACTS. Do not add numbers, employers, skills or achievements.
- Say that the tailored resume is attached. Max 170 words in the body. Plain text, no markdown.
- Never promise anything on the recipient's behalf. Reply with ONE JSON object: {"subject": str, "body": str}"""

PLACEHOLDER_RE = re.compile(r"\{\{\s*\w+\s*\}\}")


@dataclass
class EmailDraft:
    draft_id: str
    job_id: str
    slot: str
    to_email: str
    to_name: str
    subject: str
    body: str
    resume_path: str
    resume_sha256: str
    content_hash: str
    sendable: bool
    block_reason: str = ""


def content_hash(job_id: str, to_email: str, subject: str, body: str, resume_sha: str) -> str:
    return sha256_hex(json.dumps([job_id, to_email.lower(), subject, body, resume_sha], ensure_ascii=False))


def parse_template(text: str) -> tuple[str, str]:
    m = re.match(r"\s*Subject:\s*(.+?)\r?\n(.*)$", text, re.S)
    if not m:
        raise ValueError("referral template must start with a 'Subject: ...' line")
    return m.group(1).strip(), m.group(2).strip()


def fill(tpl: str, values: dict) -> str:
    return PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(0).strip("{} ").strip(), m.group(0))), tpl)


def template_values(job: Job, c: Contact, profile: Profile) -> dict:
    first = c.name.split()[0] if c.name else ""
    cand = profile.raw.get("candidate", {})
    return {"recipient_name": c.name, "recipient_first_name": first, "recipient_role": c.position or SLOT_LABEL[c.slot],
            "company": job.company, "job_title": job.title, "job_url": job.url, "candidate_name": profile.name,
            "candidate_summary": profile.summary, "candidate_email": cand.get("email", ""),
            "candidate_phone": cand.get("phone", ""), "candidate_linkedin": cand.get("linkedin", "")}


def validate_draft(subject: str, body: str, job: Job, c: Contact, profile: Profile, lexicon: list[str]) -> list[str]:
    errs = []
    if not subject.strip() or len(subject) > 150 or "\n" in subject:
        errs.append("subject empty/too long/multi-line")
    if len(body) < 80 or len(body) > 2000:
        errs.append("body length out of range")
    if PLACEHOLDER_RE.search(subject + body):
        errs.append("unfilled {{placeholder}} remains")
    if job.url and job.url not in body:
        errs.append("job link missing from body")
    if job.company.lower() not in (subject + " " + body).lower():
        errs.append("company not mentioned")
    if job.title.lower() not in (subject + " " + body).lower():
        errs.append("job title not mentioned")
    if c.name and c.name.split()[0].lower() not in body.lower()[:200]:
        errs.append("recipient first name missing from the greeting")
    if profile.name.split()[0].lower() not in body.lower()[-300:]:
        errs.append("candidate name missing from the sign-off")
    if "attach" not in body.lower():
        errs.append("body does not mention the attached resume")
    allowed_nums = numbers_in(profile.raw.get("candidate_facts_text", "") + profile.summary + json.dumps(profile.raw.get("candidate", {})) +
                              " ".join(b["text"] for x in profile.experience + profile.projects for b in x.get("bullets", [])) +
                              job.url + job.title + job.company + job.job_id)
    if numbers_in(body) - allowed_nums:
        errs.append("body contains numbers not found in the profile or job")
    known = terms_in(json.dumps(profile.raw, ensure_ascii=False), lexicon) | terms_in(job.title, lexicon)
    if terms_in(body, lexicon) - known:
        errs.append("body mentions technologies not in the profile")
    return errs


class ReferralService:
    def __init__(self, settings, llm, base_dir: str | Path | None = None):
        self.s, self.llm = settings, llm
        self.base = Path(base_dir) if base_dir else Path.cwd()
        self.lexicon = load_lexicon()

    def _template(self) -> tuple[str, str]:
        p = self.s.path(self.s.referral_template_file, self.base)
        if not p.exists():
            raise FileNotFoundError(f"referral template not found: {p} (copy config/referral_template.example.txt)")
        return parse_template(p.read_text(encoding="utf-8"))

    def draft_for(self, job: Job, c: Contact, profile: Profile, resume: dict) -> EmailDraft:
        subj_t, body_t = self._template()
        vals = template_values(job, c, profile)
        subject, body = fill(subj_t, vals), fill(body_t, vals)
        origin = "template"
        facts = {"summary": profile.summary, "skills": profile.skills,
                 "projects": [{"name": x.get("name"), "tech": x.get("tech"), "bullets": [b["text"] for b in x.get("bullets", [])]} for x in profile.projects],
                 "experience": [{"role": x.get("role"), "org": x.get("org"), "bullets": [b["text"] for b in x.get("bullets", [])]} for x in profile.experience]}
        user = (f"TEMPLATE SUBJECT: {subj_t}\nTEMPLATE BODY:\n{body_t}\n\nRECIPIENT: {c.name}, {c.position} at {c.company} "
                f"({SLOT_LABEL[c.slot]})\nJOB: {job.title} at {job.company}\nJOB LINK: {job.url}\n"
                f"CANDIDATE NAME: {profile.name}\nCANDIDATE FACTS: {json.dumps(facts, ensure_ascii=False)}\n"
                f"PLACEHOLDER VALUES (use these literally): {json.dumps(vals, ensure_ascii=False)}")
        try:
            out = self.llm.complete_json(EMAIL_SYSTEM, user, max_tokens=800)
            s2, b2 = clean_text(str(out.get("subject", "")), 200), str(out.get("body", "")).strip()
            errs = validate_draft(s2, b2, job, c, profile, self.lexicon)
            if errs:
                log.warning("AI draft for %s rejected (%s); using plain template", c.slot, errs)
            else:
                subject, body, origin = s2, b2, "ai"
        except LLMError as exc:
            log.warning("AI personalisation unavailable (%s); using plain template", exc)
        errs = validate_draft(subject, body, job, c, profile, self.lexicon)
        if errs and origin == "template":
            raise ValueError(f"template-based draft for {c.slot} is invalid: {errs} (check config/referral_template.txt)")
        sendable, why = True, ""
        if not c.email:
            sendable, why = False, "no email address found"
        elif c.email_status not in self.s.sendable_email_statuses:
            sendable, why = False, f"email status is '{c.email_status}', not one of {list(self.s.sendable_email_statuses)}"
        elif not resume.get("sha256"):
            sendable, why = False, "no validated resume"
        ch = content_hash(job.job_id, c.email, subject, body, resume.get("sha256", ""))
        draft_id = sha256_hex(f"{job.job_id}|{c.slot}|{ch}")[:16]
        return EmailDraft(draft_id, job.job_id, c.slot, c.email, c.name, subject, body, resume.get("pdf_path", ""),
                          resume.get("sha256", ""), ch, sendable, why)
