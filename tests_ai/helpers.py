"""Shared fixtures for the test-suite: a self-contained fake workspace."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.ai_pipeline import hook
from app.ai_pipeline.contacts import ContactService
from app.ai_pipeline.destinations import load_registry
from app.ai_pipeline.fakes import RecordingMailer, RecordingTelegram, ScriptedLLM, StaticHunter, StaticSearch
from app.ai_pipeline.settings import Settings
from app.ai_pipeline.store import Store

ROOT = Path(__file__).resolve().parent.parent
TOKEN = lambda n: f"{100000000 + n}:" + "A" * 35  # noqa: E731
NAMES = [("one_min", "1 min", True), ("fresher_bot", "Fresher bot", True), ("five_thirty", "5 min and 30 min ago", True),
         ("fifteen", "15 min", False), ("one_hour", "1 hour", False), ("ten_hours", "10 hours", False)]
CHATS = {k: str(-1001000000000 - i) for i, (k, _, _) in enumerate(NAMES)}
CONTROL_CHAT = "555000111"
AUTH_USER = 424242


def dest_data(resume_flags=None) -> dict:
    flags = resume_flags or {k: r for k, _, r in NAMES}
    return {"destinations": [{"key": k, "name": n, "bot_token_env": f"TOK_{k}", "chat_id_env": f"CHAT_{k}",
                              "job_alerts": True, "resume_pdf": flags[k]} for k, n, _ in NAMES],
            "control": {"name": "control", "bot_token_env": "TOK_control", "chat_id_env": "CHAT_control"}}


def env_for(extra: dict | None = None) -> dict:
    env = {}
    for i, (k, _, _) in enumerate(NAMES):
        env[f"TOK_{k}"], env[f"CHAT_{k}"] = TOKEN(i + 1), CHATS[k]
    env["TOK_control"], env["CHAT_control"] = TOKEN(99), CONTROL_CHAT
    env.update({"AI_PROVIDER": "anthropic", "AI_API_KEY": "test-key", "AI_DRY_RUN": "false",
                "AI_TELEGRAM_AUTHORIZED_USER_ID": str(AUTH_USER),
                "SMTP_HOST": "smtp.test", "SMTP_USER": "me@test.dev", "SMTP_PASSWORD": "pw", "SMTP_FROM": "me@test.dev",
                "AI_MIN_DESC_CHARS": "120", "CONTACT_SEARCH_PROVIDER": "brave", "BRAVE_API_KEY": "x", "HUNTER_API_KEY": "y",
                "AI_WORKER_THREADS": "2"})
    env.update(extra or {})
    return env


PROFILE = {
    "candidate": {"name": "Demo Candidate", "email": "demo@example.com", "phone": "+91-90000-00000"},
    "summary": "Computer science graduate who has built backend services and APIs in Python.",
    "required_text": ["Education"],
    "skills": {"Languages": ["Python", "Java", "SQL"], "Frameworks": ["FastAPI", "Flask"], "Tools": ["Git", "Docker", "PostgreSQL"]},
    "experience": [],
    "projects": [
        {"id": "p1", "name": "Inventory API", "tech": ["Python", "FastAPI", "PostgreSQL"], "dates": "2025",
         "bullets": [{"id": "p1.b1", "text": "Built a REST API with FastAPI and PostgreSQL to track inventory & orders (100% test pass)."},
                     {"id": "p1.b2", "text": "Added pytest tests for the core endpoints."}]},
        {"id": "p2", "name": "Console Bank", "tech": ["Java", "SQL"], "dates": "2024",
         "bullets": [{"id": "p2.b1", "text": "Implemented a Java console application backed by a SQL database."}]},
    ],
}
TEMPLATE_TXT = (ROOT / "config" / "referral_template.example.txt").read_text()

GOOD_DESC = ("Software Engineer I at Acme Technologies. About the role: you will build and maintain backend services in Python "
             "and Java, write unit tests and work with SQL databases. Requirements: 0-2 years of experience in software "
             "development. Freshers and 2025 graduates are welcome to apply. Strong fundamentals in data structures.")


def make_workspace(tmp: Path) -> Path:
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    (tmp / "templates").mkdir(exist_ok=True)
    tex = (ROOT / "templates" / "resume_template.tex").read_text().replace("YOUR NAME", "DEMO CANDIDATE")
    (tmp / "templates" / "resume_template.tex").write_text(tex)
    (tmp / "config" / "resume_profile.json").write_text(json.dumps(PROFILE))
    (tmp / "config" / "referral_template.txt").write_text(TEMPLATE_TXT)
    (tmp / "config" / "telegram_destinations.json").write_text(json.dumps(dest_data()))
    return tmp


def result(name, title, url, snippet=""):
    return {"title": f"{name} - {title} - Acme Technologies | LinkedIn", "url": url,
            "snippet": snippet or f"Gurugram · {title} · Acme Technologies · Experience"}


SEARCH_RESULTS = {
    "hr": [result("Priya Nair", "Talent Acquisition Specialist", "https://in.linkedin.com/in/priya-nair-1")],
    "senior_swe": [result("Rahul Verma", "Senior Software Engineer", "https://in.linkedin.com/in/rahul-verma-2")],
    "swe": [result("Neha Gupta", "Software Engineer", "https://in.linkedin.com/in/neha-gupta-3")],
}


def make_pipeline(tmp: Path, llm=None, tg=None, mailer=None, search=None, hunter=None, env_extra=None, fetcher=None, registry=None):
    ws = make_workspace(tmp)
    env = env_for(env_extra)
    s = Settings.from_env(env)
    store = Store(None, ":memory:")
    tg = tg or RecordingTelegram()
    mailer = mailer or RecordingMailer()
    search = search if search is not None else StaticSearch(SEARCH_RESULTS)
    hunter = hunter if hunter is not None else StaticHunter("acme.example", "valid")
    pipe = hook.build(s, env=env, base_dir=ws, store=store, llm=llm, tg=tg, mailer=mailer, search=search, hunter=hunter,
                      fetcher=fetcher, registry=registry)
    return pipe, tg, mailer, store, s
