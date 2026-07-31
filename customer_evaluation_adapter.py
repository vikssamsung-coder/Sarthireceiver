"""Allow-listed commands for the integrated Customer Final Evaluation pipeline."""
from __future__ import annotations

import os
import sys
from pathlib import Path


FIXED_ROOT = Path(r"D:\Customer Final Evaluation")
FIXED_LEADS_FILE = Path(r"D:\Sarthi\Leads\Leads.csv")

MODES = {
    "setup": "Set up Customer Evaluation folders",
    "build_360": "Refresh Sarthi Client 360",
    "process_calls": "Process evaluated call files",
    "full": "Refresh 360 and process calls",
}


def pipeline_folder() -> Path:
    return Path(__file__).resolve().parent / "client_intelligence_pipeline"


def paths() -> dict[str, Path]:
    return {
        "root": FIXED_ROOT,
        "calls": FIXED_ROOT / "01_Input" / "Call_Analysis",
        "client360": FIXED_ROOT / "01_Input" / "Client_360",
        "state": FIXED_ROOT / "03_State",
        "current": FIXED_ROOT / "04_Output" / "Current",
        "archive": FIXED_ROOT / "04_Output" / "Archive",
        "logs": FIXED_ROOT / "05_Logs",
        "review": FIXED_ROOT / "06_Review",
    }


def _latest(folder: Path, patterns: tuple[str, ...]) -> Path | None:
    if not folder.is_dir():
        return None
    found = [p for pattern in patterns for p in folder.glob(pattern) if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def capabilities() -> dict[str, object]:
    code = pipeline_folder()
    p = paths()
    return {
        "pipeline_folder": code,
        "pipeline_ready": (code / "run_pipeline.py").is_file(),
        "extractor_ready": (code / "sarthi_new_clients_360_extract.py").is_file(),
        "leads_ready": FIXED_LEADS_FILE.is_file(),
        "db_password_ready": bool(os.getenv("SARTHI_DB_PASSWORD")),
        "openai_ready": bool(os.getenv("OPENAI_API_KEY")),
        "folders_ready": all(p[key].is_dir() for key in ("calls", "client360", "state", "current")),
        "client360_file": _latest(p["client360"], ("*.xlsx", "*.csv")),
        "call_file_count": sum(1 for pattern in ("*.xlsx", "*.xlsm", "*.csv")
                               for _ in p["calls"].glob(pattern)) if p["calls"].is_dir() else 0,
        "current_workbook": p["current"] / "Sarthi_Client_Intelligence_Current.xlsx",
    }


def _build_360_command(python_exe: str, config: dict) -> list[str]:
    code = pipeline_folder()
    extractor = code / "sarthi_new_clients_360_extract.py"
    if not extractor.is_file():
        raise FileNotFoundError(f"Client 360 extractor not found: {extractor}")
    if not FIXED_LEADS_FILE.is_file():
        raise FileNotFoundError(
            f"Leads.csv was not found at its fixed location: {FIXED_LEADS_FILE}"
        )
    if not os.getenv("SARTHI_DB_PASSWORD"):
        raise RuntimeError(
            "SARTHI_DB_PASSWORD is not set. A background refresh cannot answer "
            "an interactive database-password prompt."
        )
    output = paths()["client360"] / "Sarthi_New_Client_360.xlsx"
    command = [
        python_exe, "-u", str(extractor),
        "--leads", str(FIXED_LEADS_FILE),
        "--output", str(output),
    ]
    if config.get("window") == "rolling":
        command += ["--window", "rolling", "--days", str(max(1, int(config.get("days", 60))))]
    return command


def build_commands(mode: str, config: dict) -> tuple[list[list[str]], Path]:
    """Return only approved commands; browser input can never select a script path."""
    if mode not in MODES:
        raise ValueError(f"Unsupported Customer Evaluation mode: {mode}")
    code = pipeline_folder()
    pipeline = code / "run_pipeline.py"
    if not pipeline.is_file():
        raise FileNotFoundError(f"Customer Evaluation pipeline not found: {pipeline}")
    python_exe = config.get("python_exe") or sys.executable
    setup = [python_exe, "-u", str(pipeline), "--root", str(FIXED_ROOT), "--init"]
    process = [python_exe, "-u", str(pipeline), "--root", str(FIXED_ROOT)]
    if config.get("skip_ai"):
        process.append("--skip-ai")
    elif config.get("max_ai_calls"):
        process += ["--max-ai-calls", str(max(1, int(config["max_ai_calls"])))]
    if mode == "setup":
        commands = [setup]
    elif mode == "build_360":
        commands = [setup, _build_360_command(python_exe, config)]
    elif mode == "process_calls":
        commands = [setup, process]
    else:
        commands = [setup, _build_360_command(python_exe, config), process]
    return commands, code


def output_files() -> list[Path]:
    p = paths()
    candidates = [
        p["current"] / "Sarthi_Client_Intelligence_Current.xlsx",
        p["client360"] / "Sarthi_New_Client_360.xlsx",
    ]
    return [path for path in candidates if path.is_file()]
