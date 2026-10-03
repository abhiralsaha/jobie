import logging
from pathlib import Path

from app.config import settings


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("jobie")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    log_path = Path(settings.LOG_PATH)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not logger.handlers:
        handler = logging.FileHandler(log_path, encoding="utf-8")
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    return logger
