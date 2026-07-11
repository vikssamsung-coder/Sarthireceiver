# -*- coding: utf-8 -*-
r"""
Wire the Dump Processor into your existing email_processor.py.

One import + two function swaps (+ pass the sender at the call site). Every step
still runs through your run_python_script / run_bat, so trigger-dedup, SHA and
logging are unchanged.

==============================================================================
STEP 1 — near the top of email_processor.py:

    import dump_flows, flow_engine
    from pathlib import Path
    DUMP_FLOWS_DB = Path(r"D:\Sarthi\multipart_buffer\dump_flows.sqlite3")
    dump_flows.seed_defaults(DUMP_FLOWS_DB)   # loads current flows if empty

STEP 1b (recommended) — in parse_email_metadata, add to the key list so a
stamped label routes exactly (it already parses report_key):

        "dump_type_key",
        "dump_type_handler",

==============================================================================
STEP 2 — rename your detect_dump_type -> _detect_dump_type_hardcoded (keep as
fallback). Paste this. NOTE the new `sender` argument — recognition can match on
who sent the email, so pass the effective sender (see STEP 2b).
"""


def detect_dump_type(subject, body, final_package_name=None, attachment_names=None,
                     meta=None, sender=None):
    import dump_flows  # noqa
    try:
        dt = dump_flows.resolve(
            subject=subject or "", body=body or "", sender=sender or "",
            attachments=attachment_names or [], meta=meta or {},
            final_package_name=final_package_name, db_path=DUMP_FLOWS_DB,  # noqa: F821
        )
        if dt and dt != "unknown":
            return dt
    except Exception as e:
        logger.warning(f"dump_flows.resolve failed, using hardcoded fallback: {e}")  # noqa: F821
    return _detect_dump_type_hardcoded(  # noqa: F821
        subject, body, final_package_name, attachment_names, meta)


r"""
STEP 2b — at the call site inside process_mail, pass the sender you already
computed. Change:

        dump_type = detect_dump_type(
            subject=subject, body=body,
            final_package_name=meta.get("final_package_name"),
            attachment_names=attachment_names, meta=meta,
        )
to:
        dump_type = detect_dump_type(
            subject=subject, body=body,
            final_package_name=meta.get("final_package_name"),
            attachment_names=attachment_names, meta=meta,
            sender=effective_sender,          # <-- add this
        )

==============================================================================
STEP 3 — replace run_preprocessing_for_dump's body with this hand-off. Returns
True/False exactly as before (dashboard-BAT gate unaffected).
"""


def run_preprocessing_for_dump(batch_id, dump_type, assembled_path, subject=None, sender_email=None):
    import flow_engine  # noqa
    ok, _ = flow_engine.run_dump_flow(
        batch_id=batch_id, dump_type=dump_type, assembled_path=assembled_path,
        subject=subject, sender_email=sender_email,
        run_python=run_python_script,   # noqa: F821  your existing runner
        run_bat=run_bat,                # noqa: F821  your existing runner
        leads_dir=str(LEADS_OUTPUT_DIR),  # noqa: F821
        db_path=DUMP_FLOWS_DB, log=logger.info,  # noqa: F821
    )
    return ok
