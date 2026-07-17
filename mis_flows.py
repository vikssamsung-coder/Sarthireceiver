# -*- coding: utf-8 -*-
r"""
MIS registry — the data layer for the MIS Builder.

Mirrors dump_flows.py exactly: same SQLite file, same Path-based db_path
convention, same _MIGRATIONS discipline. Pure Python + SQLite — no Outlook,
no Streamlit — so it tests anywhere.

DEFAULT_DB is IMPORTED from dump_flows. There is no second copy of that path.

Four new tables, all CREATE TABLE IF NOT EXISTS (safe on existing installs):
  mis_types       — a report: where it writes, when it fires, what it waits on
  mis_flow_steps  — its build steps, in order (same shape as dump_flow_steps)
  mis_queue       — the single funnel. Three producers, one worker.
  mis_runs        — every run recorded (same reason as flow_runs)

Nothing here writes to dump_types / dump_flow_steps / flow_runs. It only READS
flow_runs, to know whether today's dumps have landed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, date
from pathlib import Path

import dump_flows as df

DEFAULT_DB = df.DEFAULT_DB          # one path, one place. Never re-declared.

TRIGGERS = ("request", "schedule", "dump", "manual")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mis_types (
    key            TEXT PRIMARY KEY,
    name           TEXT,
    handler        TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    sort_order     INTEGER NOT NULL DEFAULT 100,
    out_folder     TEXT,
    source         TEXT NOT NULL DEFAULT 'local',
    schedule_time  TEXT,
    schedule_days  TEXT NOT NULL DEFAULT '1111100',
    trigger_json   TEXT NOT NULL DEFAULT '{"mode":"all","keys":[]}',
    last_fired_at  TEXT,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mis_flow_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mis_type_key  TEXT NOT NULL,
    step_order    INTEGER NOT NULL DEFAULT 100,
    step_name     TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'python',
    target_path   TEXT NOT NULL,
    args_json     TEXT NOT NULL DEFAULT '[]',
    working_dir   TEXT,
    on_failure    TEXT NOT NULL DEFAULT 'stop',
    timeout_sec   INTEGER NOT NULL DEFAULT 0,
    enabled       INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mis_queue (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key       TEXT NOT NULL UNIQUE,
    report_key       TEXT NOT NULL,
    params           TEXT NOT NULL DEFAULT '',
    trigger          TEXT NOT NULL,
    req_id           TEXT,
    user_key         TEXT,
    requester_email  TEXT,
    status           TEXT NOT NULL DEFAULT 'queued',
    attempts         INTEGER NOT NULL DEFAULT 0,
    message          TEXT,
    created_at       TEXT,
    claimed_at       TEXT
);

CREATE TABLE IF NOT EXISTS mis_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id     INTEGER,
    req_id       TEXT,
    report_key   TEXT,
    trigger      TEXT,
    requester    TEXT,
    output_path  TEXT,
    status       TEXT NOT NULL,
    steps_json   TEXT NOT NULL DEFAULT '[]',
    message      TEXT,
    started_at   TEXT,
    finished_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_mis_steps  ON mis_flow_steps(mis_type_key, step_order);
CREATE INDEX IF NOT EXISTS ix_mis_queue  ON mis_queue(status, created_at);
CREATE INDEX IF NOT EXISTS ix_mis_runs   ON mis_runs(report_key, id);
"""

# Column adds go HERE, keyed by table.column — never by editing _SCHEMA, or
# existing installs won't pick them up. Same rule as dump_flows._MIGRATIONS.
_MIGRATIONS = {
    "mis_types.schedule_time": "ALTER TABLE mis_types ADD COLUMN schedule_time TEXT",
    "mis_types.schedule_days": "ALTER TABLE mis_types ADD COLUMN schedule_days TEXT DEFAULT '1111100'",
    "mis_types.trigger_json": "ALTER TABLE mis_types ADD COLUMN trigger_json TEXT DEFAULT '{\"mode\":\"all\",\"keys\":[]}'",
    "mis_types.last_fired_at": "ALTER TABLE mis_types ADD COLUMN last_fired_at TEXT",
    "mis_types.source": "ALTER TABLE mis_types ADD COLUMN source TEXT DEFAULT 'local'",
    "mis_queue.attempts": "ALTER TABLE mis_queue ADD COLUMN attempts INTEGER DEFAULT 0",
    "mis_queue.message": "ALTER TABLE mis_queue ADD COLUMN message TEXT",
    "mis_runs.trigger": "ALTER TABLE mis_runs ADD COLUMN trigger TEXT",
    "mis_runs.queue_id": "ALTER TABLE mis_runs ADD COLUMN queue_id INTEGER",
    "mis_flow_steps.timeout_sec": "ALTER TABLE mis_flow_steps ADD COLUMN timeout_sec INTEGER DEFAULT 0",
}

# The dump statuses that mean "this feed landed cleanly today".
# flow_engine writes: success | partial | failed | dry-run.
# 'partial' is EXCLUDED on purpose — a step failed, so the data is suspect.
_DUMP_OK = ("success", "ok", "done", "completed")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


def _conn(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path), timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    c.executescript(_SCHEMA)
    for dotted, ddl in _MIGRATIONS.items():
        table, col = dotted.split(".", 1)
        try:
            c.execute(f"SELECT {col} FROM {table} LIMIT 1")
        except Exception:
            try:
                c.execute(ddl)
            except Exception:
                pass
    return c


def init_db(db_path: Path = DEFAULT_DB) -> None:
    _conn(db_path).close()


# ---------------------------------------------------------------------------
# mis_types
# ---------------------------------------------------------------------------
def list_mis_types(enabled_only=False, db_path: Path = DEFAULT_DB) -> list:
    sql = "SELECT * FROM mis_types"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY sort_order, key"
    with _conn(db_path) as c:
        return [dict(r) for r in c.execute(sql).fetchall()]


def get_mis_type(key, db_path: Path = DEFAULT_DB):
    with _conn(db_path) as c:
        row = c.execute("SELECT * FROM mis_types WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def upsert_mis_type(key, name, enabled=1, sort_order=100, out_folder=None,
                    handler=None, source=None, schedule_time=None,
                    schedule_days="1111100", trigger=None,
                    db_path: Path = DEFAULT_DB) -> None:
    trig = json.dumps(trigger if trigger is not None else {"mode": "all", "keys": []})
    with _conn(db_path) as c:
        exists = c.execute("SELECT 1 FROM mis_types WHERE key=?", (key,)).fetchone()
        if exists:
            c.execute(
                "UPDATE mis_types SET name=?, enabled=?, sort_order=?, out_folder=?, "
                "handler=COALESCE(?,handler), source=COALESCE(?,source), "
                "schedule_time=?, schedule_days=?, trigger_json=?, updated_at=? "
                "WHERE key=?",
                (name, int(enabled), int(sort_order), out_folder or None,
                 handler, source, schedule_time or None, schedule_days, trig,
                 _now(), key))
        else:
            c.execute(
                "INSERT INTO mis_types(key,name,enabled,sort_order,out_folder,handler,"
                "source,schedule_time,schedule_days,trigger_json,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, name or key, int(enabled), int(sort_order), out_folder or None,
                 handler, source or "local", schedule_time or None, schedule_days,
                 trig, _now()))


def delete_mis_type(key, db_path: Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.execute("DELETE FROM mis_flow_steps WHERE mis_type_key=?", (key,))
        c.execute("DELETE FROM mis_types WHERE key=?", (key,))


def mark_fired(key, when=None, db_path: Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.execute("UPDATE mis_types SET last_fired_at=? WHERE key=?",
                  (when or _now(), key))


def get_trigger(t: dict) -> dict:
    """Parse trigger_json defensively. Bad JSON -> no dump trigger, never a crash."""
    try:
        d = json.loads(t.get("trigger_json") or "{}")
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    mode = str(d.get("mode") or "all").lower()
    if mode not in ("all", "any"):
        mode = "all"
    keys = d.get("keys") or []
    if not isinstance(keys, list):
        keys = []
    return {"mode": mode, "keys": [str(k) for k in keys if str(k).strip()]}


# ---------------------------------------------------------------------------
# mis_flow_steps  (same shape + same helpers as dump_flow_steps)
# ---------------------------------------------------------------------------
def get_mis_steps(key, db_path: Path = DEFAULT_DB) -> list:
    with _conn(db_path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM mis_flow_steps WHERE mis_type_key=? ORDER BY step_order,id",
            (key,)).fetchall()]


def set_mis_steps(key, steps: list, db_path: Path = DEFAULT_DB) -> None:
    """Same semantics as dump_flows.set_steps — replaces the whole list."""
    with _conn(db_path) as c:
        c.execute("DELETE FROM mis_flow_steps WHERE mis_type_key=?", (key,))
        for i, s in enumerate(steps):
            args = s.get("args_json", [])
            if isinstance(args, str):
                try:
                    json.loads(args)          # already JSON -> keep
                except Exception:
                    args = json.dumps(args)   # template string -> wrap as JSON string
            else:
                args = json.dumps(args)
            c.execute(
                "INSERT INTO mis_flow_steps(mis_type_key,step_order,step_name,kind,"
                "target_path,args_json,working_dir,on_failure,timeout_sec,enabled,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (key, int(s.get("step_order", (i + 1) * 10)), s["step_name"],
                 s.get("kind", "python"), s["target_path"], args,
                 s.get("working_dir") or None, s.get("on_failure", "stop"),
                 int(s.get("timeout_sec", 0) or 0),
                 int(s.get("enabled", 1)), _now()))


def build_mis_steps(key, context: dict, db_path: Path = DEFAULT_DB) -> list:
    """Render each enabled step's args. Reuses dump_flows._render_args —
    the flag-dropping and Windows-path handling live in ONE place."""
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM mis_flow_steps WHERE mis_type_key=? AND enabled=1 "
            "ORDER BY step_order, id", (key,)).fetchall()
    steps = []
    for r in rows:
        steps.append({
            "step_name": r["step_name"], "kind": r["kind"],
            "target_path": r["target_path"],
            "args": df._render_args(json.loads(r["args_json"] or "[]"), context),
            "working_dir": r["working_dir"] or None,
            "on_failure": r["on_failure"] or "stop",
            "timeout_sec": (r["timeout_sec"] if "timeout_sec" in r.keys() else 0) or 0,
        })
    return steps


# ---------------------------------------------------------------------------
# mis_queue — one funnel, three producers, one worker
# ---------------------------------------------------------------------------
def enqueue(report_key, trigger, dedupe_key, params="", req_id=None,
            user_key=None, requester_email=None, db_path: Path = DEFAULT_DB):
    """INSERT OR IGNORE on dedupe_key. Returns the new row id, or None if it was
    already queued. That is the idempotency guarantee: a restart mid-tick, a
    re-delivered request and a second dump-complete callback all collapse to one."""
    with _conn(db_path) as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO mis_queue(dedupe_key,report_key,params,trigger,"
            "req_id,user_key,requester_email,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,'queued',?)",
            (dedupe_key, report_key, params or "", trigger, req_id, user_key,
             requester_email, _now()))
        return int(cur.lastrowid) if cur.rowcount else None


def claim_next(db_path: Path = DEFAULT_DB):
    """Claim the oldest queued item whose report has nothing already in flight.

    This serialises two triggers for the SAME report — a 09:00 schedule and a
    dump landing at 09:00 queue behind each other instead of racing."""
    c = _conn(db_path)
    try:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT * FROM mis_queue WHERE status='queued' "
            "AND report_key NOT IN (SELECT report_key FROM mis_queue WHERE status='claimed') "
            "ORDER BY created_at, id LIMIT 1").fetchone()
        if not row:
            c.commit()
            return None
        c.execute("UPDATE mis_queue SET status='claimed', claimed_at=?, attempts=attempts+1 "
                  "WHERE id=? AND status='queued'", (_now(), row["id"]))
        c.commit()
        return dict(row)
    except Exception:
        c.rollback()
        return None
    finally:
        c.close()


def finish_queue_item(queue_id, status, message="", db_path: Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.execute("UPDATE mis_queue SET status=?, message=? WHERE id=?",
                  (status, (message or "")[:2000], int(queue_id)))


def release_stale_claims(minutes=120, db_path: Path = DEFAULT_DB) -> int:
    """A crash mid-build leaves a 'claimed' row that would block that report forever."""
    with _conn(db_path) as c:
        cur = c.execute(
            "UPDATE mis_queue SET status='failed', "
            "message=COALESCE(message,'')||' [released: stale claim]' "
            "WHERE status='claimed' AND claimed_at IS NOT NULL "
            "AND (julianday('now','localtime') - julianday(claimed_at))*1440 > ?",
            (int(minutes),))
        return cur.rowcount or 0


def list_queue(limit=100, db_path: Path = DEFAULT_DB) -> list:
    with _conn(db_path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM mis_queue ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]


def clear_queue(status=None, db_path: Path = DEFAULT_DB) -> int:
    with _conn(db_path) as c:
        if status:
            cur = c.execute("DELETE FROM mis_queue WHERE status=?", (status,))
        else:
            cur = c.execute("DELETE FROM mis_queue")
        return cur.rowcount or 0


# ---------------------------------------------------------------------------
# mis_runs — every run recorded. This is why the app is debuggable.
# ---------------------------------------------------------------------------
def start_mis_run(report_key, trigger, queue_id=None, req_id=None, requester=None,
                  db_path: Path = DEFAULT_DB) -> int:
    with _conn(db_path) as c:
        cur = c.execute(
            "INSERT INTO mis_runs(queue_id,req_id,report_key,trigger,requester,"
            "status,started_at) VALUES(?,?,?,?,?,'running',?)",
            (queue_id, req_id, report_key, trigger, requester, _now()))
        return int(cur.lastrowid)


def finish_mis_run(run_id, status, steps=None, output_path="", message="",
                   db_path: Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.execute(
            "UPDATE mis_runs SET status=?, steps_json=?, output_path=?, message=?, "
            "finished_at=? WHERE id=?",
            (status, json.dumps(steps or []), str(output_path or ""),
             (message or "")[:4000], _now(), int(run_id)))


def list_mis_runs(limit=200, report_key=None, db_path: Path = DEFAULT_DB) -> list:
    with _conn(db_path) as c:
        if report_key:
            rows = c.execute("SELECT * FROM mis_runs WHERE report_key=? "
                             "ORDER BY id DESC LIMIT ?", (report_key, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM mis_runs ORDER BY id DESC LIMIT ?",
                             (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# READ-ONLY view of the dump side — how MIS knows a feed has landed
# ---------------------------------------------------------------------------
def dump_succeeded_today(dump_key, on_date=None, db_path: Path = DEFAULT_DB) -> bool:
    """True if flow_runs has a SUCCESS for this dump type on that date.

    flow_engine writes timestamps as ISO (2026-07-13T08:30:00), so the first
    10 chars are the date either way."""
    d = on_date or _today()
    qs = ",".join("?" * len(_DUMP_OK))
    try:
        with _conn(db_path) as c:
            row = c.execute(
                f"SELECT 1 FROM flow_runs WHERE dump_type=? "
                f"AND LOWER(status) IN ({qs}) "
                f"AND substr(COALESCE(finished_at,started_at),1,10)=? LIMIT 1",
                (dump_key, *_DUMP_OK, d)).fetchone()
        return bool(row)
    except Exception:
        return False


if __name__ == "__main__":
    import tempfile
    p = Path(tempfile.mkdtemp()) / "flows.sqlite3"
    init_db(p)
    upsert_mis_type("demo", "Demo", out_folder="/tmp", db_path=p)
    print("types:", [t["key"] for t in list_mis_types(db_path=p)])
