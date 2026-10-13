"""Telegram destination registry + the fail-closed resume-PDF allowlist.

Tokens are NEVER stored in the JSON file; the file names the *environment
variables* that hold them. Resume PDFs may only go to the three destinations
named in ``RESUME_DESTINATION_NAMES``; if they cannot be mapped unambiguously
``resume_destinations()`` raises ``AllowlistError`` and nothing is sent.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

RESUME_DESTINATION_NAMES = ("1 min", "Fresher bot", "5 min and 30 min ago")
_TOKEN_RE = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,60}$")
_CHAT_RE = re.compile(r"^(-?\d{5,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")


class AllowlistError(Exception):
    pass


class UnauthorizedDestination(Exception):
    pass


@dataclass(frozen=True)
class Destination:
    key: str
    name: str
    token: str = field(repr=False)
    chat_id: str = ""
    thread_id: int | None = None
    job_alerts: bool = False
    resume_pdf: bool = False
    role: str = "alert"          # alert | control

    @property
    def bot_id(self) -> str:
        return self.token.split(":", 1)[0]

    @property
    def target(self) -> tuple:
        return (self.bot_id, str(self.chat_id), self.thread_id)

    def label(self) -> str:
        return f"{self.name} [{self.key}]"


@dataclass
class _Entry:
    key: str
    name: str
    job_alerts: bool
    resume_pdf: bool
    dest: Destination | None
    error: str | None


class DestinationRegistry:
    def __init__(self, entries: list[_Entry], control: Destination | None, errors: list[str]):
        self._entries = entries
        self.control = control
        self.errors = errors

    def job_alert_destinations(self) -> list[Destination]:
        return [e.dest for e in self._entries if e.job_alerts and e.dest is not None]

    def unresolved(self) -> list[str]:
        return [f"{e.name}: {e.error}" for e in self._entries if e.error]

    def resume_destinations(self) -> list[Destination]:
        problems: list[str] = []
        picked: list[Destination] = []
        for name in RESUME_DESTINATION_NAMES:
            matches = [e for e in self._entries if e.name.strip() == name]
            if len(matches) != 1:
                problems.append(f"name {name!r} maps to {len(matches)} configured destinations (need exactly 1)")
                continue
            e = matches[0]
            if not e.resume_pdf:
                problems.append(f"{name!r} is not flagged resume_pdf=true")
            if e.dest is None:
                problems.append(f"{name!r} unresolved: {e.error}")
            elif e.resume_pdf:
                picked.append(e.dest)
        allowed = set(RESUME_DESTINATION_NAMES)
        for e in self._entries:
            if e.resume_pdf and e.name.strip() not in allowed:
                problems.append(f"destination {e.name!r} is flagged resume_pdf=true but is not an authorised resume destination")
        targets = [d.target for d in picked]
        if len(set(targets)) != len(targets):
            problems.append("two resume destinations resolve to the same bot/chat")
        if problems:
            raise AllowlistError("resume destinations not safely resolvable: " + "; ".join(problems))
        return picked

    def summary(self) -> dict:
        out = {"job_alert_destinations": [d.label() for d in self.job_alert_destinations()],
               "control": self.control.label() if self.control else None, "errors": list(self.errors)
               + self.unresolved()}
        try:
            out["resume_destinations"] = [d.label() for d in self.resume_destinations()]
        except AllowlistError as exc:
            out["resume_destinations"] = f"BLOCKED: {exc}"
        return out


def _resolve(item: dict, env: dict, role: str, errors: list[str]) -> tuple[Destination | None, str | None]:
    name = str(item.get("name") or "").strip()
    key = str(item.get("key") or re.sub(r"\W+", "_", name.lower()).strip("_") or "dest")
    if "bot_token" in item or "token" in item:
        return None, "do not put tokens in the file; use bot_token_env"
    tok_var, chat_var = item.get("bot_token_env"), item.get("chat_id_env")
    token = (env.get(tok_var or "") or "").strip()
    chat = str(item.get("chat_id") or env.get(chat_var or "") or "").strip()
    if not tok_var or not token:
        return None, f"env var {tok_var!r} for the bot token is not set"
    if not _TOKEN_RE.match(token):
        return None, f"env var {tok_var!r} does not look like a Telegram bot token"
    if not chat:
        return None, f"chat id missing (env var {chat_var!r} not set)"
    if not _CHAT_RE.match(chat):
        return None, f"chat id from {chat_var or 'file'!r} has an invalid format"
    thread = item.get("thread_id")
    if thread is None and item.get("thread_id_env"):
        t = (env.get(item["thread_id_env"]) or "").strip()
        thread = int(t) if t.isdigit() else None
    return Destination(key=key, name=name, token=token, chat_id=chat, thread_id=thread,
                       job_alerts=bool(item.get("job_alerts", False)) and role == "alert",
                       resume_pdf=bool(item.get("resume_pdf", False)) and role == "alert", role=role), None


def load_registry(path: str | Path | None = None, env: dict | None = None, data: dict | None = None) -> DestinationRegistry:
    env = os.environ if env is None else env
    errors: list[str] = []
    if data is None:
        p = Path(path or "config/telegram_destinations.json")
        if not p.exists():
            return DestinationRegistry([], None, [f"destinations file not found: {p}"])
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return DestinationRegistry([], None, [f"destinations file is not valid JSON: {exc.msg}"])
    entries: list[_Entry] = []
    seen_keys: set[str] = set()
    for item in data.get("destinations", []):
        dest, err = _resolve(item, env, "alert", errors)
        name = str(item.get("name") or "").strip()
        key = dest.key if dest else str(item.get("key") or name)
        if key in seen_keys:
            errors.append(f"duplicate destination key {key!r}")
        seen_keys.add(key)
        entries.append(_Entry(key, name, bool(item.get("job_alerts", False)), bool(item.get("resume_pdf", False)), dest, err))
    control = None
    if data.get("control"):
        control, err = _resolve({**data["control"], "job_alerts": False, "resume_pdf": False}, env, "control", errors)
        if err:
            errors.append(f"control bot: {err}")
    else:
        errors.append("no 'control' bot configured: referral approvals cannot be requested")
    return DestinationRegistry(entries, control, errors)
