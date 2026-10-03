import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def resolve_project_path(value: str, default: Path) -> str:
    path = Path(value) if value else default
    if not path.is_absolute():
        path = BASE_DIR / path
    return str(path.resolve())


class Settings:
    APP_NAME: str = "jobie"
    DATABASE_PATH: str = resolve_project_path(os.getenv("DATABASE_PATH", ""), BASE_DIR / "jobie.db")
    PROFILE_PATH: str = os.getenv("PROFILE_PATH", str(BASE_DIR / "config" / "candidate_profile.json"))
    LOG_PATH: str = os.getenv("LOG_PATH", str(BASE_DIR / "logs" / "jobie.log"))

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_DIGEST_BOT_TOKEN: str = os.getenv("TELEGRAM_DIGEST_BOT_TOKEN", "")
    TELEGRAM_DIGEST_CHAT_ID: str = os.getenv("TELEGRAM_DIGEST_CHAT_ID", "")
    TELEGRAM_DIGEST_INTERVAL_HOURS: int = int(
        os.getenv("TELEGRAM_DIGEST_INTERVAL_HOURS", "10")
    )
    TELEGRAM_DIGEST_5M_BOT_TOKEN: str = os.getenv("TELEGRAM_DIGEST_5M_BOT_TOKEN", "")
    TELEGRAM_DIGEST_5M_CHAT_ID: str = os.getenv("TELEGRAM_DIGEST_5M_CHAT_ID", "")
    TELEGRAM_DIGEST_15M_BOT_TOKEN: str = os.getenv("TELEGRAM_DIGEST_15M_BOT_TOKEN", "")
    TELEGRAM_DIGEST_15M_CHAT_ID: str = os.getenv("TELEGRAM_DIGEST_15M_CHAT_ID", "")
    TELEGRAM_DIGEST_30M_BOT_TOKEN: str = os.getenv("TELEGRAM_DIGEST_30M_BOT_TOKEN", "")
    TELEGRAM_DIGEST_30M_CHAT_ID: str = os.getenv("TELEGRAM_DIGEST_30M_CHAT_ID", "")
    TELEGRAM_DIGEST_1H_BOT_TOKEN: str = os.getenv("TELEGRAM_DIGEST_1H_BOT_TOKEN", "")
    TELEGRAM_DIGEST_1H_CHAT_ID: str = os.getenv("TELEGRAM_DIGEST_1H_CHAT_ID", "")

    JOBIE_API_TOKEN: str = os.getenv("JOBIE_API_TOKEN", "")
    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    RECENT_JOB_WINDOW_SECONDS: int = int(os.getenv("RECENT_JOB_WINDOW_SECONDS", "180"))
    LINKEDIN_POLL_INTERVAL_SECONDS: int = int(os.getenv("LINKEDIN_POLL_INTERVAL_SECONDS", "120"))
    LINKEDIN_LOOKBACK_HOURS: int = int(os.getenv("LINKEDIN_LOOKBACK_HOURS", "24"))
    LINKEDIN_MAX_SEARCH_PAGES: int = int(os.getenv("LINKEDIN_MAX_SEARCH_PAGES", "5"))
    LINKEDIN_SEARCH_KEYWORDS: str = os.getenv("LINKEDIN_SEARCH_KEYWORDS", "software engineer")
    LINKEDIN_SEARCH_LOCATION: str = os.getenv(
        "LINKEDIN_SEARCH_LOCATION", "Gurugram, Haryana, India"
    )
    DAILY_DIGEST_TIME: str = os.getenv("DAILY_DIGEST_TIME", "18:00")
    DAILY_DIGEST_TIMEZONE: str = os.getenv("DAILY_DIGEST_TIMEZONE", "Asia/Kolkata")
    MANUAL_DIGEST_MAX_PER_DAY: int = int(os.getenv("MANUAL_DIGEST_MAX_PER_DAY", "3"))
    MANUAL_DIGEST_COOLDOWN_SECONDS: int = int(
        os.getenv("MANUAL_DIGEST_COOLDOWN_SECONDS", "300")
    )
    AUTHORIZED_JOB_FEED_URL: str = os.getenv("AUTHORIZED_JOB_FEED_URL", "")
    AUTHORIZED_JOB_FEED_TOKEN: str = os.getenv("AUTHORIZED_JOB_FEED_TOKEN", "")
    JOBIE_API_URL: str = os.getenv("JOBIE_API_URL", "http://127.0.0.1:8000/jobs/import")
    FEED_POLL_SECONDS: int = int(os.getenv("FEED_POLL_SECONDS", "120"))


settings = Settings()
