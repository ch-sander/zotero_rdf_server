import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

UTC = timezone.utc


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)  # autocommit
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA foreign_keys=ON;")
    return con


def init_db(db_path: Path) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,

                items_json_path TEXT NOT NULL,
                params_json TEXT NOT NULL,

                cancel_requested INTEGER NOT NULL DEFAULT 0,
                error TEXT DEFAULT NULL
            );
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS job_runtime (
                job_id TEXT PRIMARY KEY REFERENCES jobs(job_id) ON DELETE CASCADE,

                locked_by TEXT DEFAULT NULL,
                lock_until TEXT DEFAULT NULL,
                heartbeat_at TEXT DEFAULT NULL,

                phase TEXT DEFAULT NULL,
                item_index INTEGER DEFAULT NULL,
                total_items INTEGER DEFAULT NULL,
                doc_id TEXT DEFAULT NULL,
                page INTEGER DEFAULT NULL,
                pages_emitted INTEGER DEFAULT NULL,

                last_update TEXT NOT NULL
            );
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at);")
    finally:
        con.close()


def create_job(
    db_path: Path,
    job_id: str,
    items_json_path: str,
    params: Dict[str, Any],
) -> str:
    init_db(db_path)
    now = _utcnow_iso()
    con = _connect(db_path)
    try:
        con.execute(
            """
            INSERT INTO jobs(job_id, state, created_at, updated_at, items_json_path, params_json, cancel_requested, error)
            VALUES (?, 'QUEUED', ?, ?, ?, ?, 0, NULL)
            """,
            (job_id, now, now, items_json_path, json.dumps(params, ensure_ascii=False)),
        )
        con.execute(
            """
            INSERT OR REPLACE INTO job_runtime(job_id, last_update)
            VALUES (?, ?)
            """,
            (job_id, now),
        )
        return job_id
    finally:
        con.close()


def set_state(db_path: Path, job_id: str, state: str, error: Optional[str] = None) -> None:
    now = _utcnow_iso()
    con = _connect(db_path)
    try:
        con.execute(
            "UPDATE jobs SET state=?, updated_at=?, error=? WHERE job_id=?",
            (state, now, error, job_id),
        )
        con.execute(
            "UPDATE job_runtime SET last_update=? WHERE job_id=?",
            (now, job_id),
        )
    finally:
        con.close()


def request_cancel(db_path: Path, job_id: str) -> None:
    now = _utcnow_iso()
    con = _connect(db_path)
    try:
        con.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE job_id=?", (now, job_id))
    finally:
        con.close()


def is_cancel_requested(db_path: Path, job_id: str) -> bool:
    con = _connect(db_path)
    try:
        row = con.execute("SELECT cancel_requested FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return bool(row and row[0] == 1)
    finally:
        con.close()


@dataclass
class Job:
    job_id: str
    state: str
    items_json_path: str
    params: Dict[str, Any]
    cancel_requested: bool
    error: Optional[str]


def get_job(db_path: Path, job_id: str) -> Job:
    con = _connect(db_path)
    try:
        row = con.execute(
            "SELECT job_id, state, items_json_path, params_json, cancel_requested, error FROM jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"unknown job_id: {job_id}")
        return Job(
            job_id=row[0],
            state=row[1],
            items_json_path=row[2],
            params=json.loads(row[3]),
            cancel_requested=bool(row[4] == 1),
            error=row[5],
        )
    finally:
        con.close()


def update_progress(
    db_path: Path,
    job_id: str,
    *,
    phase: Optional[str] = None,
    item_index: Optional[int] = None,
    total_items: Optional[int] = None,
    doc_id: Optional[str] = None,
    page: Optional[int] = None,
    pages_emitted: Optional[int] = None,
) -> None:
    now = _utcnow_iso()
    con = _connect(db_path)
    try:
        con.execute(
            """
            UPDATE job_runtime
            SET
              phase = COALESCE(?, phase),
              item_index = COALESCE(?, item_index),
              total_items = COALESCE(?, total_items),
              doc_id = COALESCE(?, doc_id),
              page = COALESCE(?, page),
              pages_emitted = COALESCE(?, pages_emitted),
              heartbeat_at = ?,
              last_update = ?
            WHERE job_id = ?
            """,
            (phase, item_index, total_items, doc_id, page, pages_emitted, now, now, job_id),
        )
        con.execute("UPDATE jobs SET updated_at=? WHERE job_id=?", (now, job_id))
    finally:
        con.close()


def claim_job(db_path: Path, job_id: str, *, worker_id: str, lease_seconds: int = 120) -> bool:
    """Try to claim an existing job by setting a lease. Returns True if claimed by this worker."""
    init_db(db_path)
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    lock_until_iso = (now + timedelta(seconds=lease_seconds)).isoformat()

    con = _connect(db_path)
    try:
        # claim if unlocked or expired
        cur = con.execute(
            """
            UPDATE job_runtime
            SET locked_by=?, lock_until=?, heartbeat_at=?, last_update=?
            WHERE job_id=?
              AND (lock_until IS NULL OR lock_until < ? OR locked_by IS NULL)
            """,
            (worker_id, lock_until_iso, now_iso, now_iso, job_id, now_iso),
        )
        return cur.rowcount == 1
    finally:
        con.close()


def renew_lease(db_path: Path, job_id: str, *, worker_id: str, lease_seconds: int = 120) -> bool:
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    lock_until_iso = (now + timedelta(seconds=lease_seconds)).isoformat()

    con = _connect(db_path)
    try:
        cur = con.execute(
            """
            UPDATE job_runtime
            SET lock_until=?, heartbeat_at=?, last_update=?
            WHERE job_id=? AND locked_by=?
            """,
            (lock_until_iso, now_iso, now_iso, job_id, worker_id),
        )
        return cur.rowcount == 1
    finally:
        con.close()


def release_lease(db_path: Path, job_id: str, *, worker_id: str) -> None:
    now = _utcnow_iso()
    con = _connect(db_path)
    try:
        con.execute(
            """
            UPDATE job_runtime
            SET locked_by=NULL, lock_until=NULL, last_update=?
            WHERE job_id=? AND locked_by=?
            """,
            (now, job_id, worker_id),
        )
    finally:
        con.close()


def get_runtime(db_path: Path, job_id: str) -> Dict[str, Any]:
    con = _connect(db_path)
    try:
        row = con.execute(
            """
            SELECT locked_by, lock_until, heartbeat_at, phase, item_index, total_items, doc_id, page, pages_emitted, last_update
            FROM job_runtime
            WHERE job_id=?
            """,
            (job_id,),
        ).fetchone()
        if not row:
            return {}
        keys = ["locked_by", "lock_until", "heartbeat_at", "phase", "item_index", "total_items", "doc_id", "page", "pages_emitted", "last_update"]
        return dict(zip(keys, row))
    finally:
        con.close()
