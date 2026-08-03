"""Command-safety tests for the integrated Customer Final Evaluation adapter."""
from __future__ import annotations

from unittest.mock import patch

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
    assert len(setup) == 1
    assert len(process) == 2
    assert "--max-ai-calls" in limited[-1] and "125" in limited[-1]
    assert len(full) == 3
    flat = [item for command in full for item in command]
    assert str(adapter.FIXED_ROOT) in flat
    assert str(adapter.FIXED_LEADS_FILE) in flat
    assert "--days" in flat and "45" in flat
    assert len(profile) == 2
    profile_flat = [item for command in profile for item in command]
    assert "new_client_profiling_report.py" in " ".join(profile_flat)
    assert "--tpp" in profile_flat and r"D:\Sarthi\TPP.xlsx" in profile_flat
    try:
        adapter.build_commands("arbitrary_script", {})
    except ValueError:
        pass
    else:
        raise AssertionError("Unknown mode was not rejected")
    print("PASS: fixed paths, approved modes, full sequence, and arbitrary-mode rejection")


if __name__ == "__main__":
    main()
