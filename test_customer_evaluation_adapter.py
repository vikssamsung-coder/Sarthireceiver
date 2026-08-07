"""Command-safety tests for the integrated Customer Final Evaluation adapter."""
from __future__ import annotations

import tempfile
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import client_intelligence_jobs as jobs
import customer_evaluation_adapter as adapter


def main() -> None:
    with patch.object(adapter.Path, "is_file", return_value=True), \
         patch.dict(adapter.os.environ, {"SARTHI_DB_PASSWORD": "test"}):
        setup, _ = adapter.build_commands("setup", {"python_exe": "python"})
        process, _ = adapter.build_commands("process_calls", {"python_exe": "python"})
        limited, _ = adapter.build_commands(
            "process_calls", {"python_exe": "python", "max_ai_calls": 125}
        )
        full, _ = adapter.build_commands(
            "full", {"python_exe": "python", "window": "rolling", "days": 45}
        )
        profile, _ = adapter.build_commands(
            "profile_new_clients",
            {"python_exe": "python", "window": "rolling", "days": 60,
             "tpp_path": r"D:\Sarthi\TPP.xlsx"},
        )
        call_analysis, _ = adapter.build_commands(
            "call_analysis_full", {"python_exe": "python", "max_ai_calls": 5000}
        )
    assert len(setup) == 1
    assert len(process) == 2
    assert "--max-ai-calls" in limited[-1] and "125" in limited[-1]
    assert len(full) == 2
    flat = [item for command in full for item in command]
    assert str(adapter.FIXED_ROOT) in flat
    assert str(adapter.FIXED_LEADS_FILE) in flat
    assert "call_analysis_clients_360.py" in " ".join(flat)
    assert "--run-intelligence" in flat
    assert len(profile) == 2
    profile_flat = [item for command in profile for item in command]
    assert "new_client_profiling_report.py" in " ".join(profile_flat)
    assert "--tpp" in profile_flat and r"D:\Sarthi\TPP.xlsx" in profile_flat
    assert len(call_analysis) == 2
    call_flat = [item for command in call_analysis for item in command]
    assert "call_analysis_clients_360.py" in " ".join(call_flat)
    assert "--run-intelligence" in call_flat
    assert "--max-ai-calls" in call_flat and "5000" in call_flat
    ready = {
        "pipeline_ready": True, "extractor_ready": True, "leads_ready": True,
        "db_password_ready": True, "profiling_ready": True,
        "call_analysis_extractor_ready": True, "call_file_count": 1,
    }
    assert adapter.run_blockers("full", ready) == []
    no_password = {**ready, "db_password_ready": False}
    assert "database password" in adapter.run_blockers("full", no_password)[0].lower()
    assert adapter.run_blockers("process_calls", no_password) == []
    call_ready = {
        **ready, "call_analysis_extractor_ready": True, "call_file_count": 1,
    }
    assert adapter.run_blockers("call_analysis_full", call_ready) == []

    with tempfile.TemporaryDirectory() as folder:
        db_path = Path(folder) / "jobs.sqlite3"
        job_id = jobs.create_job("full", {}, db_path)
        old = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
        with jobs._connect(db_path) as con:
            con.execute(
                "UPDATE client_intelligence_jobs SET created_at=? WHERE id=?",
                (old, job_id),
            )
        assert jobs.recover_orphaned_jobs(db_path, grace_seconds=120) == 1
        recovered = jobs.get_job(job_id, db_path)
        assert recovered and recovered["status"] == "failed"

        active_id = jobs.create_job("full", {}, db_path)
        with jobs._connect(db_path) as con:
            con.execute(
                "UPDATE client_intelligence_jobs SET created_at=?,pid=? WHERE id=?",
                (old, os.getpid(), active_id),
            )
        assert jobs.recover_orphaned_jobs(db_path, grace_seconds=120) == 0
        active = jobs.get_job(active_id, db_path)
        assert active and active["status"] == "queued"
    try:
        adapter.build_commands("arbitrary_script", {})
    except ValueError:
        pass
    else:
        raise AssertionError("Unknown mode was not rejected")
    print("PASS: fixed paths, approved modes, full sequence, and arbitrary-mode rejection")


if __name__ == "__main__":
    main()
