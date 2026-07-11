# -*- coding: utf-8 -*-
r"""
Dump flow registry (v2) for the Sarthi receiver.

Replaces the two hardcoded functions in email_processor.py with data:
  - detection  -> form-based recognition: rule groups of conditions on
                  sender / subject / body / attachment / anywhere.
  - sequence   -> ordered flow steps per dump type.

Also: per-type save folder, run confirmations, and Neon catalog sync.
Pure Python + SQLite — no Outlook, no Streamlit — so it tests anywhere.

Recognition model (stored as JSON on the dump type):
  groups = [
    { "mode": "all" | "any",
      "conditions": [
        {"field":"sender","op":"is_one_of","values":["a@x.co","b@x.co"]},
        {"field":"subject","op":"contains","value":"order file"},
        ...
      ] },
    ...            # groups are OR'd together
  ]
A type matches if ANY group matches. Within a group, ALL or ANY per mode.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path(r"D:\Sarthi\multipart_buffer\dump_flows.sqlite3")

FIELDS = ["sender", "subject", "body", "attachment", "anywhere"]
OPS_TEXT = ["contains", "equals", "matches"]      # subject/body/attachment/anywhere
OPS_SENDER = ["is", "is_one_of"]                   # sender

import sqlite3
import shlex

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dump_types (
    key               TEXT PRIMARY KEY,
    name              TEXT,
    enabled           INTEGER NOT NULL DEFAULT 1,
    sort_order        INTEGER NOT NULL DEFAULT 100,
    save_folder       TEXT,
    handler           TEXT,
    max_files         INTEGER,
    source            TEXT NOT NULL DEFAULT 'local',
    recognition_json  TEXT NOT NULL DEFAULT '{"groups":[]}',
    updated_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS dump_flow_steps (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dump_type_key TEXT NOT NULL,
    step_order    INTEGER NOT NULL DEFAULT 100,
    step_name     TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'python',
    target_path   TEXT NOT NULL,
    args_json     TEXT NOT NULL DEFAULT '[]',
    working_dir   TEXT,
    on_failure    TEXT NOT NULL DEFAULT 'stop',
    enabled       INTEGER NOT NULL DEFAULT 1,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS flow_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT,
    dump_type     TEXT,
    saved_path    TEXT,
    status        TEXT NOT NULL,
    steps_json    TEXT NOT NULL DEFAULT '[]',
    message       TEXT,
    started_at    TEXT,
    finished_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

_MIGRATIONS = {
    "save_folder": "ALTER TABLE dump_types ADD COLUMN save_folder TEXT",
    "handler": "ALTER TABLE dump_types ADD COLUMN handler TEXT",
    "max_files": "ALTER TABLE dump_types ADD COLUMN max_files INTEGER",
    "source": "ALTER TABLE dump_types ADD COLUMN source TEXT DEFAULT 'local'",
    "recognition_json": "ALTER TABLE dump_types ADD COLUMN recognition_json TEXT DEFAULT '{\"groups\":[]}'",
}


def _conn(db_path: Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    for col, ddl in _MIGRATIONS.items():
        try:
            c.execute(f"SELECT {col} FROM dump_types LIMIT 1")
        except Exception:
            c.execute(ddl)
    return c


def init_db(db_path: Path = DEFAULT_DB) -> None:
    _conn(db_path).close()


# ---------------------------------------------------------------------------
# recognition
# ---------------------------------------------------------------------------
def _field_text(field: str, ctx: dict) -> str:
    subj = str(ctx.get("subject") or "").lower()
    body = str(ctx.get("body") or "").lower()
    atts = " ".join(str(a or "") for a in (ctx.get("attachments") or [])).lower()
    extra = str(ctx.get("final_package_name") or "").lower()
    if field == "sender":
        return str(ctx.get("sender") or "").lower()
    if field == "subject":
        return subj
    if field == "body":
        return body
    if field == "attachment":
        return atts
    return " ".join([subj, body, atts, extra])  # anywhere


def _cond_match(cond: dict, ctx: dict) -> bool:
    field = cond.get("field", "anywhere")
    op = cond.get("op", "contains")
    text = _field_text(field, ctx)
    if op == "is_one_of":
        vals = [str(v or "").strip().lower() for v in cond.get("values", []) if str(v).strip()]
        return text.strip() in vals
    val = str(cond.get("value") or "").strip().lower()
    if not val:
        return False
    if op in ("is", "equals"):
        return text.strip() == val
    if op == "matches":
        try:
            return re.search(cond.get("value"), text, re.IGNORECASE) is not None
        except re.error:
            return False
    return val in text  # contains (default)


def evaluate(groups: list, ctx: dict) -> bool:
    """True if ANY group matches. A group matches if (mode=all → every condition)
    or (mode=any → at least one). Empty / no-condition groups never match."""
    for g in groups or []:
        conds = g.get("conditions") or []
        if not conds:
            continue
        mode = (g.get("mode") or "all").lower()
        results = [_cond_match(c, ctx) for c in conds]
        if (mode == "any" and any(results)) or (mode != "any" and all(results)):
            return True
    return False


def get_recognition(key, db_path: Path = DEFAULT_DB) -> list:
    with _conn(db_path) as c:
        row = c.execute("SELECT recognition_json FROM dump_types WHERE key=?", (key,)).fetchone()
    if not row or not row["recognition_json"]:
        return []
    try:
        return json.loads(row["recognition_json"]).get("groups", [])
    except Exception:
        return []


def set_recognition(key, groups, db_path: Path = DEFAULT_DB) -> None:
    payload = json.dumps({"groups": groups or []})
    with _conn(db_path) as c:
        c.execute("UPDATE dump_types SET recognition_json=?, updated_at=? WHERE key=?",
                  (payload, datetime.now().isoformat(timespec="seconds"), key))


def resolve(subject="", body="", sender="", attachments=None, meta=None,
            final_package_name=None, db_path: Path = DEFAULT_DB) -> str:
    """Route to a dump type key.

    1) If the email carries a stamped label (dump_type_handler / dump_type_key /
       report_key) that matches a catalog key or handler, use it.
    2) Otherwise evaluate each enabled type's recognition rules in detect order;
       first match wins.
    Returns the key or 'unknown'.
    """
    meta = meta or {}
    ctx = {"subject": subject, "body": body, "sender": sender,
           "attachments": attachments or [], "final_package_name": final_package_name}

    stamped = [str(meta.get("dump_type_handler") or "").strip(),
               str(meta.get("dump_type_key") or "").strip(),
               str(meta.get("report_key") or "").strip()]

    with _conn(db_path) as c:
        for cand in stamped:
            if not cand:
                continue
            row = c.execute(
                "SELECT key FROM dump_types WHERE (key=? OR handler=?) AND enabled=1",
                (cand, cand)).fetchone()
            if row:
                return row["key"]
        types = c.execute(
            "SELECT key, recognition_json FROM dump_types WHERE enabled=1 ORDER BY sort_order, key"
        ).fetchall()

    for t in types:
        try:
            groups = json.loads(t["recognition_json"] or '{"groups":[]}').get("groups", [])
        except Exception:
            groups = []
        if evaluate(groups, ctx):
            return t["key"]
    return "unknown"


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
class _SafeDict(dict):
    def __missing__(self, k):
        return ""


def _render_args(args_spec, context: dict) -> list:
    ctx = _SafeDict(context)
    # friendly form: a single template string, e.g. "--input-file {assembled_path} --file-mode AUTO"
    if isinstance(args_spec, str):
        # split the TEMPLATE on whitespace first, then substitute each token — this
        # keeps {subject} as one token even with spaces, and preserves Windows
        # backslashes (no shell-style escaping).
        rendered = [(tok, tok.format_map(ctx)) for tok in args_spec.split()]
        out, i = [], 0
        while i < len(rendered):
            tok, val = rendered[i]
            is_flag = tok.startswith("--") and "{" not in tok
            if is_flag and i + 1 < len(rendered):
                ntok, nval = rendered[i + 1]
                if "{" in ntok and nval.strip() == "":   # value placeholder resolved empty
                    i += 2                                # drop the flag and its empty value
                    continue
            if "{" in tok and val.strip() == "":          # lone empty placeholder
                i += 1
                continue
            out.append(val)
            i += 1
        return out
    # structured form: list of {flag, value, optional}
    out = []
    for item in args_spec or []:
        flag = (item.get("flag") or "").strip()
        raw_val = item.get("value")
        optional = bool(item.get("optional"))
        val = str(raw_val).format_map(ctx).strip() if raw_val is not None else ""
        if optional and val == "":
            continue
        if flag:
            out.append(flag)
        if raw_val is not None and val != "":
            out.append(val)
    return out


def build_steps(dump_type: str, context: dict, db_path: Path = DEFAULT_DB) -> list:
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM dump_flow_steps WHERE dump_type_key=? AND enabled=1 "
            "ORDER BY step_order, id", (dump_type,)).fetchall()
    steps = []
    for r in rows:
        steps.append({
            "step_name": r["step_name"], "kind": r["kind"], "target_path": r["target_path"],
            "args": _render_args(json.loads(r["args_json"] or "[]"), context),
            "working_dir": r["working_dir"] or None, "on_failure": r["on_failure"] or "stop",
        })
    return steps


def get_steps(key, db_path: Path = DEFAULT_DB) -> list:
    with _conn(db_path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM dump_flow_steps WHERE dump_type_key=? ORDER BY step_order,id",
            (key,)).fetchall()]


def set_steps(key, steps: list, db_path: Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.execute("DELETE FROM dump_flow_steps WHERE dump_type_key=?", (key,))
        for i, s in enumerate(steps):
            args = s.get("args_json", [])
            if isinstance(args, str):
                try:
                    json.loads(args)          # already JSON list -> keep
                except Exception:
                    args = json.dumps(args)   # template string -> wrap as JSON string
            else:
                args = json.dumps(args)
            c.execute(
                "INSERT INTO dump_flow_steps(dump_type_key,step_order,step_name,kind,"
                "target_path,args_json,working_dir,on_failure,enabled,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (key, int(s.get("step_order", (i + 1) * 10)), s["step_name"],
                 s.get("kind", "python"), s["target_path"], args,
                 s.get("working_dir") or None, s.get("on_failure", "stop"),
                 int(s.get("enabled", 1)), datetime.now().isoformat(timespec="seconds")))


# ---------------------------------------------------------------------------
# dump type CRUD
# ---------------------------------------------------------------------------
def list_dump_types(db_path: Path = DEFAULT_DB) -> list:
    with _conn(db_path) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM dump_types ORDER BY sort_order, key").fetchall()]


def get_dump_type(key, db_path: Path = DEFAULT_DB):
    with _conn(db_path) as c:
        row = c.execute("SELECT * FROM dump_types WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def upsert_dump_type(key, name, enabled=1, sort_order=100, save_folder=None,
                     handler=None, max_files=None, db_path: Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.execute(
            "INSERT INTO dump_types(key,name,enabled,sort_order,save_folder,handler,max_files,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET name=excluded.name, enabled=excluded.enabled, "
            "sort_order=excluded.sort_order, save_folder=excluded.save_folder, "
            "handler=COALESCE(excluded.handler, dump_types.handler), "
            "max_files=COALESCE(excluded.max_files, dump_types.max_files), updated_at=excluded.updated_at",
            (key, name, int(enabled), int(sort_order), save_folder or None,
             handler or None, max_files, datetime.now().isoformat(timespec="seconds")))


def delete_dump_type(key, db_path: Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.execute("DELETE FROM dump_flow_steps WHERE dump_type_key=?", (key,))
        c.execute("DELETE FROM dump_types WHERE key=?", (key,))


def get_save_folder(key, db_path: Path = DEFAULT_DB):
    with _conn(db_path) as c:
        row = c.execute("SELECT save_folder FROM dump_types WHERE key=?", (key,)).fetchone()
    return (row["save_folder"] if row else None) or None


# ---------------------------------------------------------------------------
# confirmations
# ---------------------------------------------------------------------------
def record_run(batch_id, dump_type, saved_path, status, steps_results,
               message="", started_at=None, db_path: Path = DEFAULT_DB) -> None:
    with _conn(db_path) as c:
        c.execute(
            "INSERT INTO flow_runs(batch_id,dump_type,saved_path,status,steps_json,"
            "message,started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
            (batch_id, dump_type, str(saved_path or ""), status,
             json.dumps(steps_results or []), message or "", started_at or "",
             datetime.now().isoformat(timespec="seconds")))


def list_runs(limit=200, dump_type=None, db_path: Path = DEFAULT_DB) -> list:
    with _conn(db_path) as c:
        if dump_type:
            rows = c.execute("SELECT * FROM flow_runs WHERE dump_type=? ORDER BY id DESC LIMIT ?",
                             (dump_type, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM flow_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Neon catalog sync
# ---------------------------------------------------------------------------
def sync_catalog(rows, db_path: Path = DEFAULT_DB) -> tuple:
    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "t", "yes", "y", "active")
    created = updated = 0
    now = datetime.now().isoformat(timespec="seconds")
    with _conn(db_path) as c:
        for r in rows:
            key = str(r.get("key") or "").strip()
            if not key:
                continue
            enabled = 1 if _truthy(r.get("active")) else 0
            if c.execute("SELECT 1 FROM dump_types WHERE key=?", (key,)).fetchone():
                c.execute("UPDATE dump_types SET name=?, sort_order=COALESCE(?,sort_order), "
                          "enabled=?, handler=?, max_files=?, source='neon', updated_at=? WHERE key=?",
                          (r.get("name") or key, r.get("sort_order"), enabled,
                           r.get("handler"), r.get("max_files"), now, key))
                updated += 1
            else:
                c.execute("INSERT INTO dump_types(key,name,enabled,sort_order,handler,max_files,source,updated_at) "
                          "VALUES(?,?,?,?,?,?, 'neon', ?)",
                          (key, r.get("name") or key, enabled, r.get("sort_order") or 100,
                           r.get("handler"), r.get("max_files"), now))
                created += 1
    return created, updated


# ---------------------------------------------------------------------------
# seed: current three flows, expressed in the new recognition model
# ---------------------------------------------------------------------------
def _anywhere_any(terms):
    return {"mode": "any", "conditions": [
        {"field": "anywhere", "op": "contains", "value": t} for t in terms]}


def _anywhere_all(terms):
    return {"mode": "all", "conditions": [
        {"field": "anywhere", "op": "contains", "value": t} for t in terms]}


_SEED = {
    "leadsquared": {
        "name": "LeadSquared Dump", "sort_order": 30, "handler": "leadsquared",
        "save_folder": r"D:\Sarthi\Leads",
        "recognition": [_anywhere_any(["leadsquared", "lead squared", "lsq"])],
        "steps": [
            {"step_name": "leadsquared_extract_and_map", "kind": "python",
             "target_path": r"C:\Users\Vikrant.Dale\Downloads\Sarthi\New Leadsquare extract and map.py",
             "on_failure": "stop", "args_json": [
                 {"flag": "--input-file", "value": "{assembled_path}"},
                 {"flag": "--file-mode", "value": "AUTO"},
                 {"flag": "--subject", "value": "{subject}", "optional": True},
                 {"flag": "--sender-email", "value": "{sender_email}", "optional": True}]},
        ],
    },
    "partner_master": {
        "name": "Partner Master", "sort_order": 20, "handler": "partner_master",
        "save_folder": r"D:\Sarthi\Leads",
        "recognition": [
            _anywhere_any(["partner master", "partner_master", "introducer master", "introducer_master"]),
            _anywhere_all(["introducer", "master"]),
        ],
        "steps": [
            {"step_name": "partner_lead_matching", "kind": "python",
             "target_path": r"C:\Users\Vikrant.Dale\Downloads\Sarthi\test\Partner lead matching.py",
             "on_failure": "stop", "args_json": []},
            {"step_name": "partner_master_trigger", "kind": "python",
             "target_path": r"C:\Users\Vikrant.Dale\Downloads\Sarthi\test\partner_master_trigger.py",
             "on_failure": "stop", "args_json": [
                 {"flag": "--input-file", "value": "{assembled_path}"},
                 {"flag": "--file-mode", "value": "AUTO"},
                 {"flag": "--subject", "value": "{subject}", "optional": True},
                 {"flag": "--sender-email", "value": "{sender_email}", "optional": True}]},
        ],
    },
    "partner_rm_mapping": {
        "name": "Partner RM Mapping", "sort_order": 10, "handler": "partner_rm_mapping",
        "save_folder": r"D:\Sarthi\Leads",
        "recognition": [
            _anywhere_any(["partner rm mapping", "partner_rm_mapping"]),
            _anywhere_all(["rm mapping", "partner"]),
        ],
        "steps": [
            {"step_name": "partner_rm_mapping_trigger", "kind": "python",
             "target_path": r"C:\Users\Vikrant.Dale\Downloads\Sarthi\test\partner_rm_mapping_trigger.py",
             "on_failure": "stop", "args_json": []},
        ],
    },
}


def seed_defaults(db_path: Path = DEFAULT_DB, force: bool = False) -> None:
    if not force and list_dump_types(db_path):
        return
    for key, d in _SEED.items():
        upsert_dump_type(key, d["name"], enabled=1, sort_order=d["sort_order"],
                         save_folder=d.get("save_folder"), handler=d.get("handler"), db_path=db_path)
        set_recognition(key, d["recognition"], db_path=db_path)
        set_steps(key, d["steps"], db_path=db_path)


if __name__ == "__main__":
    import tempfile
    p = Path(tempfile.mkdtemp()) / "flows.sqlite3"
    seed_defaults(p, force=True)
    print("seeded:", [t["key"] for t in list_dump_types(p)])
