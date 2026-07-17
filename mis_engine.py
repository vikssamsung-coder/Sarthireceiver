# -*- coding: utf-8 -*-
r"""
MIS engine — runs ONE report build end to end:
  1) look up the report; unknown key -> fail LOUDLY, never silently drop
  2) build the context (the analogue of flow_engine's context dict)
  3) RUN the configured steps in sequence (stop/continue on failure)
  4) capture the OUTPUT file the build produced
  5) RECORD the confirmation (mis_runs)

Mirrors flow_engine.run_dump_flow. Trigger-agnostic: it does not know or care
whether the build came from a PMD request, the clock, or a dump landing.

COM-FREE. Emailing happens in mis_mailer, called by mis_poller — the same way
handle_email keeps Outlook out of the testable core.

STRICT SEQUENCE — the guarantee:
  * steps run in step_order, ONE subprocess at a time, never overlapped
  * a failing step with on_failure='stop' (the default) aborts the run, so a
    later step never sees a half-built input
  * concurrency ACROSS runs is prevented upstream by mis_flows.claim_next()

THE OUTPUT CONTRACT — the one genuinely new thing the MIS Builder introduces:

    A build step MUST print, on stdout:
        OUTPUT=<absolute path>

    The engine takes the LAST such line printed by any step in the run.
    No OUTPUT= line -> the run FAILS.

Deliberately NOT "newest file in out_folder": build scripts write temp and
intermediate files, and that heuristic silently mails the wrong one.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import dump_flows as df
import mis_flows as mf

STEP_TIMEOUT_SEC = int(os.environ.get("SARTHI_MIS_STEP_TIMEOUT", "1800"))  # 30 min
OUTPUT_PREFIX = "OUTPUT="

# Bump when the engine's run contract changes. Printed by mis_poller on start and
# checkable with:  python -c "import mis_engine; print(mis_engine.ENGINE_VERSION)"
# If the box shows an older number than the zip, the update did not land / the
# poller was not restarted.
ENGINE_VERSION = "2026-07-17.steptimeout"


def _run_step(kind, target, args, working_dir, log, timeout_sec=0):
    """Own subprocess runner (not flow_engine's) because the OUTPUT= contract
    needs stdout back. Returns (ok, stdout, message).

    timeout_sec: per-step limit. 0 (or unset) falls back to the global
    STEP_TIMEOUT_SEC. A heavy classifier can be given a big number while quick
    steps keep a tight one.

    The child's stdio is forced to UTF-8. On Windows the console codepage is
    cp1252, so a build script that prints emoji (✅ ❌) or any non-Latin-1 text
    would otherwise die with UnicodeEncodeError — a crash in a print statement,
    not in the report logic. PYTHONIOENCODING + PYTHONUTF8 make every child
    emoji-safe without editing a single build script."""
    import os as _os
    target = str(target or "")
    if not target or not Path(target).is_file():
        return False, "", f"target not found: {target}"

    limit = int(timeout_sec) if int(timeout_sec or 0) > 0 else STEP_TIMEOUT_SEC

    cmd = [str(target)] + list(args or []) if kind == "bat" \
        else [sys.executable, str(target)] + list(args or [])
    wd = working_dir or str(Path(target).parent) or None
    log(f"run {kind} (limit {limit}s): {' '.join(str(x) for x in cmd)}")

    child_env = dict(_os.environ)
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"

    try:
        p = subprocess.run(cmd, cwd=wd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           env=child_env, timeout=limit,
                           shell=(kind == "bat"))
    except subprocess.TimeoutExpired:
        return False, "", (f"timed out after {limit}s — raise this step's timeout "
                           f"if the script legitimately needs longer, or check it "
                           f"isn't hung")
    except Exception as e:
        return False, "", f"launch failed: {e}"

    out = p.stdout or ""
    if p.returncode != 0:
        tail = (p.stderr or out or "").strip().splitlines()[-6:]
        log(f"  exit {p.returncode}: {' | '.join(tail)}")
        return False, out, f"exit {p.returncode}: {' | '.join(tail)}"
    return True, out, ""


def _scan_output(stdout: str) -> str:
    """Last OUTPUT=<path> line wins."""
    found = ""
    for line in (stdout or "").splitlines():
        s = line.strip()
        if s.startswith(OUTPUT_PREFIX):
            cand = s[len(OUTPUT_PREFIX):].strip().strip('"')
            if cand:
                found = cand
    return found


def run_mis_flow(report_key, trigger="manual", params="", req_id=None,
                 user_key=None, requester_email=None, queue_id=None,
                 *, db_path: Path = mf.DEFAULT_DB, log=print, dry_run=False) -> dict:
    """Run one report. Never raises on a bad config — records a FAILED run and
    says why.

    Returns:
      {"status": "done"|"failed", "output_path": str, "run_id": int,
       "report_key": str, "report_name": str, "requester_email": str|None,
       "steps": [...], "message": str}
    """
    t = mf.get_mis_type(report_key, db_path=db_path)

    if not t:
        run_id = mf.start_mis_run(report_key, trigger, queue_id, req_id,
                                  requester_email, db_path=db_path)
        msg = f"unknown report '{report_key}' — add handler"
        log(msg)
        mf.finish_mis_run(run_id, "failed", [], "", msg, db_path=db_path)
        return {"status": "failed", "output_path": "", "run_id": run_id,
                "report_key": report_key, "report_name": report_key,
                "requester_email": requester_email, "steps": [], "message": msg}

    run_id = mf.start_mis_run(report_key, trigger, queue_id, req_id,
                              requester_email, db_path=db_path)
    name = t.get("name") or report_key

    def _fail(msg, steps=None, out=""):
        log(msg)
        mf.finish_mis_run(run_id, "failed", steps or [], out, msg, db_path=db_path)
        return {"status": "failed", "output_path": out, "run_id": run_id,
                "report_key": report_key, "report_name": name,
                "requester_email": requester_email, "steps": steps or [],
                "message": msg}

    def _fail_phase(phase, msg):
        """A failure BEFORE any step ran (config / setup / output-contract).
        Records a single phase marker so the history screen shows where it broke
        rather than an empty step list."""
        return _fail(msg, [{"n": 0, "step": phase, "phase": phase,
                            "status": "failed", "error": msg}])

    if not int(t.get("enabled") or 0):
        return _fail_phase("config", f"report '{report_key}' is disabled")

    out_folder = t.get("out_folder") or ""
    if out_folder:
        try:
            Path(out_folder).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return _fail_phase("setup", f"cannot create out_folder '{out_folder}': {e}")

    context = {
        "req_id": str(req_id or ""),
        "report_key": report_key,
        "report_name": name,
        "params": str(params or ""),
        "requester_email": str(requester_email or ""),
        "user_key": str(user_key or ""),
        "out_folder": str(out_folder),
        "trigger": trigger,
        "run_id": str(run_id),
        "today": mf._today(),
        "output_path": "",          # filled in as steps produce it
    }

    try:
        steps = mf.build_mis_steps(report_key, context, db_path=db_path)
    except Exception as e:
        return _fail_phase("load", f"failed to load flow for '{report_key}': {e}")

    if not steps:
        return _fail_phase("config", f"no steps configured for '{report_key}'")

    results, output_path = [], ""

    # ---- STRICT SEQUENCE: one at a time, in order, stop on failure ----------
    for idx, s in enumerate(steps, 1):
        sname = s["step_name"]
        if dry_run:
            results.append({"n": idx, "step": sname, "phase": "run",
                            "status": "dry-run"})
            continue

        ok, stdout, err = _run_step(s["kind"], s["target_path"], s["args"],
                                    s["working_dir"], log,
                                    timeout_sec=s.get("timeout_sec", 0))
        entry = {"n": idx, "step": sname, "phase": "run",
                 "status": "ok" if ok else "failed"}
        if err:
            # Full reason, kept per-step so the history screen can show exactly
            # which step broke and why — not just a truncated summary line.
            entry["error"] = err
        if stdout and stdout.strip():
            # last few stdout lines help even on success (row counts etc.)
            entry["tail"] = " | ".join(stdout.strip().splitlines()[-4:])[:600]

        found = _scan_output(stdout)
        if found:
            output_path = found
            context["output_path"] = found   # a later step can consume it
            entry["output"] = found

        results.append(entry)

        if not ok and (s.get("on_failure") or "stop") == "stop":
            return _fail(f"step {idx} '{sname}' failed → {err}", results, output_path)
        if not ok:
            log(f"step {idx} '{sname}' failed but on_failure=continue.")

    if dry_run:
        mf.finish_mis_run(run_id, "dry-run", results, "", "", db_path=db_path)
        return {"status": "done", "output_path": "", "run_id": run_id,
                "report_key": report_key, "report_name": name,
                "requester_email": requester_email, "steps": results, "message": ""}

    # ---- OUTPUT contract ---------------------------------------------------
    if not output_path:
        # Every step ran but none emitted the contract line. Name the last step
        # so the fix (add OUTPUT= there) is obvious.
        last = steps[-1]["step_name"] if steps else "?"
        return _fail(f"all {len(steps)} step(s) ran but none printed "
                     f"'OUTPUT=<abs path>'. Add it to the last step ('{last}') "
                     f"after it saves the file.", results)

    if not Path(output_path).is_file():
        return _fail(f"OUTPUT path does not exist on disk: {output_path}",
                     results, output_path)

    mf.finish_mis_run(run_id, "built", results, output_path, "", db_path=db_path)
    return {"status": "done", "output_path": output_path, "run_id": run_id,
            "report_key": report_key, "report_name": name,
            "requester_email": requester_email, "steps": results, "message": ""}
