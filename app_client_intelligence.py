"""Streamlit screen and launcher for the Sarthi Evaluator."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

import client_intelligence_jobs as jobs
from evaluator_adapter import MODES, capabilities, output_files


DEFAULTS = {
    "evaluator_path": r"C:\Users\Vikrant.Dale\Downloads\Sarthi\Sarthi_Evaluator",
    "leads_file": r"D:\Sarthi\Leads\Leads.csv",
    "profile_file": (
        r"D:\New call evalution\Transaction and profile"
        r"\Sarthi_New_Client_360.xlsx"
    ),
    "output_folder": r"D:\New call evalution\quality report\Output\Facts",
}


def _settings_path(db_path) -> Path:
    return Path(db_path).resolve().parent / "client_intelligence_settings.json"


def _load_settings(db_path) -> dict:
    data = dict(DEFAULTS)
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
        "cwd": str(script.parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000 | 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, "-u", str(script), "--db", str(db_path),
         "--job-id", str(job_id)],
        **kwargs,
    )


def _tail(path: str | None, lines: int = 120) -> str:
    if not path:
        return ""
    try:
        return "\n".join(
            Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
        )
    except Exception:
        return ""


def screen(db_path) -> None:
    jobs.init_db(db_path)
    st.title("Client Intelligence")
    st.caption(
        "Build the New Client 360 workbook and run Sarthi Evaluator on this "
        "Windows machine. Only approved evaluator operations can be launched."
    )

    saved = _load_settings(db_path)
    with st.expander("Paths and configuration", expanded=True):
        evaluator_path = st.text_input(
            "Sarthi_Evaluator folder", value=saved["evaluator_path"]
        )
        leads_file = st.text_input("Leads.csv", value=saved["leads_file"])
        profile_file = st.text_input(
            "Sarthi_New_Client_360.xlsx", value=saved["profile_file"]
        )
        output_folder = st.text_input(
            "Evaluator output folder", value=saved["output_folder"]
        )
        if st.button("Save Client Intelligence settings"):
            _save_settings(db_path, {
                "evaluator_path": evaluator_path,
                "leads_file": leads_file,
                "profile_file": profile_file,
                "output_folder": output_folder,
            })
            st.success("Settings saved.")

    config = {
        "evaluator_path": evaluator_path,
        "leads_file": leads_file,
        "profile_file": profile_file,
        "output_folder": output_folder,
        "python_exe": sys.executable,
    }
    caps = capabilities(evaluator_path)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Evaluator folder", "Ready" if caps["evaluator_exists"] else "Missing")
    c2.metric("360 extractor", "Ready" if caps["build_360"] else "Missing")
    c3.metric(
        "Pipeline",
        "Optimized" if caps["optimized"] else ("Legacy" if caps["legacy"] else "Missing"),
    )
    c4.metric("Transaction-only", "Ready" if caps["transaction_only"] else "Unavailable")
    if not caps["optimized"] and caps["legacy"]:
        st.warning(
            "The optimized evaluator is not present in this checkout. Full/Test/"
            "Validate will use the legacy orchestrator until the optimized scripts "
            "are published and pulled."
        )

    st.subheader("Run")
    labels = list(MODES.values())
    by_label = {label: key for key, label in MODES.items()}
    selected = st.selectbox("Operation", labels)
    mode = by_label[selected]
    if mode == "test":
        config["limit"] = st.number_input(
            "Maximum new calls", min_value=1, max_value=1000, value=5
        )
    if mode == "build_360":
        config["window"] = st.radio(
            "Account-opening window", ["calendar", "rolling"], horizontal=True
        )
        if config["window"] == "rolling":
            config["days"] = st.number_input(
                "Rolling days", min_value=1, max_value=730, value=60
            )

    disabled = (
        not caps["evaluator_exists"]
        or (mode == "build_360" and not caps["build_360"])
        or (mode != "build_360" and not (caps["optimized"] or caps["legacy"]))
        or (mode == "transaction_only" and not caps["transaction_only"])
    )
    if st.button("Start operation", type="primary", disabled=disabled):
        job_id = jobs.create_job(mode, config, db_path)
        _launch(job_id, db_path)
        st.success(f"Client Intelligence job #{job_id} started.")
        st.rerun()

    history = jobs.list_jobs(db_path, 100)
    running = [j for j in history if j["status"] in {"queued", "running", "cancel_requested"}]
    if running:
        current = running[0]
        st.info(
            f"Job #{current['id']} · {MODES.get(current['mode'], current['mode'])} "
            f"· {current['status']}"
        )
        if st.button("Cancel current job", disabled=current["status"] == "cancel_requested"):
            jobs.request_cancel(current["id"], db_path)
            st.rerun()
        if current.get("log_path"):
            st.code(_tail(current["log_path"], 80) or "(waiting for output)")

    st.subheader("Outputs")
    available = [path for path in output_files(config) if path.is_file()]
    if not available:
        st.caption("No expected output files found at the configured paths.")
    else:
        for path in available:
            a, b = st.columns([4, 1])
            a.write(f"**{path.name}**  \n`{path}`")
            with path.open("rb") as handle:
                b.download_button(
                    "Download", handle.read(), file_name=path.name,
                    key=f"ci_download_{path}",
                )

    st.subheader("Run history")
    if not history:
        st.caption("No Client Intelligence jobs yet.")
        return
    st.dataframe(
        pd.DataFrame([{
            "job": row["id"],
            "created": row["created_at"],
            "operation": MODES.get(row["mode"], row["mode"]),
            "status": row["status"],
            "started": row["started_at"] or "",
            "finished": row["finished_at"] or "",
            "code": row["return_code"],
            "message": row["message"] or "",
        } for row in history]),
        use_container_width=True, hide_index=True,
    )
    completed_with_logs = [row for row in history if row.get("log_path")]
    if completed_with_logs:
        selected_id = st.selectbox(
            "View job log", [row["id"] for row in completed_with_logs]
        )
        row = next(item for item in completed_with_logs if item["id"] == selected_id)
        st.code(_tail(row["log_path"]) or "(empty log)")

