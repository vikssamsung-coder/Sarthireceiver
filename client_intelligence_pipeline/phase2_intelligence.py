"""Phase 2 structured call interpretation and deterministic ledger reconciliation."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Protocol

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

ISSUE_TAXONOMY = {
    "Technical": (
        "Mobile App", "Web Platform", "Login/OTP", "Performance/Slowness",
        "Rate Refresh/Market Data", "Feature Error",
    ),
    "Order & Trading": (
        "Order Placement", "Rejected Order", "Wrong Execution", "Position/Holding Display",
    ),
    "Funds": ("Fund Addition/Not Reflecting", "Fund Failure", "Withdrawal/Payout", "Ledger Mismatch"),
    "RMS & Margin": ("RMS Restriction/Square-off", "Margin Calculation", "Pledge/Collateral/MTF"),
    "Account & KYC": ("KYC/Account Opening", "Modification/Segment Activation"),
    "Subscription": ("Purchase/Activation/Benefits", "Renewal/Expiry/Refund"),
    "Algo/API": ("Activation/API/Strategy/Execution",),
    "Research/Product": ("Research/Product Understanding",),
    "Support/Service": ("Callback/RM Support", "Delayed Resolution/Incorrect Information"),
    "Charges": ("Brokerage/Taxes/AMC/DP/Penalty",),
    "Communication": ("Email/SMS/WhatsApp/Language Gap",),
    "Other": ("Other",),
}

TAXONOMY_OWNERS = {
    ("Technical", "Login/OTP"): "Technology/Product",
    ("Algo/API", "Activation/API/Strategy/Execution"): "Algo/API Support",
}

SEMANTIC_STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "by", "client", "customer", "entire",
    "for", "from", "group", "in", "is", "it", "of", "on", "partner", "please",
    "requested", "should", "that", "the", "their", "to", "user", "users", "with",
    "add", "addition", "arrange", "backup", "check", "delivery", "enable", "fix",
    "investigate", "provide", "resolve", "restore", "support", "through", "verify",
}

CORRECTIVE_VERBS = {
    "arrange", "callback", "check", "correct", "fix", "investigate", "reconcile",
    "resolve", "restore", "troubleshoot", "verify",
}

FEATURE_VERBS = {"add", "build", "create", "enable", "implement", "introduce", "launch"}

DEFAULT_LUNA_MODEL = "gpt-5.6-luna"
DEFAULT_TERRA_MODEL = "gpt-5.6-terra"
MODEL_TOKEN_PRICES_USD_PER_MILLION = {
    DEFAULT_LUNA_MODEL: {"input": 0.20, "cached": 0.02, "cache_write": 0.25, "output": 1.20},
    DEFAULT_TERRA_MODEL: {"input": 2.00, "cached": 0.20, "cache_write": 2.50, "output": 12.00},
}
TERRA_RISK_SIGNALS = (
    ("Complaint/regulatory threat", ("formal complaint", "file a complaint", "raise a complaint", "complaint to sebi", "sebi complaint", "complain to", "legal action", "consumer forum")),
    ("Account-closure/churn threat", ("close my account", "close the account", "closing the account", "account closure", "stop trading", "shift to another broker", "leave bigul")),
    ("Critical availability/trading impact", ("api outage", "api down", "server down", "unable to trade", "trading stopped", "orders not executing")),
    ("Financial-loss allegation", ("financial loss", "money lost", "caused a loss", "unauthorised trade", "unauthorized trade")),
    ("Broad or recurring impact", ("third time", "third outage", "multiple users", "entire partner group", "recurring outage", "repeated outage")),
)

PROMPT_VERSION_PREFIX = "phase2-v3-hybrid-client360"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "phase2_call_intelligence.md"


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
    system_validation: str = ""  # retained for stored-output compatibility; ignored in Phase 2


class CallIntelligence(StrictModel):
    call_summary: str
    client_sentiment: str
    assessment_confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_terra_review: bool = False
    review_reasons: list[str] = Field(default_factory=list)
    interests: list[InterestItem] = Field(default_factory=list)
    requirements: list[RequirementItem] = Field(default_factory=list)
    issues: list[IssueItem] = Field(default_factory=list)


def load_system_prompt() -> str:
    if not PROMPT_PATH.is_file():
        raise FileNotFoundError(f"Phase 2 prompt file not found: {PROMPT_PATH}")
    prompt = PROMPT_PATH.read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError(f"Phase 2 prompt file is empty: {PROMPT_PATH}")
    return prompt


def system_prompt_version(prompt: str | None = None) -> str:
    content = prompt if prompt is not None else load_system_prompt()
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"{PROMPT_VERSION_PREFIX}-{digest}"


class Extractor(Protocol):
    model_name: str

    def extract(self, payload: dict[str, Any]) -> CallIntelligence: ...


@dataclass
class ExtractionAttempt:
    model_name: str
    model_role: str
    status: str = "Success"
    error_message: str = ""
    response_id: str = ""
    output_json: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    processing_seconds: float = 0.0
    estimated_cost_usd: float = 0.0


@dataclass
class ExtractionEnvelope:
    intelligence: CallIntelligence
    model_name: str = ""
    response_id: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    processing_seconds: float = 0.0
    estimated_cost_usd: float = 0.0
    escalated_to_terra: bool = False
    escalation_status: str = "Not Required"
    escalation_reasons: list[str] = field(default_factory=list)
    attempts: list[ExtractionAttempt] = field(default_factory=list)


def env_flag(name: str, default: bool) -> bool:
    value = clean(os.getenv(name, ""))
    if not value:
        return default
    return normal(value) in {"1", "true", "yes", "on"}


def estimate_cost_usd(
    model_name: str, input_tokens: int, cached_input_tokens: int,
    cache_write_tokens: int, output_tokens: int,
) -> float:
    rates = MODEL_TOKEN_PRICES_USD_PER_MILLION.get(model_name)
    if not rates:
        return 0.0
    uncached = max(0, input_tokens - cached_input_tokens - cache_write_tokens)
    cost = (
        uncached * rates["input"]
        + cached_input_tokens * rates["cached"]
        + cache_write_tokens * rates["cache_write"]
        + output_tokens * rates["output"]
    ) / 1_000_000
    return round(cost, 8)


class OpenAIExtractor:
    """Responses API structured-output adapter; credentials stay in the environment."""

    def __init__(self, model_name: str | None = None, model_role: str = "Single") -> None:
        from openai import OpenAI

        self.model_name = model_name or os.getenv("SARTHI_AI_MODEL", "gpt-5.6")
        self.model_role = model_role
        self.client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    def extract(self, payload: dict[str, Any]) -> ExtractionEnvelope:
        started = time.perf_counter()
        response = self.client.responses.parse(
            model=self.model_name,
            input=[
                {"role": "system", "content": load_system_prompt()},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
            ],
            text_format=CallIntelligence,
        )
        if response.output_parsed is None:
            raise RuntimeError("Structured extraction returned no parsed output")
        usage = getattr(response, "usage", None)
        input_details = getattr(usage, "input_tokens_details", None)
        output_details = getattr(usage, "output_tokens_details", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cached_input_tokens = int(getattr(input_details, "cached_tokens", 0) or 0)
        cache_write_tokens = int(getattr(input_details, "cache_write_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        reasoning_tokens = int(getattr(output_details, "reasoning_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", 0) or 0)
        processing_seconds = round(time.perf_counter() - started, 3)
        cost = estimate_cost_usd(
            self.model_name, input_tokens, cached_input_tokens, cache_write_tokens, output_tokens,
        )
        attempt = ExtractionAttempt(
            model_name=self.model_name, model_role=self.model_role,
            response_id=clean(getattr(response, "id", "")),
            output_json=response.output_parsed.model_dump_json(), input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens, cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens, reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens, processing_seconds=processing_seconds,
            estimated_cost_usd=cost,
        )
        return ExtractionEnvelope(
            intelligence=response.output_parsed,
            model_name=self.model_name, response_id=attempt.response_id,
            input_tokens=input_tokens, cached_input_tokens=cached_input_tokens,
            cache_write_tokens=cache_write_tokens, output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens, total_tokens=total_tokens,
            processing_seconds=processing_seconds, estimated_cost_usd=cost,
            attempts=[attempt],
        )


def terra_escalation_reasons(
    payload: dict[str, Any], intelligence: CallIntelligence, confidence_threshold: float,
) -> list[str]:
    """Combine source-evidence risk rules with Luna's structured self-review signals."""
    # Escalate from the current call's evidence, not an older open item that happens
    # to be included as reconciliation context.
    source_text = normal(json.dumps(payload.get("call", {}), ensure_ascii=False, default=str))
    reasons = [label for label, phrases in TERRA_RISK_SIGNALS if any(normal(p) in source_text for p in phrases)]
    if intelligence.needs_terra_review:
        reasons.extend(intelligence.review_reasons or ["Luna requested Terra review"])
    if intelligence.assessment_confidence < confidence_threshold:
        reasons.append(f"Low Luna confidence ({intelligence.assessment_confidence:.2f})")
    if any(normal(item.severity) == "critical" for item in intelligence.issues):
        reasons.append("Critical issue severity")
    if normal(intelligence.client_sentiment) in {"angry", "extremely frustrated", "hostile", "threatening"}:
        reasons.append("Severe negative sentiment")
    return list(dict.fromkeys(clean(reason) for reason in reasons if clean(reason)))


class HybridOpenAIExtractor:
    """Use Luna by default and Terra only when evidence or Luna signals higher risk."""

    def __init__(
        self, luna_model: str | None = None, terra_model: str | None = None,
        confidence_threshold: float | None = None,
        luna_extractor: Any | None = None, terra_extractor: Any | None = None,
    ) -> None:
        self.luna_model = luna_model or os.getenv("SARTHI_AI_LUNA_MODEL", DEFAULT_LUNA_MODEL)
        self.terra_model = terra_model or os.getenv("SARTHI_AI_TERRA_MODEL", DEFAULT_TERRA_MODEL)
        self.confidence_threshold = (
            confidence_threshold if confidence_threshold is not None
            else float(os.getenv("SARTHI_AI_TERRA_CONFIDENCE_THRESHOLD", "0.80"))
        )
        self.model_name = f"Hybrid: {self.luna_model} -> {self.terra_model}"
        self.luna = luna_extractor or OpenAIExtractor(self.luna_model, "Luna First Pass")
        self.terra = terra_extractor or OpenAIExtractor(self.terra_model, "Terra Review")

    def extract(self, payload: dict[str, Any]) -> ExtractionEnvelope:
        luna = self.luna.extract(payload)
        reasons = terra_escalation_reasons(payload, luna.intelligence, self.confidence_threshold)
        if not reasons:
            luna.escalation_reasons = []
            return luna

        review_payload = dict(payload)
        review_payload["hybrid_review"] = {
            "instruction": "Independently verify the evidence and return the corrected final extraction.",
            "escalation_reasons": reasons,
            "luna_first_pass": luna.intelligence.model_dump(mode="json"),
        }
        try:
            terra = self.terra.extract(review_payload)
        except Exception as exc:
            luna.escalated_to_terra = True
            luna.escalation_status = "Terra Failed — Luna Fallback"
            luna.escalation_reasons = reasons
            luna.attempts.append(ExtractionAttempt(
                model_name=self.terra_model, model_role="Terra Review",
                status="Failed", error_message=clean(exc),
            ))
            return luna

        attempts = luna.attempts + terra.attempts
        return ExtractionEnvelope(
            intelligence=terra.intelligence, model_name=self.terra_model,
            response_id=terra.response_id,
            input_tokens=sum(item.input_tokens for item in attempts),
            cached_input_tokens=sum(item.cached_input_tokens for item in attempts),
            cache_write_tokens=sum(item.cache_write_tokens for item in attempts),
            output_tokens=sum(item.output_tokens for item in attempts),
            reasoning_tokens=sum(item.reasoning_tokens for item in attempts),
            total_tokens=sum(item.total_tokens for item in attempts),
            processing_seconds=round(sum(item.processing_seconds for item in attempts), 3),
            estimated_cost_usd=round(sum(item.estimated_cost_usd for item in attempts), 8),
            escalated_to_terra=True, escalation_status="Terra Completed",
            escalation_reasons=reasons, attempts=attempts,
        )


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


def stable_ident(prefix: str, *parts: Any) -> str:
    payload = "|".join(normal(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20].upper()}"


def similarity(left: Any, right: Any) -> float:
    a, b = normal(left), normal(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def semantic_tokens(value: Any) -> set[str]:
    return {token for token in normal(value).split() if len(token) > 1 and token not in SEMANTIC_STOPWORDS}


def semantic_similarity(left: Any, right: Any) -> float:
    a, b = semantic_tokens(left), semantic_tokens(right)
    token_score = len(a & b) / max(1, len(a | b)) if a and b else 0.0
    return max(similarity(left, right), token_score)


def contains_keyword(text: str, keyword: str) -> bool:
    return keyword in text if " " in keyword else keyword in set(text.split())


def canonical_category(value: Any, evidence: Any = "") -> str:
    exact = next((name for name in DEFAULT_OWNERS if normal(name) == normal(value)), "")
    if exact:
        return exact
    text = normal(f"{clean(value)} {clean(evidence)}")
    keyword_categories = (
        (("otp", "login", "authentication", "app", "platform", "technical"), "Technical"),
        (("api", "algo", "strategy", "tradetron"), "Algo/API"),
        (("order", "trade", "position", "holding"), "Order & Trading"),
        (("fund", "payment", "payin", "payout", "withdraw"), "Funds"),
        (("margin", "rms", "pledge", "collateral", "square off"), "RMS & Margin"),
        (("kyc", "account opening", "segment activation"), "Account & KYC"),
        (("subscription", "renewal", "plan purchase"), "Subscription"),
        (("research", "recommendation", "product understanding"), "Research/Product"),
        (("callback", "service", "follow up", "support"), "Support/Service"),
        (("brokerage", "charge", "tax", "penalty"), "Charges"),
        (("email", "sms", "whatsapp", "language"), "Communication"),
    )
    return next(
        (category for words, category in keyword_categories if any(contains_keyword(text, word) for word in words)),
        "Other",
    )


def canonical_issue_taxonomy(item: IssueItem) -> IssueItem:
    """Force AI labels onto the approved taxonomy before anything is persisted."""
    evidence = " ".join((item.subcategory, item.standard_title, item.description, item.product_platform))
    primary = canonical_category(item.primary_category, evidence)
    allowed = ISSUE_TAXONOMY[primary]
    subcategory = next((name for name in allowed if normal(name) == normal(item.subcategory)), "")
    if not subcategory:
        text = normal(evidence)
        rules = {
            "Technical": (
                (("otp", "login", "authentication", "password"), "Login/OTP"),
                (("slow", "slowness", "latency", "hang", "freeze"), "Performance/Slowness"),
                (("rate refresh", "market data", "price refresh", "live price"), "Rate Refresh/Market Data"),
                (("web", "browser"), "Web Platform"),
                (("mobile", "android", "ios", "app"), "Mobile App"),
            ),
            "Order & Trading": (
                (("holding", "position", "portfolio display"), "Position/Holding Display"),
                (("reject", "rejected"), "Rejected Order"),
                (("wrong execution", "incorrect execution", "wrong price"), "Wrong Execution"),
            ),
            "Funds": (
                (("withdraw", "payout"), "Withdrawal/Payout"),
                (("ledger", "mismatch"), "Ledger Mismatch"),
                (("not reflect", "not credited"), "Fund Addition/Not Reflecting"),
            ),
        }
        subcategory = next(
            (name for words, name in rules.get(primary, ()) if any(contains_keyword(text, word) for word in words)),
            allowed[-1],
        )
    return item.model_copy(update={"primary_category": primary, "subcategory": subcategory})


def requirement_is_issue_action(requirement: RequirementItem, issues: list[IssueItem]) -> bool:
    """Reject restoration/investigation tasks duplicated as standalone requirements."""
    text = normal(" ".join((requirement.description, requirement.recommended_action, requirement.expected_outcome)))
    words = set(text.split())
    if words & FEATURE_VERBS:
        return False
    if not words & CORRECTIVE_VERBS:
        return False
    for issue in issues:
        issue_text = " ".join((
            issue.standard_title, issue.description, issue.client_statement,
            issue.recommended_action, issue.product_platform,
        ))
        if semantic_similarity(text, issue_text) >= 0.22 or semantic_tokens(text) & semantic_tokens(issue_text):
            return True
    return False


def reconcile_call_intelligence(intelligence: CallIntelligence) -> CallIntelligence:
    """Apply deterministic guardrails after extraction and before ledger writes."""
    issues = [canonical_issue_taxonomy(item) for item in intelligence.issues]
    requirements = [
        item.model_copy(update={"category": canonical_category(item.category, item.description)})
        for item in intelligence.requirements
        if not requirement_is_issue_action(item, issues)
    ]
    interests = [
        item.model_copy(update={"category": canonical_category(item.category, item.description)})
        for item in intelligence.interests
    ]
    return intelligence.model_copy(update={
        "issues": issues, "requirements": requirements, "interests": interests,
    })


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
            created_at TEXT NOT NULL, completed_at TEXT,
            response_id TEXT, input_tokens INTEGER DEFAULT 0,
            cached_input_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
            processing_seconds REAL DEFAULT 0, estimated_cost_usd REAL DEFAULT 0,
            escalated_to_terra INTEGER DEFAULT 0, escalation_status TEXT,
            escalation_reasons TEXT
        );
        CREATE TABLE IF NOT EXISTS intelligence_extraction_attempts (
            attempt_id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL,
            call_version_id TEXT NOT NULL, attempt_sequence INTEGER NOT NULL,
            model_name TEXT NOT NULL, model_role TEXT NOT NULL, status TEXT NOT NULL,
            error_message TEXT, response_id TEXT, output_json TEXT,
            input_tokens INTEGER DEFAULT 0, cached_input_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0, total_tokens INTEGER DEFAULT 0,
            processing_seconds REAL DEFAULT 0, estimated_cost_usd REAL DEFAULT 0,
            created_at TEXT NOT NULL, UNIQUE(extraction_id, attempt_sequence)
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
    extraction_columns = {row[1] for row in con.execute("PRAGMA table_info(intelligence_extractions)")}
    for name, declaration in {
        "response_id": "TEXT", "input_tokens": "INTEGER DEFAULT 0",
        "cached_input_tokens": "INTEGER DEFAULT 0", "output_tokens": "INTEGER DEFAULT 0",
        "cache_write_tokens": "INTEGER DEFAULT 0",
        "reasoning_tokens": "INTEGER DEFAULT 0", "total_tokens": "INTEGER DEFAULT 0",
        "processing_seconds": "REAL DEFAULT 0",
        "estimated_cost_usd": "REAL DEFAULT 0",
        "escalated_to_terra": "INTEGER DEFAULT 0", "escalation_status": "TEXT",
        "escalation_reasons": "TEXT",
    }.items():
        if name not in extraction_columns:
            con.execute(f"ALTER TABLE intelligence_extractions ADD COLUMN {name} {declaration}")


def save_extraction_attempts(
    con: sqlite3.Connection, extraction_id: str, call_version_id: str,
    attempts: list[ExtractionAttempt],
) -> None:
    con.execute("DELETE FROM intelligence_extraction_attempts WHERE extraction_id=?", (extraction_id,))
    for sequence, attempt in enumerate(attempts, 1):
        con.execute(
            """INSERT INTO intelligence_extraction_attempts VALUES (
            ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )""",
            (
                stable_ident("ATT", extraction_id, sequence), extraction_id, call_version_id,
                sequence, attempt.model_name, attempt.model_role, attempt.status,
                attempt.error_message, attempt.response_id, attempt.output_json,
                attempt.input_tokens, attempt.cached_input_tokens, attempt.cache_write_tokens,
                attempt.output_tokens, attempt.reasoning_tokens, attempt.total_tokens,
                attempt.processing_seconds, attempt.estimated_cost_usd, now_text(),
            ),
        )


def client_identity(row: dict[str, Any]) -> tuple[str, str]:
    return clean(row.get("matched_client_code")), clean(row.get("lead_number"))


def identity_where(client_code: str, lead_number: str) -> tuple[str, tuple[str, ...]]:
    if client_code and lead_number:
        return "(client_code=? OR lead_number=?)", (client_code, lead_number)
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
        score = semantic_similarity(row.get(description_field), description)
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
        (stable_ident("HIST", source_type, source_id, call_id, event_type, previous, new),
         source_type, source_id, client_code, call_id, event_date,
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
    candidate_id = stable_ident(
        "ISS", lead or client_code, item.primary_category, item.subcategory,
        item.product_platform, item.standard_title,
    )
    if not match:
        by_id = table_rows(con, "SELECT * FROM issue_ledger WHERE issue_id=?", (candidate_id,))
        match = by_id[0] if by_id else None
    owner = TAXONOMY_OWNERS.get(
        (item.primary_category, item.subcategory),
        clean(item.assigned_team) or DEFAULT_OWNERS.get(item.primary_category, "Customer Service/RM"),
    )
    severity = item.severity if item.severity in ISSUE_SLA_DAYS else "Medium"
    has_client = bool(clean(item.client_confirmation))
    # Phase 2 never accepts AI-authored system validation. A future deterministic
    # transaction validator may supply this signal outside the extraction schema.
    has_system = False
    old_status = clean(match.get("current_status")) if match else ""
    new_status, event_type = issue_status(old_status, item.status_signal, has_client, has_system)
    closed = event_date if new_status == "Closed" else ""
    evidence = clean(item.resolution_evidence) or clean(item.client_statement)
    resolution = clean(item.resolution)
    if match:
        issue_id = match["issue_id"]
        reopened = int(match.get("reopened_count") or 0) + (1 if event_type == "Reopened" else 0)
        con.execute(
            """UPDATE issue_ledger SET client_code=?,lead_number=?,latest_call_id=?,latest_mention_date=?,repeat_count=?,
            severity=?,client_impact=?,current_status=?,assigned_team=?,resolution=?,
            resolution_evidence=?,client_confirmation=?,closed_date=?,reopened_count=?,updated_at=?
            WHERE issue_id=?""",
            (client_code or match.get("client_code", ""), lead or match.get("lead_number", ""),
             call["call_unique_id"], event_date, int(match.get("repeat_count") or 1) + 1,
             severity, item.client_impact, new_status, owner, resolution or match.get("resolution", ""),
             evidence or match.get("resolution_evidence", ""),
             clean(item.client_confirmation) or match.get("client_confirmation", ""), closed,
             reopened, now_text(), issue_id),
        )
    else:
        issue_id = candidate_id
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
                         threshold=0.55)
    candidate_id = stable_ident("REQ", lead or client_code, item.category, item.description)
    if not match:
        by_id = table_rows(con, "SELECT * FROM requirement_ledger WHERE requirement_id=?", (candidate_id,))
        match = by_id[0] if by_id else None
    old = clean(match.get("current_status")) if match else ""
    status, event = requirement_status(old, item)
    owner = clean(item.assigned_team) or DEFAULT_OWNERS.get(item.category, "Customer Service/RM")
    due = parse_date(item.due_date, (datetime.fromisoformat(event_date[:10]) + timedelta(days=3)).date().isoformat())
    closed = event_date if status == "Closed" else ""
    if match:
        record_id = match["requirement_id"]
        con.execute(
            """UPDATE requirement_ledger SET client_code=?,lead_number=?,latest_call_id=?,latest_mention_date=?,due_date=?,
            mention_count=?,current_status=?,assigned_team=?,completion_evidence=?,
            client_confirmation=?,closed_date=?,updated_at=? WHERE requirement_id=?""",
            (client_code or match.get("client_code", ""), lead or match.get("lead_number", ""),
             call["call_unique_id"], event_date, due, int(match.get("mention_count") or 1) + 1,
             status, owner, clean(item.completion_evidence) or match.get("completion_evidence", ""),
             clean(item.client_confirmation) or match.get("client_confirmation", ""), closed,
             now_text(), record_id),
        )
    else:
        record_id = candidate_id
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
    progression = {"New": 0, "Exploring": 1, "Interested": 2, "Ready to Act": 3, "Converted": 4}
    if old in progression and stage in progression and progression[old] > progression[stage]:
        stage = old
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
    candidate_id = stable_ident(
        "INT", lead or client_code, item.category, item.product_instrument,
        item.description,
    )
    if not match:
        by_id = table_rows(con, "SELECT * FROM interest_ledger WHERE interest_id=?", (candidate_id,))
        match = by_id[0] if by_id else None
    old = clean(match.get("current_status")) if match else ""
    status, event = interest_status(old, item)
    followup = (datetime.fromisoformat(event_date[:10]) + timedelta(days=3)).date().isoformat()
    action_required = "Yes" if item.action_disposition in {"Action Required", "Follow-up Later", "Nurture", "Monitor"} else "No"
    if match:
        record_id = match["interest_id"]
        con.execute(
            """UPDATE interest_ledger SET client_code=?,lead_number=?,latest_call_id=?,interest_strength=?,intent_stage=?,
            supporting_client_statement=?,latest_mention_date=?,mention_count=?,current_status=?,
            recommended_action=?,action_required=?,next_follow_up_date=?,updated_at=?
            WHERE interest_id=?""",
            (client_code or match.get("client_code", ""), lead or match.get("lead_number", ""),
             call["call_unique_id"], item.strength, item.intent_stage,
             item.client_statement or match.get("supporting_client_statement", ""), event_date,
             int(match.get("mention_count") or 1) + 1, status,
             item.recommended_action or match.get("recommended_action", ""), action_required,
             followup, now_text(), record_id),
        )
    else:
        record_id = candidate_id
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
    existing_action = table_rows(
        con, "SELECT action_id FROM action_register WHERE source_type='Interest' AND source_record_id=?",
        (record_id,),
    )
    if action_required == "Yes" or existing_action:
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
            """UPDATE action_register SET client_code=?,lead_number=?,source_call_id=?,latest_mention_date=?,category=?,
            subcategory=?,product_platform=?,item_summary=?,client_statement=?,current_status=?,
            action_disposition=?,priority=?,recommended_action=?,assigned_team=?,due_date=?,
            next_follow_up_date=?,previous_action=?,success_measure=?,closure_evidence=?,
            closed_date=?,repeat_count=?,latest_call_summary=?,updated_at=? WHERE action_id=?""",
            (client_code or old.get("client_code", ""), lead or old.get("lead_number", ""),
             call["call_unique_id"], event_date, category, subcategory, product, summary,
             statement, status, disposition, priority, recommendation, team, due_date,
             due_date, previous, success_measure, closure_evidence, closed_date, repeat_count,
             call_summary, now_text(), old["action_id"]),
        )
        return
    values = (
        stable_ident("ACT", source_type, record_id), client_code, lead, "", source_type,
        record_id, call["call_unique_id"],
        event_date, event_date, category, subcategory, product, summary, statement, "",
        status, disposition, priority, recommendation, team, "", due_date, due_date, 0,
        "", "", success_measure, "", closure_evidence, closed_date, repeat_count, "None",
        call_summary, now_text(),
    )
    con.execute(
        f"INSERT INTO action_register VALUES ({','.join('?' for _ in values)})", values
    )


def call_payload(
    con: sqlite3.Connection, call: dict[str, Any],
    client_context: dict[str, dict[str, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
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
    context = client_context or {"lead": {}, "client": {}}
    facts = context.get("client", {}).get(client_code, {}) if client_code else {}
    if not facts and lead:
        facts = context.get("lead", {}).get(lead, {})
    return {
        "call": relevant,
        "client_identity": {"client_code": client_code, "lead_number": lead},
        "client_360_facts": facts,
        "existing_open_items": open_items,
    }


@dataclass
class Phase2Counts:
    processed: int = 0
    skipped: int = 0
    eligible: int = 0
    deferred: int = 0
    failed: int = 0
    interests: int = 0
    requirements: int = 0
    issues: int = 0
    terra_reviews: int = 0
    terra_failures: int = 0


def rebuild_ledgers(con: sqlite3.Connection, run_id: str) -> None:
    """Replay only currently eligible Sarthi 360 extractions into operational ledgers.

    Successful historical extractions remain in the technical audit tables, but a
    call can affect interests, requirements, issues or actions only while its
    current match has both a lead number and a client code.
    """
    rows = table_rows(
        con,
        """SELECT cv.*, ie.output_json
        FROM call_versions cv
        JOIN intelligence_extractions ie ON ie.call_version_id=cv.call_version_id
        WHERE ie.status='Success' AND TRIM(ie.output_json)<>''
          AND cv.client_match_status='Matched'
          AND TRIM(COALESCE(cv.matched_client_code,''))<>''
          AND TRIM(COALESCE(cv.lead_number,''))<>''
          AND cv.call_version_id=(
              SELECT cv2.call_version_id
              FROM call_versions cv2
              JOIN intelligence_extractions ie2 ON ie2.call_version_id=cv2.call_version_id
              WHERE cv2.call_unique_id=cv.call_unique_id
                AND ie2.status='Success' AND TRIM(ie2.output_json)<>''
                AND cv2.client_match_status='Matched'
                AND TRIM(COALESCE(cv2.matched_client_code,''))<>''
                AND TRIM(COALESCE(cv2.lead_number,''))<>''
              ORDER BY cv2.version_number DESC
              LIMIT 1
          )
        ORDER BY cv.conversation_timestamp, cv.call_unique_id""",
    )
    con.execute("DELETE FROM ledger_history")
    con.execute("DELETE FROM action_register")
    con.execute("DELETE FROM interest_ledger")
    con.execute("DELETE FROM requirement_ledger")
    con.execute("DELETE FROM issue_ledger")
    for call in rows:
        intelligence = reconcile_call_intelligence(CallIntelligence.model_validate_json(call["output_json"]))
        raw = json.loads(call["row_json"])
        event_date = parse_date(raw.get("Conversation Timestamp"), now_text())
        for item in intelligence.interests:
            upsert_interest(con, item, call, event_date, intelligence.call_summary, run_id)
        for item in intelligence.requirements:
            upsert_requirement(con, item, call, event_date, intelligence.call_summary, run_id)
        for item in intelligence.issues:
            upsert_issue(con, item, call, event_date, intelligence.call_summary, run_id)
    con.commit()


def run_phase2(
    con: sqlite3.Connection, run_id: str, extractor: Extractor | None = None,
    max_calls: int | None = None,
    client_context: dict[str, dict[str, dict[str, Any]]] | None = None,
    sarthi_360_only: bool = True,
    checkpoint: Callable[[Phase2Counts], None] | None = None,
) -> Phase2Counts:
    """Interpret every eligible pending call, committing work in automatic batches.

    ``max_calls`` is retained as a backwards-compatible command/API name, but it
    now controls batch size rather than truncating the job.  A single Receiver
    run drains the complete eligible queue.  The detached worker can still
    cancel the child process between calls, and each successful call is already
    committed independently for safe restart.
    """
    ensure_schema(con)
    active_prompt_version = system_prompt_version()
    all_pending = table_rows(
        con,
        """SELECT cv.* FROM call_versions cv
        LEFT JOIN intelligence_extractions ie ON ie.call_version_id=cv.call_version_id
        WHERE cv.is_latest=1 AND cv.processing_status IN ('Inserted','Updated')
          AND (ie.call_version_id IS NULL OR ie.status='Failed' OR ie.prompt_version<>?)
        -- Process recent calls first so current critical incidents are not blocked
        -- behind a large historical backlog. Ledger replay remains chronological.
        ORDER BY cv.conversation_timestamp DESC, cv.call_version_id DESC""",
        (active_prompt_version,),
    )
    if sarthi_360_only:
        pending = [
            call for call in all_pending
            if clean(call.get("client_match_status")) == "Matched"
            and clean(call.get("matched_client_code"))
            and clean(call.get("lead_number"))
        ]
    else:
        pending = all_pending
    eligible = len(pending)
    counts = Phase2Counts(
        skipped=len(all_pending) - eligible,
        eligible=eligible,
        deferred=0,
    )
    rebuild_ledgers(con, run_id)
    counts.deferred = eligible
    if checkpoint is not None:
        checkpoint(counts)
    if extractor is None:
        if not os.getenv("OPENAI_API_KEY"):
            return counts
        if env_flag("SARTHI_AI_HYBRID_ENABLED", True):
            extractor = HybridOpenAIExtractor()
        else:
            extractor = OpenAIExtractor()
    batch_size = max(1, int(max_calls)) if max_calls is not None else max(1, eligible)
    for offset in range(0, eligible, batch_size):
        batch = pending[offset:offset + batch_size]
        print(
            f"AI automatic batch {offset // batch_size + 1}/"
            f"{(eligible + batch_size - 1) // batch_size}: "
            f"calls {offset + 1}-{offset + len(batch)} of {eligible}",
            flush=True,
        )
        for call in batch:
            _process_pending_call(
                con, call, run_id, extractor, active_prompt_version,
                client_context, counts,
            )
        counts.deferred = max(0, eligible - (offset + len(batch)))
        rebuild_ledgers(con, run_id)
        if checkpoint is not None:
            checkpoint(counts)
    rebuild_ledgers(con, run_id)
    return counts


def _process_pending_call(
    con: sqlite3.Connection,
    call: dict[str, Any],
    run_id: str,
    extractor: Extractor,
    active_prompt_version: str,
    client_context: dict[str, dict[str, dict[str, Any]]] | None,
    counts: Phase2Counts,
) -> None:
    """Interpret and persist one pending call within an automatic batch."""

    # Each call is committed independently so cancellation or a machine restart
    # resumes from the first genuinely unfinished call on the next Daily Run.
    # A scheduled call is a populated database row; ignore a defensive empty row.
    if call:
        extraction_id = ident("EXT")
        created = now_text()
        payload = call_payload(con, call, client_context)
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
                    """INSERT INTO intelligence_extractions (
                    extraction_id,call_version_id,call_unique_id,lead_number,client_code,
                    model_name,prompt_version,input_hash,output_json,status,error_message,
                    processing_run_id,created_at,completed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (extraction_id, call["call_version_id"], call["call_unique_id"],
                     call.get("lead_number", ""), call.get("matched_client_code", ""),
                     extractor.model_name, active_prompt_version, input_hash, "", "Processing", "",
                     run_id, created, ""),
                )
            extracted = extractor.extract(payload)
            envelope = extracted if isinstance(extracted, ExtractionEnvelope) else ExtractionEnvelope(extracted)
            intelligence = reconcile_call_intelligence(envelope.intelligence)
            final_model_name = envelope.model_name or extractor.model_name
            raw = json.loads(call["row_json"])
            event_date = parse_date(raw.get("Conversation Timestamp"), created)
            for item in intelligence.interests:
                upsert_interest(con, item, call, event_date, intelligence.call_summary, run_id)
            for item in intelligence.requirements:
                upsert_requirement(con, item, call, event_date, intelligence.call_summary, run_id)
            for item in intelligence.issues:
                upsert_issue(con, item, call, event_date, intelligence.call_summary, run_id)
            call_interests = len(intelligence.interests)
            call_requirements = len(intelligence.requirements)
            call_issues = len(intelligence.issues)
            completed = now_text()
            con.execute(
                """UPDATE intelligence_extractions SET model_name=?,prompt_version=?,input_hash=?,
                output_json=?,status='Success',error_message='',processing_run_id=?,completed_at=?,
                response_id=?,input_tokens=?,cached_input_tokens=?,cache_write_tokens=?,output_tokens=?,
                reasoning_tokens=?,total_tokens=?,processing_seconds=?,estimated_cost_usd=?,
                escalated_to_terra=?,escalation_status=?,escalation_reasons=?
                WHERE extraction_id=?""",
                (final_model_name, active_prompt_version, input_hash,
                 intelligence.model_dump_json(), run_id, completed, envelope.response_id,
                 envelope.input_tokens, envelope.cached_input_tokens, envelope.cache_write_tokens,
                 envelope.output_tokens,
                 envelope.reasoning_tokens, envelope.total_tokens, envelope.processing_seconds,
                 envelope.estimated_cost_usd, int(envelope.escalated_to_terra),
                 envelope.escalation_status, json.dumps(envelope.escalation_reasons, ensure_ascii=False),
                 extraction_id),
            )
            save_extraction_attempts(
                con, extraction_id, call["call_version_id"], envelope.attempts,
            )
            con.execute(
                "UPDATE processing_log SET ai_result='Success',ledger_result='Updated' WHERE detected_version_id=?",
                (call["call_version_id"],),
            )
            con.commit()
            counts.processed += 1
            counts.interests += call_interests
            counts.requirements += call_requirements
            counts.issues += call_issues
            counts.terra_reviews += int(envelope.escalated_to_terra)
            counts.terra_failures += int(envelope.escalation_status.startswith("Terra Failed"))
        except Exception as exc:
            con.rollback()
            if existing and existing[0].get("status") == "Success":
                con.execute(
                    """UPDATE intelligence_extractions SET error_message=?,processing_run_id=?,
                    completed_at=? WHERE extraction_id=?""",
                    (f"Upgrade attempt failed: {exc}", run_id, now_text(), extraction_id),
                )
            else:
                con.execute(
                    """INSERT OR REPLACE INTO intelligence_extractions (
                    extraction_id,call_version_id,call_unique_id,lead_number,client_code,
                    model_name,prompt_version,input_hash,output_json,status,error_message,
                    processing_run_id,created_at,completed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (extraction_id, call["call_version_id"], call["call_unique_id"],
                     call.get("lead_number", ""), call.get("matched_client_code", ""),
                     extractor.model_name, active_prompt_version, input_hash, "", "Failed", str(exc),
                     run_id, created, now_text()),
                )
            con.execute(
                "UPDATE processing_log SET ai_result='Failed',ledger_result='Error',error_message=? WHERE detected_version_id=?",
                (str(exc), call["call_version_id"]),
            )
            con.commit()
            counts.failed += 1


def refresh_action_controls(con: sqlite3.Connection, as_of: datetime | None = None) -> None:
    """Refresh deterministic escalation without altering team-entered action fields."""
    today = (as_of or datetime.now()).date()
    rows = table_rows(con, "SELECT * FROM action_register")
    for row in rows:
        due = clean(row.get("due_date"))[:10]
        escalation = "None"
        actionable = clean(row.get("action_disposition")) in {
            "Action Required", "Follow-up Later", "Nurture", "Monitor",
        }
        priority = clean(row.get("priority"))
        if (actionable and due and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)
                and clean(row.get("closed_date")) == ""):
            overdue = (today - datetime.fromisoformat(due).date()).days
            if (priority == "Critical" and overdue > 0) or (priority == "High" and overdue > 3) or overdue > 7:
                escalation = "Department Head"
            elif overdue > 0:
                escalation = "Team Lead"
        con.execute("UPDATE action_register SET escalation_level=?,updated_at=? WHERE action_id=?",
                    (escalation, now_text(), row["action_id"]))
    con.commit()