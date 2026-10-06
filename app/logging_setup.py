import logging
import sys
from pathlib import Path

from app.config import settings


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("jobie")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    # Vercel/Serverless often mounts the app directory as read-only.
    # Fall back to stdout logging if we can't create the log file.
    try:
        log_path = Path(settings.LOG_PATH)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler(sys.stdout)

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger
