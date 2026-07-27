"""Controlled command adapter for the separately checked-out Sarthi_Evaluator."""
from __future__ import annotations

import os
import sys
from pathlib import Path


MODES = {
    "build_360": "Build New Client 360",
    "validate": "Validate Intelligence",
    "test": "Test Intelligence",
    "full": "Run Full Intelligence",
    "transaction_only": "Transaction Taxonomy Only",
}


def capabilities(evaluator_path: str | Path) -> dict:
    root = Path(evaluator_path).expanduser()
    return {
        "evaluator_exists": root.is_dir(),
        "build_360": (root / "sarthi_new_clients_360_extract.py").is_file(),
        "optimized": (root / "run_optimized_pipeline.py").is_file(),
        "legacy": (root / "run_complete_pipeline.py").is_file(),
        "transaction_only": (
            (root / "10_build_transaction_taxonomy.py").is_file()
            and (root / "11_enrich_master_transaction_flags.py").is_file()
        ),
    }


def _required_file(value: str, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    return path


def build_command(mode: str, config: dict) -> tuple[list[str], Path]:
    if mode not in MODES:
        raise ValueError(f"Unsupported Client Intelligence mode: {mode}")

    root = Path(config["evaluator_path"]).expanduser().resolve()
    caps = capabilities(root)
    if not caps["evaluator_exists"]:
        raise FileNotFoundError(f"Sarthi_Evaluator folder not found: {root}")

    python_exe = config.get("python_exe") or sys.executable
    profile = Path(config["profile_file"]).expanduser()

    if mode == "build_360":
        script = root / "sarthi_new_clients_360_extract.py"
        _required_file(str(script), "360 extractor")
        leads = _required_file(config["leads_file"], "Leads CSV")
        if not os.getenv("SARTHI_DB_PASSWORD"):
            raise RuntimeError(
                "SARTHI_DB_PASSWORD is not set. Background jobs cannot answer "
                "the extractor's interactive password prompt."
            )
        profile.parent.mkdir(parents=True, exist_ok=True)
        command = [
            python_exe, "-u", str(script), "--leads", str(leads),
            "--output", str(profile),
        ]
        if config.get("window") == "rolling":
            command += ["--window", "rolling", "--days", str(config.get("days", 60))]
        return command, root

    _required_file(str(profile), "Client 360 workbook")
    if caps["optimized"]:
        script = root / "run_optimized_pipeline.py"
        command = [python_exe, "-u", str(script), "--profile-file", str(profile)]
        if mode == "validate":
            command.append("--dry-run")
        elif mode == "test":
            command += ["--limit", str(max(1, int(config.get("limit", 5))))]
        elif mode == "transaction_only":
            # The optimized orchestrator is expected to expose this safe mode.
            command.append("--transaction-only")
        return command, root

    if mode == "transaction_only":
        raise RuntimeError(
            "Transaction-only mode requires the optimized evaluator scripts."
        )
    if not caps["legacy"]:
        raise FileNotFoundError(
            "Neither run_optimized_pipeline.py nor run_complete_pipeline.py "
            f"exists in {root}."
        )
    script = root / "run_complete_pipeline.py"
    command = [
        python_exe, "-u", str(script),
        "--filter-file", str(profile), "--filter-column", "Lead Number",
    ]
    if mode == "validate":
        command.append("--dry-run")
    elif mode == "test":
        command += ["--limit", str(max(1, int(config.get("limit", 5))))]
    return command, root


def output_files(config: dict) -> list[Path]:
    profile = Path(config["profile_file"]).expanduser()
    output = Path(config["output_folder"]).expanduser()
    names = [
        "Call_Fact.xlsx", "Issue_Fact.xlsx", "Client_Status_Fact.xlsx",
        "Signal_Fact.xlsx", "Lead_Day_Fact.xlsx", "Transaction_Flags.xlsx",
        "Client_Intelligence_Master.xlsx", "Action_Fact.xlsx",
    ]
    return [profile] + [output / name for name in names]

