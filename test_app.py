"""Logic tests for the rebuilt Dump Processor (no Streamlit/Outlook)."""
import json, tempfile
from pathlib import Path
import dump_flows as df
import flow_engine
import neon_sync

DB = Path(tempfile.mkdtemp()) / "flows.sqlite3"
df.seed_defaults(DB, force=True)
fails = []

# ---- 1. form-based recognition: evaluate ----------------------------------
groups = [
    {"mode": "all", "conditions": [
        {"field": "sender", "op": "is_one_of", "values": ["crm@bigul.co", "orders@bigul.co"]},
        {"field": "subject", "op": "contains", "value": "order file"}]},
]
assert df.evaluate(groups, {"sender": "orders@bigul.co", "subject": "Daily ORDER FILE"}) is True
assert df.evaluate(groups, {"sender": "x@other.co", "subject": "order file"}) is False   # sender not in list
assert df.evaluate(groups, {"sender": "crm@bigul.co", "subject": "something else"}) is False  # subject fails (ALL)

# ANY mode
g_any = [{"mode": "any", "conditions": [
    {"field": "subject", "op": "contains", "value": "trial balance"},
    {"field": "sender", "op": "is", "value": "tb@bigul.co"}]}]
assert df.evaluate(g_any, {"sender": "tb@bigul.co", "subject": "whatever"}) is True
assert df.evaluate(g_any, {"sender": "n@n.co", "subject": "monthly trial balance"}) is True
assert df.evaluate(g_any, {"sender": "n@n.co", "subject": "nope"}) is False

# multiple groups OR'd: (senderA & subjX) OR (senderB & subjY)
multi = [
    {"mode": "all", "conditions": [{"field": "sender", "op": "is", "value": "a@x.co"},
                                   {"field": "subject", "op": "contains", "value": "cube"}]},
    {"mode": "all", "conditions": [{"field": "sender", "op": "is", "value": "b@x.co"},
                                   {"field": "subject", "op": "contains", "value": "call log"}]},
]
assert df.evaluate(multi, {"sender": "b@x.co", "subject": "daily CALL LOG"}) is True
assert df.evaluate(multi, {"sender": "a@x.co", "subject": "call log"}) is False  # a needs 'cube'
# regex
gre = [{"mode": "any", "conditions": [{"field": "subject", "op": "matches", "value": r"NSE_\d{4}"}]}]
assert df.evaluate(gre, {"subject": "file NSE_2291 today"}) is True
assert df.evaluate(gre, {"subject": "NSE report"}) is False

# ---- 2. resolve: stamped label beats recognition; recognition fallback ----
# seed types still recognize by keyword (anywhere)
assert df.resolve(subject="LeadSquared dump", db_path=DB) == "leadsquared"
assert df.resolve(subject="partner rm mapping export", db_path=DB) == "partner_rm_mapping"
assert df.resolve(subject="introducer master list", db_path=DB) == "partner_master"
assert df.resolve(subject="random news", db_path=DB) == "unknown"
# stamped handler wins even against other keywords
assert df.resolve(subject="leadsquared words", meta={"dump_type_handler": "partner_master"}, db_path=DB) == "partner_master"

# a new type recognized purely by SENDER (a plain-email watcher)
df.upsert_dump_type("cube_calllog", "Cube Call Log", 1, 15, save_folder=r"D:\Sarthi\Cube", db_path=DB)
df.set_recognition("cube_calllog", [
    {"mode": "all", "conditions": [
        {"field": "sender", "op": "is_one_of", "values": ["reports@cube.co", "ops@cube.co"]},
        {"field": "subject", "op": "contains", "value": "call log"}]}], db_path=DB)
got = df.resolve(subject="Daily Call Log", sender="ops@cube.co", db_path=DB)
if got != "cube_calllog":
    fails.append(f"sender-based recognition failed: {got}")
# wrong sender -> not recognized as cube
got2 = df.resolve(subject="Daily Call Log", sender="someone@else.co", db_path=DB)
if got2 == "cube_calllog":
    fails.append("cube matched despite wrong sender")

# ---- 3. seed step parity: leadsquared args (structured, optional drop) -----
ls = df.build_steps("leadsquared", {"assembled_path": r"D:\x.csv", "subject": "S", "sender_email": "r@b.co"}, db_path=DB)
if ls[0]["args"] != ["--input-file", r"D:\x.csv", "--file-mode", "AUTO", "--subject", "S", "--sender-email", "r@b.co"]:
    fails.append(f"leadsquared full args: {ls[0]['args']}")
ls_min = df.build_steps("leadsquared", {"assembled_path": r"D:\x.csv"}, db_path=DB)
if ls_min[0]["args"] != ["--input-file", r"D:\x.csv", "--file-mode", "AUTO"]:
    fails.append(f"leadsquared optional-drop failed: {ls_min[0]['args']}")

# ---- 4. string-template args (the friendly UI field) ----------------------
df.set_steps("cube_calllog", [
    {"step_name": "cube_ingest", "kind": "python", "target_path": r"C:\Sarthi\cube.py",
     "args_json": "--input-file {assembled_path} --subject {subject}", "on_failure": "stop"}], db_path=DB)
full = df.build_steps("cube_calllog", {"assembled_path": r"D:\c.csv", "subject": "Call Log"}, db_path=DB)
if full[0]["args"] != ["--input-file", r"D:\c.csv", "--subject", "Call Log"]:
    fails.append(f"string args render: {full[0]['args']}")
# empty {subject} -> dangling --subject dropped
empt = df.build_steps("cube_calllog", {"assembled_path": r"D:\c.csv", "subject": ""}, db_path=DB)
if empt[0]["args"] != ["--input-file", r"D:\c.csv"]:
    fails.append(f"string args empty-drop: {empt[0]['args']}")

# ---- 5. secrets.toml loader (any key name, strips channel_binding) --------
sec = Path(tempfile.mkdtemp()) / "secrets.toml"
sec.write_text('some_other = "x"\n'
               '[connections.neon]\n'
               'url = "postgresql://u:p@ep-empty-meadow.aws.neon.tech/db?sslmode=require&channel_binding=require"\n')
loaded = neon_sync.load_neon_url(sec)
if "channel_binding" in loaded or not loaded.startswith("postgresql://"):
    fails.append(f"secrets loader: {loaded}")

# ---- 6. end-to-end run via flow engine + injected runners -----------------
outdir = Path(tempfile.mkdtemp())
df.upsert_dump_type("order", "Order File", 1, 25, save_folder=str(outdir), db_path=DB)
df.set_steps("order", [
    {"step_name": "order_client_ingest", "kind": "python", "target_path": r"C:\o1.py",
     "args_json": "--input-file {assembled_path}", "on_failure": "stop"},
    {"step_name": "order_meta", "kind": "python", "target_path": r"C:\o2.py",
     "args_json": "--input-file {assembled_path}", "on_failure": "stop"}], db_path=DB)
src = Path(tempfile.mkdtemp()) / "o1.csv"; src.write_text("a\n1\n")
calls = []
ok, res = flow_engine.run_dump_flow(
    batch_id="o1", dump_type="order", assembled_path=str(src), subject="Order File",
    run_python=lambda b, d, t, s, a: (calls.append((t, a)) or True),
    run_bat=lambda b, f, p: True, db_path=DB, log=lambda m: None)
if not ok or [c[0] for c in calls] != ["order_client_ingest", "order_meta"]:
    fails.append(f"e2e order sequence: {calls}")
if not (outdir / "o1.csv").exists():
    fails.append("e2e: dump not saved to folder")
if calls and calls[0][1] != ["--input-file", str(outdir / "o1.csv")]:
    fails.append(f"e2e: saved path not passed: {calls[0][1]}")
if df.list_runs(dump_type="order", db_path=DB)[0]["status"] != "success":
    fails.append("e2e: confirmation not success")

print("REBUILD LOGIC TESTS")
if fails:
    for f in fails: print("  FAIL:", f)
    raise SystemExit(1)
print("  ALL PASSED —")
print("  recognition: sender/subject/body, ALL/ANY, multi-group OR, is_one_of, regex")
print("  resolve: stamped label wins; keyword + sender fallback; unknown safe")
print("  seed parity + optional-arg drop; friendly string-template args + empty-drop")
print("  secrets.toml loader (nested key, channel_binding stripped)")
print("  end-to-end: save-to-folder -> ordered steps -> confirmation")
