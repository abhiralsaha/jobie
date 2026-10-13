"""Owner-only Telegram assistant for Jobie operations."""
from __future__ import annotations

import html
import json
import threading
from collections.abc import Callable

from .telegram import TelegramClient, TelegramError, explain
from .util import get_logger

log = get_logger("agent")

MENU = {
    "inline_keyboard": [
        [{"text": "Worker status", "callback_data": "agent:status"},
         {"text": "Recent AI jobs", "callback_data": "agent:jobs"}],
        [{"text": "24h digest", "callback_data": "agent:digest"},
         {"text": "AI review", "callback_data": "agent:ai"}],
        [{"text": "Search now", "callback_data": "agent:search"},
         {"text": "Retry due jobs", "callback_data": "agent:retry"}],
    ]
}


class TelegramAgent:
    def __init__(self, pipeline, tg: TelegramClient, search: Callable[[], dict] | None = None):
        self.pipeline = pipeline
        self.store = pipeline.store
        self.settings = pipeline.s
        self.tg = tg
        self.search = search
        self._history: list[dict[str, str]] = []
        self._history_lock = threading.Lock()
        self._search_lock = threading.Lock()
        self._search_running = False

    @property
    def control(self):
        return self.pipeline.approvals.control

    def _authorized(self, user_id, chat_id, chat_type) -> bool:
        if self.control is None or self.settings.authorized_user_id is None:
            return False
        return (
            chat_type == "private"
            and user_id == self.settings.authorized_user_id
            and str(chat_id) == str(self.control.chat_id)
        )

    def _send(self, text: str, markup: dict | None = None) -> None:
        if self.control is None:
            return
        try:
            self.tg.send_blocks(self.control, [html.escape(text)], markup)
        except TelegramError as exc:
            log.error("Telegram agent reply failed: %s", explain(exc))

    def handle_update(self, update: dict) -> str:
        if not isinstance(update, dict):
            return "ignored"
        callback = update.get("callback_query")
        message = update.get("message")
        try:
            if isinstance(callback, dict):
                return self._handle_callback(callback)
            if isinstance(message, dict):
                return self._handle_message(message)
            return "ignored"
        except Exception as exc:
            log.exception("Telegram agent action failed")
            sender = message.get("from") if isinstance(message, dict) else None
            if not isinstance(sender, dict) and isinstance(callback, dict):
                sender = callback.get("from")
            chat = message.get("chat") if isinstance(message, dict) else None
            if not isinstance(chat, dict) and isinstance(callback, dict):
                callback_message = callback.get("message")
                chat = callback_message.get("chat") if isinstance(callback_message, dict) else None
            sender = sender if isinstance(sender, dict) else {}
            chat = chat if isinstance(chat, dict) else {}
            if self._authorized(sender.get("id"), chat.get("id"), chat.get("type")):
                self._send(f"Jobie could not complete that action ({type(exc).__name__}). Check the worker logs.")
            return "failed"

    def _handle_message(self, message: dict) -> str:
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        if not self._authorized(sender.get("id"), chat.get("id"), chat.get("type")):
            return "refused_unauthorized"
        text = str(message.get("text") or "").strip()
        if not text:
            self._send("Send a command or question as text. Use /help to see what I can do.")
            return "empty_message"
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        handlers = {
            "/start": self._show_menu,
            "/menu": self._show_menu,
            "/help": self._show_help,
            "/status": self._show_status,
            "/jobs": self._show_jobs,
            "/ai": self._show_ai,
            "/digest": self._show_digest,
            "/search": self._confirm_search,
            "/retry": self._confirm_retry,
        }
        handler = handlers.get(command)
        if handler:
            handler()
            return command[1:]
        return self._chat(text)

    def _handle_callback(self, callback: dict) -> str:
        sender = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        callback_id = str(callback.get("id") or "")
        if not self._authorized(sender.get("id"), chat.get("id"), chat.get("type")):
            if self.control:
                self.tg.answer_callback(self.control.token, callback_id, "Not authorized.", True)
            return "refused_unauthorized"
        action = str(callback.get("data") or "")
        actions = {
            "agent:menu": self._show_menu,
            "agent:status": self._show_status,
            "agent:jobs": self._show_jobs,
            "agent:ai": self._show_ai,
            "agent:digest": self._show_digest,
            "agent:search": self._confirm_search,
            "agent:retry": self._confirm_retry,
            "agent:confirm_search": self._start_search,
            "agent:confirm_retry": self._run_retries,
        }
        handler = actions.get(action)
        if not handler:
            if self.control:
                self.tg.answer_callback(self.control.token, callback_id, "Unknown Jobie action.", True)
            return "unknown_action"
        if self.control:
            self.tg.answer_callback(self.control.token, callback_id, "Working...")
        handler()
        return action.removeprefix("agent:")

    def _show_menu(self) -> None:
        self._send("Jobie Agent\nChoose an action, or ask me a question.", MENU)

    def _show_help(self) -> None:
        self._send(
            "Commands:\n"
            "/status - AI pipeline status\n"
            "/jobs - recent AI-screened jobs\n"
            "/ai - review and referral approval counts\n"
            "/digest - show recent jobs from the last 24 hours\n"
            "/search - confirm a fresh watcher search\n"
            "/retry - confirm retry of due AI jobs\n"
            "You can also ask a question in plain language. Email is sent only "
            "after you tap an approval button on its review card.",
            MENU,
        )

    def _show_status(self) -> None:
        rows = self.store.query("SELECT stage, COUNT(*) AS n FROM ai_jobs GROUP BY stage")
        stages = {str(r["stage"]): int(r["n"]) for r in rows}
        total = sum(stages.values())
        text = [
            "Jobie AI pipeline",
            f"Enabled: {'yes' if self.settings.enabled else 'no'}",
            f"Dry run: {'yes' if self.settings.dry_run else 'no'}",
            f"Tracked AI jobs: {total}",
        ]
        text.extend(f"{stage}: {count}" for stage, count in sorted(stages.items()))
        self._send("\n".join(text), MENU)

    def _show_jobs(self) -> None:
        jobs = self.store.query(
            "SELECT job_id, title, company, stage, updated_at FROM ai_jobs "
            "ORDER BY created_at DESC LIMIT 10"
        )
        if not jobs:
            self._send("No AI-screened jobs yet. New jobs appear here after AI_PIPELINE_ENABLED=true.", MENU)
            return
        lines = ["Recent AI-screened jobs"]
        for job in jobs:
            lines.append(
                f"{job['stage']} | {job['title'] or 'Untitled'} @ {job['company'] or 'Unknown'} "
                f"| ID {job['job_id']}"
            )
        self._send("\n".join(lines), MENU)

    def _show_ai(self) -> None:
        approvals = self.store.query(
            "SELECT state, COUNT(*) AS n FROM ai_email_drafts GROUP BY state"
        )
        counts = {str(row["state"]): int(row["n"]) for row in approvals}
        review = self.store.query("SELECT COUNT(*) AS n FROM ai_jobs WHERE stage = 'review'")
        lines = [
            "AI review",
            f"Jobs needing review: {int(review[0]['n']) if review else 0}",
            "Referral drafts: " + (", ".join(f"{key}={value}" for key, value in sorted(counts.items())) or "none"),
            "Eligible resumes are sent only to the three configured resume destinations. "
            "Referral email requires an explicit Approve tap.",
        ]
        self._send("\n".join(lines), MENU)

    def _show_digest(self) -> None:
        try:
            from app.linkedin_fresh_jobs import get_recent_digest_jobs

            jobs = get_recent_digest_jobs(lookback_seconds=24 * 60 * 60)
        except Exception as exc:
            log.exception("Telegram agent could not load the 24-hour digest")
            self._send(f"Could not load the digest ({type(exc).__name__}). Check the Jobie database connection.")
            return
        if not jobs:
            self._send("No stored jobs in the last 24 hours.", MENU)
            return
        lines = [f"Stored jobs from the last 24 hours ({len(jobs)} total; showing up to 8)"]
        for job in jobs[:8]:
            lines.append(f"{job.get('title') or 'Untitled'} @ {job.get('company') or 'Unknown'}\n{job.get('url') or ''}")
        self._send("\n\n".join(lines), MENU)

    def _confirm_search(self) -> None:
        if not self.search:
            self._send("Search action is unavailable in this process.", MENU)
            return
        if not self.settings.enabled:
            self._send("Enable AI_PIPELINE_ENABLED=true on the watcher to activate the Telegram agent.", MENU)
            return
        self._send(
            "Run an immediate LinkedIn fresh-watcher search? New jobs will be deduplicated and processed using the current pipeline mode.",
            {"inline_keyboard": [[{"text": "Run search", "callback_data": "agent:confirm_search"},
                                  {"text": "Cancel", "callback_data": "agent:menu"}]]},
        )

    def _start_search(self) -> None:
        if not self.search:
            self._send("Search action is unavailable in this process.", MENU)
            return
        with self._search_lock:
            if self._search_running:
                self._send("A manual search is already running.", MENU)
                return
            self._search_running = True

        self._send("Manual fresh search started. I’ll report the result here.")

        def run() -> None:
            try:
                result = self.search()
                self._send(
                    f"Manual search complete: {int(result.get('matched', 0))} matching jobs; "
                    f"{int(result.get('new', 0))} newly queued."
                )
            except Exception as exc:
                log.exception("Telegram-requested fresh search failed")
                self._send(f"Manual search failed ({type(exc).__name__}). See watcher logs for details.")
            finally:
                with self._search_lock:
                    self._search_running = False

        threading.Thread(target=run, name="jobie-telegram-search", daemon=True).start()

    def _confirm_retry(self) -> None:
        self._send(
            "Retry AI jobs whose backoff has elapsed? This may resume alerts, resume generation, or referral preparation.",
            {"inline_keyboard": [[{"text": "Retry due jobs", "callback_data": "agent:confirm_retry"},
                                  {"text": "Cancel", "callback_data": "agent:menu"}]]},
        )

    def _run_retries(self) -> None:
        try:
            count = self.pipeline.tick()
        except Exception as exc:
            log.exception("Telegram-requested AI retry failed")
            self._send(f"Could not retry jobs ({type(exc).__name__}). See watcher logs.")
            return
        self._send(f"Queued {count} due AI job(s) for retry.", MENU)

    def _chat(self, text: str) -> str:
        llm = self.pipeline.classifier.llm
        system = (
            "You are Jobie Agent, a concise private job-search assistant. "
            "Use only the supplied Jobie snapshot; never invent data or claim an action completed. "
            "Available actions: none, status, jobs, ai, digest, search, retry. "
            "Return one JSON object with string keys reply and action. "
            'Example: {"reply":"I can show status.","action":"status"}. '
            "Use search or retry only when the user clearly asks for that operation; "
            "those actions will require a separate confirmation button. "
            "Email must never be approved by chat text; only the existing explicit approval button can approve it."
        )
        with self._history_lock:
            history = list(self._history)
        context = {
            "question": text[:2000],
            "recent_conversation": history,
            "pipeline_enabled": self.settings.enabled,
            "dry_run": self.settings.dry_run,
        }
        try:
            response = llm.complete_json(system, json.dumps(context, ensure_ascii=False), max_tokens=500)
            reply = response.get("reply")
            action = response.get("action", "none")
            if not isinstance(reply, str) or action not in {"none", "status", "jobs", "ai", "digest", "search", "retry"}:
                raise ValueError("assistant response did not match the expected shape")
        except Exception as exc:
            log.warning("Telegram agent language response failed (%s)", type(exc).__name__)
            self._send(f"I couldn’t answer that just now ({type(exc).__name__}). Use /help or try again.")
            return "model_error"

        with self._history_lock:
            self._history.extend(({"role": "user", "text": text[:2000]},
                                  {"role": "assistant", "text": reply[:2000]}))
            self._history = self._history[-12:]

        self._send(reply[:3000], MENU)
        action_handlers = {
            "status": self._show_status,
            "jobs": self._show_jobs,
            "ai": self._show_ai,
            "digest": self._show_digest,
            "search": self._confirm_search,
            "retry": self._confirm_retry,
        }
        if action != "none":
            action_handlers[action]()
        return "chat"
