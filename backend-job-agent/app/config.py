import os
from dataclasses import dataclass, field

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
    supabase_url: str
    supabase_key: str
    port: int
    log_level: str


def load() -> Config:
    provider = os.getenv("LLM_PROVIDER", "anthropic").lower()
    default_model = "claude-haiku-5-5" if provider == "anthropic" else "gpt-4o-mini"
    return Config(
        telegram_token=_req("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_req("TELEGRAM_CHAT_ID"),
        llm_provider=provider,
        llm_api_key=_req("LLM_API_KEY"),
        llm_model=os.getenv("LLM_MODEL", default_model),
        llm_base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        keywords=_list("KEYWORDS", "backend developer"),
        locations=_list("LOCATIONS", "Gurgaon,Noida,Bengaluru"),
        poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        lookback=int(os.getenv("LOOKBACK_SECONDS", "120")),
        max_pages=int(os.getenv("MAX_PAGES", "2")),
        max_min_years=float(os.getenv("MAX_MIN_YEARS", "2")),
        unspecified=os.getenv("UNSPECIFIED_EXPERIENCE", "skip").lower(),
        title_blocklist=[x.lower() for x in _list(
            "TITLE_BLOCKLIST", "senior,sr.,lead,staff,principal,architect,manager,head of,director,vp,intern")],
        supabase_url=_req("SUPABASE_URL"),
        supabase_key=_req("SUPABASE_KEY"),
        port=int(os.getenv("PORT", "8080")),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
