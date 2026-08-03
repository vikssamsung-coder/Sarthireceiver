"""Detached worker for an allow-listed Customer Final Evaluation job."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import client_intelligence_jobs as jobs
from customer_evaluation_adapter import build_commands


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _cancelled(job_id: int, db_path) -> bool:
    current = jobs.get_job(job_id, db_path)
    return bool(current and current["status"] == "cancel_requested")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--job-id", required=True, type=int)
    args = parser.parse_args()
    job = jobs.get_job(args.job_id, args.db)
    if not job:
        return 2
    if _cancelled(args.job_id, args.db):
        jobs.update_job(args.job_id, args.db, status="cancelled",
                        finished_at=_now(), message="Cancelled before execution.")
        return 0

    config = json.loads(job["config_json"])
    state_dir = Path(args.db).resolve().parent
    log_dir = state_dir / "client_intelligence_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"job_{args.job_id}.log"
    try:
        commands, cwd = build_commands(job["mode"], config)
        jobs.update_job(
            args.job_id, args.db, status="running", started_at=_now(),
            command_json=json.dumps(commands), log_path=str(log_path),
        )
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"Started {_now()}\nMode: {job['mode']}\n")
            for step, command in enumerate(commands, start=1):
                if _cancelled(args.job_id, args.db):
                    jobs.update_job(args.job_id, args.db, status="cancelled",
                                    finished_at=_now(), return_code=-1,
                                    message="Cancelled by user.")
                    return 0
                log.write(f"\nStep {step}/{len(commands)}: {json.dumps(command)}\n")
                log.flush()
                process = subprocess.Popen(
                    command, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=0x08000000 if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                jobs.update_job(args.job_id, args.db, pid=process.pid)
                while process.poll() is None:
                    if _cancelled(args.job_id, args.db):
                        if os.name == "nt":
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           capture_output=True, timeout=20)
                        else:
                            process.terminate()
                        jobs.update_job(args.job_id, args.db, status="cancelled",
                                        finished_at=_now(), return_code=-1,
                                        message="Cancelled by user.")
                        return 0
                    time.sleep(1)
                rc = int(process.returncode or 0)
                if rc:
                    jobs.update_job(args.job_id, args.db, status="failed",
                                    finished_at=_now(), return_code=rc,
                                    message=f"Step {step} exited with code {rc}. See the job log.")
                    return rc
        jobs.update_job(args.job_id, args.db, status="success", finished_at=_now(),
                        return_code=0, message="Completed successfully.")
        return 0
    except Exception as exc:
        log_path.write_text(f"Worker failed before launch:\n{exc}\n", encoding="utf-8")
        jobs.update_job(args.job_id, args.db, status="failed", finished_at=_now(),
                        return_code=1, message=str(exc), log_path=str(log_path))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

