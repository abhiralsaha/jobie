"""Telegram-gated referral e-mail approval.

Invariants (each covered by tests):
  * Nothing is sent without an explicit Approve tap by AI_TELEGRAM_AUTHORIZED_USER_ID
    in the control chat. Timeouts, errors, AI output and restarts are never approval.
  * The button carries draft id + 12 hex of the content hash. The send happens only if
    the stored draft still hashes to what was approved AND the PDF on disk still has the
    approved SHA-256; otherwise the draft is marked 'stale' and needs a new request.
  * pending -> sending is ONE atomic UPDATE; a double tap, a Telegram retry or a second
    worker cannot win it twice, so at most one e-mail is ever submitted per draft.
  * After an ambiguous failure (connection lost mid-transfer) the draft becomes
    'send_unknown' and is never auto-resent.
"""
from __future__ import annotations

import html
import re
from datetime import timedelta
from pathlib import Path

from .contacts import SLOT_LABEL, Contact
from .destinations import DestinationRegistry
from .mailer import ACCEPTED, NOT_SUBMITTED, UNKNOWN
from .models import Job
from .referral import EmailDraft, content_hash
from .store import Store
from .telegram import TelegramClient, TelegramError, explain, split_escaped
from .util import get_logger, iso, sha256_file, utcnow

log = get_logger("approval")
CALLBACK_RE = re.compile(r"^([art]):([0-9a-f]{16}):([0-9a-f]{12})$")


class ApprovalService:
    def __init__(self, settings, store: Store, tg: TelegramClient, registry: DestinationRegistry, mailer, now=utcnow):
        self.s, self.store, self.tg, self.registry, self.mailer, self.now = settings, store, tg, registry, mailer, now

    # ------------------------------------------------------------ helpers
    @property
    def control(self):
        return self.registry.control

    def _get(self, draft_id: str) -> dict | None:
        return self.store.one("SELECT * FROM ai_email_drafts WHERE draft_id = ?", (draft_id,))

    def _cas(self, draft_id: str, from_states: tuple, to_state: str, **fields) -> bool:
        marks = ",".join("?" for _ in from_states)
        sets = ", ".join([f"{k} = ?" for k in fields] + ["state = ?", "updated_at = ?"])
        rc = self.store.execute(f"UPDATE ai_email_drafts SET {sets} WHERE draft_id = ? AND state IN ({marks})",
                                tuple(fields.values()) + (to_state, iso(self.now()), draft_id) + tuple(from_states))
        return rc == 1

    def _notify(self, text_html: str, reply_markup: dict | None = None) -> None:
        if not self.control:
            return
        try:
            self.tg.send_blocks(self.control, [text_html[:3900]], reply_markup)
        except TelegramError as exc:
            log.error("control-bot notification failed: %s", explain(exc))

    def notify_control(self, text_html: str) -> None:
        self._notify(text_html)

    @staticmethod
    def _kb(draft_id: str, ch: str, *, retry: bool = False) -> dict:
        h = ch[:12]
        if retry:
            return {"inline_keyboard": [[{"text": "\U0001F501 Retry send", "callback_data": f"t:{draft_id}:{h}"}]]}
        return {"inline_keyboard": [[{"text": "\u2705 Approve & send", "callback_data": f"a:{draft_id}:{h}"},
                                     {"text": "\u274C Reject", "callback_data": f"r:{draft_id}:{h}"}]]}

    # ------------------------------------------------------ review package
    def send_review_summary(self, job: Job, cls, resume: dict, contacts: dict[str, Contact], problems: list[str]) -> bool:
        """One private overview per job (job, contacts, verification status, unfilled roles, resume)."""
        if not self.control or not self.store.claim_delivery(job.job_id, "review", "control", self.now()):
            return False
        e = html.escape
        lines = [f"<b>Application package</b>\n<b>{e(job.title)}</b> @ {e(job.company)}\n{e(job.url)}\nJob ID <code>{e(job.job_id)}</code>"
                 f"\nExperience: {e(cls.experience_label())}",
                 f"<b>Resume</b>: <code>{e(resume.get('filename', 'none'))}</code> (sha256 {e(resume.get('sha256', '')[:12])})"]
        for slot in ("hr", "senior_swe", "swe"):
            c = contacts.get(slot)
            if not c:
                lines.append(f"<b>{SLOT_LABEL[slot]}</b>: \u26A0\uFE0F not found (left unfilled)")
                continue
            lines.append(f"<b>{SLOT_LABEL[slot]}</b>: {e(c.name)} - {e(c.position)}\n{e(c.profile_url)}\n"
                         f"email: {e(c.email or 'none')} [{e(c.email_status)}]\nevidence: {e(c.affiliation_evidence[0] if c.affiliation_evidence else '-')}"
                         f"\nwhy: {e(c.reason)}")
        if problems:
            lines.append("Notes: " + e("; ".join(problems)[:600]))
        try:
            self.tg.send_blocks(self.control, ["\n\n".join(lines)[:3900]])
            self.store.finish_delivery(job.job_id, "review", "control", True, "ok", self.now())
            return True
        except TelegramError as exc:
            self.store.finish_delivery(job.job_id, "review", "control", False, explain(exc), self.now())
            raise

    # ------------------------------------------------------ request approval
    def request(self, d: EmailDraft, job: Job, c: Contact) -> str:
        """Create the draft row and ask the user. Returns the draft state."""
        if not self.control:
            raise RuntimeError("no control bot configured; cannot request approval")
        ts = iso(self.now())
        self.store.execute(
            "INSERT INTO ai_email_drafts (draft_id, job_id, slot, to_email, to_name, subject, body, resume_path, resume_sha256, "
            "content_hash, state, sendable, block_reason, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?) "
            "ON CONFLICT (draft_id) DO NOTHING",
            (d.draft_id, d.job_id, d.slot, d.to_email, d.to_name, d.subject, d.body, d.resume_path, d.resume_sha256,
             d.content_hash, 1 if d.sendable else 0, d.block_reason, ts, ts))
        for old in self.store.query("SELECT draft_id, tg_message_id FROM ai_email_drafts WHERE job_id = ? AND slot = ? "
                                    "AND draft_id <> ? AND state IN ('pending', 'new', 'requesting')", (d.job_id, d.slot, d.draft_id)):
            if self._cas(old["draft_id"], ("pending", "new", "requesting"), "superseded"):
                if old.get("tg_message_id"):
                    self.tg.clear_buttons(self.control, int(old["tg_message_id"]))
        if not self._cas(d.draft_id, ("new",), "requesting"):
            row = self._get(d.draft_id)
            return row["state"] if row else "unknown"                  # already requested/decided: idempotent
        e = html.escape
        resume_line = (f"Resume: <code>{e(Path(d.resume_path).name)}</code> sha256 {e(d.resume_sha256[:12])} "
                       "(the PDF itself went to the approved resume destinations)")
        head = (f"<b>\U0001F4E8 Referral e-mail - {e(SLOT_LABEL[c.slot])}</b>\n<b>{e(job.title)}</b> @ {e(job.company)}\n{e(job.url)}\n"
                f"Job ID <code>{e(job.job_id)}</code>\n\n<b>To:</b> {e(c.name)} - {e(c.position)} @ {e(c.company)}\n"
                f"<b>Email:</b> {e(d.to_email or 'none')} [{e(c.email_status)}]\nProfile: {e(c.profile_url)}\n{resume_line}")
        blocks = [head, f"<b>Subject:</b> {e(d.subject)}"] + [f"<b>Body</b>\n{x}" if i == 0 else x for i, x in enumerate(split_escaped(d.body))]
        if d.sendable:
            blocks.append(f"Approve sends exactly this subject, body and PDF (draft <code>{d.draft_id}</code>, hash {d.content_hash[:12]}). "
                          "No tap = nothing is sent.")
            markup = self._kb(d.draft_id, d.content_hash)
            new_state = "pending"
        else:
            blocks.append(f"\U0001F6AB <b>NOT SENDABLE</b>: {e(d.block_reason)}. No Approve button is offered; use LinkedIn/manual outreach if you wish.")
            markup, new_state = None, "blocked"
        try:
            ids = self.tg.send_blocks(self.control, blocks, markup)
        except TelegramError as exc:
            self._cas(d.draft_id, ("requesting",), "new")
            raise RuntimeError(f"could not deliver approval request: {explain(exc)}") from None
        self._cas(d.draft_id, ("requesting",), new_state, tg_chat_id=str(self.control.chat_id), tg_message_id=ids[-1])
        return new_state

    # ------------------------------------------------------ handling taps
    def handle_update(self, update: dict) -> str:
        """Process one Telegram update. Returns a short status string (used by tests/logs)."""
        cb = update.get("callback_query")
        if not isinstance(cb, dict) or not self.control:
            return "ignored"
        token, cb_id = self.control.token, cb.get("id", "")
        uid = (cb.get("from") or {}).get("id")
        chat_id = ((cb.get("message") or {}).get("chat") or {}).get("id")
        if self.s.authorized_user_id is None:
            self.tg.answer_callback(token, cb_id, "Approvals are disabled: authorised user id not configured.", True)
            return "refused_unconfigured"
        if uid != self.s.authorized_user_id:
            log.warning("callback from unauthorised user id=%s ignored", uid)
            self.tg.answer_callback(token, cb_id, "You are not authorised to approve.", True)
            return "refused_unauthorised"
        if str(chat_id) != str(self.control.chat_id):
            self.tg.answer_callback(token, cb_id, "Wrong chat.", True)
            return "refused_wrong_chat"
        m = CALLBACK_RE.match(cb.get("data") or "")
        if not m:
            self.tg.answer_callback(token, cb_id, "Invalid button.", True)
            return "refused_bad_data"
        action, draft_id, h12 = m.groups()
        d = self._get(draft_id)
        if not d:
            self.tg.answer_callback(token, cb_id, "Unknown draft.", True)
            return "unknown_draft"
        if not d["content_hash"].startswith(h12):
            self.tg.answer_callback(token, cb_id, "This button is for an older version of the draft.", True)
            return "stale_button"
        approver = str(uid)
        if action == "r":
            if self._cas(draft_id, ("pending",), "rejected", approver_id=approver, decided_at=iso(self.now())):
                self._clear(d)
                self.tg.answer_callback(token, cb_id, "Rejected. Nothing will be sent.")
                self._notify(f"\u274C Rejected: {html.escape(d['to_name'] or d['to_email'] or '')} ({html.escape(d['slot'])}). Nothing was sent.")
                return "rejected"
            self.tg.answer_callback(token, cb_id, f"Already {d['state']}.")
            return "noop"
        allowed_from = ("pending",) if action == "a" else ("failed",)
        if d["state"] not in allowed_from:
            self.tg.answer_callback(token, cb_id, f"Already {d['state']}; no action taken.")
            return "noop"
        if not d["sendable"]:
            self.tg.answer_callback(token, cb_id, "This draft is not sendable.", True)
            return "refused_unsendable"
        problem = self._integrity_problem(d)
        if problem:
            self._cas(draft_id, allowed_from, "stale")
            self._clear(d)
            self.tg.answer_callback(token, cb_id, "Draft or attachment changed; a new approval is required.", True)
            self._notify(f"\u26A0\uFE0F Draft {draft_id} is stale ({html.escape(problem)}). Nothing was sent; a new approval request is needed.")
            return "stale"
        if not self._cas(draft_id, allowed_from, "sending", approver_id=approver, decided_at=iso(self.now())):
            self.tg.answer_callback(token, cb_id, "Already being handled.")
            return "noop"                                   # lost the race: someone else owns the send
        self._clear(d)
        self.tg.answer_callback(token, cb_id, "Approved. Sending...")
        return self._send(self._get(draft_id))

    def _integrity_problem(self, d: dict) -> str:
        if content_hash(d["job_id"], d["to_email"] or "", d["subject"], d["body"], d["resume_sha256"]) != d["content_hash"]:
            return "content no longer matches the approved hash"
        p = Path(d["resume_path"] or "")
        if not p.is_file():
            return "resume file missing"
        if sha256_file(p) != d["resume_sha256"]:
            return "resume PDF changed since approval was requested"
        return ""

    def _clear(self, d: dict) -> None:
        if d.get("tg_message_id") and self.control:
            self.tg.clear_buttons(self.control, int(d["tg_message_id"]))

    def _send(self, d: dict) -> str:
        problem = self._integrity_problem(d)                # re-check right before the mail leaves
        if problem:
            self._cas(d["draft_id"], ("sending",), "stale", outcome=problem)
            self._notify(f"\u26A0\uFE0F Not sent: {html.escape(problem)}. A new approval is required.")
            return "stale"
        try:
            out = self.mailer.send(d["to_email"], d["to_name"] or "", d["subject"], d["body"], d["resume_path"])
        except Exception as exc:  # noqa: BLE001
            self._cas(d["draft_id"], ("sending",), "send_unknown", outcome=f"unexpected error {type(exc).__name__}")
            self._notify(f"\u2753 Outcome unknown for draft {d['draft_id']} ({type(exc).__name__}). Check your Sent folder; I will not resend automatically.")
            return "send_unknown"
        e = html.escape
        who = f"{e(d['to_name'] or '')} &lt;{e(d['to_email'])}&gt;"
        if out.status == ACCEPTED:
            self._cas(d["draft_id"], ("sending",), "sent", sent_at=iso(self.now()), outcome=f"{out.detail}; {out.message_id}")
            self._notify(f"\u2705 <b>Submitted</b> to {who}: the SMTP server accepted it for delivery "
                         f"(Message-ID <code>{e(out.message_id)}</code>). That is not a guarantee it reached the inbox.")
            return "sent"
        if out.status == NOT_SUBMITTED:
            self._cas(d["draft_id"], ("sending",), "failed", outcome=out.detail)
            self._notify(f"\u274C <b>Not sent</b> to {who}: {e(out.detail)}. Nothing left the server, so a retry is safe.",
                         self._kb(d["draft_id"], d["content_hash"], retry=True))
            return "failed"
        self._cas(d["draft_id"], ("sending",), "send_unknown", outcome=out.detail)
        self._notify(f"\u2753 <b>Unknown outcome</b> for {who}: {e(out.detail)}. Check your Sent folder; I will not resend automatically.")
        return "send_unknown"

    # ------------------------------------------------------ housekeeping
    def expire_stale(self) -> int:
        """Pending drafts past the deadline become 'expired'. Expiry NEVER sends."""
        cutoff = iso(self.now() - timedelta(days=self.s.approval_expire_days))
        n = 0
        for r in self.store.query("SELECT draft_id, tg_message_id FROM ai_email_drafts WHERE state = 'pending' AND created_at < ?", (cutoff,)):
            if self._cas(r["draft_id"], ("pending",), "expired"):
                n += 1
                if r.get("tg_message_id") and self.control:
                    self.tg.clear_buttons(self.control, int(r["tg_message_id"]))
        return n

    def recover_after_restart(self) -> None:
        cutoff = iso(self.now() - timedelta(minutes=5))
        for r in self.store.query("SELECT draft_id FROM ai_email_drafts WHERE state = 'sending' AND updated_at < ?", (cutoff,)):
            if self._cas(r["draft_id"], ("sending",), "send_unknown", outcome="process restarted while sending"):
                self._notify(f"\u2753 Draft {r['draft_id']} was mid-send when the process restarted. Check your Sent folder; not resending automatically.")
        for r in self.store.query("SELECT draft_id FROM ai_email_drafts WHERE state = 'requesting' AND updated_at < ?", (cutoff,)):
            self._cas(r["draft_id"], ("requesting",), "new")


class ApprovalPoller:
    """Long-polls getUpdates for the control bot. Run in exactly ONE process."""

    def __init__(self, service: ApprovalService, store: Store, tg: TelegramClient, agent=None):
        self.svc, self.store, self.tg, self.agent = service, store, tg, agent

    def poll_once(self, timeout: int = 25) -> int:
        ctrl = self.svc.control
        if not ctrl:
            return 0
        key = f"tg_offset:{ctrl.bot_id}"
        offset = self.store.kv_get(key)
        updates = self.tg.get_updates(ctrl.token, int(offset) if offset else None, timeout)
        for u in updates:
            try:
                callback = u.get("callback_query")
                callback = callback if isinstance(callback, dict) else {}
                if self.agent and (
                    u.get("message")
                    or str(callback.get("data") or "").startswith("agent:")
                ):
                    self.agent.handle_update(u)
                elif callback and self.svc.s.dry_run:
                    ctrl = self.svc.control
                    if ctrl:
                        self.tg.answer_callback(
                            ctrl.token,
                            str(callback.get("id") or ""),
                            "AI_DRY_RUN=true: referral email was not sent.",
                            True,
                        )
                else:
                    self.svc.handle_update(u)
            except Exception:  # noqa: BLE001 - one bad update must not wedge the queue
                log.exception("failed handling update %s", u.get("update_id"))
            self.store.kv_set(key, str(int(u["update_id"]) + 1))
        return len(updates)

    def run_forever(self, stop) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                self.poll_once()
                backoff = 1.0
            except TelegramError as exc:
                if exc.code == 409:
                    log.error("getUpdates conflict: a webhook is set or another poller is running for the control bot")
                elif exc.code == 401:
                    log.error("control bot token rejected; approval poller stopping")
                    return
                else:
                    log.error("approval poll failed: %s", explain(exc))
                stop.wait(min(60.0, backoff))
                backoff = min(60.0, backoff * 2)
