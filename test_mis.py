# -*- coding: utf-8 -*-
r"""
test_mis.py — the MIS Builder, tested against the REAL receiver modules
(dump_flows, flow_engine, extractor). Temp DB. Touches nothing on the box.

    python test_mis.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, date
from pathlib import Path

TMP = Path(tempfile.mkdtemp())
DB = TMP / "dump_flows.sqlite3"
OUT = TMP / "mis_out"
OUT.mkdir()

import pandas as pd

import dump_flows as df
import flow_engine
import mis_flows as mf
import mis_engine
import mis_triggers as mt

HERE = Path(__file__).resolve().parent
TODAY = date.today().isoformat()

ok = bad = 0


def check(label, cond):
    global ok, bad
    if cond:
        ok += 1
        print(f"  PASS  {label}")
    else:
        bad += 1
        print(f"  FAIL  {label}")


# --- helper scripts ---------------------------------------------------------
BUILD = TMP / "build.py"
BUILD.write_text(
    "import argparse,os,sys\n"
    "a=argparse.ArgumentParser();a.add_argument('--out');a.add_argument('--params',default='')\n"
    "a.add_argument('--report',default='r');x=a.parse_args()\n"
    "os.makedirs(x.out,exist_ok=True)\n"
    "p=os.path.join(x.out,x.report+'.csv')\n"
    "open(p,'w').write('report,params\\n%s,%s\\n'%(x.report,x.params))\n"
    "print('built')\nprint('OUTPUT='+os.path.abspath(p))\n", encoding="utf-8")

BOOM = TMP / "boom.py"
BOOM.write_text("import sys\nprint('exploding')\nsys.exit(3)\n", encoding="utf-8")

QUIET = TMP / "quiet.py"
QUIET.write_text("print('did work but said nothing')\n", encoding="utf-8")

ORDER = TMP / "order.txt"
SEQ = TMP / "seq.py"
SEQ.write_text(
    f"import sys\nopen(r'{ORDER}','a').write(sys.argv[1]+'\\n')\nprint('ok')\n",
    encoding="utf-8")

print("\n=== 1. schema: mis_* tables added, dump tables untouched ===")
df.init_db(DB)
df.seed_defaults(DB, force=True)          # the real 3 seeded flows
before = len(df.list_dump_types(DB))
mf.init_db(DB)
mf.init_db(DB)                             # idempotent
after = len(df.list_dump_types(DB))
c = sqlite3.connect(DB)
tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
c.close()
check("mis_* tables created",
      {"mis_types", "mis_flow_steps", "mis_queue", "mis_runs"} <= tabs)
check("dump tables still present",
      {"dump_types", "dump_flow_steps", "flow_runs"} <= tabs)
check(f"dump_types row count unchanged ({before})", before == after and before == 3)
check("MIS shares dump_flows.DEFAULT_DB", mf.DEFAULT_DB == df.DEFAULT_DB)

print("\n=== 2. happy path ===")
mf.upsert_mis_type("daily_pnl", "Daily P&L", out_folder=str(OUT), db_path=DB)
mf.set_mis_steps("daily_pnl", [{
    "step_order": 10, "step_name": "build", "kind": "python",
    "target_path": str(BUILD),
    "args_json": "--out {out_folder} --report {report_key} --params {params}",
    "on_failure": "stop", "enabled": 1}], db_path=DB)
r = mis_engine.run_mis_flow("daily_pnl", trigger="manual", params="jul",
                            db_path=DB, log=lambda m: None)
check(f"status done ({r['message']})", r["status"] == "done")
check("output exists", Path(r["output_path"]).is_file())
check("params reached the script", "jul" in Path(r["output_path"]).read_text())

print("\n=== 3. args rendering uses dump_flows._render_args ===")
ctx = {"out_folder": "/o", "params": "", "report_key": "x"}
a = df._render_args("--out {out_folder} --params {params}", ctx)
check("empty placeholder drops its flag", a == ["--out", "/o"])

print("\n=== 4. unknown key fails loudly ===")
r = mis_engine.run_mis_flow("nope", db_path=DB, log=lambda m: None)
check("failed", r["status"] == "failed")
check("says 'add handler'", "add handler" in r["message"])

print("\n=== 5. no OUTPUT= line -> failed, never a silent success ===")
mf.upsert_mis_type("quiet", "Quiet", out_folder=str(OUT), db_path=DB)
mf.set_mis_steps("quiet", [{"step_order": 10, "step_name": "b", "kind": "python",
                            "target_path": str(QUIET), "args_json": "",
                            "on_failure": "stop", "enabled": 1}], db_path=DB)
r = mis_engine.run_mis_flow("quiet", db_path=DB, log=lambda m: None)
check("failed", r["status"] == "failed")
check("explains the contract", "OUTPUT=" in r["message"])

print("\n=== 6. STRICT SEQUENCE: a 'stop' step halts the chain ===")
mf.upsert_mis_type("chain", "Chain", out_folder=str(OUT), db_path=DB)
mf.set_mis_steps("chain", [
    {"step_order": 10, "step_name": "one", "kind": "python",
     "target_path": str(SEQ), "args_json": "one", "on_failure": "stop", "enabled": 1},
    {"step_order": 20, "step_name": "boom", "kind": "python",
     "target_path": str(BOOM), "args_json": "", "on_failure": "stop", "enabled": 1},
    {"step_order": 30, "step_name": "three", "kind": "python",
     "target_path": str(SEQ), "args_json": "three", "on_failure": "stop", "enabled": 1},
], db_path=DB)
r = mis_engine.run_mis_flow("chain", db_path=DB, log=lambda m: None)
seen = ORDER.read_text().split() if ORDER.is_file() else []
check("run failed", r["status"] == "failed")
check("step 1 ran", "one" in seen)
check("step 3 NEVER ran after the stop", "three" not in seen)
check("only 2 steps logged", len(r["steps"]) == 2)

print("\n=== 7. on_failure=continue keeps going ===")
mf.upsert_mis_type("chain2", "Chain2", out_folder=str(OUT), db_path=DB)
mf.set_mis_steps("chain2", [
    {"step_order": 10, "step_name": "boom", "kind": "python",
     "target_path": str(BOOM), "args_json": "", "on_failure": "continue", "enabled": 1},
    {"step_order": 20, "step_name": "build", "kind": "python",
     "target_path": str(BUILD),
     "args_json": "--out {out_folder} --report {report_key}",
     "on_failure": "stop", "enabled": 1},
], db_path=DB)
r = mis_engine.run_mis_flow("chain2", db_path=DB, log=lambda m: None)
check("continued to a done run", r["status"] == "done")

print("\n=== 8. queue: dedupe + same-report serialisation ===")
q1 = mf.enqueue("daily_pnl", "request", "req:rr_abc", req_id="rr_abc", db_path=DB)
q2 = mf.enqueue("daily_pnl", "request", "req:rr_abc", req_id="rr_abc", db_path=DB)
check("duplicate insert deduped", q1 is not None and q2 is None)
mf.enqueue("daily_pnl", "schedule", "sched:daily_pnl:2026-07-13 09:00", db_path=DB)
first = mf.claim_next(db_path=DB)
second = mf.claim_next(db_path=DB)
check("claimed one", first is not None)
check("same report NOT claimed twice at once", second is None)
mf.finish_queue_item(first["id"], "done", db_path=DB)
third = mf.claim_next(db_path=DB)
check("next claimable once the first finishes", third is not None)
mf.finish_queue_item(third["id"], "done", db_path=DB)

print("\n=== 9. schedule: fires regardless of lateness, once per slot ===")
mf.upsert_mis_type("morning", "Morning", out_folder=str(OUT),
                   schedule_time="09:00", schedule_days="1111111", db_path=DB)
late = datetime.now().replace(hour=18, minute=30, second=0, microsecond=0)
check("a LATE slot still fires", "morning" in mt.tick_schedules(now=late, db_path=DB))
check("does not re-fire the same slot",
      "morning" not in mt.tick_schedules(now=late + timedelta(minutes=1), db_path=DB))

mf.upsert_mis_type("wd", "Weekdays", out_folder=str(OUT),
                   schedule_time="09:00", schedule_days="1111100", db_path=DB)
sun = datetime.now()
while sun.weekday() != 6:
    sun += timedelta(days=1)
check("skips a day whose bit is 0",
      "wd" not in mt.tick_schedules(now=sun.replace(hour=10), db_path=DB))

print("\n=== 10. dump trigger, mode 'all' — via the REAL flow_engine ===")
ob_dir, tb_dir = TMP / "ob", TMP / "tb"
ob_dir.mkdir(); tb_dir.mkdir()
ob_file = ob_dir / "orderbook.csv"
pd.DataFrame({"client": ["C1", "C2", "C3", "C1"], "qty": [10, 5, 20, 8],
              "value": [25000.0, 18000.5, 30000.0, 6400.0]}).to_csv(ob_file, index=False)
tb_file = tb_dir / "tb.csv"
pd.DataFrame({"client": ["C1", "C2"], "ledger": [-15000.0, 42000.0]}).to_csv(tb_file, index=False)

df.upsert_dump_type("orderbook", "Order Book", save_folder=str(ob_dir), db_path=DB)
df.upsert_dump_type("trial_balance", "Trial Balance", save_folder=str(tb_dir), db_path=DB)
noop = TMP / "noop.py"
noop.write_text("print('ingested')\n", encoding="utf-8")
for k in ("orderbook", "trial_balance"):
    df.set_steps(k, [{"step_order": 10, "step_name": f"{k}_ingest", "kind": "python",
                      "target_path": str(noop), "args_json": "", "on_failure": "stop",
                      "enabled": 1}], db_path=DB)

mf.upsert_mis_type("combo", "Combo MIS", out_folder=str(OUT),
                   trigger={"mode": "all", "keys": ["orderbook", "trial_balance"]},
                   db_path=DB)
mf.set_mis_steps("combo", [{
    "step_order": 10, "step_name": "build from dumps", "kind": "python",
    "target_path": str(HERE / "mis_build_from_dumps.py"),
    "args_json": "--dumps orderbook,trial_balance --out {out_folder} "
                 "--report {report_key} --db " + str(DB),
    "working_dir": str(HERE), "on_failure": "stop", "enabled": 1}], db_path=DB)

n0 = len(mf.list_queue(db_path=DB))
okrun, _ = flow_engine.run_dump_flow("b1", "orderbook", str(ob_file),
                                     db_path=DB, log=lambda m: None)
check("real dump flow ran", okrun)
check("partial set does NOT fire the MIS", len(mf.list_queue(db_path=DB)) == n0)
check("pending_dumps names the missing one",
      mt.pending_dumps("combo", db_path=DB) == ["trial_balance"])

flow_engine.run_dump_flow("b2", "trial_balance", str(tb_file),
                          db_path=DB, log=lambda m: None)
queued = [q for q in mf.list_queue(db_path=DB) if q["report_key"] == "combo"]
check("the LAST dump fires it", len(queued) == 1)
check("trigger recorded as 'dump'", queued and queued[0]["trigger"] == "dump")

flow_engine.run_dump_flow("b3", "trial_balance", str(tb_file),
                          db_path=DB, log=lambda m: None)
check("does not double-fire the same day",
      len([q for q in mf.list_queue(db_path=DB) if q["report_key"] == "combo"]) == 1)

print("\n=== 11. the worker builds it from the REAL saved dumps ===")
item = mf.claim_next(db_path=DB)
while item and item["report_key"] != "combo":
    mf.finish_queue_item(item["id"], "done", db_path=DB)
    item = mf.claim_next(db_path=DB)
check("claimed the combo build", item is not None)
r = mis_engine.run_mis_flow("combo", trigger="dump", queue_id=item["id"],
                            db_path=DB, log=lambda m: None)
check(f"built ({r['message']})", r["status"] == "done")
if r["status"] == "done":
    xl = pd.ExcelFile(r["output_path"])
    check("Summary sheet", "Summary" in xl.sheet_names)
    check("orderbook sheet", "orderbook" in xl.sheet_names)
    check("trial_balance sheet", "trial_balance" in xl.sheet_names)
    obs = xl.parse("orderbook")
    check("real rows carried through (4)", len(obs) == 4)
    check("real columns preserved", "client" in obs.columns and "value" in obs.columns)
    tot = xl.parse("orderbook_totals")
    v = float(tot.loc[tot["column"] == "value", "sum"].iloc[0])
    check(f"totals reconcile (79400.5) got {v}", abs(v - 79400.5) < 0.01)

print("\n=== 12. a 'partial' dump must NOT feed an MIS ===")
df.set_steps("orderbook", [{"step_order": 10, "step_name": "bad", "kind": "python",
                            "target_path": str(BOOM), "args_json": "",
                            "on_failure": "continue", "enabled": 1}], db_path=DB)
mf.upsert_mis_type("eager", "Eager", out_folder=str(OUT),
                   trigger={"mode": "any", "keys": ["orderbook"]}, db_path=DB)
before_q = len(mf.list_queue(db_path=DB))
flow_engine.run_dump_flow("b4", "orderbook", str(ob_file), db_path=DB, log=lambda m: None)
runs = df.list_runs(limit=1, dump_type="orderbook", db_path=DB)
check("dump recorded as partial", runs and runs[0]["status"] == "partial")
check("partial did NOT queue an MIS", len(mf.list_queue(db_path=DB)) == before_q)

print("\n=== 13. missing dump -> build fails, nothing gets mailed ===")
mf.upsert_mis_type("bad", "Bad", out_folder=str(OUT), db_path=DB)
mf.set_mis_steps("bad", [{
    "step_order": 10, "step_name": "b", "kind": "python",
    "target_path": str(HERE / "mis_build_from_dumps.py"),
    "args_json": "--dumps orderbook,ghost --out {out_folder} --report {report_key} "
                 "--db " + str(DB),
    "working_dir": str(HERE), "on_failure": "stop", "enabled": 1}], db_path=DB)
r = mis_engine.run_mis_flow("bad", db_path=DB, log=lambda m: None)
check("failed", r["status"] == "failed")
check("no output path", not r["output_path"])

print(f"\n{ok} passed, {bad} failed")
sys.exit(1 if bad else 0)
