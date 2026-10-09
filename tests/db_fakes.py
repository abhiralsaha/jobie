"""Small PostgreSQL-shaped fakes used by unit tests.

The application owns connection lifecycle through ``get_connection``.  These
fakes model the cursor/context-manager and dictionary-row behavior exposed by
psycopg2's ``RealDictCursor`` without requiring a database server.
"""

from types import ModuleType
import sys
import types


def install_psycopg2_import_stub():
    """Allow tests to import the DB layer when optional psycopg2 is absent."""
    try:
        import psycopg2  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    psycopg2 = ModuleType("psycopg2")
    psycopg2.extras = ModuleType("psycopg2.extras")
    psycopg2.extras.RealDictCursor = object
    psycopg2.pool = ModuleType("psycopg2.pool")
    psycopg2.pool.ThreadedConnectionPool = object
    psycopg2.extensions = ModuleType("psycopg2.extensions")
    psycopg2.extensions.connection = object
    sys.modules.update({
        "psycopg2": psycopg2,
        "psycopg2.extras": psycopg2.extras,
        "psycopg2.pool": psycopg2.pool,
        "psycopg2.extensions": psycopg2.extensions,
    })


class FakeCursor:
    def __init__(self, tables):
        self.tables = tables
        self.executed = []
        self.query = ""
        self.params = None
        self._rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, query, params=None):
        self.query = query
        self.params = params
        self.executed.append((query, params))
        sql = " ".join(query.lower().split())
        self._rows = []

        if sql.startswith("select 1 from seen_jobs"):
            job_id = params[0]
            self._rows = [{"?column?": 1}] if job_id in self.tables["seen_jobs"] else []
        elif sql.startswith("select job_id, title, company, url, posted_at from seen_jobs"):
            self._rows = [
                {key: row.get(key) for key in ("job_id", "title", "company", "url", "posted_at")}
                for row in self.tables["seen_jobs"].values()
            ]
        elif sql.startswith("select requested_at from manual_digest_runs"):
            local_date = params[0]
            self._rows = [
                row for row in reversed(self.tables["manual_digest_runs"])
                if row["local_date"] == local_date
            ]
        elif sql.startswith("select 1 from digest_runs"):
            self._rows = [{"?column?": 1}] if params[0] in self.tables["digest_runs"] else []
        elif sql.startswith("insert into seen_jobs"):
            job_id, title, company, first_seen, url, posted_at = params
            self.tables["seen_jobs"].setdefault(job_id, {
                "job_id": job_id, "title": title, "company": company,
                "first_seen": first_seen, "url": url, "posted_at": posted_at,
            })
        elif sql.startswith("update seen_jobs"):
            title, company, url, posted_at, job_id = params
            self.tables["seen_jobs"][job_id].update(
                title=title, company=company, url=url, posted_at=posted_at
            )
        elif sql.startswith("delete from seen_jobs"):
            cutoff = params[0]
            self.tables["seen_jobs"] = {
                key: row for key, row in self.tables["seen_jobs"].items()
                if row["first_seen"] >= cutoff
            }
        elif sql.startswith("insert into digest_runs"):
            digest_date, sent_at = params
            self.tables["digest_runs"].setdefault(digest_date, {"sent_at": sent_at})
        elif sql.startswith("insert into manual_digest_runs"):
            local_date, requested_at = params
            row = {"id": len(self.tables["manual_digest_runs"]) + 1,
                   "local_date": local_date, "requested_at": requested_at,
                   "status": "pending"}
            self.tables["manual_digest_runs"].append(row)
            self._rows = [{"id": row["id"]}]
        elif sql.startswith("update manual_digest_runs"):
            request_id = params[0]
            for row in self.tables["manual_digest_runs"]:
                if row["id"] == request_id:
                    row["status"] = "failed" if "failed" in sql else "sent"

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self):
        self.tables = {"seen_jobs": {}, "digest_runs": {}, "manual_digest_runs": []}
        self.fake_cursor = FakeCursor(self.tables)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def cursor(self):
        return self.fake_cursor


def connection_context(connection):
    return connection
