import sqlite3
from pathlib import Path

from app.config import settings


def get_connection() -> sqlite3.Connection:
    db_path = Path(settings.DATABASE_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs_seen (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE,
                title TEXT,
                company TEXT,
                url TEXT,
                posted_at TEXT,
                first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'new'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs_applied (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT UNIQUE,
                title TEXT,
                company TEXT,
                url TEXT,
                applied_at TEXT DEFAULT CURRENT_TIMESTAMP,
                result TEXT,
                notes TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts_sent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT,
                message TEXT,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'sent'
            )
            """
        )
        conn.commit()
