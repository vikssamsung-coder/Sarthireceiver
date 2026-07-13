# -*- coding: utf-8 -*-
r"""
MIS screens for app.py.

Mirrors the dump-type screens exactly: a card list, a Configure sub-screen, and
a run history. Uses the same pill/mono CSS classes app.py already defines, so it
looks native rather than bolted on.

No recognition_builder here — MIS requests route by report_key, never by rules.
What replaces it is the TRIGGER block: schedule, and/or after dumps land.

app.py calls:
    app_mis.screen_mis(DB_PATH, types, goto)
    app_mis.screen_configure_mis(DB_PATH, types, goto)
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

import mis_flows as mf
import mis_triggers as mt

try:
    import mis_neon
except Exception:
    mis_neon = None

ss = st.session_state


def _args_to_text(raw):
    """Same helper as app.py's — stored args_json -> friendly one-liner."""
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


def _recipients_line(key):
    if mis_neon is None:
        return None, "psycopg not installed — cannot resolve recipients"
    try:
        r = mis_neon.resolve_recipients(key)
    except Exception as e:
        return None, str(e)
    if r["error"]:
        return None, r["error"]
    return f"{', '.join(r['emails'])}  ({r['source']})", None


# ===========================================================================
# MIS REPORTS  (card list)
# ===========================================================================
def screen_mis(DB_PATH: Path, dump_types: list, goto) -> None:
    reports = mf.list_mis_types(db_path=DB_PATH)

    tc, tb = st.columns([3, 1])
    tc.title("MIS reports")
    tc.caption("What each report builds, when it fires, and who receives it.")
    with tb:
        st.write("")
        if st.button("➕ New MIS report", use_container_width=True):
            goto("Configure MIS", mis_sel="__new__")
            st.rerun()
        if mis_neon is not None and st.button("Sync from Neon", use_container_width=True):
            res = mis_neon.sync_catalog(DB_PATH)
            if res.get("error"):
                st.error(res["error"])
            else:
                st.success(f"{res['total']} reports · {res['created']} new, "
                           f"{res['updated']} updated")
                st.rerun()

    st.markdown("""
    <div class="rail">
      <div class="stn"><div class="n">1</div><div class="c">Trigger</div><div class="t">A request, the clock, or a dump landing</div></div>
      <div class="stn"><div class="n">2</div><div class="c">Queue</div><div class="t">One funnel, no double-builds</div></div>
      <div class="stn"><div class="n">3</div><div class="c">Build</div><div class="t">Its scripts, strictly in order</div></div>
      <div class="stn"><div class="n">4</div><div class="c">Send</div><div class="t">Email the file, record the run</div></div>
    </div>""", unsafe_allow_html=True)

    if not reports:
        st.info("No MIS reports yet. Add one, or sync the catalog from Neon in the sidebar.")
        return

    cols = st.columns(2)
    for i, t in enumerate(reports):
        with cols[i % 2]:
            with st.container(border=True):
                a, b = st.columns([3, 1])
                a.markdown(f"**{t['name']}**  \n<span class='mono'>{t['key']}</span>",
                           unsafe_allow_html=True)
                b.markdown(f"<span class='pill {'on' if t['enabled'] else 'off'}'>"
                           f"{'Active' if t['enabled'] else 'Off'}</span>",
                           unsafe_allow_html=True)
                if t.get("source") == "neon":
                    b.markdown("<span class='pill neon'>Neon</span>",
                               unsafe_allow_html=True)

                trig = mf.get_trigger(t)
                bits = []
                if t.get("schedule_time"):
                    bits.append(f"<span class='pill neon'>⏰ {t['schedule_time']} "
                                f"{'·'.join(mt.mask_to_days(t.get('schedule_days')))}</span>")
                if trig["keys"]:
                    bits.append(f"<span class='pill neon'>after {trig['mode'].upper()} of "
                                f"{', '.join(trig['keys'])}</span>")
                bits.append("<span class='pill off'>on request</span>")
                st.markdown(" ".join(bits), unsafe_allow_html=True)

                st.markdown(f"<span class='mono'>writes to {t.get('out_folder') or '(not set)'}</span>",
                            unsafe_allow_html=True)

                steps = mf.get_mis_steps(t["key"], db_path=DB_PATH)
                if steps:
                    st.markdown(" ".join(
                        f"<span class='pill neon'>{s['step_name']}</span>" for s in steps),
                        unsafe_allow_html=True)
                else:
                    st.markdown("<span class='pill off'>no steps yet</span>",
                                unsafe_allow_html=True)

                if t.get("enabled") and not steps:
                    st.markdown("<span class='pill fail'>won't run — no steps</span>",
                                unsafe_allow_html=True)

                o1, o2 = st.columns(2)
                if o1.button("Open", key=f"mopen_{t['key']}", use_container_width=True):
                    goto("Configure MIS", mis_sel=t["key"])
                    st.rerun()
                if o2.button("Build now", key=f"mbuild_{t['key']}", use_container_width=True):
                    mf.enqueue(t["key"], "manual", f"manual:{t['key']}:{mf._now()}",
                               db_path=DB_PATH)
                    st.success("Queued. The MIS poller picks it up on its next pass "
                               "(or run: python mis_poller.py --once).")

    st.divider()
    st.subheader("Queue")
    q = mf.list_queue(20, db_path=DB_PATH)
    if not q:
        st.caption("Nothing queued.")
    else:
        st.dataframe(pd.DataFrame([{
            "when": r["created_at"], "report": r["report_key"], "trigger": r["trigger"],
            "status": r["status"], "tries": r["attempts"], "note": r["message"] or "",
        } for r in q]), use_container_width=True, hide_index=True)
        if st.button("Release stale claims"):
            n = mf.release_stale_claims(120, db_path=DB_PATH)
            st.success(f"Released {n}.")
            st.rerun()


# ===========================================================================
# CONFIGURE an MIS report
# ===========================================================================
def screen_configure_mis(DB_PATH: Path, dump_types: list, goto) -> None:
    if st.button("← All MIS reports"):
        goto("MIS reports")
        st.rerun()

    is_new = ss.get("mis_sel") == "__new__"
    t = None if is_new else mf.get_mis_type(ss.get("mis_sel"), db_path=DB_PATH)
    if not is_new and not t:
        st.warning("That report no longer exists.")
        st.stop()

    st.title("New MIS report" if is_new else t["name"])
    if not is_new and t.get("source") == "neon":
        st.markdown(f"<span class='pill neon'>from Neon catalog</span> &nbsp; "
                    f"handler <span class='mono'>{t.get('handler') or t['key']}</span>",
                    unsafe_allow_html=True)

    # ---- header ------------------------------------------------------------
    h1, h2, h3 = st.columns([2, 1, 1])
    key_in = h1.text_input("Key (short id)", value="" if is_new else t["key"],
                           disabled=not is_new,
                           help="lowercase, no spaces. Must match report_key in Neon "
                                "if PMD users will request it.")
    name_in = h1.text_input("Name", value="" if is_new else (t["name"] or ""))
    enabled_in = h2.toggle("Active", value=True if is_new else bool(t["enabled"]))
    order_in = h3.number_input("Sort order", value=100 if is_new else int(t["sort_order"]),
                               step=10)
    folder_in = st.text_input("Output folder — the built report is written here",
                              value="" if is_new else (t.get("out_folder") or ""),
                              placeholder=r"e.g. D:\Sarthi\MIS")

    if is_new:
        if st.button("Create MIS report", type="primary"):
            k = key_in.strip().lower().replace(" ", "_")
            if not k:
                st.error("Give it a key.")
            elif mf.get_mis_type(k, db_path=DB_PATH):
                st.error("That key already exists.")
            else:
                mf.upsert_mis_type(k, name_in.strip() or k, int(enabled_in),
                                   int(order_in), out_folder=folder_in.strip() or None,
                                   db_path=DB_PATH)
                goto("Configure MIS", mis_sel=k)
                st.rerun()
        st.stop()

    key = t["key"]
    trig = mf.get_trigger(t)

    st.divider()

    # ---- 1. TRIGGERS -------------------------------------------------------
    st.subheader("① When it runs")
    st.caption("A report can fire three ways, and they never collide — the queue "
               "serialises them, so the same report is never built twice at once.")

    st.markdown("<span class='pill off'>Always on</span> &nbsp; **On request** — a PMD "
                "user asks for it; the finished file goes back to them.",
                unsafe_allow_html=True)

    sched_on = st.checkbox("⏰ On a schedule", value=bool(t.get("schedule_time")))
    sched_time, sched_days = None, t.get("schedule_days") or "1111100"
    if sched_on:
        s1, s2 = st.columns([1, 3])
        raw = t.get("schedule_time") or "09:00"
        try:
            hh, mm = str(raw).split(":")
            hh, mm = int(hh), int(mm)
        except Exception:
            hh, mm = 9, 0
        from datetime import time as _time
        tt = s1.time_input("Fire at", value=_time(hh, mm), key=f"mtime_{key}")
        sched_time = tt.strftime("%H:%M")
        picked = s2.multiselect("Days", mt.DAYS,
                                default=mt.mask_to_days(t.get("schedule_days")),
                                key=f"mdays_{key}")
        sched_days = mt.days_to_mask(picked)
        st.caption("If the box was off at that time, the report fires as soon as the "
                   "poller comes back up — it is not skipped.")

    dump_on = st.checkbox("📥 After dumps land", value=bool(trig["keys"]))
    picked_dumps, mode = [], trig["mode"]
    if dump_on:
        d1, d2 = st.columns([3, 1])
        keys = [d["key"] for d in dump_types]
        picked_dumps = d1.multiselect(
            "Waits on these dumps", keys,
            default=[k for k in trig["keys"] if k in keys], key=f"mdumps_{key}")
        mode = d2.radio("Mode", ["all", "any"],
                        index=0 if trig["mode"] == "all" else 1, key=f"mmode_{key}")
        if mode == "all":
            st.caption("**ALL** — waits until every dump above has landed cleanly today. "
                       "The last one to arrive fires the build. Once per day.")
        else:
            st.caption("**ANY** — fires as soon as any one of them lands.")
        if picked_dumps:
            waiting = mt.pending_dumps(key, db_path=DB_PATH)
            if waiting:
                st.info("Still waiting on today: " + ", ".join(waiting))
            else:
                st.success("All required dumps have landed today.")

    # ---- 2. RECIPIENTS -----------------------------------------------------
    st.subheader("② Who gets it")
    st.caption("Requested reports go back to the requester. Scheduled and dump-triggered "
               "reports go to whoever has access in Neon (`mis_report_access`). No access "
               "rows means admins only — access is denied by default, never assumed.")
    line, err = _recipients_line(key)
    if err:
        st.error(f"Recipients: {err}")
    else:
        st.markdown(f"<span class='mono'>{line}</span>", unsafe_allow_html=True)

    # ---- 3. STEPS ----------------------------------------------------------
    st.subheader("③ What builds it, in order")
    st.caption("Steps run strictly in order, one at a time — a “stop” step that fails "
               "halts the rest, so no later step ever sees a half-built input. "
               "Placeholders: {out_folder}, {params}, {report_key}, {report_name}, "
               "{requester_email}, {user_key}, {req_id}, {today}, {output_path}.")
    st.warning("**The one rule:** a step must print `OUTPUT=<full path>` on its last "
               "line. That file is what gets emailed. No OUTPUT= line and the run fails "
               "— the engine will not guess which file you meant.", icon="⚠️")

    if ss.get("mis_steps_key") != key:
        rows = mf.get_mis_steps(key, db_path=DB_PATH)
        ss.mis_steps = [{"step_name": r["step_name"], "kind": r["kind"],
                         "target_path": r["target_path"],
                         "args": _args_to_text(r["args_json"]),
                         "on_failure": r["on_failure"], "enabled": bool(r["enabled"])}
                        for r in rows]
        ss.mis_steps_key = key

    for si, s in enumerate(ss.mis_steps):
        with st.container(border=True):
            top = st.columns([0.5, 6, 0.6, 0.6, 0.6])
            top[0].markdown(f"### {si + 1}")
            s["step_name"] = top[1].text_input("Step name", value=s["step_name"],
                                               key=f"msn_{key}_{si}",
                                               placeholder="e.g. build daily pnl")
            if top[2].button("▲", key=f"mup_{key}_{si}", disabled=si == 0):
                ss.mis_steps[si - 1], ss.mis_steps[si] = ss.mis_steps[si], ss.mis_steps[si - 1]
                st.rerun()
            if top[3].button("▼", key=f"mdn_{key}_{si}", disabled=si == len(ss.mis_steps) - 1):
                ss.mis_steps[si + 1], ss.mis_steps[si] = ss.mis_steps[si], ss.mis_steps[si + 1]
                st.rerun()
            if top[4].button("✕", key=f"mdel_{key}_{si}"):
                ss.mis_steps.pop(si)
                st.rerun()

            r1 = st.columns([1, 3])
            s["kind"] = r1[0].selectbox("Type", ["python", "bat"],
                                        index=0 if s["kind"] == "python" else 1,
                                        key=f"mk_{key}_{si}")
            s["target_path"] = r1[1].text_input("Script / .bat path", value=s["target_path"],
                                                key=f"mp_{key}_{si}",
                                                placeholder=r"D:\dump_processor_app\mis_build_from_dumps.py")
            r2 = st.columns([3, 1])
            s["args"] = r2[0].text_input("Arguments", value=s["args"], key=f"ma_{key}_{si}",
                                         placeholder="--dumps orderbook --out {out_folder} --report {report_key}")
            s["on_failure"] = r2[1].selectbox("On failure", ["stop", "continue"],
                                              index=0 if s["on_failure"] == "stop" else 1,
                                              key=f"mf_{key}_{si}")

    b1, b2 = st.columns([1, 4])
    if b1.button("+ Add a step"):
        ss.mis_steps.append({"step_name": "", "kind": "python", "target_path": "",
                             "args": "", "on_failure": "stop", "enabled": True})
        st.rerun()

    # ---- SAVE everything ---------------------------------------------------
    st.divider()
    sv1, sv2, sv3 = st.columns([1, 1, 3])
    if sv1.button("Save report", type="primary"):
        mf.upsert_mis_type(
            key, name_in.strip() or key, int(enabled_in), int(order_in),
            out_folder=folder_in.strip() or None,
            schedule_time=sched_time if sched_on else None,
            schedule_days=sched_days,
            trigger={"mode": mode, "keys": picked_dumps if dump_on else []},
            db_path=DB_PATH)

        payload = []
        for i, s in enumerate(ss.mis_steps):
            if not s["step_name"].strip() or not s["target_path"].strip():
                continue
            payload.append({"step_order": (i + 1) * 10,
                            "step_name": s["step_name"].strip(),
                            "kind": s["kind"], "target_path": s["target_path"].strip(),
                            "args_json": s["args"].strip(),
                            "on_failure": s["on_failure"], "enabled": 1})
        mf.set_mis_steps(key, payload, db_path=DB_PATH)
        ss.mis_steps_key = None
        st.success("Saved — details, triggers and steps.")
        st.rerun()

    if sv2.button("Build now"):
        mf.enqueue(key, "manual", f"manual:{key}:{mf._now()}", db_path=DB_PATH)
        st.success("Queued. Run `python mis_poller.py --once` (or leave the service running).")

    if sv3.button("Delete this report"):
        mf.delete_mis_type(key, db_path=DB_PATH)
        ss.mis_steps_key = None
        goto("MIS reports")
        st.rerun()

    # ---- runs --------------------------------------------------------------
    st.divider()
    st.subheader("Recent runs of this report")
    runs = mf.list_mis_runs(limit=25, report_key=key, db_path=DB_PATH)
    if not runs:
        st.caption("No runs yet.")
    else:
        for r in runs:
            icon = {"success": "✅", "built": "📄", "failed": "❌"}.get(r["status"], "⏳")
            with st.expander(f"{icon}  {r['finished_at']}  ·  {r['trigger']}  ·  {r['status']}"):
                if r.get("message"):
                    st.write(r["message"])
                if r.get("output_path"):
                    st.markdown(f"<span class='mono'>{r['output_path']}</span>",
                                unsafe_allow_html=True)
                    p = Path(r["output_path"])
                    if p.is_file():
                        st.download_button("Download", p.read_bytes(), file_name=p.name,
                                           key=f"mdl_{r['id']}")
                try:
                    steps = json.loads(r.get("steps_json") or "[]")
                except Exception:
                    steps = []
                if steps:
                    st.dataframe(pd.DataFrame(steps), use_container_width=True,
                                 hide_index=True)


# ===========================================================================
# MIS RUN HISTORY (all reports)
# ===========================================================================
def screen_mis_history(DB_PATH: Path) -> None:
    st.title("MIS history")
    st.caption("Every report the builder ran, how each step went, and where it went.")
    reports = mf.list_mis_types(db_path=DB_PATH)
    opts = ["(all)"] + [t["key"] for t in reports]
    flt = st.selectbox("Show", opts)
    runs = mf.list_mis_runs(limit=300,
                            report_key=None if flt == "(all)" else flt,
                            db_path=DB_PATH)
    if not runs:
        st.info("No MIS runs recorded yet.")
        return
    st.dataframe(pd.DataFrame([{
        "when": r["finished_at"], "report": r["report_key"], "trigger": r["trigger"],
        "result": r["status"], "output": r["output_path"] or "",
        "steps": ", ".join(f"{x['step']}:{x['status']}"
                           for x in json.loads(r["steps_json"] or "[]")) or "—",
        "note": r["message"] or "",
    } for r in runs]), use_container_width=True, hide_index=True)
