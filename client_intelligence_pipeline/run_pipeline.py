#!/usr/bin/env python3
"""Bigul Sarthi Client Intelligence ingestion, extraction, and ledger pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from phase2_intelligence import Phase2Counts, ensure_schema, refresh_action_controls, run_phase2


DEFAULT_ROOT = Path(r"D:\Customer Final Evaluation")
SUPPORTED = {".xlsx", ".xlsm", ".csv"}

SYSTEM_COLUMNS = [
    "Call_Unique_ID", "Call_Version_ID", "Version_Number", "Processing_Run_ID",
    "Source_File_Name", "Source_File_Date", "Source_Row_Number", "Source_Row_Hash",
    "Analysis_Content_Hash", "First_Received_At", "Last_Received_At", "Processed_At",
    "Processing_Status", "Duplicate_Count", "Is_Latest_Version", "Previous_Version_ID",
    "Client_Match_Status", "Matched_Client_Code",
]

CANONICAL_ALIASES = {
    "Workspace": ["Workspace"],
    "Conversation Timestamp": ["Conversation Timestamp", "Call Timestamp", "Conversation Date Time"],
    "Customer Name": ["Customer Name", "Lead Name"],
    "Customer Email": ["Customer Email"],
    "Customer Phone": ["Customer Phone", "Phone", "Mobile Number"],
    "Agent Name": ["Agent Name"],
    "Agent Email": ["Agent Email"],
    "AgentID": ["AgentID", "Agent ID"],
    "Duration": ["Duration", "Call Duration"],
    "AI Disposition Status": ["AI Disposition Status"],
    "CRM Status": ["CRM Status"],
    "Fatal Call": ["Fatal Call"],
    "% Score": ["% Score", "Score Percentage", "Score"],
    "Intent": ["Intent"],
    "Feedback Accepted At": ["Feedback Accepted At"],
    "Default NA": ["Default NA"],
    "Default NA Justification": ["Default NA Justification"],
    "Owner": ["Owner"],
    "Created On": ["Created On"],
    "Lead Stage": ["Lead Stage"],
    "Lead Number": ["Lead Number", "Lead No", "Lead Code", "Prospect ID"],
    "Lead Source": ["Lead Source"],
    "Lead Assigned On": ["Lead Assigned On", "Lead Assigned on"],
    "Connected Disposition": ["Connected Disposition"],
    "AppsFlyer Campaign": ["AppsFlyer Campaign", "Appsflyer Campaign"],
    "Source Campaign": ["Source Campaign"],
    "Lead Name": ["Lead Name"],
    "Last Traded Date": ["Last Traded Date"],
    "Ready To Trade Date": ["Ready To Trade Date"],
    "Owner Name": ["Owner Name"],
    "FTM Payin": ["FTM Payin", "FTM Pay-in"],
    "Conversation Recording Link": ["Conversation Recording Link", "Recording Link"],
    "Summary": ["Summary", "Call Summary"],
}

ANALYSIS_FIELDS = [
    "AI Disposition Status", "CRM Status", "Fatal Call", "% Score", "Intent",
    "Lead Stage", "Connected Disposition", "Summary", "Disposition", "Follow Up",
    "Client'S Story", "Closing&Next Steps", "Further Assistance/Service Excellence",
    "Probing & Profiling", "Product Knowledge/Market Insights", "Product Pitching",
]

ACTION_COLUMNS = [
    "Action ID", "Client Code", "Lead Number", "Client Name", "Source Type",
    "Source Record ID", "Source Call ID", "Identified Date", "Latest Mention Date",
    "Category", "Subcategory", "Product/Platform", "Item Summary", "Client Statement",
    "Transaction Context", "Current Status", "Action Disposition", "Priority",
    "Recommended Action", "Assigned Team", "Assigned Employee", "Due Date",
    "Next Follow-up Date", "Attempts", "Previous Action", "Latest Action Taken",
    "Success Measure", "Outcome", "Closure Evidence", "Closed Date", "Repeat Count",
    "Days Open", "SLA Status", "Escalation Level", "Latest Call Summary",
]

INTEREST_COLUMNS = [
    "Interest ID", "Client Code", "Lead Number", "First Call ID", "Latest Call ID",
    "Interest Category", "Product/Instrument", "Interest Description", "Evidence Type",
    "Interest Strength", "Intent Stage", "Supporting Client Statement", "First Detected Date",
    "Latest Mention Date", "Mention Count", "Current Status", "Recommended Action",
    "Action Required", "Next Follow-up Date",
]

REQUIREMENT_COLUMNS = [
    "Requirement ID", "Client Code", "Lead Number", "First Call ID", "Latest Call ID",
    "Requirement Category", "Requirement Description", "Expected Outcome", "Commitment Made",
    "Committed By", "First Raised Date", "Latest Mention Date", "Due Date", "Mention Count",
    "Current Status", "Assigned Team", "Completion Evidence", "Client Confirmation", "Closed Date",
]

ISSUE_COLUMNS = [
    "Issue ID", "Client Code", "Lead Number", "Primary Category", "Subcategory",
    "Product/Platform", "Standard Issue Title", "Issue Description", "First Call ID",
    "Latest Call ID", "First Raised Date", "Latest Mention Date", "Repeat Count", "Severity",
    "Client Impact", "Current Status", "Assigned Team", "SLA Date", "Root Cause", "Resolution",
    "Resolution Evidence", "Client Confirmation", "Closed Date", "Reopened Count",
]

HISTORY_COLUMNS = [
    "History ID", "Source Type", "Source Record ID", "Client Code", "Call ID", "Event Date",
    "Event Type", "Previous Status", "New Status", "New Evidence", "Resolution Statement",
    "Changed By", "Processing Run ID",
]

TAXONOMY = [
    ("Technical", "Mobile App", "Technology/Product"),
    ("Technical", "Web Platform", "Technology/Product"),
    ("Technical", "Login/OTP", "Technology/Product"),
    ("Technical", "Performance/Slowness", "Technology/Product"),
    ("Technical", "Rate Refresh/Market Data", "Technology/Product"),
    ("Technical", "Feature Error", "Technology/Product"),
    ("Order & Trading", "Order Placement", "Dealing"),
    ("Order & Trading", "Rejected Order", "Dealing"),
    ("Order & Trading", "Wrong Execution", "Dealing"),
    ("Order & Trading", "Position/Holding Display", "Dealing"),
    ("Funds", "Fund Addition/Not Reflecting", "Accounts/Funds"),
    ("Funds", "Fund Failure", "Accounts/Funds"),
    ("Funds", "Withdrawal/Payout", "Accounts/Funds"),
    ("Funds", "Ledger Mismatch", "Accounts/Funds"),
    ("RMS & Margin", "RMS Restriction/Square-off", "RMS"),
    ("RMS & Margin", "Margin Calculation", "RMS"),
    ("RMS & Margin", "Pledge/Collateral/MTF", "RMS"),
    ("Account & KYC", "KYC/Account Opening", "KYC/Operations"),
    ("Account & KYC", "Modification/Segment Activation", "KYC/Operations"),
    ("Subscription", "Purchase/Activation/Benefits", "Subscription Team"),
    ("Subscription", "Renewal/Expiry/Refund", "Subscription Team"),
    ("Algo/API", "Activation/API/Strategy/Execution", "Algo/API Support"),
    ("Research/Product", "Research/Product Understanding", "Research/RM"),
    ("Support/Service", "Callback/RM Support", "Customer Service/RM"),
    ("Support/Service", "Delayed Resolution/Incorrect Information", "Customer Service/RM"),
    ("Charges", "Brokerage/Taxes/AMC/DP/Penalty", "Customer Service/Accounts"),
    ("Communication", "Email/SMS/WhatsApp/Language Gap", "Customer Service/RM"),
    ("Other", "Other", "Customer Service/RM"),
]


def clean_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = re.sub(r"\s+", " ", str(value).strip())
    return "" if text.lower() in {"nan", "none", "null", "nat"} else text


def clean_id(value: Any) -> str:
    text = clean_text(value)
    return text[:-2] if text.endswith(".0") and text[:-2].isdigit() else text


def norm_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", clean_text(value).lower())


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def parse_timestamp(value: Any) -> pd.Timestamp | None:
    if not clean_text(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=False)
    return None if pd.isna(parsed) else pd.Timestamp(parsed)


def duration_seconds(value: Any) -> int | None:
    text = clean_text(value)
    if not text:
        return None
    parts = text.split(":")
    try:
        nums = [int(float(part)) for part in parts]
        if len(nums) == 3:
            return nums[0] * 3600 + nums[1] * 60 + nums[2]
        if len(nums) == 2:
            return nums[0] * 60 + nums[1]
        return int(float(text))
    except ValueError:
        return None


def find_column(columns: Iterable[str], aliases: Iterable[str]) -> str | None:
    lookup: dict[str, str] = {}
    for col in columns:
        lookup.setdefault(norm_header(col), col)
    for alias in aliases:
        if norm_header(alias) in lookup:
            return lookup[norm_header(alias)]
    return None


def read_tabular(path: Path, preferred_sheet: str | None = None) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        last: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
            try:
                return pd.read_csv(path, dtype=str, low_memory=False, encoding=encoding)
            except UnicodeDecodeError as exc:
                last = exc
        raise RuntimeError(f"Unable to decode {path}: {last}")
    book = pd.ExcelFile(path)
    sheet = preferred_sheet if preferred_sheet in book.sheet_names else book.sheet_names[0]
    return pd.read_excel(path, sheet_name=sheet, dtype=str)


def make_paths(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "calls": root / "01_Input" / "Call_Analysis",
        "client360": root / "01_Input" / "Client_360",
        "processed": root / "01_Input" / "Processed",
        "config": root / "02_Config",
        "state": root / "03_State",
        "current": root / "04_Output" / "Current",
        "archive": root / "04_Output" / "Archive",
        "logs": root / "05_Logs",
        "review": root / "06_Review",
    }


def initialize(root: Path) -> dict[str, Path]:
    paths = make_paths(root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    taxonomy_path = paths["config"] / "Taxonomy_Master.xlsx"
    if not taxonomy_path.exists():
        pd.DataFrame(TAXONOMY, columns=["Primary Category", "Subcategory", "Default Owner"]).to_excel(
            taxonomy_path, index=False
        )
    return paths


def connect_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS call_versions (
            call_version_id TEXT PRIMARY KEY,
            call_unique_id TEXT NOT NULL,
            version_number INTEGER NOT NULL,
            is_latest INTEGER NOT NULL,
            previous_version_id TEXT,
            lead_number TEXT,
            conversation_timestamp TEXT,
            agent_identity TEXT,
            recording_identity TEXT,
            duration_seconds INTEGER,
            source_file_name TEXT,
            source_file_date TEXT,
            source_row_number INTEGER,
            source_row_hash TEXT NOT NULL,
            analysis_content_hash TEXT NOT NULL,
            first_received_at TEXT NOT NULL,
            last_received_at TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            processing_run_id TEXT NOT NULL,
            processing_status TEXT NOT NULL,
            duplicate_count INTEGER NOT NULL DEFAULT 0,
            client_match_status TEXT,
            matched_client_code TEXT,
            row_json TEXT NOT NULL,
            UNIQUE(call_unique_id, version_number)
        );
        CREATE INDEX IF NOT EXISTS idx_call_latest ON call_versions(call_unique_id, is_latest);
        CREATE INDEX IF NOT EXISTS idx_call_lead_time ON call_versions(lead_number, conversation_timestamp);
        CREATE TABLE IF NOT EXISTS processing_log (
            log_id TEXT PRIMARY KEY,
            processing_run_id TEXT NOT NULL,
            source_file_name TEXT,
            source_row_number INTEGER,
            detected_call_id TEXT,
            detected_version_id TEXT,
            lead_number TEXT,
            conversation_timestamp TEXT,
            row_hash TEXT,
            content_hash TEXT,
            import_result TEXT,
            existing_call_id TEXT,
            duplicate_reason TEXT,
            ai_required TEXT,
            ai_result TEXT,
            ledger_result TEXT,
            error_message TEXT,
            processed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS duplicate_review (
            review_id TEXT PRIMARY KEY,
            processing_run_id TEXT,
            source_file_name TEXT,
            source_row_number INTEGER,
            proposed_call_id TEXT,
            reason TEXT,
            row_json TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS processing_errors (
            error_id TEXT PRIMARY KEY,
            processing_run_id TEXT,
            source_file_name TEXT,
            source_row_number INTEGER,
            error_type TEXT,
            error_message TEXT,
            traceback TEXT,
            created_at TEXT
        );
        """
    )
    ensure_schema(con)
    return con


def recording_identity(link: Any) -> str:
    text = clean_text(link)
    if not text:
        return ""
    nums = re.findall(r"(?<!\d)\d{5,}(?!\d)", text)
    return "-".join(nums[-2:]) if nums else stable_hash(text)[:16]


def canonicalize(raw: dict[str, Any]) -> dict[str, Any]:
    out = {clean_text(k): clean_text(v) for k, v in raw.items()}
    for target, aliases in CANONICAL_ALIASES.items():
        source = find_column(out.keys(), aliases)
        if source is not None:
            out[target] = clean_text(out.get(source))
        else:
            out.setdefault(target, "")
    out["Lead Number"] = clean_id(out.get("Lead Number"))
    return out


def call_identity(row: dict[str, Any]) -> tuple[str, str, str, pd.Timestamp | None, str, int | None]:
    lead = clean_id(row.get("Lead Number"))
    ts = parse_timestamp(row.get("Conversation Timestamp"))
    agent = clean_id(row.get("AgentID")) or clean_text(row.get("Agent Email")).lower() or clean_text(row.get("Agent Name")).lower()
    rec = recording_identity(row.get("Conversation Recording Link"))
    dur = duration_seconds(row.get("Duration"))
    stamp = ts.strftime("%Y%m%d-%H%M%S") if ts is not None else "NO-TIMESTAMP"
    readable_lead = re.sub(r"[^A-Za-z0-9_-]", "", lead) or "NO-LEAD"
    base = f"CALL-{readable_lead}-{stamp}"
    identity_parts = {"lead": lead, "timestamp": ts.isoformat() if ts is not None else "", "recording": rec}
    if not rec:
        identity_parts.update({"agent": agent, "duration": dur})
    suffix = stable_hash(identity_parts)[:10].upper()
    return f"{base}-{suffix}", lead, agent, ts, rec, dur


def latest_client_360(paths: dict[str, Path]) -> tuple[pd.DataFrame, Path | None]:
    files = [p for p in paths["client360"].iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith("~$")]
    if not files:
        return pd.DataFrame(), None
    path = max(files, key=lambda p: p.stat().st_mtime)
    frame = read_tabular(path, "Client Detail")
    lead_col = find_column(frame.columns, ["Lead Number", "Lead No", "Lead Code"])
    client_col = find_column(frame.columns, ["Client Code", "Terminal Code"])
    if lead_col:
        frame["_lead_key"] = frame[lead_col].map(clean_id)
    else:
        frame["_lead_key"] = ""
    if client_col:
        frame["_client_key"] = frame[client_col].map(clean_id)
    else:
        frame["_client_key"] = ""
    return frame, path


def client_lookup(frame: pd.DataFrame) -> dict[str, str]:
    if frame.empty or "_lead_key" not in frame:
        return {}
    return {
        clean_id(row["_lead_key"]): clean_id(row.get("_client_key", ""))
        for _, row in frame.iterrows() if clean_id(row["_lead_key"])
    }


def log_import(con: sqlite3.Connection, values: dict[str, Any]) -> None:
    columns = list(values)
    con.execute(
        f"INSERT INTO processing_log ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
        [values[c] for c in columns],
    )


def process_row(
    con: sqlite3.Connection,
    raw: dict[str, Any],
    source: Path,
    row_number: int,
    run_id: str,
    clients: dict[str, str],
) -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    row = canonicalize(raw)
    call_id, lead, agent, ts, rec, dur = call_identity(row)
    row_hash = stable_hash(row)
    content_hash = stable_hash({key: row.get(key, "") for key in ANALYSIS_FIELDS})
    existing = con.execute(
        "SELECT * FROM call_versions WHERE call_unique_id=? AND is_latest=1", (call_id,)
    ).fetchone()
    columns = [d[0] for d in con.execute("SELECT * FROM call_versions LIMIT 0").description]
    old = dict(zip(columns, existing)) if existing else None
    match_status = "Matched" if lead and lead in clients else ("No Client 360 Match" if lead else "Missing Lead Number")
    client_code = clients.get(lead, "")
    ts_text = ts.isoformat(sep=" ") if ts is not None else ""

    collision = None
    if not old and lead and ts_text:
        possible = con.execute(
            """SELECT * FROM call_versions
               WHERE lead_number=? AND conversation_timestamp=? AND is_latest=1
                 AND call_unique_id<>?""",
            (lead, ts_text, call_id),
        ).fetchall()
        for candidate_tuple in possible:
            candidate = dict(zip(columns, candidate_tuple))
            same_agent = bool(agent and agent == clean_text(candidate["agent_identity"]))
            same_duration = dur is not None and dur == candidate["duration_seconds"]
            if same_agent or same_duration:
                collision = candidate
                break

    if collision:
        version_id = ""
        reason = "Same lead and timestamp with conflicting call identity; manual review required"
        con.execute(
            "INSERT INTO duplicate_review VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), run_id, source.name, row_number, call_id, reason,
                json.dumps(row, ensure_ascii=False, default=str), now,
            ),
        )
        log_import(con, {
            "log_id": str(uuid.uuid4()), "processing_run_id": run_id,
            "source_file_name": source.name, "source_row_number": row_number,
            "detected_call_id": call_id, "detected_version_id": "",
            "lead_number": lead, "conversation_timestamp": ts_text, "row_hash": row_hash,
            "content_hash": content_hash, "import_result": "Review",
            "existing_call_id": collision["call_unique_id"], "duplicate_reason": reason,
            "ai_required": "No", "ai_result": "Skipped", "ledger_result": "No Change",
            "error_message": "", "processed_at": now,
        })
        return "Review"

    if old and old["source_row_hash"] == row_hash:
        con.execute(
            "UPDATE call_versions SET duplicate_count=duplicate_count+1,last_received_at=? WHERE call_version_id=?",
            (now, old["call_version_id"]),
        )
        result, version_id, reason, ai = "Duplicate", old["call_version_id"], "Same call identity and complete row hash", "No"
    else:
        version_no = int(old["version_number"]) + 1 if old else 1
        version_id = f"{call_id}-V{version_no:03d}"
        if old:
            con.execute("UPDATE call_versions SET is_latest=0 WHERE call_version_id=?", (old["call_version_id"],))
        result = "Updated" if old else "Inserted"
        reason, ai = ("Same call with changed content", "Yes") if old else ("", "Yes")
        con.execute(
            """INSERT INTO call_versions (
                call_version_id,call_unique_id,version_number,is_latest,previous_version_id,
                lead_number,conversation_timestamp,agent_identity,recording_identity,duration_seconds,
                source_file_name,source_file_date,source_row_number,source_row_hash,analysis_content_hash,
                first_received_at,last_received_at,processed_at,processing_run_id,processing_status,
                duplicate_count,client_match_status,matched_client_code,row_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                version_id, call_id, version_no, 1, old["call_version_id"] if old else None,
                lead, ts_text, agent, rec, dur, source.name,
                datetime.fromtimestamp(source.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
                row_number, row_hash, content_hash, old["first_received_at"] if old else now,
                now, now, run_id, result, 0, match_status, client_code,
                json.dumps(row, ensure_ascii=False, default=str),
            ),
        )

    log_import(con, {
        "log_id": str(uuid.uuid4()), "processing_run_id": run_id,
        "source_file_name": source.name, "source_row_number": row_number,
        "detected_call_id": call_id, "detected_version_id": version_id,
        "lead_number": lead, "conversation_timestamp": ts_text, "row_hash": row_hash,
        "content_hash": content_hash, "import_result": result,
        "existing_call_id": old["call_unique_id"] if old else "",
        "duplicate_reason": reason, "ai_required": ai, "ai_result": "Pending" if ai == "Yes" else "Skipped",
        "ledger_result": "Pending Phase 2" if ai == "Yes" else "No Change",
        "error_message": "", "processed_at": now,
    })
    return result


def process_files(con: sqlite3.Connection, paths: dict[str, Path], clients: dict[str, str], run_id: str) -> dict[str, int]:
    counts = {"Inserted": 0, "Updated": 0, "Duplicate": 0, "Review": 0, "Error": 0}
    files = sorted(
        [p for p in paths["calls"].iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith("~$")],
        key=lambda p: (p.stat().st_mtime, p.name.lower()),
    )
    for source in files:
        try:
            frame = read_tabular(source)
            for idx, (_, series) in enumerate(frame.iterrows(), start=2):
                try:
                    result = process_row(con, series.to_dict(), source, idx, run_id, clients)
                    counts[result] += 1
                except Exception as exc:
                    counts["Error"] += 1
                    now = datetime.now().astimezone().isoformat(timespec="seconds")
                    con.execute(
                        "INSERT INTO processing_errors VALUES (?,?,?,?,?,?,?,?)",
                        (str(uuid.uuid4()), run_id, source.name, idx, type(exc).__name__, str(exc), traceback.format_exc(), now),
                    )
            con.commit()
        except Exception as exc:
            counts["Error"] += 1
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            con.execute(
                "INSERT INTO processing_errors VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, source.name, None, type(exc).__name__, str(exc), traceback.format_exc(), now),
            )
            con.commit()
    return counts


def query_frame(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)


def expand_calls(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(columns=SYSTEM_COLUMNS)
    expanded = pd.json_normalize(frame.pop("row_json").map(json.loads))
    rename = {
        "call_unique_id": "Call_Unique_ID", "call_version_id": "Call_Version_ID",
        "version_number": "Version_Number", "processing_run_id": "Processing_Run_ID",
        "source_file_name": "Source_File_Name", "source_file_date": "Source_File_Date",
        "source_row_number": "Source_Row_Number", "source_row_hash": "Source_Row_Hash",
        "analysis_content_hash": "Analysis_Content_Hash", "first_received_at": "First_Received_At",
        "last_received_at": "Last_Received_At", "processed_at": "Processed_At",
        "processing_status": "Processing_Status", "duplicate_count": "Duplicate_Count",
        "is_latest": "Is_Latest_Version", "previous_version_id": "Previous_Version_ID",
        "client_match_status": "Client_Match_Status", "matched_client_code": "Matched_Client_Code",
    }
    controls = frame.rename(columns=rename)
    controls["Is_Latest_Version"] = controls["Is_Latest_Version"].map({1: "Yes", 0: "No"})
    internal = {"lead_number", "conversation_timestamp", "agent_identity", "recording_identity", "duration_seconds"}
    controls = controls[[c for c in controls.columns if c not in internal]]
    ordered_controls = [c for c in SYSTEM_COLUMNS if c in controls]
    return pd.concat([controls[ordered_controls].reset_index(drop=True), expanded.reset_index(drop=True)], axis=1)


def empty_frame(columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=columns)


def load_taxonomy(paths: dict[str, Path]) -> pd.DataFrame:
    path = paths["config"] / "Taxonomy_Master.xlsx"
    return pd.read_excel(path, dtype=str) if path.exists() else pd.DataFrame(TAXONOMY, columns=["Primary Category", "Subcategory", "Default Owner"])


def ledger_frame(con: sqlite3.Connection, table: str, rename: dict[str, str], columns: list[str]) -> pd.DataFrame:
    frame = query_frame(con, f"SELECT * FROM {table}")
    if frame.empty:
        return empty_frame(columns)
    frame = frame.rename(columns=rename)
    return frame[[column for column in columns if column in frame.columns]]


def action_frame(con: sqlite3.Connection) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = query_frame(con, "SELECT * FROM action_register")
    if frame.empty:
        return empty_frame(ACTION_COLUMNS), empty_frame(ACTION_COLUMNS)
    rename = {
        "action_id": "Action ID", "client_code": "Client Code", "lead_number": "Lead Number",
        "client_name": "Client Name", "source_type": "Source Type",
        "source_record_id": "Source Record ID", "source_call_id": "Source Call ID",
        "identified_date": "Identified Date", "latest_mention_date": "Latest Mention Date",
        "category": "Category", "subcategory": "Subcategory", "product_platform": "Product/Platform",
        "item_summary": "Item Summary", "client_statement": "Client Statement",
        "transaction_context": "Transaction Context", "current_status": "Current Status",
        "action_disposition": "Action Disposition", "priority": "Priority",
        "recommended_action": "Recommended Action", "assigned_team": "Assigned Team",
        "assigned_employee": "Assigned Employee", "due_date": "Due Date",
        "next_follow_up_date": "Next Follow-up Date", "attempts": "Attempts",
        "previous_action": "Previous Action", "latest_action_taken": "Latest Action Taken",
        "success_measure": "Success Measure", "outcome": "Outcome",
        "closure_evidence": "Closure Evidence", "closed_date": "Closed Date",
        "repeat_count": "Repeat Count", "escalation_level": "Escalation Level",
        "latest_call_summary": "Latest Call Summary",
    }
    frame = frame.rename(columns=rename)
    today = pd.Timestamp.now().normalize()
    opened = pd.to_datetime(frame["Identified Date"], errors="coerce")
    due = pd.to_datetime(frame["Due Date"], errors="coerce")
    frame["Days Open"] = (today - opened.dt.normalize()).dt.days.clip(lower=0)
    frame["SLA Status"] = "Within SLA"
    active = frame["Closed Date"].fillna("").astype(str).str.strip().eq("")
    frame.loc[active & due.notna() & due.lt(today), "SLA Status"] = "Overdue"
    frame.loc[~active, "SLA Status"] = "Closed"
    frame["_priority_rank"] = frame["Priority"].map({"Critical": 1, "High": 2, "Medium": 3, "Low": 4}).fillna(9)
    frame["_due_sort"] = due
    frame = frame.sort_values(["_priority_rank", "_due_sort", "Latest Mention Date"], na_position="last")
    output = frame[[column for column in ACTION_COLUMNS if column in frame.columns]].copy()
    closed = output[output["Closed Date"].fillna("").astype(str).str.strip().ne("")].copy()
    active_output = output[output["Closed Date"].fillna("").astype(str).str.strip().eq("")].copy()
    return active_output, closed


def write_workbook(
    paths: dict[str, Path], con: sqlite3.Connection, client360: pd.DataFrame,
    client360_path: Path | None, counts: dict[str, int], phase2: Phase2Counts, run_id: str,
) -> Path:
    latest_raw = query_frame(con, "SELECT * FROM call_versions WHERE is_latest=1 ORDER BY lead_number, conversation_timestamp")
    latest = expand_calls(latest_raw.copy())
    timeline_cols = [
        c for c in ["Lead Number", "Matched_Client_Code", "Conversation Timestamp", "Call_Unique_ID",
                    "Agent Name", "Agent Email", "Lead Stage", "Connected Disposition", "Intent", "Summary",
                    "Conversation Recording Link", "Client_Match_Status"] if c in latest.columns
    ]
    timeline = latest[timeline_cols].copy() if timeline_cols else pd.DataFrame()
    all_versions = expand_calls(query_frame(con, "SELECT * FROM call_versions ORDER BY lead_number, conversation_timestamp, version_number"))
    logs = query_frame(con, "SELECT * FROM processing_log ORDER BY processed_at DESC")
    dupes = query_frame(con, "SELECT * FROM duplicate_review ORDER BY created_at DESC")
    errors = query_frame(con, "SELECT * FROM processing_errors ORDER BY created_at DESC")
    unmatched = latest[latest.get("Client_Match_Status", pd.Series(dtype=str)).ne("Matched")].copy() if not latest.empty else latest.copy()
    safe_client360 = client360.drop(columns=["_lead_key", "_client_key"], errors="ignore")
    actions, closed_actions = action_frame(con)
    interests = ledger_frame(con, "interest_ledger", {
        "interest_id": "Interest ID", "client_code": "Client Code", "lead_number": "Lead Number",
        "first_call_id": "First Call ID", "latest_call_id": "Latest Call ID",
        "interest_category": "Interest Category", "product_instrument": "Product/Instrument",
        "interest_description": "Interest Description", "evidence_type": "Evidence Type",
        "interest_strength": "Interest Strength", "intent_stage": "Intent Stage",
        "supporting_client_statement": "Supporting Client Statement",
        "first_detected_date": "First Detected Date", "latest_mention_date": "Latest Mention Date",
        "mention_count": "Mention Count", "current_status": "Current Status",
        "recommended_action": "Recommended Action", "action_required": "Action Required",
        "next_follow_up_date": "Next Follow-up Date",
    }, INTEREST_COLUMNS)
    requirements = ledger_frame(con, "requirement_ledger", {
        "requirement_id": "Requirement ID", "client_code": "Client Code", "lead_number": "Lead Number",
        "first_call_id": "First Call ID", "latest_call_id": "Latest Call ID",
        "requirement_category": "Requirement Category", "requirement_description": "Requirement Description",
        "expected_outcome": "Expected Outcome", "commitment_made": "Commitment Made",
        "committed_by": "Committed By", "first_raised_date": "First Raised Date",
        "latest_mention_date": "Latest Mention Date", "due_date": "Due Date",
        "mention_count": "Mention Count", "current_status": "Current Status",
        "assigned_team": "Assigned Team", "completion_evidence": "Completion Evidence",
        "client_confirmation": "Client Confirmation", "closed_date": "Closed Date",
    }, REQUIREMENT_COLUMNS)
    issues = ledger_frame(con, "issue_ledger", {
        "issue_id": "Issue ID", "client_code": "Client Code", "lead_number": "Lead Number",
        "primary_category": "Primary Category", "subcategory": "Subcategory",
        "product_platform": "Product/Platform", "standard_issue_title": "Standard Issue Title",
        "issue_description": "Issue Description", "first_call_id": "First Call ID",
        "latest_call_id": "Latest Call ID", "first_raised_date": "First Raised Date",
        "latest_mention_date": "Latest Mention Date", "repeat_count": "Repeat Count",
        "severity": "Severity", "client_impact": "Client Impact", "current_status": "Current Status",
        "assigned_team": "Assigned Team", "sla_date": "SLA Date", "root_cause": "Root Cause",
        "resolution": "Resolution", "resolution_evidence": "Resolution Evidence",
        "client_confirmation": "Client Confirmation", "closed_date": "Closed Date",
        "reopened_count": "Reopened Count",
    }, ISSUE_COLUMNS)
    history = ledger_frame(con, "ledger_history", {
        "history_id": "History ID", "source_type": "Source Type", "source_record_id": "Source Record ID",
        "client_code": "Client Code", "call_id": "Call ID", "event_date": "Event Date",
        "event_type": "Event Type", "previous_status": "Previous Status", "new_status": "New Status",
        "new_evidence": "New Evidence", "resolution_statement": "Resolution Statement",
        "changed_by": "Changed By", "processing_run_id": "Processing Run ID",
    }, HISTORY_COLUMNS)
    extractions = query_frame(con, "SELECT * FROM intelligence_extractions ORDER BY created_at DESC")
    summary = pd.DataFrame([
        ("Processing Run ID", run_id),
        ("Client 360 Source", client360_path.name if client360_path else "Not supplied"),
        ("Client 360 Rows", len(safe_client360)),
        ("Current Unique Calls", len(latest)),
        ("New Calls This Run", counts["Inserted"]),
        ("Changed Calls This Run", counts["Updated"]),
        ("Exact Duplicates This Run", counts["Duplicate"]),
        ("Duplicate Reviews This Run", counts["Review"]),
        ("Errors This Run", counts["Error"]),
        ("Unmatched Current Calls", len(unmatched)),
        ("Calls Interpreted This Run", phase2.processed),
        ("AI Failures This Run", phase2.failed),
        ("Interests Extracted This Run", phase2.interests),
        ("Requirements Extracted This Run", phase2.requirements),
        ("Issues Extracted This Run", phase2.issues),
        ("Open Actions", len(actions)),
        ("Open Issues", int(issues["Closed Date"].fillna("").astype(str).str.strip().eq("").sum()) if not issues.empty else 0),
        ("Phase", "Phase 2 — structured interpretation, ledgers and unified worklist"),
    ], columns=["Metric", "Value"])

    output = paths["current"] / "Sarthi_Client_Intelligence_Current.xlsx"
    with pd.ExcelWriter(output, engine="xlsxwriter", datetime_format="dd-mmm-yyyy hh:mm:ss") as writer:
        sheets = {
            "Management_Summary": summary,
            "Action_Worklist": actions,
            "Client_Call_Timeline": timeline,
            "Common_Call_Master": latest,
            "Call_Processing_Log": logs,
            "Interest_Ledger": interests,
            "Requirement_Ledger": requirements,
            "Issue_Ledger": issues,
            "Ledger_History": history,
            "Closed_Actions": closed_actions,
            "Client_360": safe_client360,
            "Duplicate_Review": dupes,
            "Unmatched_Calls": unmatched,
            "Processing_Errors": errors,
            "Taxonomy_Master": load_taxonomy(paths),
            "AI_Extraction_Audit": extractions,
            "Call_Versions_Audit": all_versions,
        }
        header = writer.book.add_format({"bold": True, "font_color": "white", "bg_color": "#17365D", "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True})
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)
            ws = writer.sheets[name[:31]]
            ws.freeze_panes(1, 0)
            if len(frame.columns):
                ws.autofilter(0, 0, len(frame), len(frame.columns) - 1)
            ws.set_row(0, 32)
            for idx, col in enumerate(frame.columns):
                ws.write(0, idx, col, header)
                length = max(len(str(col)), min(50, int(frame[col].fillna("").astype(str).str.len().max()) if len(frame) else len(str(col))))
                ws.set_column(idx, idx, min(max(length + 2, 11), 42))

    archive = paths["archive"] / f"Sarthi_Client_Intelligence_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    shutil.copy2(output, archive)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Sarthi Client Intelligence ledgers and worklist.")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Production root folder.")
    parser.add_argument("--init", action="store_true", help="Create the folder tree and taxonomy template.")
    parser.add_argument("--skip-ai", action="store_true", help="Ingest calls without structured AI interpretation.")
    parser.add_argument("--max-ai-calls", type=int, help="Limit new/changed calls interpreted in this run.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = initialize(args.root)
    print("=" * 88)
    print("BIGUL · SARTHI CLIENT INTELLIGENCE · PHASE 2")
    print("=" * 88)
    print(f"Root folder : {paths['root']}")
    if args.init:
        print("Folder structure and taxonomy are ready.")
        return 0

    run_id = f"RUN-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6].upper()}"
    client360, client360_path = latest_client_360(paths)
    print(f"Client 360  : {client360_path or 'Not supplied'}")
    print(f"Client rows : {len(client360):,}")
    con = connect_db(paths["state"] / "sarthi_client_intelligence.db")
    try:
        counts = process_files(con, paths, client_lookup(client360), run_id)
        phase2 = Phase2Counts() if args.skip_ai else run_phase2(con, run_id, max_calls=args.max_ai_calls)
        refresh_action_controls(con)
        output = write_workbook(paths, con, client360, client360_path, counts, phase2, run_id)
    finally:
        con.close()
    print(f"Inserted    : {counts['Inserted']:,}")
    print(f"Updated     : {counts['Updated']:,}")
    print(f"Duplicates  : {counts['Duplicate']:,}")
    print(f"For review  : {counts['Review']:,}")
    print(f"Errors      : {counts['Error']:,}")
    print(f"AI processed: {phase2.processed:,}")
    print(f"AI failed   : {phase2.failed:,}")
    print(f"Ledger items: {phase2.interests + phase2.requirements + phase2.issues:,}")
    print(f"Output      : {output}")
    print("DONE")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
