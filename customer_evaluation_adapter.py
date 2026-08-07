"""Allow-listed commands for the integrated Customer Final Evaluation pipeline."""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path


FIXED_ROOT = Path(r"D:\Customer Final Evaluation")
FIXED_LEADS_FILE = Path(r"D:\Sarthi\Leads\Leads.csv")

MODES = {
    "setup": "Set up Customer Evaluation folders",
    "build_360": "Refresh Sarthi Client 360",
    "process_calls": "Process evaluated call files",
    "rebuild_reports": "Rebuild complete reports from saved analysis - No AI",
    "full": "Daily Run - Build call-driven 360 and all reports (Recommended)",
    "profile_new_clients": "Build New Client Profiling Report",
    "call_analysis_full": "Build Call Analysis Client Report and Run Intelligence",
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


def profiling_output() -> Path:
    return paths()["current"] / "Sarthi_New_Client_Profiling_Current.xlsx"


def _latest(folder: Path, patterns: tuple[str, ...]) -> Path | None:
    if not folder.is_dir():
        return None
    found = [p for pattern in patterns for p in folder.glob(pattern) if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _db_password_ready() -> bool:
    """Recognize every password source supported by the 360 extractor.

    The Receiver runs the extractor in the background, so it must establish
    readiness without prompting. Never returns or logs the password itself.
    """
    if os.getenv("SARTHI_DB_PASSWORD"):
        return True

    try:
        from common_config import DB_CONFIG  # type: ignore

        if isinstance(DB_CONFIG, dict) and DB_CONFIG.get("password"):
            return True
    except (ImportError, AttributeError):
        pass

    # Support an existing local DB_PASSWORD fallback without importing or
    # executing the extractor. Repository copies keep this value blank.
    extractor = pipeline_folder() / "sarthi_new_clients_360_extract.py"
    try:
        tree = ast.parse(extractor.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if not any(isinstance(target, ast.Name) and target.id == "DB_PASSWORD"
                       for target in targets):
                continue
            value = node.value
            return bool(value.value) if isinstance(value, ast.Constant) else False
    except (OSError, SyntaxError):
        pass
    return False


def capabilities() -> dict[str, object]:
    code = pipeline_folder()
    p = paths()
    prompt_file = code / "prompts" / "phase2_call_intelligence.md"
    return {
        "pipeline_folder": code,
        "pipeline_ready": (code / "run_pipeline.py").is_file() and prompt_file.is_file(),
        "prompt_file": prompt_file,
        "prompt_ready": prompt_file.is_file(),
        "extractor_ready": (code / "sarthi_new_clients_360_extract.py").is_file(),
        "profiling_ready": (code / "new_client_profiling_report.py").is_file(),
        "call_analysis_extractor_ready": (code / "call_analysis_clients_360.py").is_file(),
        "leads_ready": FIXED_LEADS_FILE.is_file(),
        "db_password_ready": _db_password_ready(),
        "openai_ready": bool(os.getenv("OPENAI_API_KEY")),
        "folders_ready": all(p[key].is_dir() for key in ("calls", "client360", "state", "current")),
        "client360_file": _latest(p["client360"], ("*.xlsx", "*.csv")),
        "call_file_count": sum(1 for pattern in ("*.xlsx", "*.xlsm", "*.csv")
                               for _ in p["calls"].glob(pattern)) if p["calls"].is_dir() else 0,
        "current_workbook": p["current"] / "Sarthi_Client_Intelligence_Current.xlsx",
        "rm_action_workbook": p["current"] / "RM_Action_Sheet_Current.xlsx",
        "management_workbook": p["current"] / "Management_Dashboard_Current.xlsx",
    }


def run_blockers(mode: str, caps: dict[str, object] | None = None) -> list[str]:
    """Return clear preflight errors without making the Run button inert."""
    if mode not in MODES:
        return [f"Unsupported operation: {mode}"]
    current = caps or capabilities()
    blockers: list[str] = []
    if not current.get("pipeline_ready"):
        blockers.append("Client Intelligence pipeline or AI prompt file is missing.")
    needs_database = mode in {"build_360", "full", "profile_new_clients", "call_analysis_full"}
    needs_standard_360 = mode in {"build_360", "profile_new_clients"}
    if needs_standard_360 and not current.get("extractor_ready"):
        blockers.append("Client 360 extractor is missing.")
    if needs_database and not current.get("leads_ready"):
        blockers.append(f"Leads.csv is missing: {FIXED_LEADS_FILE}")
    if needs_database and not current.get("db_password_ready"):
        blockers.append(
            "The database password is not available to Client 360. Configure it in "
            "SARTHI_DB_PASSWORD, common_config.DB_CONFIG, or the extractor's local "
            "DB_PASSWORD setting."
        )
    if mode == "profile_new_clients" and not current.get("profiling_ready"):
        blockers.append("New Client Profiling report script is missing.")
    if mode in {"full", "call_analysis_full"}:
        if not current.get("call_analysis_extractor_ready"):
            blockers.append("Call Analysis Client 360 extractor is missing.")
        if not current.get("call_file_count"):
            blockers.append(f"No Call Analysis input files were found in: {paths()['calls']}")
    return blockers


def _build_360_command(python_exe: str, config: dict) -> list[str]:
    code = pipeline_folder()
    extractor = code / "sarthi_new_clients_360_extract.py"
    if not extractor.is_file():
        raise FileNotFoundError(f"Client 360 extractor not found: {extractor}")
    if not FIXED_LEADS_FILE.is_file():
        raise FileNotFoundError(
            f"Leads.csv was not found at its fixed location: {FIXED_LEADS_FILE}"
        )
    if not _db_password_ready():
        raise RuntimeError(
            "No database password is available to the background Client 360 refresh. "
            "Configure SARTHI_DB_PASSWORD, common_config.DB_CONFIG, or the extractor's "
            "local DB_PASSWORD fallback."
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


def _build_profiling_command(python_exe: str, config: dict) -> list[str]:
    code = pipeline_folder()
    report = code / "new_client_profiling_report.py"
    if not report.is_file():
        raise FileNotFoundError(f"New Client Profiling report not found: {report}")
    if not FIXED_LEADS_FILE.is_file():
        raise FileNotFoundError(
            f"Leads.csv was not found at its fixed location: {FIXED_LEADS_FILE}"
        )
    if not _db_password_ready():
        raise RuntimeError(
            "No database password is available to the background profiling report. "
            "Configure SARTHI_DB_PASSWORD, common_config.DB_CONFIG, or the Client "
            "360 extractor's local DB_PASSWORD fallback."
        )
    command = [
        python_exe, "-u", str(report),
        "--leads", str(FIXED_LEADS_FILE),
        "--output", str(profiling_output()),
    ]
    if config.get("window") == "rolling":
        command += ["--window", "rolling", "--days", str(max(1, int(config.get("days", 60))))]
    tpp_path = str(config.get("tpp_path") or "").strip()
    if tpp_path:
        command += ["--tpp", tpp_path]
    return command


def _build_call_analysis_command(python_exe: str, config: dict) -> list[str]:
    script = pipeline_folder() / "call_analysis_clients_360.py"
    if not script.is_file():
        raise FileNotFoundError(f"Call Analysis Client 360 extractor not found: {script}")
    command = [
        python_exe, "-u", str(script),
        "--root", str(FIXED_ROOT),
        "--call-input", str(paths()["calls"]),
        "--leads", str(FIXED_LEADS_FILE),
        "--output", str(paths()["client360"] / "Sarthi_Call_Analysis_Client_360.xlsx"),
        "--run-intelligence",
    ]
    if config.get("skip_ai"):
        command.append("--skip-ai")
    elif config.get("max_ai_calls"):
        command += ["--max-ai-calls", str(max(1, int(config["max_ai_calls"])))]
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
    elif mode == "rebuild_reports":
        commands = [setup, process + ["--skip-ai"]]
    elif mode == "full":
        commands = [setup, _build_call_analysis_command(python_exe, config)]
    elif mode == "call_analysis_full":
        commands = [setup, _build_call_analysis_command(python_exe, config)]
    else:
        commands = [setup, _build_profiling_command(python_exe, config)]
    return commands, code


def output_files() -> list[Path]:
    p = paths()
    candidates = [
        p["current"] / "RM_Action_Sheet_Current.xlsx",
        p["current"] / "Management_Dashboard_Current.xlsx",
        p["current"] / "Sarthi_Client_Intelligence_Current.xlsx",
        p["client360"] / "Sarthi_New_Client_360.xlsx",
        p["client360"] / "Sarthi_Call_Analysis_Client_360.xlsx",
        profiling_output(),
    ]
    return [path for path in candidates if path.is_file()]