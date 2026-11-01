"""Authenticated upload progress shared by processes using the same SQLite database."""
import json
import hashlib
import logging
import secrets
import sqlite3
import time
from contextlib import closing

from fastapi import HTTPException

from .storage import db_conn

LOGGER = logging.getLogger(__name__)


def admin_owner(token: str) -> str:
    return "admin:" + hashlib.sha256(token.encode()).hexdigest()


def create_job(owner: str) -> dict:
    job_id = secrets.token_urlsafe(24)
    data = {"jobId": job_id, "status": "waiting", "phase": "waiting", "percent": 0}
    with closing(db_conn()) as conn, conn:
        conn.execute("DELETE FROM import_jobs WHERE updated_at < ?", (time.time() - 86400,))
        conn.execute("INSERT INTO import_jobs VALUES (?,?,?,?,?)",
                     (job_id, owner, "waiting", json.dumps(data), time.time()))
    return data


def read_job(owner: str, job_id: str) -> dict:
    with closing(db_conn()) as conn:
        row = conn.execute("SELECT progress FROM import_jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Import progress not found")
    return json.loads(row["progress"])


class ImportProgress:
    def __init__(self, owner: str, job_id: str):
        self.owner, self.job_id = owner, job_id
        self.data = read_job(owner, job_id)
        with closing(db_conn()) as conn, conn:
            claimed = conn.execute("UPDATE import_jobs SET status='running',updated_at=? WHERE id=? AND owner=? AND status='waiting'",
                                   (time.time(), job_id, owner)).rowcount
        if not claimed:
            raise HTTPException(status_code=409, detail="Import progress already used")
        self.last_write = 0
        self.update(status="running", phase="analyzing")

    def update(self, **values):
        previous_phase = self.data.get("phase")
        previous_file = self.data.get("fileIndex")
        self.data.update(values)
        now = time.monotonic()
        if (values.get("phase") == previous_phase and values.get("fileIndex") == previous_file
                and self.data["status"] == "running" and now - self.last_write < 0.4):
            return
        try:
            with closing(db_conn()) as conn, conn:
                # Progress is auxiliary: do not stall ffmpeg/the event loop behind a writer.
                conn.execute("PRAGMA busy_timeout = 100")
                conn.execute("UPDATE import_jobs SET status=?,progress=?,updated_at=? WHERE id=? AND owner=?",
                             (self.data["status"], json.dumps(self.data, ensure_ascii=False), time.time(), self.job_id, self.owner))
        except sqlite3.OperationalError:
            LOGGER.warning("Import progress update temporarily unavailable")
        self.last_write = now

    def finish(self):
        self.update(status="complete", phase="complete", percent=100, etaSeconds=None)

    def fail(self, error):
        detail = getattr(error, "detail", None)
        self.update(status="failed", phase="failed", error=str(detail or "Import interrupted or failed"), etaSeconds=None)
