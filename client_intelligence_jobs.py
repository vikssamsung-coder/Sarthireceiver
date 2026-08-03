"""SQLite job registry for the Sarthi Evaluator integration."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: str | Path) -> None:
    with _connect(db_path) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS client_intelligence_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                config_json TEXT NOT NULL,
                command_json TEXT,
                pid INTEGER,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                return_code INTEGER,
                message TEXT,
                log_path TEXT
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_ci_jobs_status "
            "ON client_intelligence_jobs(status, id)"
        )


def create_job(mode: str, config: dict, db_path: str | Path) -> int:
    init_db(db_path)
    with _connect(db_path) as con:
        cur = con.execute(
            "INSERT INTO client_intelligence_jobs "
            "(mode,status,config_json,created_at) VALUES (?,?,?,?)",
            (mode, "queued", json.dumps(config), _now()),
        )
        return int(cur.lastrowid)


def get_job(job_id: int, db_path: str | Path) -> dict | None:
    with _connect(db_path) as con:
        row = con.execute(
            "SELECT * FROM client_intelligence_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return dict(row) if row else None


def list_jobs(db_path: str | Path, limit: int = 100) -> list[dict]:
    init_db(db_path)
    with _connect(db_path) as con:
        rows = con.execute(
            "SELECT * FROM client_intelligence_jobs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def _process_exists(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ProcessLookupError, ValueError):
        return False


def recover_orphaned_jobs(db_path: str | Path, grace_seconds: int = 120) -> int:
    """Release UI locks left behind when a detached worker never starts or exits."""
    now = datetime.now()
    recovered = 0
    for job in list_jobs(db_path, 500):
        if job["status"] not in {"queued", "running", "cancel_requested"}:
            continue
        stamp = job.get("started_at") or job.get("created_at")
        try:
            age = (now - datetime.fromisoformat(stamp)).total_seconds()
        except (TypeError, ValueError):
            age = grace_seconds + 1
        if age <= grace_seconds:
            continue
        if job.get("pid") and _process_exists(job["pid"]):
            continue
        final_status = "cancelled" if job["status"] == "cancel_requested" else "failed"
        update_job(
            job["id"], db_path, status=final_status, finished_at=_now(),
            return_code=-1,
            message="Previous worker is no longer running. The job lock was cleared automatically.",
        )
        recovered += 1
    return recovered


def update_job(job_id: int, db_path: str | Path, **fields) -> None:
    allowed = {
        "status", "command_json", "pid", "started_at", "finished_at",
        "return_code", "message", "log_path",
    }
    values = {k: v for k, v in fields.items() if k in allowed}
    if not values:
        return
    sql = ", ".join(f"{key}=?" for key in values)
    with _connect(db_path) as con:
        con.execute(
            f"UPDATE client_intelligence_jobs SET {sql} WHERE id=?",
            (*values.values(), job_id),
        )


def request_cancel(job_id: int, db_path: str | Path) -> bool:
    job = get_job(job_id, db_path)
    if not job or job["status"] not in {"queued", "running", "cancel_requested"}:
        return False
    update_job(
        job_id, db_path, status="cancel_requested",
        message="Cancellation requested by user.",
    )
    return True
