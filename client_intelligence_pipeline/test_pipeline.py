"""End-to-end regression for Phase 1 ingestion and Client 360 context."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from run_pipeline import (
    client_context_lookup,
    client_lookup,
    connect_db,
    initialize,
    latest_client_360,
    process_files,
    query_frame,
    refresh_client_matches,
)


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        paths = initialize(root)
        pd.DataFrame([
            {
                "Client Code": "CLIENT001",
                "Lead Number": "1094906",
                "Customer Phone": "must-not-enter-ai-context",
                "Funds Collected": 50000,
                "Current Total Margin": 25000,
                "Executed Orders": 0,
                "Subscription Purchased": "No",
            }
        ]).to_excel(paths["client360"] / "Sarthi_New_Client_360.xlsx", sheet_name="Client Detail", index=False)
        pd.DataFrame([
            {
                "Conversation Timestamp": "2026-07-23 17:30:00",
                "Lead Number": "1094906",
                "Agent Email": "agent@bigul.co",
                "Duration": "0:06:31",
                "Summary": "Client wants an algo demo",
                "Conversation Recording Link": "https://example.test/calls/17844907",
            }
        ]).to_csv(paths["calls"] / "evaluated_calls.csv", index=False)

        client360, _ = latest_client_360(paths)
        context = client_context_lookup(client360)
        assert context["lead"]["1094906"]["Current Total Margin"] == "25000"
        assert "Customer Phone" not in context["lead"]["1094906"]

        con = connect_db(paths["state"] / "test.db")
        try:
            first = process_files(con, paths, {}, "RUN-1")
            second = process_files(con, paths, client_lookup(client360), "RUN-2")
            assert first["Inserted"] == 1 and first["SkippedFiles"] == 0
            assert second["Inserted"] == 0 and second["Duplicate"] == 0
            assert second["SkippedFiles"] == 1
            calls = query_frame(con, "SELECT * FROM call_versions")
            assert len(calls) == 1 and calls.iloc[0]["client_match_status"] == "No Client 360 Match"
            assert refresh_client_matches(con, client_lookup(client360)) == 1
            calls = query_frame(con, "SELECT * FROM call_versions")
            assert calls.iloc[0]["matched_client_code"] == "CLIENT001"
            assert con.execute(
                "SELECT COUNT(*) FROM ingested_files"
            ).fetchone()[0] == 1
        finally:
            con.close()
    print("PASS: Phase 1 ingestion, unchanged-file skip, matching, and privacy-minimised 360 context")


if __name__ == "__main__":
    main()
