import time
from typing import Dict, List

from app.config import settings
from app.db import init_db
from app.job_intake import import_jobs
from app.logging_setup import setup_logger
from app.messaging import TelegramNotifier
from app.profile import CandidateProfile

logger = setup_logger()


class Scheduler:
    """Idle worker for maintenance; jobs enter through the explicit intake API."""

    def __init__(self) -> None:
        self.notifier = TelegramNotifier()
        self.profile = CandidateProfile()
        init_db()

    def process_jobs(self, jobs: List[Dict[str, str]]) -> List[Dict[str, str]]:
        imported = import_jobs(jobs)
        for job in imported:
            logger.info("New job imported: %s (%s)", job["title"], job["company"])
            try:
                self.notifier.send_alert(job["title"], job["company"], job["url"])
            except Exception as exc:
                logger.warning("Could not send optional alert: %s", exc)
        return imported

    def run_once(self) -> None:
        logger.info("Scheduler is idle; import jobs through the approved intake API")

    def run_forever(self) -> None:
        logger.info("Jobie maintenance scheduler started")
        while True:
            try:
                self.run_once()
            except Exception as exc:
                logger.exception("Unexpected scheduler error: %s", exc)
            time.sleep(settings.POLL_INTERVAL_SECONDS)


def run_scheduler() -> None:
    Scheduler().run_forever()
