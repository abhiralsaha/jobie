from fastapi import FastAPI, Header
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.config import settings
from app.freshness import posted_within_seconds
from app.job_intake import import_jobs
from app.logging_setup import setup_logger
from app.messaging import TelegramNotifier
from app.security import RequestBodyLimitMiddleware, require_api_bearer_token
from app.ai_pipeline import hook as ai_hook

app = FastAPI(title="Jobie API")
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=64 * 1024)
logger = setup_logger()
notifier = TelegramNotifier()


class JobSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    job_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=300)
    company: str = Field(min_length=1, max_length=200)
    url: HttpUrl
    posted_at: str = Field(default="", max_length=64)
    source: str = Field(default="manual", min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=30000)

    @field_validator("url")
    @classmethod
    def require_https_url(cls, value: HttpUrl) -> HttpUrl:
        if value.scheme != "https":
            raise ValueError("Job URLs must use HTTPS")
        return value


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": settings.APP_NAME, "mode": "human_review_only"}


@app.post("/jobs/import")
async def import_job(
    job: JobSubmission,
    authorization: str | None = Header(default=None),
) -> dict:
    require_api_bearer_token(authorization, settings.JOBIE_API_TOKEN)

    imported = import_jobs([job.model_dump(mode="json")])
    is_recent = posted_within_seconds(job.posted_at, settings.RECENT_JOB_WINDOW_SECONDS)
    alert_sent = False
    ai_pipeline_submitted = False
    if imported:
        if ai_hook.enabled():
            ai_hook.set_project_http_client()
            ai_pipeline_submitted = ai_hook.on_new_job(
                job_id=job.job_id,
                title=job.title,
                company=job.company,
                url=str(job.url),
                posted_at=job.posted_at or None,
                description=job.description,
                source=job.source,
            )
        elif is_recent:
            try:
                notifier.send_alert(job.title, job.company, str(job.url))
                alert_sent = True
            except Exception as exc:
                logger.warning(
                    "Recent job imported but Telegram alert failed (%s)", type(exc).__name__
                )

    return {
        "status": "ok",
        "imported": bool(imported),
        "duplicate": not bool(imported),
        "recent": is_recent,
        "telegram_alert_sent": alert_sent,
        "ai_pipeline_submitted": ai_pipeline_submitted,
    }

