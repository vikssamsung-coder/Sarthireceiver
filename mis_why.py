# -*- coding: utf-8 -*-
r"""
mis_why.py — why did (or didn't) my report fire?

Checks, per report, every reason it could have been skipped, and tells you
whether anything is actually watching the clock.

    python mis_why.py
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import dump_flows as df
import mis_flows as mf
import mis_triggers as mt

DB = mf.DEFAULT_DB
now = datetime.now()

print(f"now      : {now:%Y-%m-%d %H:%M:%S}  ({mt.DAYS[now.weekday()]})")
print(f"registry : {DB}")
print(f"exists   : {Path(DB).is_file()}")

# --- is anything ticking? ---------------------------------------------------
print("\n--- is a poller running? ---")
running = False
try:
    import subprocess
    out = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'", "get", "commandline"],
        capture_output=True, text=True, timeout=15).stdout
    for line in out.splitlines():
        s = line.strip()
        if "mis_poller" in s or "sarthi_service" in s:
            print(f"  YES: {s[:110]}")
            running = True
except Exception as e:
    print(f"  (could not check: {e})")

if not running:
    print("  NO — nothing is watching the clock.")
    print("  The Streamlit app does NOT fire schedules. mis_poller.py does.")
    print("  Fix:  run_sarthi.bat        (receiver + MIS, stays up)")
    print("  Or:   python mis_poller.py --once     (catch up right now)")

# --- per report -------------------------------------------------------------
reports = mf.list_mis_types(db_path=DB)
if not reports:
    print("\nNo MIS reports defined.")
    sys.exit(0)

for t in reports:
    key = t["key"]
    print(f"\n=== {key} — {t.get('name')} ===")

    if not t.get("enabled"):
        print("  SKIPPED: report is disabled.")
        continue

    steps = mf.get_mis_steps(key, db_path=DB)
    if not steps:
        print("  WILL FAIL: no steps configured — nothing to build.")
    else:
        print(f"  steps: {', '.join(s['step_name'] for s in steps)}")

    if not t.get("out_folder"):
        print("  WARNING: no output folder set.")

    # ---- schedule ----
    hhmm = (t.get("schedule_time") or "").strip()
    if not hhmm:
        print("  schedule: none")
    else:
        mask = t.get("schedule_days") or "1111100"
        day_ok = len(mask) >= 7 and mask[now.weekday()] == "1"
        slot = mt._slot(now.date(), hhmm)
        last = (t.get("last_fired_at") or "").strip()
        print(f"  schedule: {hhmm} on {', '.join(mt.mask_to_days(mask))}")
        print(f"  last_fired_at: {last or '(never)'}")

        if not day_ok:
            print(f"  -> NOT due: {mt.DAYS[now.weekday()]} is not in the day mask.")
        elif slot is None:
            print(f"  -> NOT due: '{hhmm}' is not a valid HH:MM.")
        elif now < slot:
            print(f"  -> NOT due yet: today's slot is {slot:%H:%M}, it is {now:%H:%M}.")
        else:
            already = False
            if last:
                try:
                    already = datetime.fromisoformat(last) >= slot
                except Exception:
                    pass
            if already:
                print(f"  -> ALREADY FIRED for today's {slot:%H:%M} slot.")
            else:
                print(f"  -> DUE NOW for the {slot:%H:%M} slot "
                      f"({int((now - slot).total_seconds() // 60)} min late). "
                      f"It fires the moment a poller runs — lateness is never a reason "
                      f"to skip.")

    # ---- dump trigger ----
    trig = mf.get_trigger(t)
    if not trig["keys"]:
        print("  after dumps: none")
    else:
        print(f"  after dumps: {trig['mode'].upper()} of {', '.join(trig['keys'])}")
        for k in trig["keys"]:
            landed = mf.dump_succeeded_today(k, db_path=DB)
            print(f"     {k:<24} {'landed today' if landed else 'NOT landed today'}")
        pend = mt.pending_dumps(key, db_path=DB)
        if trig["mode"] == "all" and pend:
            print(f"  -> waiting on: {', '.join(pend)}")

    # ---- recent activity ----
    q = [r for r in mf.list_queue(200, db_path=DB) if r["report_key"] == key]
    if q:
        r = q[0]
        print(f"  last queued: {r['created_at']}  {r['trigger']}  -> {r['status']}"
              + (f"  ({r['message']})" if r.get("message") else ""))
    else:
        print("  never queued.")

    runs = mf.list_mis_runs(limit=1, report_key=key, db_path=DB)
    if runs:
        r = runs[0]
        print(f"  last run   : {r['finished_at']}  {r['status']}"
              + (f"  {r['message']}" if r.get("message") else ""))
    else:
        print("  never run.")

print("\n" + ("-" * 60))
if not running:
    print("BOTTOM LINE: no poller is running, so no schedule can fire.")
    print("Run:  python mis_poller.py --once     to catch up now")
    print("Then: run_sarthi.bat                  to keep it up")
