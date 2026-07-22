# -*- coding: utf-8 -*-
r"""
Flow engine — runs one dump end to end:
  1) SAVE + EXTRACT the dump into the dump type's folder
       zip  -> unzipped; the CSV/XLSX inside becomes the input
       csv/xlsx -> placed as-is
  2) RUN the configured code steps in sequence (stop/continue on failure)
  3) RECORD the confirmation back into the app (flow_runs)

Used from email_processor.py (pass your run_python_script / run_bat as runners)
or standalone (built-in subprocess runner). Runner contracts match
email_processor.py:
  run_python(batch_id, dump_type, trigger_name, script_path, extra_args) -> bool
  run_bat(batch_id, final_package_name, bat_path) -> bool
"""
from __future__ import annotations

import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path

import dump_flows as df
import extractor


STEP_TIMEOUT_SEC = int(os.environ.get("SARTHI_FLOW_STEP_TIMEOUT", "1800"))


def _default_run_python(batch_id, dump_type, trigger_name, script_path, extra_args,
                        log, working_dir=None):
    if not Path(script_path).is_file():
        log(f"  target not found: {script_path}")
        return False
    cmd = [sys.executable, str(script_path)] + list(extra_args or [])
    log(f"run python: {' '.join(cmd)}")
    try:
        p = subprocess.run(
            cmd, cwd=working_dir or str(Path(script_path).parent),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=STEP_TIMEOUT_SEC,
        )
        if p.returncode != 0:
            log(f"  exit {p.returncode}: {p.stderr[-400:]}")
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"  timed out after {STEP_TIMEOUT_SEC}s")
        return False
    except Exception as e:
        log(f"  error: {e}")
        return False


def _default_run_bat(batch_id, final_package_name, bat_path, log, working_dir=None):
    if not Path(bat_path).is_file():
        log(f"  target not found: {bat_path}")
        return False
    log(f"run bat: {bat_path}")
    try:
        p = subprocess.run(
            [str(bat_path)], cwd=working_dir or str(Path(bat_path).parent),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=STEP_TIMEOUT_SEC, shell=(os.name == "nt"),
        )
        if p.returncode != 0:
            log(f"  exit {p.returncode}: {p.stderr[-400:]}")
        return p.returncode == 0
    except subprocess.TimeoutExpired:
        log(f"  timed out after {STEP_TIMEOUT_SEC}s")
        return False
    except Exception as e:
        log(f"  error: {e}")
        return False


def run_dump_flow(batch_id, dump_type, assembled_path, subject=None, sender_email=None,
                  *, run_python=None, run_bat=None, leads_dir="", db_path=df.DEFAULT_DB,
                  log=print, dry_run=False):
    """Run the full flow for one dump. Returns (ok: bool, results: list)."""
    started_at = datetime.now().isoformat(timespec="seconds")

    # 1) SAVE + EXTRACT
    save_folder = None
    has_input = bool(str(assembled_path or "").strip())
    saved_path = Path(assembled_path) if has_input else None
    extracted_files = []
    try:
        save_folder = df.get_save_folder(dump_type, db_path=db_path)
        if has_input and not Path(assembled_path).is_file() and not dry_run:
            raise FileNotFoundError(f"input dump is not a file: {assembled_path}")
        if save_folder and has_input and not dry_run:
            res = extractor.extract_dump(assembled_path, save_folder, log=log)
            if res.get("primary"):
                saved_path = Path(res["primary"])
            else:
                raise ValueError("extractor did not return a primary input file")
            extracted_files = [str(f) for f in res.get("files", [])]
        elif save_folder and has_input:
            saved_path = Path(save_folder) / Path(assembled_path).name
    except Exception as e:
        message = f"extract/save into '{save_folder}' failed: {e}"
        log(message)
        try:
            df.record_run(batch_id, dump_type, saved_path, "failed", [],
                          message=message, started_at=started_at, db_path=db_path)
        except Exception as record_error:
            log(f"could not record confirmation: {record_error}")
        return False, []

    context = {
        "batch_id": batch_id, "assembled_path": str(saved_path) if saved_path else "",
        "save_folder": str(save_folder or ""), "subject": str(subject or ""),
        "sender_email": str(sender_email or ""), "dump_type": dump_type,
        "leads_dir": str(leads_dir),
        "final_package_name": saved_path.name if saved_path else "",
        "extracted_files": ";".join(extracted_files),
    }

    def _confirm(status, results, message=""):
        try:
            df.record_run(batch_id, dump_type, saved_path or "", status, results,
                          message=message, started_at=started_at, db_path=db_path)
        except Exception as e:
            log(f"could not record confirmation: {e}")

    try:
        steps = df.build_steps(dump_type, context, db_path=db_path)
    except Exception as e:
        log(f"failed to load flow for '{dump_type}': {e}")
        _confirm("failed", [], f"flow load error: {e}")
        return False, []

    if not steps:
        log(f"no steps configured for '{dump_type}'")
        _confirm("failed", [], "no steps configured")
        return False, []

    # 2) RUN
    results = []
    for s in steps:
        name, kind, target = s["step_name"], s["kind"], s["target_path"]
        args, on_failure = s["args"], s.get("on_failure", "stop")
        if dry_run:
            results.append({"step": name, "status": "dry-run"})
            continue
        if kind == "bat":
            ok = (run_bat(batch_id, context["final_package_name"], target)
                  if run_bat else _default_run_bat(
                      batch_id, context["final_package_name"], target, log,
                      working_dir=s.get("working_dir")))
        else:
            ok = (run_python(batch_id, dump_type, name, target, args)
                  if run_python else _default_run_python(
                      batch_id, dump_type, name, target, args, log,
                      working_dir=s.get("working_dir")))
        results.append({"step": name, "status": "ok" if ok else "failed"})
        if not ok and on_failure == "stop":
            log(f"step '{name}' failed (stop). halting flow for {batch_id}.")
            _confirm("failed", results, f"stopped at step '{name}'")
            return False, results
        if not ok:
            log(f"step '{name}' failed but on_failure=continue.")

    if dry_run:
        _confirm("dry-run", results)
        return True, results
    any_failed = any(r["status"] == "failed" for r in results)
    _confirm("partial" if any_failed else "success", results)

    # --- MIS chain -----------------------------------------------------------
    # Only a clean success feeds a report. 'partial' means a step failed, so the
    # data is suspect — never build an MIS on it.
    # Enqueue only: a few SELECTs and an INSERT OR IGNORE. Never builds, never
    # mails, never blocks the dump. Wrapped so an MIS problem CANNOT break a dump.
    if not any_failed:
        try:
            import mis_triggers
            fired = mis_triggers.on_dump_complete(dump_type, batch_id, db_path=db_path)
            for k in fired:
                log(f"MIS queued: {k}")
        except Exception as e:
            log(f"MIS trigger skipped: {e}")

    return True, results
