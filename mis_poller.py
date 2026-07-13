# -*- coding: utf-8 -*-
r"""
The MIS process. Kept separate from the Outlook poller — different source,
different failure modes — but launched by the same supervisor (sarthi_service.py).

Three loops, one process:

  producer A : Neon report_requests -> claim -> mis_queue
  producer B : schedule ticker      -> mis_queue          (every pass)
  producer C : dump-complete        -> mis_queue          (from flow_engine, not here)
  worker     : mis_queue -> run_mis_flow -> email         (SINGLE THREAD)

The worker is deliberately single-threaded. Strict sequence is the requirement;
throughput is not the problem here.

    python mis_poller.py --once            one pass, then exit
    python mis_poller.py --watch           run forever
    python mis_poller.py --build daily_mis force one build now
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import mis_flows as mf
import mis_engine
import mis_triggers
import mis_mailer

try:
    import mis_neon
except Exception:                      # psycopg missing -> local-only still works
    mis_neon = None

POLL_SEC = 60
STALE_CLAIM_MIN = 120
DB_PATH = mf.DEFAULT_DB


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Producer A — Neon report_requests
# ---------------------------------------------------------------------------
def poll_requests(db_path: Path = DB_PATH) -> int:
    if mis_neon is None:
        return 0
    try:
        rows = mis_neon.fetch_requested()
    except Exception as e:
        log(f"neon poll failed: {e}")
        return 0

    n = 0
    for r in rows:
        req_id = r.get("req_id")
        if not req_id:
            continue
        try:
            if not mis_neon.claim_request(req_id):
                continue                    # another poller got it — correct, skip
        except Exception as e:
            log(f"claim {req_id} failed: {e}")
            continue

        mf.enqueue(r.get("report_key") or "", "request", f"req:{req_id}",
                   params=r.get("params") or "", req_id=req_id,
                   user_key=r.get("user_key"),
                   requester_email=r.get("requester_email"), db_path=db_path)
        n += 1
        log(f"queued request {req_id} -> {r.get('report_key')}")
    return n


# ---------------------------------------------------------------------------
# Producer B — schedule
# ---------------------------------------------------------------------------
def tick(db_path: Path = DB_PATH) -> int:
    try:
        fired = mis_triggers.tick_schedules(db_path=db_path)
    except Exception as e:
        log(f"schedule tick failed: {e}")
        return 0
    for k in fired:
        log(f"queued schedule -> {k}")
    return len(fired)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
def _recipients_for(item, report_key) -> dict:
    """request -> the requester. schedule/dump/manual -> Neon mis_report_access."""
    if item.get("trigger") == "request":
        em = (item.get("requester_email") or "").strip()
        if em and "@" in em:
            return {"emails": [em], "source": "requester", "error": None}
        return {"emails": [], "source": "requester",
                "error": "the request carries no requester_email"}
    if mis_neon is None:
        return {"emails": [], "source": "none", "error": "psycopg not installed"}
    return mis_neon.resolve_recipients(report_key)


def _mark_neon(item, status):
    """Only request-triggered items have a Neon row to update."""
    if mis_neon is None or not item.get("req_id"):
        return
    try:
        mis_neon.set_request_status(item["req_id"], status)
    except Exception as e:
        log(f"could not set neon status for {item['req_id']}: {e}")


def work_one(db_path: Path = DB_PATH) -> bool:
    item = mf.claim_next(db_path=db_path)
    if not item:
        return False

    qid, key, trig = int(item["id"]), item["report_key"], item["trigger"]
    log(f"building {key} ({trig})")

    try:
        res = mis_engine.run_mis_flow(
            key, trigger=trig, params=item.get("params") or "",
            req_id=item.get("req_id"), user_key=item.get("user_key"),
            requester_email=item.get("requester_email"), queue_id=qid,
            db_path=db_path, log=log)
    except Exception:
        msg = traceback.format_exc(limit=3)
        log(f"engine crashed on {key}:\n{msg}")
        mf.finish_queue_item(qid, "failed", msg, db_path=db_path)
        _mark_neon(item, "failed")
        return True

    if res["status"] != "done":
        log(f"FAILED {key}: {res['message']}")
        mf.finish_queue_item(qid, "failed", res["message"], db_path=db_path)
        _mark_neon(item, "failed")
        return True

    # ---- deliver ----
    rec = _recipients_for(item, key)
    if not rec["emails"]:
        msg = f"built but NOT sent — {rec.get('error') or 'no recipients'}"
        log(f"{key}: {msg}")
        mf.finish_mis_run(res["run_id"], "failed", res["steps"], res["output_path"],
                          msg, db_path=db_path)
        mf.finish_queue_item(qid, "failed", msg, db_path=db_path)
        _mark_neon(item, "failed")
        return True

    err = mis_mailer.send_report(
        rec["emails"],
        f"{res['report_name']} — {mf._today()}",
        mis_mailer.build_body(res["report_name"], trig, item.get("params") or "",
                              res["output_path"]),
        attachment=res["output_path"])

    if err:
        log(f"send failed for {key}: {err}")
        mf.finish_mis_run(res["run_id"], "failed", res["steps"], res["output_path"],
                          f"send failed: {err}", db_path=db_path)
        mf.finish_queue_item(qid, "failed", err, db_path=db_path)
        _mark_neon(item, "failed")
        return True

    note = f"sent to {', '.join(rec['emails'])} ({rec['source']})"
    mf.finish_mis_run(res["run_id"], "success", res["steps"], res["output_path"],
                      note, db_path=db_path)
    mf.finish_queue_item(qid, "done", note, db_path=db_path)
    _mark_neon(item, "done")
    log(f"done {key} -> {note}")
    return True


def drain(db_path: Path = DB_PATH) -> int:
    n = 0
    while work_one(db_path):
        n += 1
    return n


def one_pass(db_path: Path = DB_PATH) -> None:
    mf.init_db(db_path)
    released = mf.release_stale_claims(STALE_CLAIM_MIN, db_path=db_path)
    if released:
        log(f"released {released} stale claim(s)")
    poll_requests(db_path)
    tick(db_path)
    drain(db_path)


def main(argv) -> int:
    ap = argparse.ArgumentParser(description="Sarthi MIS poller")
    ap.add_argument("--once", action="store_true", help="one pass, then exit")
    ap.add_argument("--watch", action="store_true", help="run forever")
    ap.add_argument("--interval", type=int, default=POLL_SEC)
    ap.add_argument("--build", help="build one report now, then exit")
    ap.add_argument("--db", default=None, help="override the registry path")
    a = ap.parse_args(argv)

    db = Path(a.db) if a.db else DB_PATH
    mf.init_db(db)

    if a.build:
        mf.enqueue(a.build, "manual", f"manual:{a.build}:{mf._now()}", db_path=db)
        drain(db)
        return 0

    if a.once or not a.watch:
        one_pass(db)
        return 0

    log(f"MIS poller watching (every {a.interval}s) — db {db}")
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
