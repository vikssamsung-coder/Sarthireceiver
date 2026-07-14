# -*- coding: utf-8 -*-
r"""
Sarthi Dump Processor — management app (rebuilt to the approved design).

Screens: Overview · Dump types · Configure a type · Run history · Neon catalog.
Recognition is form-based (sender / subject / body / attachment / anywhere),
All-or-Any per group, multiple groups OR'd. Neon URL is read from secrets.toml.

Run on the Sarthi box:
    streamlit run app.py
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

import dump_flows as df
import flow_engine
import neon_sync
import mis_flows as mf
import app_mis
import service_manager

DB_PATH = df.DEFAULT_DB          # one path, defined once, in dump_flows

st.set_page_config(page_title="Sarthi Dump Processor", layout="wide")
df.init_db(DB_PATH)
mf.init_db(DB_PATH)               # adds the mis_* tables; touches nothing existing

# Launching the app IS launching the system. The receiver and the MIS poller come
# up behind it as one detached background process. PID-locked, so Streamlit's
# reruns cannot spawn duplicates; already-running is a cheap no-op.
SVC = service_manager.ensure_running()

# ---- light styling to match the design ------------------------------------
st.markdown("""
<style>
 .block-container{padding-top:2.2rem;max-width:1080px}
 h1,h2,h3{font-family:'Space Grotesk',system-ui,sans-serif}
 .rail{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 4px}
 .stn{flex:1;min-width:150px;background:#F6F8FB;border:1px solid #E1E7F0;border-radius:11px;padding:13px 14px}
 .stn .n{width:22px;height:22px;border-radius:7px;background:#38499E;color:#fff;font-weight:600;
   display:inline-grid;place-items:center;font-size:12px;margin-bottom:8px}
 .stn .c{font-size:10px;letter-spacing:.6px;text-transform:uppercase;color:#8A94A3;font-weight:600}
 .stn .t{font-size:13px;font-weight:600;color:#161B24;margin-top:2px}
 .pill{font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px}
 .ok{background:#E1F1E9;color:#1F8C5C}.fail{background:#F8E4E2;color:#C0433D}
 .on{background:#E1F1E9;color:#1F8C5C}.off{background:#EEF1F5;color:#8A94A3}
 .neon{background:#E9ECF7;color:#2B3878}
 .mono{font-family:'IBM Plex Mono',monospace;font-size:12px;color:#5C6675}
 div[data-testid="stVerticalBlockBorderWrapper"]{border-radius:12px}
</style>
""", unsafe_allow_html=True)

# ---- session ---------------------------------------------------------------
ss = st.session_state
ss.setdefault("screen", "Overview")
ss.setdefault("sel", None)          # selected dump type key (for Configure)
ss.setdefault("rec_key", None)      # which type's recognition is loaded
ss.setdefault("groups", [])         # recognition groups being edited
ss.setdefault("steps_key", None)
ss.setdefault("steps", [])
ss.setdefault("mis_sel", None)       # selected MIS report (for Configure MIS)
ss.setdefault("mis_steps_key", None)
ss.setdefault("mis_steps", [])


def goto(screen, sel=None, mis_sel=None):
    ss.screen = screen
    if sel is not None:
        ss.sel = sel
    if mis_sel is not None:
        ss.mis_sel = mis_sel


def _args_to_text(raw):
    """Turn stored args_json (JSON list of {flag,value,optional} OR a template
    string) into a friendly one-line arguments string for editing."""
    try:
        spec = json.loads(raw or "[]")
    except Exception:
        return raw or ""
    if isinstance(spec, str):
        return spec
    parts = []
    for item in spec:
        flag = (item.get("flag") or "").strip()
        val = item.get("value")
        if flag:
            parts.append(flag)
        if val is not None and str(val) != "":
            parts.append(str(val))
    return " ".join(parts)


FIELD_LABELS = {"sender": "Sender", "subject": "Subject", "body": "Body",
                "attachment": "Attachment name", "anywhere": "Anywhere (subject+body)"}
OP_LABELS = {"is": "is", "is_one_of": "is one of", "contains": "contains",
             "equals": "equals", "matches": "matches (regex)"}


def recognition_builder(key):
    """Render the form-based recognition editor for a dump type (mutates ss.groups)."""
    if ss.rec_key != key:
        ss.groups = df.get_recognition(key, DB_PATH) or []
        ss.rec_key = key
    fields = list(FIELD_LABELS.keys())

    if not ss.groups:
        st.caption("No conditions yet.")

    for gi, g in enumerate(ss.groups):
        with st.container(border=True):
            m1, m2 = st.columns([3, 1])
            g["mode"] = m1.radio(
                f"Match when … (group {gi + 1})", ["all", "any"],
                index=0 if (g.get("mode", "all") == "all") else 1, horizontal=True,
                format_func=lambda x: "ALL of these are true" if x == "all" else "ANY of these are true",
                key=f"mode_{key}_{gi}")
            if m2.button("Remove group", key=f"delg_{key}_{gi}"):
                ss.groups.pop(gi); st.rerun()

            for ci, cond in enumerate(g.get("conditions", [])):
                f, o, v, x = st.columns([2, 2, 4, 1])
                fld = f.selectbox("Field", fields, index=fields.index(cond.get("field", "anywhere")),
                                  format_func=lambda k: FIELD_LABELS[k],
                                  key=f"f_{key}_{gi}_{ci}", label_visibility="collapsed")
                cond["field"] = fld
                ops = df.OPS_SENDER if fld == "sender" else df.OPS_TEXT
                cur_op = cond.get("op", ops[0])
                if cur_op not in ops:
                    cur_op = ops[0]
                op = o.selectbox("Op", ops, index=ops.index(cur_op),
                                 format_func=lambda x: OP_LABELS[x],
                                 key=f"o_{key}_{gi}_{ci}", label_visibility="collapsed")
                cond["op"] = op
                if op == "is_one_of":
                    val = v.text_input("Values", value=", ".join(cond.get("values", [])),
                                       placeholder="a@bigul.co, b@bigul.co",
                                       key=f"v_{key}_{gi}_{ci}", label_visibility="collapsed")
                    cond["values"] = [s.strip() for s in val.split(",") if s.strip()]
                    cond.pop("value", None)
                else:
                    val = v.text_input("Value", value=cond.get("value", ""),
                                       placeholder="e.g. order file",
                                       key=f"v_{key}_{gi}_{ci}", label_visibility="collapsed")
                    cond["value"] = val
                    cond.pop("values", None)
                if x.button("✕", key=f"delc_{key}_{gi}_{ci}"):
                    g["conditions"].pop(ci); st.rerun()

            if st.button("+ Add condition", key=f"addc_{key}_{gi}"):
                g.setdefault("conditions", []).append(
                    {"field": "subject", "op": "contains", "value": ""})
                st.rerun()

    ga, gb = st.columns([1, 4])
    if ga.button("+ Add rule group", key=f"addg_{key}"):
        ss.groups.append({"mode": "all", "conditions": [
            {"field": "subject", "op": "contains", "value": ""}]})
        st.rerun()
    if gb.button("Save identifier", type="primary", key=f"saverec_{key}"):
        df.set_recognition(key, ss.groups, DB_PATH)
        st.success("Saved.")


# ===========================================================================
# SIDEBAR
# ===========================================================================
with st.sidebar:
    st.markdown("### ◈ Dump Processor")
    st.caption("Sarthi · receiver")
    NAV = ["Overview", "Dump types", "MIS reports", "Run history", "MIS history",
           "Neon catalog", "Services", "Settings"]
    # Configure screens are sub-screens — keep the nav on their parent while editing
    _PARENT = {"Configure": "Dump types", "Configure MIS": "MIS reports"}
    current_nav = _PARENT.get(ss.screen,
                              ss.screen if ss.screen in NAV else "Overview")
    nav = st.radio("Go to", NAV, index=NAV.index(current_nav),
                   label_visibility="collapsed")
    if nav != current_nav:
        ss.screen = nav

    st.divider()
    if SVC.get("running"):
        st.markdown('<span class="pill on">● Receiver &amp; MIS running</span>',
                    unsafe_allow_html=True)
        st.caption(f"since {SVC.get('started_at') or '—'}")
    else:
        st.markdown('<span class="pill fail">● Services DOWN</span>',
                    unsafe_allow_html=True)
        st.caption(SVC.get("error") or "Mail is not being polled and schedules "
                                       "will not fire.")
        if st.button("Start services", type="primary", use_container_width=True):
            service_manager.ensure_running()
            st.rerun()

    st.divider()
    url = neon_sync.load_neon_url()
    if url:
        st.markdown('<span class="pill on">● Neon connected</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="pill off">● Neon URL not found</span>', unsafe_allow_html=True)
    st.caption("Reads DB URL from\n`.streamlit/secrets.toml`")
    if st.button("Sync from Neon", use_container_width=True):
        res = neon_sync.sync(DB_PATH, url or None)
        if res.get("error"):
            st.error(res["error"])
        else:
            st.success(f"{res['total']} types · {res['created']} new, {res['updated']} updated")

types = df.list_dump_types(DB_PATH)


def type_row(key):
    return next((t for t in types if t["key"] == key), None)


# ===========================================================================
# OVERVIEW
# ===========================================================================
if ss.screen == "Overview":
    st.title("Overview")
    st.caption("What happens when a dump email arrives, and what ran recently.")
    st.markdown("""
    <div class="rail">
      <div class="stn"><div class="n">1</div><div class="c">Recognize</div><div class="t">Which dump is it?</div></div>
      <div class="stn"><div class="n">2</div><div class="c">Save</div><div class="t">Put the file in its folder</div></div>
      <div class="stn"><div class="n">3</div><div class="c">Run</div><div class="t">Run its scripts, in order</div></div>
      <div class="stn"><div class="n">4</div><div class="c">Record</div><div class="t">Save the result here</div></div>
    </div>""", unsafe_allow_html=True)

    runs = df.list_runs(limit=200, db_path=DB_PATH)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Dump types", len(types))
    c2.metric("Runs recorded", len(runs))
    c3.metric("Succeeded", sum(1 for r in runs if r["status"] == "success"))
    c4.metric("Failed", sum(1 for r in runs if r["status"] == "failed"))

    st.subheader("Latest runs")
    if not runs:
        st.info("No runs yet. Once the processor handles a dump, it shows up here.")
    else:
        rows = []
        for r in runs[:15]:
            sr = json.loads(r["steps_json"] or "[]")
            rows.append({"when": r["finished_at"], "dump type": r["dump_type"],
                         "batch": r["batch_id"],
                         "steps": ", ".join(f"{x['step']}:{x['status']}" for x in sr) or "—",
                         "result": r["status"]})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ===========================================================================
# DUMP TYPES (cards)
# ===========================================================================
elif ss.screen == "Dump types":
    tc, tb = st.columns([3, 1])
    tc.title("Dump types")
    tc.caption("How each dump is recognized, where its file goes, and what runs.")
    with tb:
        st.write("")
        if st.button("➕ New dump type", use_container_width=True):
            goto("Configure", sel="__new__")
            st.rerun()

    if not types:
        st.info("No dump types yet. Add one, or sync the catalog from Neon in the sidebar.")
        if st.button("Seed current 3 flows"):
            df.seed_defaults(DB_PATH, force=True)
            st.rerun()

    cols = st.columns(2)
    for i, t in enumerate(types):
        with cols[i % 2]:
            with st.container(border=True):
                a, b = st.columns([3, 1])
                a.markdown(f"**{t['name']}**  \n<span class='mono'>{t['key']}</span>",
                           unsafe_allow_html=True)
                b.markdown(f"<span class='pill {'on' if t['enabled'] else 'off'}'>"
                           f"{'Active' if t['enabled'] else 'Off'}</span>", unsafe_allow_html=True)
                origin = t.get("origin", "direct")
                b.markdown(f"<span class='pill {'neon' if origin == 'pmd' else 'off'}'>"
                           f"{'PMD' if origin == 'pmd' else 'Direct'}</span>", unsafe_allow_html=True)
                steps = df.get_steps(t["key"], DB_PATH)
                st.markdown(f"<span class='mono'>saves to {t['save_folder'] or '(not set)'}</span>",
                            unsafe_allow_html=True)
                if steps:
                    st.markdown(" ".join(
                        f"<span class='pill neon'>{s['step_name']}</span>" for s in steps),
                        unsafe_allow_html=True)
                else:
                    st.markdown("<span class='pill off'>no steps yet</span>", unsafe_allow_html=True)
                if st.button("Open", key=f"open_{t['key']}", use_container_width=True):
                    goto("Configure", sel=t["key"])
                    st.rerun()

# ===========================================================================
# CONFIGURE a dump type
# ===========================================================================
elif ss.screen == "Configure":
    if st.button("← All dump types"):
        goto("Dump types"); st.rerun()

    is_new = ss.sel == "__new__"
    t = None if is_new else type_row(ss.sel)
    if not is_new and not t:
        st.warning("That dump type no longer exists.")
        st.stop()

    # header form
    st.title("New dump type" if is_new else t["name"])
    if not is_new and (t.get("source") == "neon"):
        st.markdown(f"<span class='pill neon'>from Neon catalog</span> &nbsp; "
                    f"handler <span class='mono'>{t.get('handler') or t['key']}</span> · "
                    f"up to {t.get('max_files') if t.get('max_files') is not None else '—'} files",
                    unsafe_allow_html=True)

    h1, h2, h3 = st.columns([2, 1, 1])
    key_in = h1.text_input("Key (short id)", value="" if is_new else t["key"],
                           disabled=not is_new, help="lowercase, no spaces, e.g. order")
    name_in = h1.text_input("Name", value="" if is_new else (t["name"] or ""))
    enabled_in = h2.toggle("Active", value=True if is_new else bool(t["enabled"]))
    order_in = h3.number_input("Detect order", value=100 if is_new else int(t["sort_order"]),
                               step=10, help="Lower is checked first. Specific types before broad ones.")
    folder_in = st.text_input("Save folder — the dump is copied here before anything runs",
                              value="" if is_new else (t["save_folder"] or ""),
                              placeholder=r"e.g. D:\Sarthi\Orders")
    st.caption("Incoming .zip is unzipped here; .csv/.xlsx are saved as-is. Scripts read the "
               "extracted data file via {assembled_path}.")

    origin_cur = t.get("origin", "direct") if not is_new else "direct"
    origin_in = st.radio(
        "Comes from", ["pmd", "direct"],
        index=0 if origin_cur == "pmd" else 1, horizontal=True,
        format_func=lambda x: "Plan My Day — fixed format, matched by label"
        if x == "pmd" else "Direct email — other system, matched by an identifier you set")

    if is_new:
        if st.button("Create dump type", type="primary"):
            k = key_in.strip().lower().replace(" ", "_")
            if not k:
                st.error("Give it a key.")
            elif type_row(k):
                st.error("That key already exists.")
            else:
                df.upsert_dump_type(k, name_in.strip() or k, int(enabled_in), int(order_in),
                                    save_folder=folder_in.strip() or None, origin=origin_in,
                                    db_path=DB_PATH)
                goto("Configure", sel=k); st.rerun()
        st.stop()

    key = t["key"]
    hb1, hb2 = st.columns([1, 4])
    if hb1.button("Save details", type="primary"):
        df.upsert_dump_type(key, name_in.strip() or key, int(enabled_in), int(order_in),
                            save_folder=folder_in.strip() or None, origin=origin_in, db_path=DB_PATH)
        st.success("Saved.")
    if hb2.button("Delete this dump type"):
        df.delete_dump_type(key, DB_PATH); goto("Dump types"); st.rerun()

    st.divider()

    # ---- 1. RECOGNITION ---------------------------------------------------
    st.subheader("① How it's recognized")
    if origin_in == "pmd":
        st.info(f"Comes from **Plan My Day** in the fixed format, so it's recognized "
                f"automatically by its label — **handler `{t.get('handler') or key}`**. "
                "The sender can be anyone; you don't need sender/subject rules here.")
        with st.expander("Add backup keyword rules (optional)"):
            st.caption("Only used if the label is ever missing. Most PMD dumps never need this.")
            recognition_builder(key)
    else:
        st.caption("This email doesn't come from PMD, so set an identifier — match on who "
                   "sent it, the subject, the body, or a combination. Add rule groups for "
                   "\"(sender A AND subject X) OR (sender B AND subject Y)\".")
        recognition_builder(key)

    st.divider()

    # ---- 2. SAVE FOLDER already above; ---- 3. STEPS ----------------------
    st.subheader("② What runs, in order")
    st.caption("The dump is first extracted into the save folder — a .zip is unzipped, "
               "a .csv/.xlsx is placed as-is. Each step then runs after the one above; a "
               "“stop” step that fails halts the rest. In arguments, {assembled_path} is the "
               "extracted data file, {extract_dir} is the folder, plus {subject}, {sender_email}, {batch_id}.")

    if ss.steps_key != key:
        rows = df.get_steps(key, DB_PATH)
        ss.steps = [{"step_name": r["step_name"], "kind": r["kind"], "target_path": r["target_path"],
                     "args": _args_to_text(r["args_json"]), "on_failure": r["on_failure"],
                     "enabled": bool(r["enabled"])} for r in rows]
        ss.steps_key = key

    for si, s in enumerate(ss.steps):
        with st.container(border=True):
            top = st.columns([0.5, 6, 0.6, 0.6, 0.6])
            top[0].markdown(f"### {si + 1}")
            s["step_name"] = top[1].text_input("Step name", value=s["step_name"],
                                               key=f"sn_{key}_{si}", placeholder="e.g. order client ingest")
            if top[2].button("▲", key=f"up_{key}_{si}", disabled=si == 0):
                ss.steps[si - 1], ss.steps[si] = ss.steps[si], ss.steps[si - 1]; st.rerun()
            if top[3].button("▼", key=f"dn_{key}_{si}", disabled=si == len(ss.steps) - 1):
                ss.steps[si + 1], ss.steps[si] = ss.steps[si], ss.steps[si + 1]; st.rerun()
            if top[4].button("✕", key=f"delstep_{key}_{si}"):
                ss.steps.pop(si); st.rerun()

            r1 = st.columns([1, 3])
            s["kind"] = r1[0].selectbox("Type", ["python", "bat"],
                                        index=0 if s["kind"] == "python" else 1, key=f"sk_{key}_{si}")
            s["target_path"] = r1[1].text_input("Script / .bat path", value=s["target_path"],
                                                key=f"sp_{key}_{si}",
                                                placeholder=r"C:\Sarthi\order client ingest.py")
            r2 = st.columns([3, 1])
            s["args"] = r2[0].text_input("Arguments", value=s["args"], key=f"sa_{key}_{si}",
                                         placeholder="--input-file {assembled_path}")
            s["on_failure"] = r2[1].selectbox("On failure", ["stop", "continue"],
                                              index=0 if s["on_failure"] == "stop" else 1,
                                              key=f"sf_{key}_{si}")

    b1, b2 = st.columns([1, 4])
    if b1.button("+ Add a step"):
        ss.steps.append({"step_name": "", "kind": "python", "target_path": "",
                         "args": "", "on_failure": "stop", "enabled": True})
        st.rerun()
    if b2.button("Save steps", type="primary"):
        payload = []
        for i, s in enumerate(ss.steps):
            if not s["step_name"].strip() or not s["target_path"].strip():
                continue
            payload.append({"step_order": (i + 1) * 10, "step_name": s["step_name"].strip(),
                            "kind": s["kind"], "target_path": s["target_path"].strip(),
                            "args_json": s["args"].strip(), "on_failure": s["on_failure"],
                            "enabled": 1})
        df.set_steps(key, payload, DB_PATH)
        ss.steps_key = None
        st.success("Steps saved.")

    st.divider()
    st.subheader("Recent runs of this type")
    rr = df.list_runs(limit=50, dump_type=key, db_path=DB_PATH)
    if not rr:
        st.caption("No runs yet.")
    else:
        st.dataframe(pd.DataFrame([{
            "when": r["finished_at"], "batch": r["batch_id"], "result": r["status"],
            "steps": ", ".join(f"{x['step']}:{x['status']}" for x in json.loads(r["steps_json"] or "[]")) or "—",
        } for r in rr]), use_container_width=True, hide_index=True)

# ===========================================================================
# MIS REPORTS  ·  CONFIGURE MIS  ·  MIS HISTORY
# ===========================================================================
elif ss.screen == "MIS reports":
    app_mis.screen_mis(DB_PATH, types, goto)

elif ss.screen == "Configure MIS":
    app_mis.screen_configure_mis(DB_PATH, types, goto)

elif ss.screen == "MIS history":
    app_mis.screen_mis_history(DB_PATH)

# ===========================================================================
# RUN HISTORY
# ===========================================================================
elif ss.screen == "Run history":
    st.title("Run history")
    st.caption("Every dump the receiver handled, and how each step went.")
    opts = ["(all)"] + [t["key"] for t in types]
    flt = st.selectbox("Show", opts)
    runs = df.list_runs(limit=300, dump_type=None if flt == "(all)" else flt, db_path=DB_PATH)
    if not runs:
        st.info("No runs recorded yet.")
    else:
        st.dataframe(pd.DataFrame([{
            "when": r["finished_at"], "dump type": r["dump_type"], "batch": r["batch_id"],
            "saved to": r["saved_path"],
            "steps": ", ".join(f"{x['step']}:{x['status']}" for x in json.loads(r["steps_json"] or "[]")) or "—",
            "result": r["status"], "note": r["message"] or "",
        } for r in runs]), use_container_width=True, hide_index=True)

# ===========================================================================
# NEON CATALOG
# ===========================================================================
elif ss.screen == "Neon catalog":
    st.title("Neon catalog")
    st.caption("The list of dump types that can be sent — managed in Plan My Day. "
               "Sync pulls it here; your local steps and folders are kept.")
    if st.button("Sync now", type="primary"):
        res = neon_sync.sync(DB_PATH)
        if res.get("error"):
            st.error(res["error"])
        else:
            st.success(f"{res['total']} types · {res['created']} new, {res['updated']} updated")
            st.rerun()
    cat = [t for t in types if t.get("source") == "neon"]
    if not cat:
        st.info("Nothing synced yet. Set the Neon URL in secrets.toml and click Sync.")
    else:
        st.dataframe(pd.DataFrame([{
            "key": t["key"], "name": t["name"], "handler": t.get("handler"),
            "active": bool(t["enabled"]), "max_files": t.get("max_files"),
            "has steps": bool(df.get_steps(t["key"], DB_PATH)),
            "save folder": t["save_folder"] or "",
        } for t in cat]), use_container_width=True, hide_index=True)
        missing = [t["key"] for t in cat if t["enabled"] and not df.get_steps(t["key"], DB_PATH)]
        if missing:
            st.warning("Active in the catalog but no steps yet (won't run until configured): "
                       + ", ".join(missing))

# ===========================================================================
# SERVICES
# ===========================================================================
elif ss.screen == "Services":
    st.title("Services")
    st.caption("The receiver polls Outlook. The MIS poller fires schedules, picks up "
               "report requests, and drains the build queue. Both start with this app "
               "and keep running after you close it.")

    svc = service_manager.status(force=True)
    if svc["running"]:
        st.markdown(f"<span class='pill on'>● Running</span> &nbsp; "
                    f"<span class='mono'>pid {svc['pid']} · since {svc['started_at']}</span>",
                    unsafe_allow_html=True)
    else:
        st.markdown("<span class='pill fail'>● Not running</span>", unsafe_allow_html=True)
        st.error("Mail is not being polled, and no schedule can fire.")

    b1, b2, b3 = st.columns([1, 1, 3])
    if b1.button("Start" if not svc["running"] else "Restart", type="primary"):
        r = service_manager.restart() if svc["running"] else service_manager.ensure_running()
        if r.get("error"):
            st.error(r["error"])
        st.rerun()
    if b2.button("Stop", disabled=not svc["running"]):
        if service_manager.stop():
            st.warning("Stopped. Mail will not be processed and schedules will not fire.")
        else:
            st.error("Could not stop it — kill the pid manually.")
        st.rerun()
    if b3.button("Run MIS pass now"):
        import mis_poller
        with st.spinner("Polling requests, ticking schedules, draining the queue…"):
            try:
                mis_poller.one_pass(DB_PATH)
                st.success("Pass complete.")
            except Exception as e:
                st.error(f"Pass failed: {e}")
        st.rerun()

    st.divider()
    st.subheader("Log")
    st.caption(f"{service_manager.LOGFILE}")
    st.code(service_manager.tail_log(120) or "(empty)")
    if st.button("Refresh log"):
        st.rerun()

# ===========================================================================
# SETTINGS  (HTTPS update — no git, like PMD)
# ===========================================================================
elif ss.screen == "Settings":
    REPO = "https://github.com/vikssamsung-coder/Sarthireceiver"
    appdir = Path(__file__).resolve().parent

    st.title("Settings")
    st.caption(f"App folder: {appdir}")

    st.subheader("Update from GitHub")
    st.markdown(f"**Repo:** {REPO}")
    st.caption("Downloads the latest code over HTTPS — no git, no install (the same way "
               "PMD updates itself). Your database and secrets are left untouched. After it "
               "finishes, restart the app so the new code loads: Ctrl+C in the terminal, then "
               "`run_app.bat` (or `streamlit run app.py`).")

    if st.button("Update now", type="primary"):
        import updater
        with st.spinner("Downloading latest from GitHub…"):
            try:
                files = updater.update_from_github(str(appdir), log=lambda m: None)
                st.success(f"Updated {len(files)} file(s). Now restart the app to load the new code.")
                with st.expander("Files updated"):
                    for f in sorted(files):
                        st.write("•", f)
            except Exception as e:
                st.error(f"Update failed: {e}\n\nCheck the internet connection and that the "
                         "repo is reachable. Private repo? Put a GitHub token in secrets.toml.")

    st.divider()
    st.subheader("Database")
    st.caption(f"Registry: {DB_PATH}")
    st.caption("Neon URL is read from D:\\PMD-Desktop-main\\.streamlit\\secrets.toml.")
