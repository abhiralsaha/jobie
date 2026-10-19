import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # dotenv optional in prod where real env vars are set
    pass


def _list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise SystemExit(f"Missing required environment variable: {name}")
    return v


@dataclass(frozen=True)
class Config:
    telegram_token: str
    telegram_chat_id: str
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_base_url: str
    keywords: list[str]
    locations: list[str]
    poll_interval: int
    lookback: int
    max_pages: int
    max_min_years: float
    unspecified: str
    title_blocklist: list[str]
    keepalive_url: str
    keepalive_interval: int
    db_path: str
    supabase_url: str | None
    supabase_key: str | None
    port: int
    log_level: str
    log_file: str


def load() -> Config:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    default_model = "claude-haiku-5-5" if provider == "anthropic" else "gpt-4o-mini"
    supabase_url = os.getenv("SUPABASE_URL", "").strip() or None
    supabase_key = os.getenv("SUPABASE_KEY", "").strip() or None
    if (supabase_url is None) != (supabase_key is None):
        raise SystemExit("SUPABASE_URL and SUPABASE_KEY must both be set to use Supabase")
    if supabase_key and (
        supabase_key.startswith(("postgres://", "postgresql://"))
        or ".pooler.supabase.com" in supabase_key
    ):
        raise SystemExit(
            "SUPABASE_KEY must be a Supabase API secret/service-role key, not a PostgreSQL connection string. "
            "Copy it from your project's API Keys settings."
        )

    keepalive_interval = int(os.getenv("KEEPALIVE_INTERVAL_SECONDS", "780"))
    if keepalive_interval < 1:
        raise SystemExit("KEEPALIVE_INTERVAL_SECONDS must be greater than zero")
    keepalive_url = os.getenv("KEEPALIVE_URL", "https://jobie-1.onrender.com").strip()
    try:
        parsed_keepalive_url = urlsplit(keepalive_url)
        keepalive_port = parsed_keepalive_url.port
    except ValueError:
        raise SystemExit("KEEPALIVE_URL must be a valid HTTP or HTTPS URL")
    if (
        parsed_keepalive_url.scheme not in ("http", "https")
        or not parsed_keepalive_url.hostname
        or (keepalive_port is not None and not 1 <= keepalive_port <= 65535)
    ):
        raise SystemExit("KEEPALIVE_URL must be a valid HTTP or HTTPS URL")

    return Config(
        telegram_token=_req("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_req("TELEGRAM_CHAT_ID"),
        llm_provider=provider,
        llm_api_key=_req("LLM_API_KEY"),
        llm_model=os.getenv("LLM_MODEL", default_model),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        keywords=_list("KEYWORDS", "full stack developer"),
        locations=_list("LOCATIONS", "Gurgaon,Noida,Bengaluru"),
        poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        lookback=int(os.getenv("LOOKBACK_SECONDS", "120")),
        max_pages=int(os.getenv("MAX_PAGES", "2")),
        max_min_years=float(os.getenv("MAX_MIN_YEARS", "2")),
        unspecified=os.getenv("UNSPECIFIED_EXPERIENCE", "skip").lower(),
        title_blocklist=[x.lower() for x in _list(
            "TITLE_BLOCKLIST", "senior,sr.,lead,staff,principal,architect,manager,head of,director,vp,intern")],
        keepalive_url=keepalive_url,
        keepalive_interval=keepalive_interval,
        db_path=os.getenv("DB_PATH", "data/jobs.db"),
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        port=int(os.getenv("PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        log_file=os.getenv("LOG_FILE", "logs/agent.log"),
    )
