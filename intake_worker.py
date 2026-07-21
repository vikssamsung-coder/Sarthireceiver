# -*- coding: utf-8 -*-
r"""
intake_worker.py — drains intake_queue and processes each job through the SAME
core the poller used (df.resolve -> flow_engine.run_dump_flow).

COM-FREE. It never touches Outlook — the VBA already saved the attachment and
handed us a file path. So this worker cannot collide with MIS or Outlook. That
is the whole point of the queue architecture.

    python intake_worker.py --once
    python intake_worker.py --watch
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import dump_flows as df
import flow_engine
import intake_queue as iq

DB_PATH = df.DEFAULT_DB
STALE_MIN = 30


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def process_job(job, db_path: Path = DB_PATH) -> None:
    jid = int(job["id"])
    raw = (job.get("file_path") or "").strip()
    has_file = bool(raw)
    src = Path(raw) if has_file else None

    if has_file and not src.exists():
        msg = f"file missing: {src}"
        log(f"job {jid} FAILED — {msg}")
        iq.finish(jid, "failed", msg, db_path=db_path)
        return

    dt = (job.get("dump_type") or "").strip() or df.resolve(
        subject=job.get("subject") or "", body=job.get("body") or "",
        sender=job.get("sender") or "", attachments=[src.name] if src else [],
        db_path=db_path)

    if not dt or dt == "unknown":
        msg = (f"no dump type matched (sender={job.get('sender')!r}, "
               f"subject={job.get('subject')!r}) — check the feed's rules")
        log(f"job {jid} UNRESOLVED — {msg}")
        iq.finish(jid, "failed", msg, db_path=db_path)
        return

    batch = f"intake_{jid}_" + (src.stem if src else "nofile")
    try:
        # run_dump_flow extracts the file into the type's save_folder (if there
        # is one), then runs the steps. A no-file trigger passes "" and the flow
        # just runs its steps.
        ok, results = flow_engine.run_dump_flow(
            batch_id=batch, dump_type=dt,
            assembled_path=str(src) if src else "",
            subject=job.get("subject") or "", sender_email=job.get("sender") or "",
            db_path=db_path, log=log)
    except Exception:
        msg = traceback.format_exc(limit=3)
        log(f"job {jid} CRASHED:\n{msg}")
        iq.finish(jid, "failed", msg, db_path=db_path)
        return

    if ok:
        iq.finish(jid, "done", f"dump_type={dt}", db_path=db_path)
        log(f"job {jid} done -> {dt}")
    else:
        # find the failing step for a useful message
        failed = [r for r in (results or []) if r.get("status") == "failed"]
        detail = failed[0].get("step") if failed else "flow failed"
        iq.finish(jid, "failed", f"{dt}: {detail}", db_path=db_path)
        log(f"job {jid} FAILED -> {dt}: {detail}")


def drain(db_path: Path = DB_PATH) -> int:
    n = 0
    while True:
        job = iq.claim_next(db_path=db_path)
        if not job:
            break
        process_job(job, db_path=db_path)
        n += 1
    return n


def one_pass(db_path: Path = DB_PATH) -> None:
    iq.init_db(db_path)
    requeued = iq.release_stale(STALE_MIN, db_path=db_path)
    if requeued:
        log(f"requeued {requeued} stale intake job(s)")
    drain(db_path)


def main(argv):
    ap = argparse.ArgumentParser(description="Sarthi intake worker")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--db", default=None)
    a = ap.parse_args(argv)

    db = Path(a.db) if a.db else DB_PATH
    iq.init_db(db)

    if a.once or not a.watch:
        one_pass(db)
        return 0

    log(f"intake worker watching (every {a.interval}s) — db {db}")
    while True:
        try:
            one_pass(db)
        except KeyboardInterrupt:
            log("stopped")
            return 0
        except Exception:
            log("pass failed:\n" + traceback.format_exc(limit=3))
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
