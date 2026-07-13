# -*- coding: utf-8 -*-
r"""
The ONLY place in the MIS Builder that touches Outlook COM.

Keeps mis_engine.run_mis_flow testable on any machine — the same discipline that
keeps handle_email COM-free in sarthi_receiver.py.

pywin32 is already a dependency of the receiver; no new mail library.
"""
from __future__ import annotations

import os
from pathlib import Path

MAX_ATTACH_MB = int(os.environ.get("SARTHI_MAX_ATTACH_MB", "20"))


def send_report(to, subject, body, attachment=None) -> str | None:
    """Returns None on success, an error string on failure."""
    to = [t.strip() for t in (to or []) if t and "@" in t]
    if not to:
        return "no recipients"

    if attachment:
        p = Path(attachment)
        if not p.is_file():
            return f"attachment missing: {attachment}"
        mb = p.stat().st_size / (1024.0 * 1024.0)
        if mb > MAX_ATTACH_MB:
            body += (f"\n\nThe report is {mb:.1f} MB, over the {MAX_ATTACH_MB} MB mail "
                     f"limit, so it is not attached. It is on the Sarthi box at:\n{p}")
            attachment = None

    try:
        import win32com.client
    except ImportError:
        return "pywin32 not available — cannot send"

    try:
        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        mail.To = "; ".join(to)
        mail.Subject = subject
        mail.Body = body
        if attachment:
            mail.Attachments.Add(str(Path(attachment).resolve()))
        mail.Send()
        return None
    except Exception as e:
        return f"Outlook send failed: {e}"


def build_body(report_name, trigger, params="", output_path="") -> str:
    reason = {"request": "You requested this from Plan My Day.",
              "schedule": "Scheduled run.",
              "dump": "Triggered automatically once the source dumps landed.",
              "manual": "Built manually from the Sarthi app."}
    lines = [f"{report_name} is attached.", "",
             reason.get(trigger, f"Trigger: {trigger}")]
    if params:
        lines.append(f"Parameters: {params}")
    if output_path:
        lines.append(f"File: {Path(output_path).name}")
    lines += ["", "-- Sarthi"]
    return "\n".join(lines)
