# -*- coding: utf-8 -*-
r"""
intake_queue.py — the durable queue between "Outlook caught a mail" and
"the processor handled it".

WHY A QUEUE (the architecture you asked for):
  The old poll opened a second Outlook COM server that fought with MIS. The new
  path is event-driven: Outlook's own VBA ItemAdd fires the instant mail lands,
  saves the attachment, and drops a JOB ROW here. A worker in the app drains it.

  Result:
    * no polling, no 60s scan, no second COM server -> no 0x80080005 collisions
    * catching mail is decoupled from processing it — a slow flow never blocks
      Outlook, and a burst of mail just queues up
    * every job is durable: if the app is down when mail lands, the row waits;
      the worker picks it up when the app is next running

Same SQLite file as everything else (dump_flows.sqlite3), so the VBA and the
app agree on it without configuration. New table only; touches nothing existing.

The VBA writes rows via sqlite3.exe (see SarthiDirectReceiver.bas). Python reads
and drains them here. INSERT is the ONLY thing the VBA does — all the logic
stays in Python.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import dump_flows as df

DEFAULT_DB = df.DEFAULT_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS intake_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL DEFAULT 'vba',   -- 'vba' | 'manual' | 'poll'
    file_path   TEXT NOT NULL,                 -- the attachment the watcher saved
    subject     TEXT,
    sender      TEXT,
    body        TEXT,
    dump_type   TEXT,                           -- optional: watcher named it
    entry_id    TEXT,                           -- Outlook EntryID, for dedupe
    status      TEXT NOT NULL DEFAULT 'queued', -- queued|claimed|done|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    message     TEXT,
    created_at  TEXT,
    claimed_at  TEXT
);
CREATE INDEX IF NOT EXISTS ix_intake_status ON intake_queue(status, id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_intake_entry ON intake_queue(entry_id)
    WHERE entry_id IS NOT NULL AND entry_id <> '';
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _conn(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.executescript(_SCHEMA)
    return c


def init_db(db_path: Path = DEFAULT_DB) -> None:
    _conn(db_path).close()


def enqueue(file_path, subject="", sender="", body="", dump_type="",
            entry_id="", source="manual", db_path: Path = DEFAULT_DB):
    """Add a job. Returns row id, or None if this entry_id was already queued
    (Outlook can fire ItemAdd more than once for the same mail — the unique
    index on entry_id collapses duplicates)."""
    with _conn(db_path) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO intake_queue"
            "(source,file_path,subject,sender,body,dump_type,entry_id,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,'queued',?)",
            (source, str(file_path), subject or "", sender or "", body or "",
             dump_type or "", entry_id or "", _now()))
        return int(cur.lastrowid) if cur.rowcount else None


def claim_next(db_path: Path = DEFAULT_DB):
    """Claim the oldest queued job. Single worker, but this is still atomic so a
    second worker (or a manual drain) can't grab the same row."""
    c = _conn(db_path)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT * FROM intake_queue WHERE status='queued' "
                        "ORDER BY id LIMIT 1").fetchone()
        if not row:
            c.commit()
            return None
        c.execute("UPDATE intake_queue SET status='claimed', claimed_at=?, "
                  "attempts=attempts+1 WHERE id=? AND status='queued'",
                  (_now(), row["id"]))
        c.commit()
        return dict(row)
    except sqlite3.Error:
        c.rollback()
        return None
    finally:
        c.close()


def finish(job_id, status, message="", db_path: Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.execute("UPDATE intake_queue SET status=?, message=? WHERE id=?",
                  (status, (message or "")[:2000], int(job_id)))


def release_stale(minutes=30, db_path: Path = DEFAULT_DB) -> int:
    """A claimed job whose worker died gets re-queued (not failed — mail intake
    should retry). Distinct from MIS, where a stale claim fails."""
    with _conn(db_path) as c:
        cur = c.execute(
            "UPDATE intake_queue SET status='queued', "
            "message=COALESCE(message,'')||' [requeued: stale claim]' "
            "WHERE status='claimed' AND claimed_at IS NOT NULL "
            "AND (julianday('now','localtime')-julianday(claimed_at))*1440 > ?",
            (int(minutes),))
        return cur.rowcount or 0


def list_jobs(limit=100, status=None, db_path: Path = DEFAULT_DB) -> list:
    with _conn(db_path) as c:
        if status:
            rows = c.execute("SELECT * FROM intake_queue WHERE status=? "
                             "ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM intake_queue ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
    return [dict(r) for r in rows]


def counts(db_path: Path = DEFAULT_DB) -> dict:
    with _conn(db_path) as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM intake_queue "
                         "GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def last_activity(db_path: Path = DEFAULT_DB):
    """Newest created_at — used by the app to warn if intake has gone quiet."""
    with _conn(db_path) as c:
        r = c.execute("SELECT MAX(created_at) m FROM intake_queue").fetchone()
    return r["m"] if r else None
