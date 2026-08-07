"""Regression coverage for the three complete Client Intelligence reports."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd

from phase2_intelligence import Phase2Counts
from run_pipeline import (
    REPORT_FILENAMES,
    build_rm_action_sheet,
    connect_db,
    initialize,
    write_workbook,
)


def main() -> None:
    actions = pd.DataFrame([
        {
            "Action ID": "ACT-INTERNAL-1", "Client Code": "CLIENT001",
            "Lead Number": "1001", "Client Name": "Test Client", "Source Type": "Issue",
            "Source Call ID": "CALL-1", "Latest Mention Date": "2026-08-06",
            "Category": "Algo/API", "Subcategory": "API outage", "Product/Platform": "API V2",
            "Item Summary": "API V2 down repeatedly", "Client Statement": "Third outage this week",
            "Priority": "Critical", "Recommended Action": "Restore service and call client",
            "Assigned Team": "Algo/API Support", "Assigned Employee": "RM One",
            "Due Date": "2026-08-06", "Next Follow-up Date": "2026-08-07",
            "Repeat Count": 3, "Days Open": 2, "SLA Status": "Overdue",
            "Latest Call Summary": "Client may close accounts",
        },
        {
            "Action ID": "ACT-INTERNAL-2", "Client Code": "CLIENT001",
            "Lead Number": "1001", "Client Name": "Test Client", "Source Type": "Requirement",
            "Source Call ID": "CALL-1", "Latest Mention Date": "2026-08-06",
            "Category": "API", "Subcategory": "Stability", "Product/Platform": "API V2",
            "Item Summary": "Stable API access", "Client Statement": "Needs stable Tradetron access",
            "Priority": "High", "Recommended Action": "Share RCA and prevention plan",
            "Assigned Team": "Algo/API Support", "Assigned Employee": "RM One",
            "Due Date": "2026-08-07", "Next Follow-up Date": "2026-08-07",
            "Repeat Count": 1, "Days Open": 2, "SLA Status": "Within SLA",
            "Latest Call Summary": "RCA requested",
        },
    ])
    timeline = pd.DataFrame([{
        "Call_Unique_ID": "CALL-1",
        "Conversation Recording Link": "https://example.test/calls/1",
    }])
    rm = build_rm_action_sheet(actions, timeline)
    assert len(rm) == 1
    assert rm.iloc[0]["Client Code"] == "CLIENT001"
    assert rm.iloc[0]["Open Action Count"] == 2
    assert "API V2 down repeatedly" in rm.iloc[0]["Issue Identified"]
    assert "Stable API access" in rm.iloc[0]["Requirement Identified"]
    assert rm.iloc[0]["Call Recording Link"] == "https://example.test/calls/1"
    assert "Action ID" not in rm.columns

    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        paths = initialize(root)
        con = connect_db(paths["state"] / "reports.db")
        try:
            counts = {
                "Inserted": 0, "Updated": 0, "Duplicate": 0, "Review": 0,
                "Error": 0, "SkippedFiles": 0, "RefreshedMatches": 0,
            }
            write_workbook(
                paths, con, pd.DataFrame(), None, counts, Phase2Counts(), "RUN-REPORT-TEST"
            )
        finally:
            con.close()

        for filename in REPORT_FILENAMES.values():
            path = paths["current"] / filename
            assert path.is_file(), f"Missing report: {filename}"
            assert "Report Metadata" in pd.ExcelFile(path).sheet_names

        rm_book = pd.ExcelFile(paths["current"] / REPORT_FILENAMES["rm"])
        management_book = pd.ExcelFile(paths["current"] / REPORT_FILENAMES["management"])
        intelligence_book = pd.ExcelFile(paths["current"] / REPORT_FILENAMES["intelligence"])
        assert {"RM Action Sheet", "Supporting Open Actions"}.issubset(rm_book.sheet_names)
        assert {"Executive Dashboard", "Issue Ranking", "Recurring Clients", "Friction Hotspots"}.issubset(management_book.sheet_names)
        assert {"Issue Ledger", "Requirement Ledger", "Interest Ledger", "Client Call Timeline"}.issubset(intelligence_book.sheet_names)

        manifest = json.loads(
            (paths["current"] / "Client_Intelligence_Output_Manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["processing_run_id"] == "RUN-REPORT-TEST"
        assert set(manifest["reports"]) == {"rm", "management", "intelligence"}
        archived = list(paths["archive"].glob("*.xlsx"))
        assert len(archived) == 3

    print("PASS: one-row-per-client RM output and all three complete current/archive reports")


if __name__ == "__main__":
    main()
