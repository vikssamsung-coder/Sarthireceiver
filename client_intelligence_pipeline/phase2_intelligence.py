"""Phase 2 structured call interpretation and deterministic ledger reconciliation."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


OPEN_STATUSES = {
    "New", "Assigned", "In Progress", "Awaiting Client", "Awaiting Internal Team",
    "Resolved Pending Confirmation", "Fulfilled Pending Confirmation", "Exploring",
    "Interested", "Ready to Act", "Nurture", "Follow-up Later", "Monitor",
}

ISSUE_SLA_DAYS = {"Critical": 0, "High": 1, "Medium": 3, "Low": 5}
PRIORITY_RANK = {"Critical": 1, "High": 2, "Medium": 3, "Low": 4}

DEFAULT_OWNERS = {
    "Technical": "Technology/Product",
    "Order & Trading": "Dealing",
    "Funds": "Accounts/Funds",
    "RMS & Margin": "RMS",
    "Account & KYC": "KYC/Operations",
    "Subscription": "Subscription Team",
    "Algo/API": "Algo/API Support",
    "Research/Product": "Research/RM",
    "Support/Service": "Customer Service/RM",
    "Charges": "Customer Service/Accounts",
    "Communication": "Customer Service/RM",
    "Other": "Customer Service/RM",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InterestItem(StrictModel):
    category: str
    product_instrument: str = ""
    description: str
    evidence_type: str = "Stated"
    strength: str = "Medium"
    intent_stage: str = "Exploring"
    client_statement: str = ""
    recommended_action: str = ""
    action_disposition: str = "Nurture"
    status_signal: str = "Mentioned"


class RequirementItem(StrictModel):
    category: str
    description: str
    expected_outcome: str = ""
    commitment_made: str = ""
    committed_by: str = ""
    due_date: str = ""
    assigned_team: str = ""
    client_statement: str = ""
    recommended_action: str = ""
    priority: str = "Medium"
    status_signal: str = "Mentioned"
    completion_evidence: str = ""
    client_confirmation: str = ""


class IssueItem(StrictModel):
    primary_category: str
    subcategory: str
    product_platform: str = ""
    standard_title: str
    description: str
    severity: str = "Medium"
    client_impact: str = "Inconvenience"
    client_statement: str = ""
    assigned_team: str = ""
    recommended_action: str = ""
    status_signal: str = "Mentioned"
    resolution: str = ""
    resolution_evidence: str = ""
    client_confirmation: str = ""
    system_validation: str = ""


class CallIntelligence(StrictModel):
    call_summary: str
    client_sentiment: str
    interests: list[InterestItem] = Field(default_factory=list)
    requirements: list[RequirementItem] = Field(default_factory=list)
    issues: list[IssueItem] = Field(default_factory=list)


SYSTEM_PROMPT = """You extract operational client intelligence from Bigul call-analysis data.
Return only facts supported by the supplied call. Separate every distinct issue, requirement,
and interest. Do not treat an employee's claim of resolution as client confirmation. Use
status_signal values Mentioned, Progress, ResolvedReported, ClientConfirmed, SystemValidated,
or Reopened. Use only these issue primary categories: Technical, Order & Trading, Funds,
RMS & Margin, Account & KYC, Subscription, Algo/API, Research/Product, Support/Service,
Charges, Communication, Other. Severity must be Critical, High, Medium, or Low. Interest
action disposition should be Action Required, Follow-up Later, Nurture, Monitor,
No Immediate Action, Completed, or Not Interested. Preserve short evidence statements;
do not invent amounts, dates, promises, products, or outcomes."""


class Extractor(Protocol):
    model_name: str

    def extract(self, payload: dict[str, Any]) -> CallIntelligence: ...


class OpenAIExtractor:
    """Responses API structured-output adapter; credentials stay in the environment."""

    def __init__(self, model_name: str | None = None) -> None:
        from openai import OpenAI

        self.model_name = model_name or os.getenv("SARTHI_AI_MODEL", "gpt-5.6")
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def extract(self, payload: dict[str, Any]) -> CallIntelligence:
        response = self.client.responses.parse(
            model=self.model_name,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
            text_format=CallIntelligence,
        )
        if response.output_parsed is None:
            raise RuntimeError("Structured extraction returned no parsed output")
        return response.output_parsed


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def clean(value: Any) -> str:
    if value is None:
        return ""
    value = re.sub(r"\s+", " ", str(value).strip())
    return "" if value.lower() in {"nan", "none", "null", "nat"} else value


def normal(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def ident(prefix: str) -> str:
    return f"{prefix}-{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:12].upper()}"


def similarity(left: Any, right: Any) -> float:
    a, b = normal(left), normal(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def parse_date(value: Any, fallback: str) -> str:
    text = clean(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d-%b-%Y %H:%M"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return fallback[:10]


def table_rows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    cur = con.execute(sql, params)
    cols = [item[0] for item in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS intelligence_extractions (
            extraction_id TEXT PRIMARY KEY,
            call_version_id TEXT NOT NULL UNIQUE,
            call_unique_id TEXT NOT NULL,
            lead_number TEXT,
            client_code TEXT,
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            output_json TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            processing_run_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS interest_ledger (
            interest_id TEXT PRIMARY KEY, client_code TEXT, lead_number TEXT,
            first_call_id TEXT, latest_call_id TEXT, interest_category TEXT,
            product_instrument TEXT, interest_description TEXT, evidence_type TEXT,
            interest_strength TEXT, intent_stage TEXT, supporting_client_statement TEXT,
            first_detected_date TEXT, latest_mention_date TEXT, mention_count INTEGER,
            current_status TEXT, recommended_action TEXT, action_required TEXT,
            next_follow_up_date TEXT, match_key TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_interest_client ON interest_ledger(client_code, lead_number, current_status);
        CREATE TABLE IF NOT EXISTS requirement_ledger (
            requirement_id TEXT PRIMARY KEY, client_code TEXT, lead_number TEXT,
            first_call_id TEXT, latest_call_id TEXT, requirement_category TEXT,
            requirement_description TEXT, expected_outcome TEXT, commitment_made TEXT,
            committed_by TEXT, first_raised_date TEXT, latest_mention_date TEXT,
            due_date TEXT, mention_count INTEGER, current_status TEXT, assigned_team TEXT,
            completion_evidence TEXT, client_confirmation TEXT, closed_date TEXT,
            match_key TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_requirement_client ON requirement_ledger(client_code, lead_number, current_status);
        CREATE TABLE IF NOT EXISTS issue_ledger (
            issue_id TEXT PRIMARY KEY, client_code TEXT, lead_number TEXT,
            primary_category TEXT, subcategory TEXT, product_platform TEXT,
            standard_issue_title TEXT, issue_description TEXT, first_call_id TEXT,
            latest_call_id TEXT, first_raised_date TEXT, latest_mention_date TEXT,
            repeat_count INTEGER, severity TEXT, client_impact TEXT, current_status TEXT,
            assigned_team TEXT, sla_date TEXT, root_cause TEXT, resolution TEXT,
            resolution_evidence TEXT, client_confirmation TEXT, closed_date TEXT,
            reopened_count INTEGER, match_key TEXT, updated_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_issue_client ON issue_ledger(client_code, lead_number, current_status);
        CREATE TABLE IF NOT EXISTS ledger_history (
            history_id TEXT PRIMARY KEY, source_type TEXT, source_record_id TEXT,
            client_code TEXT, call_id TEXT, event_date TEXT, event_type TEXT,
            previous_status TEXT, new_status TEXT, new_evidence TEXT,
            resolution_statement TEXT, changed_by TEXT, processing_run_id TEXT
        );
        CREATE TABLE IF NOT EXISTS action_register (
            action_id TEXT PRIMARY KEY, client_code TEXT, lead_number TEXT, client_name TEXT,
            source_type TEXT, source_record_id TEXT, source_call_id TEXT,
            identified_date TEXT, latest_mention_date TEXT, category TEXT,
            subcategory TEXT, product_platform TEXT, item_summary TEXT,
            client_statement TEXT, transaction_context TEXT, current_status TEXT,
            action_disposition TEXT, priority TEXT, recommended_action TEXT,
            assigned_team TEXT, assigned_employee TEXT, due_date TEXT,
            next_follow_up_date TEXT, attempts INTEGER, previous_action TEXT,
            latest_action_taken TEXT, success_measure TEXT, outcome TEXT,
            closure_evidence TEXT, closed_date TEXT, repeat_count INTEGER,
            escalation_level TEXT, latest_call_summary TEXT, updated_at TEXT,
            UNIQUE(source_type, source_record_id)
        );
        CREATE INDEX IF NOT EXISTS idx_action_status ON action_register(current_status, due_date, priority);
        """
    )


def client_identity(row: dict[str, Any]) -> tuple[str, str]:
    return clean(row.get("matched_client_code")), clean(row.get("lead_number"))


def identity_where(client_code: str, lead_number: str) -> tuple[str, tuple[str, ...]]:
    if client_code:
        return "client_code=?", (client_code,)
    return "client_code='' AND lead_number=?", (lead_number,)


def current_records(con: sqlite3.Connection, table: str, client_code: str, lead_number: str) -> list[dict[str, Any]]:
    where, params = identity_where(client_code, lead_number)
    return table_rows(con, f"SELECT * FROM {table} WHERE {where}", params)


def match_record(
    rows: list[dict[str, Any]], *, category_field: str, category: str,
    description_field: str, description: str, product_field: str = "",
    product: str = "", threshold: float = 0.72,
) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        if normal(row.get(category_field)) != normal(category):
            continue
        if product_field and product and row.get(product_field):
            product_score = similarity(row.get(product_field), product)
            if product_score < 0.55:
                continue
        score = similarity(row.get(description_field), description)
        if score >= threshold:
            candidates.append((score, row))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def add_history(
    con: sqlite3.Connection, source_type: str, source_id: str, client_code: str,
    call_id: str, event_date: str, event_type: str, previous: str, new: str,
    evidence: str, resolution: str, run_id: str,
) -> None:
    con.execute(
        "INSERT INTO ledger_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ident("HIST"), source_type, source_id, client_code, call_id, event_date,
         event_type, previous, new, evidence, resolution, "Pipeline", run_id),
    )


def issue_status(old: str, signal: str, has_client_confirmation: bool, has_system_validation: bool) -> tuple[str, str]:
    signal = normal(signal).replace(" ", "")
    if has_client_confirmation:
        return "Closed", "Client Confirmed"
    if has_system_validation:
        return "Closed", "System Validated"
    if signal == "resolvedreported":
        return "Resolved Pending Confirmation", "Resolution Reported"
    if signal == "reopened" or (old == "Closed" and signal in {"mentioned", "progress"}):
        return "Reopened", "Reopened"
    if signal == "progress":
        return "In Progress", "Progress"
    return old or "New", "Mentioned Again" if old else "Created"


def upsert_issue(
    con: sqlite3.Connection, item: IssueItem, call: dict[str, Any], event_date: str,
    summary: str, run_id: str,
) -> tuple[str, str]:
    client_code, lead = client_identity(call)
    rows = current_records(con, "issue_ledger", client_code, lead)
    candidates = [row for row in rows if normal(row["primary_category"]) == normal(item.primary_category)
                  and normal(row["subcategory"]) == normal(item.subcategory)]
    match = match_record(
        candidates, category_field="subcategory", category=item.subcategory,
        description_field="issue_description", description=item.description,
        product_field="product_platform", product=item.product_platform, threshold=0.64,
    )
    owner = clean(item.assigned_team) or DEFAULT_OWNERS.get(item.primary_category, "Customer Service/RM")
    severity = item.severity if item.severity in ISSUE_SLA_DAYS else "Medium"
    has_client = bool(clean(item.client_confirmation))
    has_system = bool(clean(item.system_validation))
    old_status = clean(match.get("current_status")) if match else ""
    new_status, event_type = issue_status(old_status, item.status_signal, has_client, has_system)
    closed = event_date if new_status == "Closed" else ""
    evidence = clean(item.system_validation) or clean(item.resolution_evidence) or clean(item.client_statement)
    resolution = clean(item.resolution)
    if match:
        issue_id = match["issue_id"]
        reopened = int(match.get("reopened_count") or 0) + (1 if event_type == "Reopened" else 0)
        con.execute(
            """UPDATE issue_ledger SET latest_call_id=?,latest_mention_date=?,repeat_count=?,
            severity=?,client_impact=?,current_status=?,assigned_team=?,resolution=?,
            resolution_evidence=?,client_confirmation=?,closed_date=?,reopened_count=?,updated_at=?
            WHERE issue_id=?""",
            (call["call_unique_id"], event_date, int(match.get("repeat_count") or 1) + 1,
             severity, item.client_impact, new_status, owner, resolution or match.get("resolution", ""),
             evidence or match.get("resolution_evidence", ""),
             clean(item.client_confirmation) or match.get("client_confirmation", ""), closed,
             reopened, now_text(), issue_id),
        )
    else:
        issue_id = ident("ISS")
        sla = (datetime.fromisoformat(event_date[:10]) + timedelta(days=ISSUE_SLA_DAYS[severity])).date().isoformat()
        match_key = "|".join(map(normal, [item.primary_category, item.subcategory, item.product_platform, item.standard_title]))
        values = (
            issue_id, client_code, lead, item.primary_category, item.subcategory,
            item.product_platform, item.standard_title, item.description, call["call_unique_id"],
            call["call_unique_id"], event_date, event_date, 1, severity, item.client_impact,
            new_status or "New", owner, sla, "", resolution, evidence,
            clean(item.client_confirmation), closed, 0, match_key, now_text(),
        )
        con.execute(f"INSERT INTO issue_ledger VALUES ({','.join('?' for _ in values)})", values)
        event_type = "Created" if new_status != "Closed" else event_type
    add_history(con, "Issue", issue_id, client_code, call["call_unique_id"], event_date,
                event_type, old_status, new_status, evidence, resolution, run_id)
    disposition = "Completed" if new_status == "Closed" else "Action Required"
    priority = severity
    success = "Client confirms resolution or transaction/system data validates resolution"
    upsert_action(con, "Issue", issue_id, call, event_date, item.primary_category,
                  item.subcategory, item.product_platform, item.standard_title,
                  item.client_statement, summary, new_status, disposition, priority,
                  item.recommended_action, owner, sla if not match else clean(match.get("sla_date")),
                  success, evidence, closed, int(match.get("repeat_count") or 0) + 1 if match else 1)
    return issue_id, event_type


def requirement_status(old: str, item: RequirementItem) -> tuple[str, str]:
    signal = normal(item.status_signal).replace(" ", "")
    if clean(item.client_confirmation):
        return "Closed", "Client Confirmed"
    if clean(item.completion_evidence) or signal in {"systemvalidated", "clientconfirmed"}:
        return "Fulfilled Pending Confirmation", "Fulfilment Reported"
    if signal == "progress":
        return "In Progress", "Progress"
    if signal == "reopened" or old == "Closed":
        return "Reopened", "Reopened"
    return old or "New", "Mentioned Again" if old else "Created"


def upsert_requirement(
    con: sqlite3.Connection, item: RequirementItem, call: dict[str, Any], event_date: str,
    summary: str, run_id: str,
) -> tuple[str, str]:
    client_code, lead = client_identity(call)
    rows = current_records(con, "requirement_ledger", client_code, lead)
    match = match_record(rows, category_field="requirement_category", category=item.category,
                         description_field="requirement_description", description=item.description,
                         threshold=0.68)
    old = clean(match.get("current_status")) if match else ""
    status, event = requirement_status(old, item)
    owner = clean(item.assigned_team) or DEFAULT_OWNERS.get(item.category, "Customer Service/RM")
    due = parse_date(item.due_date, (datetime.fromisoformat(event_date[:10]) + timedelta(days=3)).date().isoformat())
    closed = event_date if status == "Closed" else ""
    if match:
        record_id = match["requirement_id"]
        con.execute(
            """UPDATE requirement_ledger SET latest_call_id=?,latest_mention_date=?,due_date=?,
            mention_count=?,current_status=?,assigned_team=?,completion_evidence=?,
            client_confirmation=?,closed_date=?,updated_at=? WHERE requirement_id=?""",
            (call["call_unique_id"], event_date, due, int(match.get("mention_count") or 1) + 1,
             status, owner, clean(item.completion_evidence) or match.get("completion_evidence", ""),
             clean(item.client_confirmation) or match.get("client_confirmation", ""), closed,
             now_text(), record_id),
        )
    else:
        record_id = ident("REQ")
        key = "|".join(map(normal, [item.category, item.description]))
        con.execute(
            "INSERT INTO requirement_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, client_code, lead, call["call_unique_id"], call["call_unique_id"],
             item.category, item.description, item.expected_outcome, item.commitment_made,
             item.committed_by, event_date, event_date, due, 1, status, owner,
             item.completion_evidence, item.client_confirmation, closed, key, now_text()),
        )
    evidence = item.completion_evidence or item.client_statement
    add_history(con, "Requirement", record_id, client_code, call["call_unique_id"], event_date,
                event, old, status, evidence, item.expected_outcome, run_id)
    disposition = "Completed" if status == "Closed" else "Action Required"
    success = clean(item.expected_outcome) or "Requirement completed and confirmed by client"
    upsert_action(con, "Requirement", record_id, call, event_date, item.category, "", "",
                  item.description, item.client_statement, summary, status, disposition,
                  item.priority if item.priority in PRIORITY_RANK else "Medium",
                  item.recommended_action, owner, due, success, item.completion_evidence,
                  closed, int(match.get("mention_count") or 0) + 1 if match else 1)
    return record_id, event


def interest_status(old: str, item: InterestItem) -> tuple[str, str]:
    signal = normal(item.status_signal).replace(" ", "")
    if item.action_disposition in {"Completed", "Not Interested"}:
        return item.action_disposition, item.action_disposition
    if signal == "reopened":
        return "Interested", "Reopened"
    stage = item.intent_stage if item.intent_stage in {
        "New", "Exploring", "Interested", "Ready to Act", "Converted", "Nurture",
        "Follow-up Later", "Not Interested", "Lost", "Closed",
    } else "Exploring"
    return stage or old or "Exploring", "Mentioned Again" if old else "Created"


def upsert_interest(
    con: sqlite3.Connection, item: InterestItem, call: dict[str, Any], event_date: str,
    summary: str, run_id: str,
) -> tuple[str, str]:
    client_code, lead = client_identity(call)
    rows = current_records(con, "interest_ledger", client_code, lead)
    match = match_record(rows, category_field="interest_category", category=item.category,
                         description_field="interest_description", description=item.description,
                         product_field="product_instrument", product=item.product_instrument,
                         threshold=0.64)
    old = clean(match.get("current_status")) if match else ""
    status, event = interest_status(old, item)
    followup = (datetime.fromisoformat(event_date[:10]) + timedelta(days=3)).date().isoformat()
    action_required = "Yes" if item.action_disposition in {"Action Required", "Follow-up Later", "Nurture", "Monitor"} else "No"
    if match:
        record_id = match["interest_id"]
        con.execute(
            """UPDATE interest_ledger SET latest_call_id=?,interest_strength=?,intent_stage=?,
            supporting_client_statement=?,latest_mention_date=?,mention_count=?,current_status=?,
            recommended_action=?,action_required=?,next_follow_up_date=?,updated_at=?
            WHERE interest_id=?""",
            (call["call_unique_id"], item.strength, item.intent_stage,
             item.client_statement or match.get("supporting_client_statement", ""), event_date,
             int(match.get("mention_count") or 1) + 1, status,
             item.recommended_action or match.get("recommended_action", ""), action_required,
             followup, now_text(), record_id),
        )
    else:
        record_id = ident("INT")
        key = "|".join(map(normal, [item.category, item.product_instrument, item.description]))
        con.execute(
            "INSERT INTO interest_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (record_id, client_code, lead, call["call_unique_id"], call["call_unique_id"],
             item.category, item.product_instrument, item.description, item.evidence_type,
             item.strength, item.intent_stage, item.client_statement, event_date, event_date,
             1, status, item.recommended_action, action_required, followup, key, now_text()),
        )
    add_history(con, "Interest", record_id, client_code, call["call_unique_id"], event_date,
                event, old, status, item.client_statement, "", run_id)
    priority = "High" if item.intent_stage == "Ready to Act" else "Medium" if item.strength == "High" else "Low"
    owner = DEFAULT_OWNERS.get(item.category, "Research/RM")
    success = "Client starts the recommended journey, trial, activation, or purchase"
    closed = event_date if status in {"Converted", "Completed", "Not Interested", "Lost", "Closed"} else ""
    upsert_action(con, "Interest", record_id, call, event_date, item.category, "",
                  item.product_instrument, item.description, item.client_statement, summary,
                  status, item.action_disposition, priority, item.recommended_action, owner,
                  followup, success, "", closed,
                  int(match.get("mention_count") or 0) + 1 if match else 1)
    return record_id, event


def upsert_action(
    con: sqlite3.Connection, source_type: str, record_id: str, call: dict[str, Any],
    event_date: str, category: str, subcategory: str, product: str, summary: str,
    statement: str, call_summary: str, status: str, disposition: str, priority: str,
    recommendation: str, team: str, due_date: str, success_measure: str,
    closure_evidence: str, closed_date: str, repeat_count: int,
) -> None:
    client_code, lead = client_identity(call)
    existing = table_rows(con, "SELECT * FROM action_register WHERE source_type=? AND source_record_id=?",
                          (source_type, record_id))
    if existing:
        old = existing[0]
        previous = clean(old.get("latest_action_taken")) or clean(old.get("previous_action"))
        con.execute(
            """UPDATE action_register SET source_call_id=?,latest_mention_date=?,category=?,
            subcategory=?,product_platform=?,item_summary=?,client_statement=?,current_status=?,
            action_disposition=?,priority=?,recommended_action=?,assigned_team=?,due_date=?,
            next_follow_up_date=?,previous_action=?,success_measure=?,closure_evidence=?,
            closed_date=?,repeat_count=?,latest_call_summary=?,updated_at=? WHERE action_id=?""",
            (call["call_unique_id"], event_date, category, subcategory, product, summary,
             statement, status, disposition, priority, recommendation, team, due_date,
             due_date, previous, success_measure, closure_evidence, closed_date, repeat_count,
             call_summary, now_text(), old["action_id"]),
        )
        return
    values = (
        ident("ACT"), client_code, lead, "", source_type, record_id, call["call_unique_id"],
        event_date, event_date, category, subcategory, product, summary, statement, "",
        status, disposition, priority, recommendation, team, "", due_date, due_date, 0,
        "", "", success_measure, "", closure_evidence, closed_date, repeat_count, "None",
        call_summary, now_text(),
    )
    con.execute(
        f"INSERT INTO action_register VALUES ({','.join('?' for _ in values)})", values
    )


def call_payload(con: sqlite3.Connection, call: dict[str, Any]) -> dict[str, Any]:
    raw = json.loads(call["row_json"])
    client_code, lead = client_identity(call)
    relevant = {
        key: raw.get(key, "") for key in (
            "Conversation Timestamp", "Lead Number", "Customer Name", "Agent Name", "Duration",
            "CRM Status", "Lead Stage", "Connected Disposition", "Intent", "Summary",
            "Client'S Story", "Closing&Next Steps", "Follow Up", "Disposition",
            "Further Assistance/Service Excellence", "Product Knowledge/Market Insights",
        ) if raw.get(key)
    }
    open_items: dict[str, list[dict[str, Any]]] = {}
    for name, table in (("interests", "interest_ledger"), ("requirements", "requirement_ledger"), ("issues", "issue_ledger")):
        rows = current_records(con, table, client_code, lead)
        open_items[name] = [row for row in rows if clean(row.get("current_status")) in OPEN_STATUSES]
    return {
        "call": relevant,
        "client_identity": {"client_code": client_code, "lead_number": lead},
        "existing_open_items": open_items,
    }


@dataclass
class Phase2Counts:
    processed: int = 0
    skipped: int = 0
    failed: int = 0
    interests: int = 0
    requirements: int = 0
    issues: int = 0


def run_phase2(
    con: sqlite3.Connection, run_id: str, extractor: Extractor | None = None,
    max_calls: int | None = None,
) -> Phase2Counts:
    ensure_schema(con)
    if extractor is None:
        if not os.getenv("OPENAI_API_KEY"):
            return Phase2Counts()
        extractor = OpenAIExtractor()
    pending = table_rows(
        con,
        """SELECT cv.* FROM call_versions cv
        LEFT JOIN intelligence_extractions ie ON ie.call_version_id=cv.call_version_id
        WHERE cv.is_latest=1 AND cv.processing_status IN ('Inserted','Updated')
          AND (ie.call_version_id IS NULL OR ie.status='Failed')
        ORDER BY cv.conversation_timestamp, cv.call_version_id""",
    )
    if max_calls is not None:
        pending = pending[:max(0, max_calls)]
    counts = Phase2Counts()
    for call in pending:
        extraction_id = ident("EXT")
        created = now_text()
        payload = call_payload(con, call)
        input_hash = __import__("hashlib").sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        existing = table_rows(con, "SELECT * FROM intelligence_extractions WHERE call_version_id=?",
                              (call["call_version_id"],))
        try:
            if existing:
                extraction_id = existing[0]["extraction_id"]
                con.execute("UPDATE intelligence_extractions SET status='Processing',error_message='',processing_run_id=? WHERE extraction_id=?",
                            (run_id, extraction_id))
            else:
                con.execute(
                    "INSERT INTO intelligence_extractions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (extraction_id, call["call_version_id"], call["call_unique_id"],
                     call.get("lead_number", ""), call.get("matched_client_code", ""),
                     extractor.model_name, "phase2-v1", input_hash, "", "Processing", "",
                     run_id, created, ""),
                )
            intelligence = extractor.extract(payload)
            raw = json.loads(call["row_json"])
            event_date = parse_date(raw.get("Conversation Timestamp"), created)
            for item in intelligence.interests:
                upsert_interest(con, item, call, event_date, intelligence.call_summary, run_id)
                counts.interests += 1
            for item in intelligence.requirements:
                upsert_requirement(con, item, call, event_date, intelligence.call_summary, run_id)
                counts.requirements += 1
            for item in intelligence.issues:
                upsert_issue(con, item, call, event_date, intelligence.call_summary, run_id)
                counts.issues += 1
            completed = now_text()
            con.execute(
                "UPDATE intelligence_extractions SET output_json=?,status='Success',completed_at=? WHERE extraction_id=?",
                (intelligence.model_dump_json(), completed, extraction_id),
            )
            con.execute(
                "UPDATE processing_log SET ai_result='Success',ledger_result='Updated' WHERE detected_version_id=?",
                (call["call_version_id"],),
            )
            con.commit()
            counts.processed += 1
        except Exception as exc:
            con.rollback()
            con.execute(
                "INSERT OR REPLACE INTO intelligence_extractions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (extraction_id, call["call_version_id"], call["call_unique_id"],
                 call.get("lead_number", ""), call.get("matched_client_code", ""),
                 extractor.model_name, "phase2-v1", input_hash, "", "Failed", str(exc),
                 run_id, created, now_text()),
            )
            con.execute(
                "UPDATE processing_log SET ai_result='Failed',ledger_result='Error',error_message=? WHERE detected_version_id=?",
                (str(exc), call["call_version_id"]),
            )
            con.commit()
            counts.failed += 1
    return counts


def refresh_action_controls(con: sqlite3.Connection, as_of: datetime | None = None) -> None:
    """Refresh deterministic escalation without altering team-entered action fields."""
    today = (as_of or datetime.now()).date()
    rows = table_rows(con, "SELECT * FROM action_register")
    for row in rows:
        due = clean(row.get("due_date"))[:10]
        escalation = "None"
        if due and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) and clean(row.get("closed_date")) == "":
            overdue = (today - datetime.fromisoformat(due).date()).days
            if overdue > 3:
                escalation = "Department Head"
            elif overdue > 0:
                escalation = "Team Lead"
        con.execute("UPDATE action_register SET escalation_level=?,updated_at=? WHERE action_id=?",
                    (escalation, now_text(), row["action_id"]))
    con.commit()
