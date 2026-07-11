# -*- coding: utf-8 -*-
r"""
Flow engine — runs one dump end to end:
  1) save the dump into the dump type's predefined folder
  2) run the configured code steps in sequence (stop/continue on failure)
  3) write the confirmation back into the app (flow_runs)

Used two ways, from the SAME function:
  - from email_processor.py: pass your run_python_script / run_bat as runners,
    so all your existing trigger-dedup, SHA and logging stay in force.
  - standalone (the app's dry-run / manual run): omit runners and it uses a
    built-in subprocess runner.

Runner contracts (match email_processor.py exactly):
  run_python(batch_id, dump_type, trigger_name, script_path, extra_args) -> bool
  run_bat(batch_id, final_package_name, bat_path) -> bool
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import dump_flows as df


# ---- default runners (used when the processor's runners aren't injected) ----
def _default_run_python(batch_id, dump_type, trigger_name, script_path, extra_args, log):
    cmd = [sys.executable, str(script_path)] + list(extra_args or [])
    log(f"run python: {' '.join(cmd)}")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            log(f"  exit {p.returncode}: {p.stderr[-400:]}")
        return p.returncode == 0
    except Exception as e:
        log(f"  error: {e}")
        return False


def _default_run_bat(batch_id, final_package_name, bat_path, log):
    log(f"run bat: {bat_path}")
    try:
        p = subprocess.run([str(bat_path)], capture_output=True, text=True, shell=False)
        if p.returncode != 0:
            log(f"  exit {p.returncode}: {p.stderr[-400:]}")
        return p.returncode == 0
    except Exception as e:
        log(f"  error: {e}")
        return False


def run_dump_flow(batch_id, dump_type, assembled_path, subject=None, sender_email=None,
                  *, run_python=None, run_bat=None, leads_dir="", db_path=df.DEFAULT_DB,
                  log=print, dry_run=False):
    """Run the full flow for one dump. Returns (ok: bool, results: list).

    results: [{step, status}]  — also persisted to flow_runs for the UI.
    dry_run: resolve + render + record, but do not execute steps (for testing).
    """
    started_at = datetime.now().isoformat(timespec="seconds")

    # 1) SAVE to the type's predefined folder
    save_folder = None
    saved_path = Path(assembled_path)
    try:
        save_folder = df.get_save_folder(dump_type, db_path=db_path)
        if save_folder and not dry_run:
            dest_dir = Path(save_folder)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / Path(assembled_path).name
            if Path(dest).resolve() != Path(assembled_path).resolve():
                shutil.copy2(assembled_path, dest)
                log(f"saved dump to predefined folder | {dest}")
            saved_path = dest
        elif save_folder:
            saved_path = Path(save_folder) / Path(assembled_path).name
    except Exception as e:
        log(f"could not route to save_folder='{save_folder}': {e}")

    context = {
        "batch_id": batch_id, "assembled_path": str(saved_path),
        "save_folder": str(save_folder or ""), "subject": str(subject or ""),
        "sender_email": str(sender_email or ""), "dump_type": dump_type,
        "leads_dir": str(leads_dir), "final_package_name": Path(saved_path).name,
    }

    def _confirm(status, results, message=""):
        try:
            df.record_run(batch_id, dump_type, saved_path, status, results,
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

    # 2) RUN steps in sequence
    rp = run_python or (lambda b, d, t, s, a: _default_run_python(b, d, t, s, a, log))
    rb = run_bat or (lambda b, f, p: _default_run_bat(b, f, p, log))

    results = []
    for s in steps:
        name, kind, target = s["step_name"], s["kind"], s["target_path"]
        args, on_failure = s["args"], s.get("on_failure", "stop")

        if dry_run:
            results.append({"step": name, "status": "dry-run"})
            continue

        ok = rb(batch_id, context["final_package_name"], target) if kind == "bat" \
            else rp(batch_id, dump_type, name, target, args)
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
    return True, results
