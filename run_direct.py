# -*- coding: utf-8 -*-
r"""
Entry point for DIRECT (non-PMD) emails caught by the Outlook VBA watchers.

The watcher saves the attachment and calls this with the file + who/what:

    python run_direct.py --file "D:\Sarthi\Incoming\cube_2291.zip" ^
                         --subject "Daily Call Log" ^
                         --sender "ops@cube.co"

It figures out the dump type from the identifier rules you set in the app
(recognition on sender/subject/body), then runs that type's flow:
save+extract into its folder -> run the steps in order -> record the result
(which then shows in the app's Run history).

You can skip recognition by naming the type explicitly (most reliable for a
bespoke watcher that already knows what it caught):

    python run_direct.py --file "..." --dump-type cube_calllog

Exit codes: 0 queued/processed, 2 duplicate skipped, 3 invalid input/job,
4 no dump type matched, 5 a processing step failed.
Must sit next to dump_flows.py / flow_engine.py / extractor.py (the app folder).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import dump_flows as df
import flow_engine


def _result(status: str, code: int, *, cleanup: Path | None = None, **details) -> int:
    if cleanup is not None and code in (0, 2):
        try:
            cleanup.unlink()
        except OSError:
            pass
    print(json.dumps({"status": status, **details}, ensure_ascii=True))
    return code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="", help="attachment the watcher saved")
    ap.add_argument("--job-file", default="",
                    help="UTF-8 JSON job; avoids passing mail metadata through cmd.exe")
    ap.add_argument("--subject", default="")
    ap.add_argument("--sender", default="", help="SMTP address of the sender")
    ap.add_argument("--body", default="")
    ap.add_argument("--dump-type", default="", help="skip recognition and use this type")
    ap.add_argument("--batch-id", default="")
    ap.add_argument("--entry-id", default="", help="Outlook EntryID, for dedupe")
    ap.add_argument("--enqueue", action="store_true",
                    help="just drop the job on intake_queue and return "
                         "(the app's worker processes it) — this is what the VBA calls")
    ap.add_argument("--db", default=str(df.DEFAULT_DB))
    args = ap.parse_args()
    cleanup_job = None

    if args.job_file:
        job_path = Path(args.job_file)
        try:
            job = json.loads(job_path.read_text(encoding="utf-8-sig"))
            if not isinstance(job, dict):
                raise ValueError("job JSON must be an object")
        except Exception as exc:
            return _result("invalid_job", 3, message=str(exc), job_file=str(job_path))
        args.file = str(job.get("file") or "(none)")
        args.subject = str(job.get("subject") or "")
        args.sender = str(job.get("sender") or "")
        args.body = str(job.get("body") or "")
        args.dump_type = str(job.get("dump_type") or "")
        args.batch_id = str(job.get("batch_id") or "")
        args.entry_id = str(job.get("entry_id") or "")
        args.enqueue = bool(job.get("enqueue", True))
        if job.get("delete_after_read"):
            cleanup_job = job_path

    if not args.file:
        return _result("invalid_input", 3, cleanup=cleanup_job,
                       message="--file or --job-file is required")

    db = Path(args.db)
    no_file = args.file.strip() in ("(none)", "", "none")
    src = Path(args.file)
    if not no_file and not src.exists():
        return _result("invalid_input", 3, cleanup=cleanup_job,
                       message=f"file not found: {src}")

    # ---- enqueue mode: the queue architecture --------------------------------
    # The VBA watcher calls this. It returns immediately so Outlook is never
    # blocked on processing; intake_worker (in the app) drains the queue.
    if args.enqueue:
        import intake_queue as iq
        jid = iq.enqueue(file_path=("" if no_file else str(src)), subject=args.subject,
                         sender=args.sender, body=args.body,
                         dump_type=args.dump_type, entry_id=args.entry_id,
                         source="vba", db_path=db)
        if jid is None:
            return _result("duplicate", 2, cleanup=cleanup_job,
                           entry_id=args.entry_id)
        else:
            return _result("queued", 0, cleanup=cleanup_job, job_id=jid,
                           file="(no file)" if no_file else src.name,
                           dump_type=args.dump_type)

    # ---- inline mode: process now (fallback / manual use) --------------------
    dt = args.dump_type.strip() or df.resolve(
        subject=args.subject, body=args.body, sender=args.sender,
        attachments=[src.name] if not no_file else [], db_path=db)

    if not dt or dt == "unknown":
        return _result("unresolved", 4, cleanup=cleanup_job,
                       sender=args.sender, subject=args.subject,
                       message="check the identifier rules in the app")

    batch = args.batch_id.strip() or (src.stem if not no_file else f"manual_{dt}")
    ok, results = flow_engine.run_dump_flow(
        batch_id=batch, dump_type=dt, assembled_path="" if no_file else str(src),
        subject=args.subject, sender_email=args.sender, db_path=db, log=print)
    return _result("processed" if ok else "failed", 0 if ok else 5,
                   cleanup=cleanup_job,
                   dump_type=dt, ok=ok, steps=results)


if __name__ == "__main__":
    sys.exit(main())
