"""python -m app.ai_pipeline.cli <command>

  init-db             create the ai_* tables
  check-config        show what is configured (never prints secrets)
  check-destinations  verify every Telegram destination WITHOUT sending anything
  classify FILE       classify one job JSON file ({job_id,title,company,description,...})
  demo                full offline walk-through with fake AI/Telegram/e-mail (real LaTeX if installed)
  report              recent jobs, decisions and reasons (read this during a dry-run)
  worker              run retry ticker + approval poller as a standalone process
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from .destinations import load_registry
from .settings import Settings
from .util import mask


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_check_config(_a) -> int:
    s = Settings.from_env()
    reg = load_registry(s.path(s.destinations_file))
    tools = {t: bool(shutil.which(t)) for t in ("latexmk", "pdflatex", "pdftotext")}
    _print({
        "ai": {"provider": s.ai_provider or "NOT SET", "model": s.ai_model or "NOT SET", "key": mask(s.ai_api_key)},
        "pipeline_enabled": s.enabled, "dry_run": s.dry_run, "database": "postgres" if s.db_url else f"sqlite:{s.sqlite_path}",
        "authorized_user_id": mask(str(s.authorized_user_id) if s.authorized_user_id else ""),
        "telegram": reg.summary(), "smtp_problems": s.smtp_problems(), "latex_tools": tools,
        "files": {n: s.path(getattr(s, n)).exists() for n in ("resume_profile_file", "resume_template_file",
                                                               "referral_template_file", "destinations_file")},
        "contact_search": s.contact_search_provider, "hunter": mask(s.hunter_api_key), "problems": s.problems()})
    return 1 if s.problems() else 0


def cmd_check_destinations(_a) -> int:
    from .telegram import TelegramClient, preflight

    s = Settings.from_env()
    reg = load_registry(s.path(s.destinations_file))
    rows = preflight(reg, TelegramClient())
    for r in rows:
        print(("OK   " if r["ok"] else "FAIL ") + r["destination"] + "  " + r["detail"])
    print(json.dumps(reg.summary(), indent=2))
    return 0 if all(r["ok"] for r in rows) else 1


def cmd_init_db(_a) -> int:
    from .store import Store

    s = Settings.from_env()
    Store(s.db_url, str(s.path(s.sqlite_path))).init_schema()
    print("ai_* tables ready")
    return 0


def cmd_classify(a) -> int:
    from . import hook
    from .models import Job

    d = json.loads(Path(a.file).read_text())
    pipe = hook.build()
    job = Job.from_dict(d)
    from .jd_fetch import is_complete

    c = pipe.classifier.classify(job, d.get("description"), is_complete(d.get("description"), pipe.s.min_desc_chars))
    _print(c.to_dict())
    return 0


def cmd_report(_a) -> int:
    """Recent jobs with stage/decision/reason: what to read during a dry-run day."""
    from .store import Store

    s = Settings.from_env()
    st = Store(s.db_url, str(s.path(s.sqlite_path)))
    rows = st.query("SELECT job_id, title, company, stage, attempts, last_error, created_at FROM ai_jobs ORDER BY created_at DESC LIMIT 40")
    for r in rows:
        c = st.latest_classification(r["job_id"])
        why = json.loads(c["final_json"]).get("reason", "") if c else ""
        print(f"{r['created_at']}  {r['job_id']:<12} {r['stage']:<16} {(c['decision'] if c else '-'):<8} {r['title'][:34]:<34} | {why[:90]}"
              + (f"  [ERR: {r['last_error'][:80]}]" if r["last_error"] else ""))
    drafts = st.query("SELECT state, COUNT(*) AS n FROM ai_email_drafts GROUP BY state")
    print("referral drafts by state:", {d["state"]: d["n"] for d in drafts} or "none")
    return 0


def cmd_worker(_a) -> int:
    from . import hook

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    hook.start_background()
    print("worker running (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        hook.stop_background()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="jobie-ai")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init-db", "check-config", "check-destinations", "worker", "demo", "report"):
        sub.add_parser(name)
    c = sub.add_parser("classify")
    c.add_argument("file")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if a.cmd == "demo":
        from .demo import run_demo

        return run_demo()
    return {"init-db": cmd_init_db, "check-config": cmd_check_config, "check-destinations": cmd_check_destinations,
            "classify": cmd_classify, "worker": cmd_worker, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
