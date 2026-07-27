"""Detached worker for one approved Client Intelligence job."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import client_intelligence_jobs as jobs
from evaluator_adapter import build_command


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--job-id", required=True, type=int)
    args = parser.parse_args()

    job = jobs.get_job(args.job_id, args.db)
    if not job:
        return 2
    if job["status"] == "cancel_requested":
        jobs.update_job(
            args.job_id, args.db, status="cancelled", finished_at=_now(),
            message="Cancelled before execution.",
        )
        return 0

    config = json.loads(job["config_json"])
    state_dir = Path(args.db).resolve().parent
    log_dir = state_dir / "client_intelligence_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"job_{args.job_id}.log"

    try:
        command, cwd = build_command(job["mode"], config)
        jobs.update_job(
            args.job_id, args.db, status="running", started_at=_now(),
            command_json=json.dumps(command), log_path=str(log_path),
        )
        with log_path.open("a", encoding="utf-8", errors="replace") as log:
            log.write(f"Started {_now()}\nMode: {job['mode']}\n")
            log.write("Command: " + json.dumps(command) + "\n\n")
            process = subprocess.Popen(
                command, cwd=str(cwd), stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=0x08000000 if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            jobs.update_job(args.job_id, args.db, pid=process.pid)
            while process.poll() is None:
                current = jobs.get_job(args.job_id, args.db)
                if current and current["status"] == "cancel_requested":
                    try:
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                capture_output=True, timeout=20,
                            )
                        else:
                            process.terminate()
                    finally:
                        jobs.update_job(
                            args.job_id, args.db, status="cancelled",
                            finished_at=_now(), return_code=-1,
                            message="Cancelled by user.",
                        )
                    return 0
                time.sleep(1)
            rc = int(process.returncode or 0)
        jobs.update_job(
            args.job_id, args.db,
            status="success" if rc == 0 else "failed",
            finished_at=_now(), return_code=rc,
            message="Completed successfully." if rc == 0
            else f"Evaluator exited with code {rc}. See the job log.",
        )
        return rc
    except Exception as exc:
        log_path.write_text(f"Worker failed before launch:\n{exc}\n", encoding="utf-8")
        jobs.update_job(
            args.job_id, args.db, status="failed", finished_at=_now(),
            return_code=1, message=str(exc), log_path=str(log_path),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

