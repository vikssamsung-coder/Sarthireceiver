"""Regression test for deterministic Phase 2 ledger reconciliation."""
from __future__ import annotations

import tempfile
from pathlib import Path

from phase2_intelligence import (
    CallIntelligence, InterestItem, IssueItem, RequirementItem, run_phase2, table_rows,
)
from run_pipeline import connect_db, process_row


class FakeExtractor:
    model_name = "fake-structured-extractor"

    def extract(self, payload):
        summary = payload["call"].get("Summary", "")
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


def raw_call(timestamp: str, summary: str) -> dict:
    return {
        "Conversation Timestamp": timestamp,
        "Lead Number": "1094906",
        "Agent Email": "agent@bigul.co",
        "Duration": "0:06:31",
        "Summary": summary,
        "Conversation Recording Link": f"https://example.test/calls/{timestamp[-8:].replace(':', '')}",
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "phase2.db"
        source = Path(folder) / "calls.xlsx"
        source.touch()
        con = connect_db(db)
        try:
            clients = {"1094906": "CLIENT001"}
            process_row(con, raw_call("2026-07-23 17:30:00", "Strategies not showing; demo requested"),
                        source, 2, "RUN-1", clients)
            con.commit()
            first = run_phase2(con, "RUN-1", FakeExtractor())
            assert first.processed == 1 and first.issues == 1 and first.requirements == 1 and first.interests == 1
            assert len(table_rows(con, "SELECT * FROM action_register")) == 3

            process_row(con, raw_call("2026-07-24 11:00:00", "team says resolved"),
                        source, 3, "RUN-2", clients)
            con.commit()
            second = run_phase2(con, "RUN-2", FakeExtractor())
            issues = table_rows(con, "SELECT * FROM issue_ledger")
            assert second.processed == 1 and len(issues) == 1
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
        finally:
            con.close()
    print("PASS: Phase 2 extraction, matching, pending confirmation, closure, history, and unified actions")


if __name__ == "__main__":
    main()
