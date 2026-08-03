"""Streamlit screen for the integrated Sarthi Customer Final Evaluation pipeline."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

import client_intelligence_jobs as jobs
from customer_evaluation_adapter import (
    FIXED_LEADS_FILE, FIXED_ROOT, MODES, capabilities, output_files, paths,
)


def _settings_path(db_path) -> Path:
    return Path(db_path).resolve().parent / "client_intelligence_settings.json"


def _load_settings(db_path) -> dict:
    data = {"window": "calendar", "days": 60, "max_ai_calls": 100}
    try:
        data.update(json.loads(_settings_path(db_path).read_text(encoding="utf-8")))
    except Exception:
        pass
    return data


def _save_settings(db_path, data: dict) -> None:
    path = _settings_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _launch(job_id: int, db_path) -> None:
    script = Path(__file__).resolve().with_name("client_intelligence_worker.py")
    kwargs = {
        "cwd": str(script.parent), "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000 | 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, "-u", str(script), "--db", str(db_path),
         "--job-id", str(job_id)], **kwargs,
    )


def _tail(path: str | None, lines: int = 120) -> str:
    try:
        return "\n".join(Path(path).read_text(
            encoding="utf-8", errors="replace").splitlines()[-lines:]) if path else ""
    except Exception:
        return ""


def screen(db_path) -> None:
    jobs.init_db(db_path)
    st.title("Client Intelligence")
    st.caption(
        "Build Client 360, consolidate evaluated calls, deduplicate and version them, "
        "and generate the client-wise intelligence workbook."
    )

    p = paths()
    caps = capabilities()
    saved = _load_settings(db_path)

    st.subheader("Fixed production locations")
    st.code(
        f"Working folder : {FIXED_ROOT}\n"
        f"Leads.csv      : {FIXED_LEADS_FILE}\n"
        f"Call inputs    : {p['calls']}\n"
        f"Current output : {p['current']}"
    )
    st.caption("Leads.csv stays in its existing location and is read directly; it is not copied.")

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Pipeline", "Ready" if caps["pipeline_ready"] else "Missing")
    c2.metric("Leads.csv", "Ready" if caps["leads_ready"] else "Missing")
    c3.metric("Call files", int(caps["call_file_count"]))
    c4.metric("Client 360", "Ready" if caps["client360_file"] else "Not built")
    c5.metric("AI extraction", "Ready" if caps["openai_ready"] else "Ingest only")
    if not caps["db_password_ready"]:
        st.warning(
            "No database password was found for the Client 360 refresh. Configure "
            "SARTHI_DB_PASSWORD (recommended), common_config.DB_CONFIG, or the "
            "extractor's local DB_PASSWORD fallback. Call-file processing does not "
            "require it."
        )
    if not caps["openai_ready"]:
        st.info(
            "OPENAI_API_KEY is not set. Calls will still be ingested, deduplicated and "
            "versioned, but new intelligence will remain pending until the key is available."
        )

    st.subheader("Run")
    labels = list(MODES.values())
    by_label = {label: key for key, label in MODES.items()}
    selected = st.selectbox("Operation", labels)
    mode = by_label[selected]
    config = {"python_exe": sys.executable}
    if mode in {"process_calls", "full"}:
        config["max_ai_calls"] = st.number_input(
            "Maximum new or changed calls to interpret in this run",
            min_value=1, max_value=5000, value=int(saved.get("max_ai_calls", 100)), step=25,
            help="Exact duplicate calls never use AI and do not count toward this limit.",
        )
        if mode == "process_calls":
            _save_settings(db_path, {**saved, "max_ai_calls": int(config["max_ai_calls"])})
    if mode in {"build_360", "full"}:
        default_window = saved.get("window", "calendar")
        window = st.radio(
            "Account-opening window", ["calendar", "rolling"], horizontal=True,
            index=0 if default_window == "calendar" else 1,
        )
        config["window"] = window
        if window == "rolling":
            config["days"] = st.number_input(
                "Rolling days", min_value=1, max_value=730,
                value=int(saved.get("days", 60)),
            )
        _save_settings(db_path, {
            "window": window, "days": int(config.get("days", 60)),
            "max_ai_calls": int(config.get("max_ai_calls", saved.get("max_ai_calls", 100))),
        })

    history = jobs.list_jobs(db_path, 100)
    active = [row for row in history if row["status"] in {
        "queued", "running", "cancel_requested"}]
    needs_360 = mode in {"build_360", "full"}
    disabled = (
        bool(active) or not caps["pipeline_ready"]
        or (needs_360 and (not caps["extractor_ready"]
                          or not caps["leads_ready"]
                          or not caps["db_password_ready"]))
    )
    if st.button("Start operation", type="primary", disabled=disabled):
        job_id = jobs.create_job(mode, config, db_path)
        _launch(job_id, db_path)
        st.success(f"Client Intelligence job #{job_id} started.")
        st.rerun()

    if active:
        current = active[0]
        st.info(f"Job #{current['id']} · {MODES.get(current['mode'], current['mode'])} · {current['status']}")
        if st.button("Cancel current job", disabled=current["status"] == "cancel_requested"):
            jobs.request_cancel(current["id"], db_path)
            st.rerun()
        if current.get("log_path"):
            st.code(_tail(current["log_path"], 80) or "(waiting for output)")

    st.subheader("Outputs")
    available = output_files()
    if not available:
        st.caption("No Client Intelligence outputs have been generated yet.")
    for path in available:
        a, b = st.columns([4, 1])
        a.write(f"**{path.name}**  \n`{path}`")
        with path.open("rb") as handle:
            b.download_button("Download", handle.read(), file_name=path.name,
                              key=f"ci_download_{path}")

    st.subheader("Run history")
    if not history:
        st.caption("No Client Intelligence jobs yet.")
        return
    st.dataframe(pd.DataFrame([{
        "job": row["id"], "created": row["created_at"],
        "operation": MODES.get(row["mode"], row["mode"]),
        "status": row["status"], "started": row["started_at"] or "",
        "finished": row["finished_at"] or "", "code": row["return_code"],
        "message": row["message"] or "",
    } for row in history]), use_container_width=True, hide_index=True)
    with_logs = [row for row in history if row.get("log_path")]
    if with_logs:
        selected_id = st.selectbox("View job log", [row["id"] for row in with_logs])
        row = next(item for item in with_logs if item["id"] == selected_id)
        st.code(_tail(row["log_path"]) or "(empty log)")
