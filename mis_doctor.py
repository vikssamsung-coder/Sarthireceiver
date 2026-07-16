# -*- coding: utf-8 -*-
r"""
mis_doctor.py — find out WHY the MIS poller isn't firing. Run this on the box:

    cd /d D:\dump_processor_app
    python mis_doctor.py

It checks, in order:
  1. Are the MIS files even here?
  2. Do they import without error? (a crash on import = poller dies instantly)
  3. Is the poller process actually running?
  4. Does a real pass tick the schedule and queue the work?
  5. If something is queued, does it build?

Every failure prints the exact fix.
"""
from __future__ import annotations

import importlib
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
FILES = ["mis_flows.py", "mis_engine.py", "mis_triggers.py", "mis_poller.py",
         "mis_mailer.py", "mis_neon.py", "sarthi_service.py", "service_manager.py"]

fail = 0


def bad(msg):
    global fail
    fail += 1
    print("  ✗ " + msg)


def ok(msg):
    print("  ✓ " + msg)


print("=" * 64)
print("MIS DOCTOR  ·  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("folder:", HERE)
print("=" * 64)

# --- 1. files present -------------------------------------------------------
print("\n[1] files present")
missing = [f for f in FILES if not (HERE / f).is_file()]
if missing:
    bad("MISSING: " + ", ".join(missing))
    print("      -> the new zip was not fully unpacked into this folder.")
    print("      -> unzip Sarthireceiver-MIS.zip here so these sit beside app.py,")
    print("         then push/pull if this box updates from GitHub.")
    print("\nStop — nothing else can work until the files are here.")
    sys.exit(1)
ok("all present")

# --- 2. imports -------------------------------------------------------------
print("\n[2] imports (a crash here means the poller dies the instant it starts)")
mods = {}
for name in ("dump_flows", "mis_flows", "mis_triggers", "mis_engine",
             "mis_poller", "neon_sync", "mis_neon", "service_manager"):
    try:
        mods[name] = importlib.import_module(name)
        ok(name)
    except Exception:
        bad(name + " failed to import:")
        print(traceback.format_exc())

if "mis_poller" not in mods or "mis_flows" not in mods:
    print("\nStop — core modules won't import. The traceback above is the reason;")
    print("most often a missing package. Try:  pip install pandas openpyxl psycopg")
    sys.exit(1)

mf = mods["mis_flows"]
mt = mods["mis_triggers"]
mp = mods["mis_poller"]
df = mods["dump_flows"]

print("\n[3] registry")
DB = mf.DEFAULT_DB
print("    db path:", DB)
if mf.DEFAULT_DB != df.DEFAULT_DB:
    bad("mis_flows and dump_flows disagree on the DB path!")
else:
    ok("shares dump_flows.DEFAULT_DB")
if not Path(DB).is_file():
    bad("db file does not exist yet — init on first run")
else:
    ok("db file exists")
mf.init_db(DB)

# --- 4. is a poller running? -----------------------------------------------
print("\n[4] is a poller process running right now?")
running = False
try:
    out = subprocess.run(
        ["wmic", "process", "where", "name='python.exe'", "get", "CommandLine"],
        capture_output=True, text=True, timeout=15).stdout
    for line in out.splitlines():
        s = line.strip()
        if "mis_poller" in s or "sarthi_service" in s:
            ok("running: " + s[:90])
            running = True
except Exception as e:
    print("    (couldn't check via wmic:", e, ")")
if not running:
    bad("NO mis_poller / sarthi_service process found")
    print("      -> `--once` runs a single pass and EXITS; it is not a service.")
    print("      -> to keep firing schedules, keep one of these running:")
    print("            streamlit run app.py     (starts services behind it)")
    print("            run_sarthi.bat           (services only, headless)")

# --- 5. schedule state per report ------------------------------------------
print("\n[5] scheduled reports and whether they are due")
now = datetime.now()
reports = mf.list_mis_types(enabled_only=True, db_path=DB)
sched = [t for t in reports if (t.get("schedule_time") or "").strip()]
if not sched:
    print("    (no reports have a schedule set)")
for t in sched:
    slot = mt._due(t, now)
    tag = "DUE NOW" if slot else "not due"
    print(f"    {t['key']:<22} {t.get('schedule_time')} "
          f"{''.join(mt.mask_to_days(t.get('schedule_days')))}  "
          f"last_fired={t.get('last_fired_at') or 'never'}  -> {tag}")

# --- 6. force one pass ------------------------------------------------------
print("\n[6] forcing ONE pass now (tick + drain)")
before = {(q["report_key"], q["status"]) for q in mf.list_queue(db_path=DB)}
try:
    mp.one_pass(DB)
    ok("pass ran without raising")
except Exception:
    bad("one_pass raised:")
    print(traceback.format_exc())

after = mf.list_queue(db_path=DB)
print("    queue after pass:")
for q in after[:10]:
    print(f"      {q['report_key']:<22} {q['trigger']:<9} {q['status']:<8} "
          f"{q.get('message') or ''}")

runs = mf.list_mis_runs(limit=5, db_path=DB)
print("    recent runs:")
for r in runs:
    print(f"      {r['finished_at']}  {r['report_key']:<22} {r['status']:<8} "
          f"{r.get('message') or ''}")

print("\n" + "=" * 64)
if fail:
    print(f"{fail} problem(s) above — each has a '->' fix line.")
else:
    print("No problems found. If schedules still don't fire, it's because nothing")
    print("stays running: launch `streamlit run app.py` or `run_sarthi.bat`.")
print("=" * 64)
sys.exit(1 if fail else 0)
