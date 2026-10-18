"""Offline end-to-end demonstration (no network, no secrets, nothing is sent anywhere)."""
from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from . import hook
from .fakes import DemoLLM, RecordingMailer, RecordingTelegram, StaticHunter, StaticSearch
from .models import Job
from .settings import Settings
from .store import Store

ACME_FRESHER = ("Software Engineer I at Acme Technologies. You will build backend services in Python and Java, write tests and "
                "work with SQL databases. Requirements: 0-2 years of experience in software development. Freshers and "
                "2025 graduates are welcome to apply. Strong fundamentals in data structures and algorithms.")
SENIOR_DESC = ("Backend Engineer at Globex. We are looking for an engineer with 5+ years of experience building distributed "
               "systems in Java, strong knowledge of Kafka and AWS, and experience leading small teams of engineers.")
AMBIGUOUS = ("Developer at Initech. Join our friendly team to build internal tools using Python. You will collaborate with "
             "designers and product managers, ship features weekly and take ownership of your work from start to finish.")


def _result(name, title, url):
    return {"title": f"{name} - {title} - Acme Technologies | LinkedIn", "url": url,
            "snippet": f"Gurugram \u00b7 {title} \u00b7 Acme Technologies"}


def run_demo() -> int:
    root = Path(__file__).resolve().parents[2]
    ws = Path(tempfile.mkdtemp(prefix="jobie_demo_"))
    (ws / "config").mkdir()
    (ws / "templates").mkdir()
    tex = (root / "templates" / "resume_template.tex").read_text().replace("YOUR NAME", "DEMO CANDIDATE")
    (ws / "templates" / "resume_template.tex").write_text(tex)
    profile = {"candidate": {"name": "Demo Candidate", "email": "demo@example.com", "phone": "+91-90000-00000"},
               "summary": "Computer science graduate who has built backend services and APIs in Python.",
               "required_text": ["Education"],
               "skills": {"Languages": ["Python", "Java", "SQL"], "Tools": ["Git", "Docker"]}, "experience": [],
               "projects": [{"id": "p1", "name": "Inventory API", "tech": ["Python", "SQL"], "dates": "2025",
                             "bullets": [{"id": "p1.b1", "text": "Built a REST API in Python backed by a SQL database."}]}]}
    (ws / "config" / "resume_profile.json").write_text(json.dumps(profile))
    shutil.copy(root / "config" / "referral_template.example.txt", ws / "config" / "referral_template.txt")
    names = [("one_min", "1 min", True), ("fresher_bot", "Fresher bot", True), ("five_min", "5 min", True),
             ("thirty_min", "30 min", True),
             ("fifteen", "15 min", False), ("hourly", "1 hour", False)]
    env = {"AI_PROVIDER": "anthropic", "AI_API_KEY": "demo", "AI_TELEGRAM_AUTHORIZED_USER_ID": "1", "AI_MIN_DESC_CHARS": "120",
           "TOK_control": "100000099:" + "A" * 35, "CHAT_control": "555000111"}
    dests = []
    for i, (k, n, r) in enumerate(names):
        env[f"TOK_{k}"], env[f"CHAT_{k}"] = f"10000000{i + 1}:" + "A" * 35, str(-1001000 - i)
        dests.append({"key": k, "name": n, "bot_token_env": f"TOK_{k}", "chat_id_env": f"CHAT_{k}", "job_alerts": True, "resume_pdf": r})
    (ws / "config" / "telegram_destinations.json").write_text(json.dumps(
        {"destinations": dests, "control": {"name": "control", "bot_token_env": "TOK_control", "chat_id_env": "CHAT_control"}}))
    s = Settings.from_env(env)
    tg, mailer = RecordingTelegram(), RecordingMailer()
    search = StaticSearch({"hr": [_result("Priya Nair", "Talent Acquisition Specialist", "https://in.linkedin.com/in/priya-nair-1")],
                           "senior_swe": [_result("Rahul Verma", "Senior Software Engineer", "https://in.linkedin.com/in/rahul-verma-2")],
                           "swe": [_result("Neha Gupta", "Software Engineer", "https://in.linkedin.com/in/neha-gupta-3")]})
    pipe = hook.build(s, env=env, base_dir=ws, store=Store(None, ":memory:"), llm=DemoLLM(), tg=tg, mailer=mailer,
                      search=search, hunter=StaticHunter("acme.example", "valid"))
    jobs = [Job("1001", "Software Engineer I", "Acme Technologies", "Gurugram", "https://example.com/jobs/1001", "just now", ACME_FRESHER),
            Job("1002", "Backend Engineer", "Globex", "Remote", "https://example.com/jobs/1002", "just now", SENIOR_DESC),
            Job("1003", "Developer", "Initech", "Noida", "https://example.com/jobs/1003", "just now", AMBIGUOUS),
            Job("1004", "Senior Software Engineer", "Hooli", "Pune", "https://example.com/jobs/1004", "just now", None)]
    for j in jobs:
        pipe.submit(j)
    pipe.wait_idle(120)
    print("\n=== job outcomes ===")
    for j in jobs:
        r = pipe.store.get_job(j.job_id)
        c = pipe.store.latest_classification(j.job_id)
        why = json.loads(c["final_json"])["reason"] if c else "-"
        print(f"{j.job_id} {j.title!r:32} stage={r['stage']:<8} decision={c['decision'] if c else '-':<8} {why}")
    print("\n=== Telegram routing (fake bot, nothing sent) ===")
    for method in ("sendMessage", "sendDocument"):
        per = {}
        for c in tg.sent(method):
            per[c["payload"]["chat_id"]] = per.get(c["payload"]["chat_id"], 0) + 1
        print(method, per)
    print("PDF chats:", sorted({c["payload"]["chat_id"] for c in tg.sent("sendDocument")}))
    drafts = pipe.store.query("SELECT slot, to_email, state, sendable FROM ai_email_drafts")
    print("\n=== referral drafts (awaiting YOUR approval; mailer.sent =", len(mailer.sent), ") ===")
    for d in drafts:
        print(d)
    print("\nresumes written to:", ws / "resumes")
    return 0
