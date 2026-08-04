"""Regression test for deterministic Phase 2 ledger reconciliation."""
from __future__ import annotations

import tempfile
import sqlite3
from datetime import datetime
from pathlib import Path

from phase2_intelligence import (
    CallIntelligence, ExtractionAttempt, ExtractionEnvelope, HybridOpenAIExtractor,
    InterestItem, IssueItem, RequirementItem, ensure_schema,
    canonical_category, reconcile_call_intelligence, refresh_action_controls, run_phase2, table_rows,
    save_extraction_attempts, upsert_interest, upsert_issue, upsert_requirement,
)
from run_pipeline import connect_db, process_row


class FakeExtractor:
    model_name = "fake-structured-extractor"

    def __init__(self):
        self.last_payload = None

    def extract(self, payload):
        self.last_payload = payload
        summary = payload["call"].get("Summary", "")
        if "corrected no issue" in summary:
            return CallIntelligence(
                call_summary=summary, client_sentiment="Neutral",
                interests=[], requirements=[], issues=[],
            )
        if "confirmed fixed" in summary:
            return CallIntelligence(
                call_summary=summary, client_sentiment="Positive",
                issues=[IssueItem(
                    primary_category="Technical", subcategory="Feature Error",
                    product_platform="Mobile App", standard_title="Strategies not displaying",
                    description="Scalper Algo strategies are not visible in the mobile app",
                    severity="High", client_impact="Unable to Trade",
                    client_statement="It is fixed now", status_signal="ClientConfirmed",
                    resolution="Strategies became visible", client_confirmation="Client confirmed fixed",
                )],
            )
        if "team says resolved" in summary:
            return CallIntelligence(
                call_summary=summary, client_sentiment="Neutral",
                issues=[IssueItem(
                    primary_category="Technical", subcategory="Feature Error",
                    product_platform="Mobile App", standard_title="Strategies not displaying",
                    description="Scalper Algo strategies are not visible in the mobile app",
                    severity="High", client_impact="Unable to Trade",
                    status_signal="ResolvedReported", resolution="Team says configuration corrected",
                )],
            )
        return CallIntelligence(
            call_summary=summary, client_sentiment="Concerned",
            interests=[InterestItem(
                category="Algo/API", product_instrument="Scalper Algo",
                description="Client wants to understand and use Scalper Algo",
                strength="High", intent_stage="Ready to Act", client_statement="Show me Scalper Algo",
                recommended_action="Arrange assisted product walkthrough",
                action_disposition="Action Required",
            )],
            requirements=[RequirementItem(
                category="Algo/API", description="Client requested a follow-up demonstration",
                expected_outcome="Demo completed", commitment_made="Follow-up call promised",
                committed_by="Agent", assigned_team="Algo/API Support",
                recommended_action="Schedule demo", priority="High",
            )],
            issues=[IssueItem(
                primary_category="Technical", subcategory="Feature Error",
                product_platform="Mobile App", standard_title="Strategies not displaying",
                description="Scalper Algo strategies are not visible in the mobile app",
                severity="High", client_impact="Unable to Trade",
                client_statement="Strategies are not showing", status_signal="Mentioned",
                recommended_action="Verify account configuration and strategy entitlement",
            )],
        )


class FakeModelPass:
    def __init__(self, model_name: str, intelligence: CallIntelligence, fail: bool = False):
        self.model_name = model_name
        self.intelligence = intelligence
        self.fail = fail
        self.calls = 0

    def extract(self, payload):
        self.calls += 1
        if self.fail:
            raise RuntimeError("simulated Terra failure")
        role = "Luna First Pass" if "luna" in self.model_name else "Terra Review"
        attempt = ExtractionAttempt(
            model_name=self.model_name, model_role=role, response_id=f"resp-{self.calls}",
            output_json=self.intelligence.model_dump_json(), input_tokens=100,
            output_tokens=20, total_tokens=120, estimated_cost_usd=0.0001,
        )
        return ExtractionEnvelope(
            intelligence=self.intelligence, model_name=self.model_name,
            response_id=attempt.response_id, input_tokens=100, output_tokens=20,
            total_tokens=120, estimated_cost_usd=0.0001, attempts=[attempt],
        )


def raw_call(timestamp: str, summary: str) -> dict:
    return {
        "Conversation Timestamp": timestamp,
        "Lead Number": "1094906",
        "Agent Email": "agent@bigul.co",
        "Duration": "0:06:31",
        "Summary": summary,
        "Conversation Recording Link": f"https://example.test/calls/{timestamp[-8:].replace(':', '')}",
    }


def guardrail_checks() -> None:
    assert canonical_category("Email/WhatsApp", "WhatsApp message not received") == "Communication"
    extracted = CallIntelligence(
        call_summary="OTP failed and email OTP was requested", client_sentiment="Concerned",
        issues=[IssueItem(
            primary_category="Authentication / OTP", subcategory="OTP Delivery",
            standard_title="OTP not received", description="OTP not received on two numbers",
            recommended_action="Check logs and restore OTP delivery", severity="Critical",
        )],
        requirements=[
            RequirementItem(
                category="Authentication / OTP", description="Restore OTP delivery on two numbers",
                recommended_action="Investigate and fix OTP delivery",
            ),
            RequirementItem(
                category="Authentication / OTP", description="Enable email OTP as a backup to mobile OTP",
                expected_outcome="Email OTP is available as another channel",
            ),
        ],
    )
    guarded = reconcile_call_intelligence(extracted)
    assert len(guarded.requirements) == 1
    assert guarded.requirements[0].category == "Technical"
    assert guarded.issues[0].primary_category == "Technical"
    assert guarded.issues[0].subcategory == "Login/OTP"

    con = sqlite3.connect(":memory:")
    ensure_schema(con)
    call1 = {"matched_client_code": "", "lead_number": "1037323", "call_unique_id": "CALL-1"}
    call2 = {"matched_client_code": "", "lead_number": "1037323", "call_unique_id": "CALL-2"}
    first = guarded.requirements[0]
    second = RequirementItem(
        category="Technical",
        description="Enable OTP delivery through email in addition to mobile for the entire partner group",
        expected_outcome="Partner users also receive email OTP",
    )
    upsert_requirement(con, first, call1, "2026-07-01", "First OTP call", "RUN-G1")
    upsert_requirement(con, second, call2, "2026-07-02", "Second OTP call", "RUN-G2")
    requirements = table_rows(con, "SELECT * FROM requirement_ledger")
    assert len(requirements) == 1 and requirements[0]["mention_count"] == 2
    assert requirements[0]["latest_call_id"] == "CALL-2"

    issue1 = guarded.issues[0]
    issue2 = issue1.model_copy(update={"description": "Authentication messages never arrived after repeated attempts"})
    upsert_issue(con, issue1, call1, "2026-07-01", "First issue call", "RUN-G1")
    upsert_issue(con, issue2, call2, "2026-07-02", "Second issue call", "RUN-G2")
    issues = table_rows(con, "SELECT * FROM issue_ledger")
    assert len(issues) == 1 and issues[0]["repeat_count"] == 2

    no_action_interest = InterestItem(
        category="Algo/API", product_instrument="Bigul API V2",
        description="Client asked which environment to use",
        action_disposition="No Immediate Action", intent_stage="Exploring",
    )
    upsert_interest(con, no_action_interest, call1, "2026-07-01", "Clarification answered", "RUN-G1")
    assert not table_rows(con, "SELECT * FROM action_register WHERE source_type='Interest'")

    con.execute(
        "UPDATE action_register SET priority='Low',action_disposition='Action Required',due_date='2026-07-01' "
        "WHERE source_type='Requirement'"
    )
    refresh_action_controls(con, datetime.fromisoformat("2026-07-05"))
    low = table_rows(con, "SELECT escalation_level FROM action_register WHERE source_type='Requirement'")
    assert low and low[0]["escalation_level"] == "Team Lead"
    con.close()


def hybrid_checks() -> None:
    routine = CallIntelligence(
        call_summary="The client accepted the navigation guidance.",
        client_sentiment="Neutral", assessment_confidence=0.95,
    )
    terra_final = CallIntelligence(
        call_summary="Third API V2 outage affected multiple users and requires immediate restoration.",
        client_sentiment="Extremely frustrated", assessment_confidence=0.98,
        issues=[IssueItem(
            primary_category="Algo/API", subcategory="Activation/API/Strategy/Execution",
            standard_title="Recurring API V2 outage", description="Third outage affected multiple users",
            severity="Critical", client_impact="Unable to Trade",
        )],
    )
    luna = FakeModelPass("gpt-5.6-luna", routine)
    terra = FakeModelPass("gpt-5.6-terra", terra_final)
    hybrid = HybridOpenAIExtractor(
        luna_extractor=luna, terra_extractor=terra, confidence_threshold=0.80,
    )

    routine_result = hybrid.extract({"call": {"Summary": "Navigation guidance was completed."}})
    assert not routine_result.escalated_to_terra and terra.calls == 0
    assert routine_result.model_name == "gpt-5.6-luna" and len(routine_result.attempts) == 1

    critical_result = hybrid.extract({
        "call": {"Summary": "Third API outage this week; multiple users are unable to trade."}
    })
    assert critical_result.escalated_to_terra and critical_result.escalation_status == "Terra Completed"
    assert critical_result.model_name == "gpt-5.6-terra" and len(critical_result.attempts) == 2
    assert critical_result.total_tokens == 240

    low_confidence = routine.model_copy(update={
        "assessment_confidence": 0.60, "needs_terra_review": True,
        "review_reasons": ["Conflicting statements"],
    })
    fallback = HybridOpenAIExtractor(
        luna_extractor=FakeModelPass("gpt-5.6-luna", low_confidence),
        terra_extractor=FakeModelPass("gpt-5.6-terra", terra_final, fail=True),
        confidence_threshold=0.80,
    ).extract({"call": {"Summary": "The evidence is contradictory."}})
    assert fallback.escalated_to_terra
    assert fallback.escalation_status == "Terra Failed — Luna Fallback"
    assert fallback.model_name == "gpt-5.6-luna"
    assert len(fallback.attempts) == 2 and fallback.attempts[-1].status == "Failed"

    con = sqlite3.connect(":memory:")
    ensure_schema(con)
    save_extraction_attempts(con, "EXT-1", "CALL-VERSION-1", critical_result.attempts)
    attempts = table_rows(
        con, "SELECT * FROM intelligence_extraction_attempts ORDER BY attempt_sequence"
    )
    assert [row["model_role"] for row in attempts] == ["Luna First Pass", "Terra Review"]
    assert sum(row["total_tokens"] for row in attempts) == critical_result.total_tokens
    con.close()


def main() -> None:
    guardrail_checks()
    hybrid_checks()
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "phase2.db"
        source = Path(folder) / "calls.xlsx"
        source.touch()
        con = connect_db(db)
        try:
            clients = {"1094906": "CLIENT001"}
            extractor = FakeExtractor()
            process_row(con, raw_call("2026-07-23 17:30:00", "Strategies not showing; demo requested"),
                        source, 2, "RUN-1", clients)
            con.commit()
            context = {
                "lead": {"1094906": {"Current Total Margin": 25000, "Executed Orders": 0}},
                "client": {},
            }
            first = run_phase2(con, "RUN-1", extractor, client_context=context)
            assert first.processed == 1 and first.issues == 1 and first.requirements == 1 and first.interests == 1
            assert len(table_rows(con, "SELECT * FROM action_register")) == 3
            assert extractor.last_payload["client_360_facts"]["Current Total Margin"] == 25000

            unmatched = raw_call("2026-07-23 18:30:00", "Unmatched call must not use AI")
            unmatched["Lead Number"] = "9999999"
            process_row(con, unmatched, source, 22, "RUN-1B", clients)
            con.commit()
            scoped = run_phase2(con, "RUN-1B", FakeExtractor())
            assert scoped.processed == 0 and scoped.skipped == 1
            assert scoped.eligible == 0 and scoped.deferred == 0

            process_row(con, raw_call("2026-07-24 11:00:00", "team says resolved"),
                        source, 3, "RUN-2", clients)
            con.commit()
            second = run_phase2(con, "RUN-2", FakeExtractor())
            issues = table_rows(con, "SELECT * FROM issue_ledger")
            assert second.processed == 1 and len(issues) == 1
            assert issues[0]["client_code"] == "CLIENT001"
            assert issues[0]["current_status"] == "Resolved Pending Confirmation"
            assert issues[0]["repeat_count"] == 2

            process_row(con, raw_call("2026-07-25 12:00:00", "client confirmed fixed"),
                        source, 4, "RUN-3", clients)
            con.commit()
            third = run_phase2(con, "RUN-3", FakeExtractor())
            issues = table_rows(con, "SELECT * FROM issue_ledger")
            actions = table_rows(con, "SELECT * FROM action_register WHERE source_type='Issue'")
            assert third.processed == 1 and len(issues) == 1
            assert issues[0]["current_status"] == "Closed"
            assert issues[0]["client_confirmation"] == "Client confirmed fixed"
            assert actions[0]["closed_date"] == "2026-07-25"
            assert len(table_rows(con, "SELECT * FROM ledger_history WHERE source_type='Issue'")) == 3

            con.execute("DELETE FROM intelligence_extractions")
            con.execute("DELETE FROM ledger_history")
            con.execute("DELETE FROM action_register")
            con.execute("DELETE FROM interest_ledger")
            con.execute("DELETE FROM requirement_ledger")
            con.execute("DELETE FROM issue_ledger")
            con.execute("DELETE FROM call_versions")
            con.commit()
            process_row(con, raw_call("2026-07-26 09:00:00", "Strategies not showing"),
                        source, 5, "RUN-4", clients)
            con.commit()
            run_phase2(con, "RUN-4", FakeExtractor())
            assert len(table_rows(con, "SELECT * FROM issue_ledger")) == 1
            process_row(con, raw_call("2026-07-26 09:00:00", "corrected no issue"),
                        source, 5, "RUN-5", clients)
            con.commit()
            run_phase2(con, "RUN-5", FakeExtractor())
            assert not table_rows(con, "SELECT * FROM issue_ledger")
        finally:
            con.close()
    print("PASS: Phase 2 extraction, matching, pending confirmation, closure, history, and unified actions")


if __name__ == "__main__":
    main()
