"""Job-alert and resume-PDF delivery with per-destination idempotency.

Routing rules (enforced twice: here and in ``TelegramClient.send_document``):
  * eligible job alerts   -> every destination flagged ``job_alerts``
  * resume PDF + job info -> ONLY the three allow-listed resume destinations
Each (job, kind, destination) is claimed in the DB before sending, so retries
and restarts never double-send to a destination that already succeeded.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path

from .classifier import Classification
from .destinations import DestinationRegistry, AllowlistError
from .freshness import format_relative, parse_posted_at
from .models import Job
from .store import Store
from .telegram import TelegramClient, TelegramError, explain, split_escaped
from .util import get_logger, sha256_file, utcnow

log = get_logger("delivery")


@dataclass
class DeliveryReport:
    sent: list[str] = field(default_factory=list)
    already_sent: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not self.failed


def format_alert_blocks(job: Job, c: Classification, now) -> list[str]:
    e = html.escape
    posted = parse_posted_at(job.posted_at, now)
    posted_txt = f"{posted.strftime('%Y-%m-%d %H:%M UTC')} ({format_relative(posted, now)})" if posted else "not verified"
    head = [f"<b>{e(job.title)}</b>", f"\U0001F3E2 {e(job.company)}", f"\U0001F4CD {e(job.location or 'not stated')}",
            f"\U0001F9D1\u200D\U0001F4BB Experience required: {e(c.experience_label())}",
            f"\U0001F552 Posted: {e(posted_txt)}"]
    if c.technologies:
        head.append("\U0001F6E0 " + e(", ".join(c.technologies)))
    head.append(f"\u2705 Eligible: {e(c.reason)}")
    head.append(f'\U0001F517 <a href="{e(job.url, quote=True)}">Open posting</a>' if job.url else "\U0001F517 (no URL)")
    head.append(f"ID: <code>{e(job.job_id)}</code>")
    blocks = ["\n".join(head)]
    for i, chunk in enumerate(split_escaped(c.summary or "", limit=3800)):   # headroom for the "Summary" heading
        if chunk:
            blocks.append(("<b>Summary</b>\n" if i == 0 else "") + chunk)
    return blocks


class Deliverer:
    def __init__(self, store: Store, tg: TelegramClient, registry: DestinationRegistry, now=utcnow, dry_run: bool = False):
        self.store, self.tg, self.registry, self.now, self.dry_run = store, tg, registry, now, dry_run

    def send_alerts(self, job: Job, c: Classification) -> DeliveryReport:
        if not c.included:
            raise ValueError("refusing to alert for a job that is not an 'include' classification")
        rep = DeliveryReport()
        blocks = format_alert_blocks(job, c, self.now())
        destinations = self.registry.job_alert_destinations()
        if not destinations:
            reason = "no usable job-alert destinations are configured"
            rep.failed["configuration"] = reason
            log.error("alert job=%s FAILED: %s", job.job_id, reason)
            for unresolved in self.registry.unresolved():
                log.error("destination not usable: %s", unresolved)
            return rep
        for d in destinations:
            if not self.store.claim_delivery(job.job_id, "alert", d.key, self.now()):
                rep.already_sent.append(d.key)
                continue
            try:
                self.tg.send_blocks(d, blocks)
                self.store.finish_delivery(job.job_id, "alert", d.key, True, "ok", self.now())
                rep.sent.append(d.key)
            except TelegramError as exc:
                msg = explain(exc)
                self.store.finish_delivery(job.job_id, "alert", d.key, False, msg, self.now())
                rep.failed[d.key] = msg
                log.error("alert job=%s dest=%s FAILED: %s", job.job_id, d.label(), msg)
        for k in self.registry.unresolved():
            log.error("destination not usable: %s", k)
        if not rep.failed:
            log.info("alert job=%s delivered to %d destination(s)", job.job_id, len(rep.sent))
        return rep

    def send_resume(self, job: Job, c: Classification, resume: dict) -> DeliveryReport:
        """Send the validated PDF to the three allow-listed destinations only."""
        dests = self.registry.resume_destinations()          # raises AllowlistError -> fail closed
        pdf = Path(resume["pdf_path"])
        if resume.get("status") != "ok" or not pdf.exists():
            raise RuntimeError("resume is not in a validated, deliverable state")
        if sha256_file(pdf) != resume["pdf_sha256"]:
            raise RuntimeError("PDF on disk no longer matches the validated checksum; refusing to send")
        e = html.escape
        caption = (f"<b>{e(job.title)}</b> @ {e(job.company)}\n{e(job.location or '')}\n"
                   f"Experience: {e(c.experience_label())}\n{e(job.url)}\n"
                   f"File: <code>{e(resume['filename'])}</code>\nJob ID: <code>{e(job.job_id)}</code>")
        rep = DeliveryReport()
        for d in dests:
            if not self.store.claim_delivery(job.job_id, "resume", d.key, self.now()):
                rep.already_sent.append(d.key)
                continue
            try:
                self.tg.send_document(d, pdf, caption)
                self.store.finish_delivery(job.job_id, "resume", d.key, True, "ok", self.now())
                rep.sent.append(d.key)
            except TelegramError as exc:
                msg = explain(exc)
                self.store.finish_delivery(job.job_id, "resume", d.key, False, msg, self.now())
                rep.failed[d.key] = msg
                log.error("resume job=%s dest=%s FAILED: %s", job.job_id, d.label(), msg)
        return rep
