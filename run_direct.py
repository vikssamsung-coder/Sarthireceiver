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

Exit codes: 0 ok, 1 a step failed, 2 no dump type matched.
Must sit next to dump_flows.py / flow_engine.py / extractor.py (the app folder).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import dump_flows as df
import flow_engine


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="attachment the watcher saved")
    ap.add_argument("--subject", default="")
    ap.add_argument("--sender", default="", help="SMTP address of the sender")
    ap.add_argument("--body", default="")
    ap.add_argument("--dump-type", default="", help="skip recognition and use this type")
    ap.add_argument("--batch-id", default="")
    ap.add_argument("--db", default=str(df.DEFAULT_DB))
    args = ap.parse_args()

    db = Path(args.db)
    src = Path(args.file)
    if not src.exists():
        print(f"FILE NOT FOUND: {src}")
        return 2

    dt = args.dump_type.strip() or df.resolve(
        subject=args.subject, body=args.body, sender=args.sender,
        attachments=[src.name], db_path=db)

    if not dt or dt == "unknown":
        print(f"UNRESOLVED: no dump type matched (sender={args.sender!r}, "
              f"subject={args.subject!r}). Check the identifier rules in the app.")
        return 2

    batch = args.batch_id.strip() or src.stem
    ok, results = flow_engine.run_dump_flow(
        batch_id=batch, dump_type=dt, assembled_path=str(src),
        subject=args.subject, sender_email=args.sender, db_path=db, log=print)
    print(f"dump_type={dt} ok={ok} steps={results}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
