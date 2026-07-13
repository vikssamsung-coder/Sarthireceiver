# -*- coding: utf-8 -*-
r"""
MIS triggers — the two non-request producers.

Both do ONE thing: put a row on mis_queue. They never build, never email, never
block. Idempotency is the dedupe_key; the worker does the rest.

SCHEDULE — fire regardless (the chosen behaviour):
  Slot = today at schedule_time, if today's weekday bit is set in schedule_days.
  Fire when now >= slot and last_fired_at < slot.
  There is NO catch-up window: a box booted at 18:00 WILL fire the 09:00 report.
  To change that, the only edit is a max-age check in _due().

DUMP-COMPLETE — mode 'all' by default:
  'any' -> fire as soon as any listed dump type lands.
  'all' -> fire only once EVERY listed dump has a successful flow_runs entry
           today. The last one to land is what fires it. Once per day.

Called from flow_engine.run_dump_flow after a successful run. It must never
raise into the dump flow — the caller wraps it, and everything here is cheap
(a few SELECTs and an INSERT OR IGNORE).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import mis_flows as mf

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def mask_to_days(mask) -> list:
    m = (mask or "1111100")
    if len(m) < 7:
        m = "1111100"
    return [DAYS[i] for i in range(7) if m[i] == "1"]


def days_to_mask(sel) -> str:
    return "".join("1" if d in (sel or []) else "0" for d in DAYS)


def _slot(d, hhmm):
    try:
        h, m = str(hhmm).strip().split(":")
        return datetime(d.year, d.month, d.day, int(h), int(m))
    except Exception:
        return None


def _due(t: dict, now: datetime):
    """Return the slot this report is due for, or None."""
    hhmm = (t.get("schedule_time") or "").strip()
    if not hhmm:
        return None
    mask = t.get("schedule_days") or "1111100"
    if len(mask) < 7:
        mask = "1111100"
    if mask[now.weekday()] != "1":
        return None
    slot = _slot(now.date(), hhmm)
    if slot is None or now < slot:
        return None
    last = (t.get("last_fired_at") or "").strip()
    if last:
        try:
            if datetime.fromisoformat(last) >= slot:
                return None                     # already fired this slot
        except Exception:
            pass
    return slot                                  # fire regardless of lateness


def tick_schedules(now: datetime = None, db_path: Path = mf.DEFAULT_DB) -> list:
    """Call once a minute. Returns the report keys enqueued."""
    now = now or datetime.now()
    fired = []
    for t in mf.list_mis_types(enabled_only=True, db_path=db_path):
        slot = _due(t, now)
        if slot is None:
            continue
        key = t["key"]
        dedupe = f"sched:{key}:{slot.strftime('%Y-%m-%d %H:%M')}"
        qid = mf.enqueue(key, "schedule", dedupe, db_path=db_path)
        # Stamp last_fired_at even on a dedupe hit, so we stop re-evaluating it.
        mf.mark_fired(key, now.isoformat(timespec="seconds"), db_path=db_path)
        if qid:
            fired.append(key)
    return fired


def on_dump_complete(dump_type, batch_id="", db_path: Path = mf.DEFAULT_DB) -> list:
    """Call from flow_engine AFTER a dump run succeeds. Returns keys enqueued."""
    today = mf._today()
    fired = []
    for t in mf.list_mis_types(enabled_only=True, db_path=db_path):
        trig = mf.get_trigger(t)
        keys = trig["keys"]
        if not keys or dump_type not in keys:
            continue

        if trig["mode"] == "any":
            dedupe = f"dump:{batch_id or today}:{t['key']}"
        else:
            if not all(mf.dump_succeeded_today(k, today, db_path=db_path) for k in keys):
                continue                          # the set isn't complete yet
            dedupe = f"dump:{t['key']}:{today}"   # once per day per report

        qid = mf.enqueue(t["key"], "dump", dedupe, db_path=db_path)
        if qid:
            fired.append(t["key"])
    return fired


def pending_dumps(report_key, db_path: Path = mf.DEFAULT_DB) -> list:
    """UI helper: which required dumps have NOT landed today."""
    t = mf.get_mis_type(report_key, db_path=db_path)
    if not t:
        return []
    today = mf._today()
    return [k for k in mf.get_trigger(t)["keys"]
            if not mf.dump_succeeded_today(k, today, db_path=db_path)]
