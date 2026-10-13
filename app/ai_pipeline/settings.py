"""Environment-driven settings. Secrets are read from env vars only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MODELS = {
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "openai/gpt-4o-mini",
    "openai_compat": "",
}
DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
# (env var holding an existing key, provider it implies)
_KEY_VARS = [
    ("ANTHROPIC_API_KEY", "anthropic"), ("OPENAI_API_KEY", "openai"),
    ("GEMINI_API_KEY", "gemini"), ("GOOGLE_API_KEY", "gemini"),
    ("GROQ_API_KEY", "groq"), ("OPENROUTER_API_KEY", "openrouter"),
]


def _b(v, default=False) -> bool:
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _i(v, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _f(v, default: float) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # master switches
    enabled: bool = False
    dry_run: bool = True
    run_approval_poller: bool = True
    worker_threads: int = 4
    # persistence
    db_url: str | None = None
    sqlite_path: str = "data/ai_pipeline.sqlite3"
    # AI
    ai_provider: str = ""
    ai_api_key: str = field(default="", repr=False)
    ai_model: str = ""
    ai_base_url: str = ""
    ai_max_rpm: int = 20
    ai_timeout: float = 45.0
    ai_max_retries: int = 4
    # classification policy
    min_desc_chars: int = 250
    max_required_min_years: float = 2.0
    max_required_max_years: float = 2.0
    max_job_attempts: int = 8
    # files
    destinations_file: str = "config/telegram_destinations.json"
    resume_profile_file: str = "config/resume_profile.json"
    resume_template_file: str = "templates/resume_template.tex"
    referral_template_file: str = "config/referral_template.txt"
    company_domains_file: str = "config/company_domains.json"
    resumes_dir: str = "resumes"
    # resume
    resume_max_pages: int = 1
    resume_max_projects: int = 3
    resume_compile_timeout: int = 60
    # telegram approvals
    authorized_user_id: int | None = None
    approval_expire_days: int = 3
    # smtp
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = field(default="", repr=False)
    smtp_security: str = "ssl"  # ssl | starttls
    smtp_from: str = ""
    smtp_from_name: str = ""
    # contacts
    contact_search_provider: str = "none"  # brave | google_cse | serpapi | none
    brave_api_key: str = field(default="", repr=False)
    google_cse_api_key: str = field(default="", repr=False)
    google_cse_cx: str = ""
    serpapi_api_key: str = field(default="", repr=False)
    hunter_api_key: str = field(default="", repr=False)
    contact_location_hint: str = ""
    search_daily_cap: int = 90
    hunter_daily_cap: int = 20
    referral_daily_job_cap: int = 15
    sendable_email_statuses: tuple = ("verified_service", "public_evidence")

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Settings":
        e = os.environ if env is None else env
        provider = (e.get("AI_PROVIDER") or "").strip().lower()
        key = (e.get("AI_API_KEY") or "").strip()
        if not key:  # reuse whatever key the project already has configured
            if provider:
                for var, prov in _KEY_VARS:
                    if prov == provider and e.get(var):
                        key = e[var].strip()
                        break
            else:
                for var, prov in _KEY_VARS:
                    if e.get(var):
                        key, provider = e[var].strip(), prov
                        break
        model = (e.get("AI_MODEL") or "").strip() or DEFAULT_MODELS.get(provider, "")
        base = (e.get("AI_BASE_URL") or "").strip() or DEFAULT_BASE_URLS.get(provider, "")
        uid = (e.get("AI_TELEGRAM_AUTHORIZED_USER_ID") or "").strip()
        statuses = tuple(s.strip() for s in (e.get("SENDABLE_EMAIL_STATUSES")
                         or "verified_service,public_evidence").split(",") if s.strip())
        return cls(
            enabled=_b(e.get("AI_PIPELINE_ENABLED")), dry_run=_b(e.get("AI_DRY_RUN"), True),
            run_approval_poller=_b(e.get("AI_RUN_APPROVAL_POLLER"), True),
            worker_threads=max(1, _i(e.get("AI_WORKER_THREADS"), 4)),
            db_url=(e.get("AI_DATABASE_URL") or e.get("DATABASE_URL") or e.get("SUPABASE_DB_URL") or None),
            sqlite_path=e.get("AI_SQLITE_PATH") or "data/ai_pipeline.sqlite3",
            ai_provider=provider, ai_api_key=key, ai_model=model, ai_base_url=base,
            ai_max_rpm=max(1, _i(e.get("AI_MAX_RPM"), 20)), ai_timeout=_f(e.get("AI_TIMEOUT_SECONDS"), 45.0),
            ai_max_retries=max(0, _i(e.get("AI_MAX_RETRIES"), 4)),
            min_desc_chars=_i(e.get("AI_MIN_DESC_CHARS"), 250),
            max_required_min_years=_f(e.get("AI_MAX_REQUIRED_MIN_YEARS"), 2.0),
            max_required_max_years=_f(e.get("AI_MAX_REQUIRED_MAX_YEARS"), 2.0),
            max_job_attempts=_i(e.get("AI_MAX_JOB_ATTEMPTS"), 8),
            destinations_file=e.get("AI_DESTINATIONS_FILE") or "config/telegram_destinations.json",
            resume_profile_file=e.get("AI_RESUME_PROFILE_FILE") or "config/resume_profile.json",
            resume_template_file=e.get("AI_RESUME_TEMPLATE_FILE") or "templates/resume_template.tex",
            referral_template_file=e.get("AI_REFERRAL_TEMPLATE_FILE") or "config/referral_template.txt",
            company_domains_file=e.get("AI_COMPANY_DOMAINS_FILE") or "config/company_domains.json",
            resumes_dir=e.get("AI_RESUMES_DIR") or "resumes",
            resume_max_pages=_i(e.get("AI_RESUME_MAX_PAGES"), 1),
            resume_max_projects=_i(e.get("AI_RESUME_MAX_PROJECTS"), 3),
            resume_compile_timeout=_i(e.get("AI_RESUME_COMPILE_TIMEOUT"), 60),
            authorized_user_id=int(uid) if uid.lstrip("-").isdigit() else None,
            approval_expire_days=_i(e.get("AI_APPROVAL_EXPIRE_DAYS"), 3),
            smtp_host=(e.get("SMTP_HOST") or "").strip(), smtp_port=_i(e.get("SMTP_PORT"), 465),
            smtp_user=(e.get("SMTP_USER") or "").strip(), smtp_password=e.get("SMTP_PASSWORD") or "",
            smtp_security=(e.get("SMTP_SECURITY") or "ssl").strip().lower(),
            smtp_from=(e.get("SMTP_FROM") or e.get("SMTP_USER") or "").strip(),
            smtp_from_name=(e.get("SMTP_FROM_NAME") or "").strip(),
            contact_search_provider=(e.get("CONTACT_SEARCH_PROVIDER") or "none").strip().lower(),
            brave_api_key=e.get("BRAVE_API_KEY") or "", google_cse_api_key=e.get("GOOGLE_CSE_API_KEY") or "",
            google_cse_cx=e.get("GOOGLE_CSE_CX") or "", serpapi_api_key=e.get("SERPAPI_API_KEY") or "",
            hunter_api_key=e.get("HUNTER_API_KEY") or "",
            contact_location_hint=(e.get("CONTACT_LOCATION_HINT") or "").strip(),
            search_daily_cap=_i(e.get("SEARCH_DAILY_CAP"), 90), hunter_daily_cap=_i(e.get("HUNTER_DAILY_CAP"), 20),
            referral_daily_job_cap=_i(e.get("REFERRAL_DAILY_JOB_CAP"), 15),
            sendable_email_statuses=statuses,
        )

    def problems(self) -> list[str]:
        """Human-readable configuration problems (never includes secret values)."""
        out = []
        if not self.ai_api_key:
            out.append("No AI key found: set AI_API_KEY (+AI_PROVIDER) or an existing ANTHROPIC/OPENAI/GEMINI/GROQ/OPENROUTER key.")
        if self.ai_api_key and not self.ai_provider:
            out.append("AI_API_KEY is set but AI_PROVIDER is not (anthropic|openai|gemini|groq|openrouter|openai_compat).")
        if self.ai_provider == "openai_compat" and not self.ai_base_url:
            out.append("AI_PROVIDER=openai_compat requires AI_BASE_URL.")
        if self.ai_provider and not self.ai_model:
            out.append("AI_MODEL is not set.")
        if self.authorized_user_id is None:
            out.append("AI_TELEGRAM_AUTHORIZED_USER_ID is not set: approvals will be refused (fail closed).")
        return out

    def smtp_problems(self) -> list[str]:
        out = []
        for name, val in (("SMTP_HOST", self.smtp_host), ("SMTP_USER", self.smtp_user),
                          ("SMTP_PASSWORD", self.smtp_password), ("SMTP_FROM", self.smtp_from)):
            if not val:
                out.append(f"{name} is not set")
        if self.smtp_security not in {"ssl", "starttls"}:
            out.append("SMTP_SECURITY must be 'ssl' or 'starttls'")
        return out

    def path(self, rel: str, base: str | Path | None = None) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else Path(base or os.getcwd()) / p
