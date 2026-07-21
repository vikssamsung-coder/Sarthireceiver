# -*- coding: utf-8 -*-
r"""
Sarthi Receiver — standalone.

Reads an Outlook inbox itself, and for each new mail:
  recognize the dump type (registry rules)  ->  save the attachment
  ->  extract (zip unzips; csv/xlsx as-is) into the type's folder
  ->  run the type's steps in order  ->  record the result.

Self-contained: this IS the email processor + extractor + flow. It doesn't
depend on the old email_processor.py (which can keep running in parallel).

The Outlook parts use pywin32 and only run on the Windows box. The processing
core (handle_email) has no COM dependency, so it's testable anywhere.

Run:
    python sarthi_receiver.py --once                 # one pass (use with Task Scheduler)
    python sarthi_receiver.py --watch --interval 60  # keep polling
Options:
    --mailbox "growth@bigul.co"   inbox to read (default below)
    --folder  "Inbox"
    --scan    50                  how many recent mails to check per pass
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path

import dump_flows as df
import flow_engine

# ---- defaults (override on the command line) ------------------------------
DEFAULT_MAILBOX = "growth@bigul.co"
DEFAULT_FOLDER = "Inbox"
SEEN_DB = Path(r"D:\Sarthi\multipart_buffer\receiver_seen.sqlite3")


# ---- de-dup: remember which mails we've already handled --------------------
def _seen_conn(seen_path: Path):
    Path(seen_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(seen_path))
    c.execute("CREATE TABLE IF NOT EXISTS seen_emails("
              "entry_id TEXT PRIMARY KEY, dump_type TEXT, subject TEXT, seen_at TEXT)")
    return c


def is_seen(seen_path, entry_id) -> bool:
    with _seen_conn(seen_path) as c:
        return c.execute("SELECT 1 FROM seen_emails WHERE entry_id=?", (entry_id,)).fetchone() is not None


def mark_seen(seen_path, entry_id, dump_type, subject):
    with _seen_conn(seen_path) as c:
        c.execute("INSERT OR REPLACE INTO seen_emails(entry_id,dump_type,subject,seen_at) VALUES(?,?,?,?)",
                  (entry_id, dump_type, subject, datetime.now().isoformat(timespec="seconds")))


# ---- processing core (no COM — testable) ----------------------------------
def handle_email(entry_id, subject, body, sender, saved_paths, *,
                 db_path=df.DEFAULT_DB, seen_path=SEEN_DB, log=print):
    """Given an email's fields and its already-saved attachment paths, resolve the
    dump type and run its flow. Returns the dump_type key (or 'unknown'/'skip')."""
    if is_seen(seen_path, entry_id):
        return "skip"

    atts = [Path(p).name for p in saved_paths]
    dt = df.resolve(subject=subject, body=body, sender=sender,
                    attachments=atts, db_path=db_path)

    if not dt or dt == "unknown":
        mark_seen(seen_path, entry_id, "unknown", subject)
        log(f"unrecognized — skipped: {subject!r} from {sender!r}")
        return "unknown"

    if not saved_paths:
        mark_seen(seen_path, entry_id, dt, subject)
        log(f"{dt}: no attachment on {subject!r} — nothing to run")
        return dt

    primary = str(saved_paths[0])   # extractor handles zip/csv/xlsx from here
    batch = f"{dt}_{hashlib.sha1(entry_id.encode()).hexdigest()[:10]}"
    ok, results = flow_engine.run_dump_flow(
        batch_id=batch, dump_type=dt, assembled_path=primary,
        subject=subject, sender_email=sender, db_path=db_path, log=log)
    mark_seen(seen_path, entry_id, dt, subject)
    log(f"{dt}: {'ok' if ok else 'failed'} — {results}")
    return dt


# ---- Outlook reading (Windows only) ---------------------------------------
def _smtp(mail) -> str:
    try:
        if getattr(mail, "SenderEmailType", "") == "EX":
            try:
                addr = mail.Sender.GetExchangeUser().PrimarySmtpAddress
                if addr:
                    return addr.lower()
            except Exception:
                pass
            try:
                addr = mail.PropertyAccessor.GetProperty(
                    "http://schemas.microsoft.com/mapi/proptag/0x39FE001E")  # PR_SMTP_ADDRESS
                if addr:
                    return addr.lower()
            except Exception:
                pass
        return (mail.SenderEmailAddress or "").lower()
    except Exception:
        return ""


def _get_folder(ns, mailbox, folder):
    if mailbox:
        return ns.Folders.Item(mailbox).Folders.Item(folder)
    return ns.GetDefaultFolder(6)  # olFolderInbox


def process_once(mailbox, folder, scan=50, db_path=df.DEFAULT_DB, seen_path=SEEN_DB, log=print) -> int:
    # Outlook is a single-instance COM server. The MIS mailer is a SECOND process
    # that also reaches for it; overlapping access throws 0x80080005 for both.
    # outlook_com serializes the two (cross-process lock) and gives each a clean
    # COM apartment. Everything Outlook-touching stays inside this 'with'.
    import outlook_com
    with outlook_com.outlook_namespace("receiver") as ns:
        inbox = _get_folder(ns, mailbox, folder)
        items = inbox.Items
        items.Sort("[ReceivedTime]", True)   # newest first
        return _scan_items(items, scan, db_path, seen_path, log)


def _scan_items(items, scan, db_path, seen_path, log) -> int:
    handled = 0
    n = min(scan, items.Count)
    for i in range(1, n + 1):
        try:
            mail = items.Item(i)
            if getattr(mail, "Class", 0) != 43:   # olMail
                continue
            entry_id = mail.EntryID
            if is_seen(seen_path, entry_id):
                continue

            subject = mail.Subject or ""
            body = mail.Body or ""
            sender = _smtp(mail)

            saved = []
            if mail.Attachments.Count > 0:
                tmp = Path(tempfile.mkdtemp(prefix="sarthi_"))
                for a in mail.Attachments:
                    p = tmp / a.FileName
                    a.SaveAsFile(str(p))
                    saved.append(p)

            res = handle_email(entry_id, subject, body, sender, saved,
                               db_path=db_path, seen_path=seen_path, log=log)
            if res not in ("skip", "unknown"):
                handled += 1
        except Exception as e:
            log(f"error on item {i}: {e}")
    return handled


def run(once, interval, mailbox, folder, scan, db_path, seen_path, log=print):
    while True:
        try:
            n = process_once(mailbox, folder, scan, db_path, seen_path, log)
            log(f"[{datetime.now():%H:%M:%S}] pass complete — handled {n}")
        except Exception as e:
            log(f"pass error: {e}")
        if once:
            break
        time.sleep(max(5, interval))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mailbox", default=DEFAULT_MAILBOX)
    ap.add_argument("--folder", default=DEFAULT_FOLDER)
    ap.add_argument("--scan", type=int, default=50)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--db", default=str(df.DEFAULT_DB))
    ap.add_argument("--seen", default=str(SEEN_DB))
    args = ap.parse_args()

    once = args.once or not args.watch
    df.init_db(Path(args.db))
    run(once, args.interval, args.mailbox, args.folder, args.scan,
        Path(args.db), Path(args.seen), log=print)


if __name__ == "__main__":
    main()
