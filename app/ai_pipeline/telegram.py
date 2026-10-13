"""Minimal Telegram Bot API client (requests-based).

* Honours 429 ``retry_after``; bounded retries.
* Splits long text on line boundaries and HTML-escapes *after* splitting.
* Errors carry Telegram's own code/description, never the bot token
  (``requests`` exception strings embed the URL, so they are not propagated).
* ``send_document`` refuses any destination that is not flagged ``resume_pdf``.
"""
from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Callable

import requests

from .destinations import Destination, UnauthorizedDestination
from .util import get_logger

log = get_logger("telegram")
API = "https://api.telegram.org/bot{token}/{method}"
MSG_LIMIT = 4000          # Telegram hard limit is 4096; keep headroom
CAPTION_LIMIT = 1000      # hard limit is 1024


class TelegramError(Exception):
    def __init__(self, description: str, code: int | None = None, retry_after: float | None = None):
        super().__init__(f"{code or '-'}: {description}")
        self.description, self.code, self.retry_after = description, code, retry_after


def explain(err: TelegramError) -> str:
    d = (err.description or "").lower()
    if err.code == 401:
        return "bot token rejected (revoked or wrong)"
    if err.code == 403 and "blocked" in d:
        return "bot was blocked by the user / kicked from the chat"
    if err.code == 403:
        return f"bot lacks permission to post here ({err.description})"
    if err.code == 400 and "chat not found" in d:
        return "chat not found: wrong chat id, or the bot was never added / the user never pressed Start"
    if err.code == 400 and "thread" in d:
        return f"message thread problem ({err.description})"
    if err.code == 429:
        return "rate limited by Telegram after retries"
    return f"{err.code}: {err.description}"


def split_escaped(raw: str, limit: int = MSG_LIMIT) -> list[str]:
    """Split plain text into chunks whose *HTML-escaped* length is <= limit."""
    chunks: list[str] = []
    cur = ""
    for line in (raw or "").split("\n"):
        while len(html.escape(line)) > limit:           # one enormous line: hard split
            cut = max(1, limit // 2)
            while len(html.escape(line[:cut])) > limit:
                cut //= 2
            piece, line = line[:cut], line[cut:]
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(html.escape(piece))
        cand = (cur + "\n" + html.escape(line)) if cur else html.escape(line)
        if len(cand) > limit:
            chunks.append(cur)
            cur = html.escape(line)
        else:
            cur = cand
    if cur or not chunks:
        chunks.append(cur)
    return chunks


def pack_blocks(blocks: list[str], limit: int = MSG_LIMIT) -> list[str]:
    """Join already-HTML-safe blocks into messages <= limit (blocks are never cut)."""
    out, cur = [], ""
    for b in blocks:
        if len(b) > limit:
            raise ValueError("block longer than the message limit; split it with split_escaped first")
        cand = (cur + "\n\n" + b) if cur else b
        if len(cand) > limit:
            out.append(cur)
            cur = b
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


class TelegramClient:
    def __init__(self, session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep,
                 max_retries: int = 3, timeout: float = 30.0):
        self.session = session or requests.Session()
        self._sleep, self.max_retries, self.timeout = sleep, max_retries, timeout

    def call(self, token: str, method: str, payload: dict | None = None, files: dict | None = None,
             timeout: float | None = None) -> dict | list | bool:
        url = API.format(token=token, method=method)
        for attempt in range(self.max_retries + 1):
            try:
                if files:
                    with open(files["document"][1], "rb") as fh:
                        r = self.session.post(url, data=payload or {}, timeout=timeout or 90,
                                              files={"document": (files["document"][0], fh, "application/pdf")})
                else:
                    r = self.session.post(url, json=payload or {}, timeout=timeout or self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt < self.max_retries:
                    self._sleep(min(30.0, 2.0 * (2 ** attempt)))
                    continue
                raise TelegramError(f"network error ({type(exc).__name__}) calling {method}") from None
            try:
                data = r.json()
            except ValueError:
                data = {"ok": False, "description": f"non-JSON response (HTTP {r.status_code})", "error_code": r.status_code}
            if data.get("ok"):
                return data.get("result")
            code = data.get("error_code", r.status_code)
            retry_after = (data.get("parameters") or {}).get("retry_after")
            if code == 429 and attempt < self.max_retries:
                wait = min(60.0, float(retry_after or 2 ** attempt))
                log.warning("telegram 429 on %s; waiting %.0fs", method, wait)
                self._sleep(wait)
                continue
            if code and code >= 500 and attempt < self.max_retries:
                self._sleep(min(30.0, 2.0 * (2 ** attempt)))
                continue
            raise TelegramError(data.get("description", "unknown error"), code, retry_after)
        raise TelegramError("retries exhausted")

    # -- sending -------------------------------------------------------------
    @staticmethod
    def _base(dest: Destination) -> dict:
        p = {"chat_id": dest.chat_id}
        if dest.thread_id:
            p["message_thread_id"] = dest.thread_id
        return p

    def send_blocks(self, dest: Destination, blocks: list[str], reply_markup: dict | None = None) -> list[int]:
        """Send HTML-safe blocks, packed into as few messages as possible. Returns message ids.
        ``reply_markup`` is attached to the LAST message only."""
        ids = []
        msgs = pack_blocks(blocks)
        for i, text in enumerate(msgs):
            payload = {**self._base(dest), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
            if reply_markup and i == len(msgs) - 1:
                payload["reply_markup"] = reply_markup
            res = self.call(dest.token, "sendMessage", payload)
            ids.append(int(res["message_id"]))
        return ids

    def send_document(self, dest: Destination, path: str | Path, caption_html: str) -> int:
        if not dest.resume_pdf or dest.role != "alert":
            raise UnauthorizedDestination(f"{dest.label()} is not an authorised resume-PDF destination")
        p = Path(path)
        payload = {**self._base(dest), "caption": caption_html[:CAPTION_LIMIT], "parse_mode": "HTML"}
        res = self.call(dest.token, "sendDocument", payload, files={"document": (p.name, str(p))})
        return int(res["message_id"])

    def answer_callback(self, token: str, cb_id: str, text: str, alert: bool = False) -> None:
        try:
            self.call(token, "answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:190], "show_alert": alert})
        except TelegramError as exc:
            log.warning("answerCallbackQuery failed: %s", exc)

    def clear_buttons(self, dest: Destination, message_id: int) -> None:
        try:
            self.call(dest.token, "editMessageReplyMarkup",
                      {"chat_id": dest.chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}})
        except TelegramError as exc:
            log.warning("could not clear buttons: %s", exc)

    def get_updates(self, token: str, offset: int | None, timeout: int = 25) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call(token, "getUpdates", payload, timeout=timeout + 15) or []

    def get_me(self, token: str) -> dict:
        return self.call(token, "getMe")

    def get_chat(self, dest: Destination) -> dict:
        return self.call(dest.token, "getChat", {"chat_id": dest.chat_id})

    def get_member(self, dest: Destination, user_id: int) -> dict:
        return self.call(dest.token, "getChatMember", {"chat_id": dest.chat_id, "user_id": user_id})


def preflight(registry, client: TelegramClient) -> list[dict]:
    """Verify every destination WITHOUT sending anything (getMe / getChat / getChatMember)."""
    rows = []
    dests = list(registry.job_alert_destinations())
    try:
        dests += [d for d in registry.resume_destinations() if d not in dests]
    except Exception:  # noqa: BLE001 - reported separately in summary()
        pass
    if registry.control:
        dests.append(registry.control)
    for d in dests:
        row = {"destination": d.label(), "alerts": d.job_alerts, "resume_pdf": d.resume_pdf, "ok": False, "detail": ""}
        try:
            me = client.get_me(d.token)
            chat = client.get_chat(d)
            detail = f"bot @{me.get('username')} can see chat type={chat.get('type')}"
            if chat.get("type") in {"group", "supergroup", "channel"}:
                member = client.get_member(d, int(me["id"]))
                status = member.get("status")
                if status not in {"administrator", "member", "creator"}:
                    raise TelegramError(f"bot status in chat is {status!r}", 403)
                if chat.get("type") == "channel" and not member.get("can_post_messages", status == "creator"):
                    raise TelegramError("bot is not allowed to post in this channel", 403)
                detail += f", bot status={status}"
            row.update(ok=True, detail=detail)
        except TelegramError as exc:
            row["detail"] = explain(exc)
        rows.append(row)
    return rows
