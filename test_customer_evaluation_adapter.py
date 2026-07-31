"""Command-safety tests for the integrated Customer Final Evaluation adapter."""
from __future__ import annotations

from unittest.mock import patch

import customer_evaluation_adapter as adapter


def main() -> None:
    with patch.object(adapter.Path, "is_file", return_value=True), \
         patch.dict(adapter.os.environ, {"SARTHI_DB_PASSWORD": "test"}):
        setup, _ = adapter.build_commands("setup", {"python_exe": "python"})
        process, _ = adapter.build_commands("process_calls", {"python_exe": "python"})
        full, _ = adapter.build_commands(
            "full", {"python_exe": "python", "window": "rolling", "days": 45}
        )
    assert len(setup) == 1
    assert len(process) == 2
    assert len(full) == 3
    flat = [item for command in full for item in command]
    assert str(adapter.FIXED_ROOT) in flat
    assert str(adapter.FIXED_LEADS_FILE) in flat
    assert "--days" in flat and "45" in flat
    try:
        adapter.build_commands("arbitrary_script", {})
    except ValueError:
        pass
    else:
        raise AssertionError("Unknown mode was not rejected")
    print("PASS: fixed paths, approved modes, full sequence, and arbitrary-mode rejection")


if __name__ == "__main__":
    main()

