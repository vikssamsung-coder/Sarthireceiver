"""Regression tests for the Call Analysis controlled Client 360 population."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from call_analysis_clients_360 import (
    build_reports,
    extract_call_leads,
    map_leads_to_clients,
    write_excel,
)


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        calls = root / "calls"
        calls.mkdir()
        pd.DataFrame([
            {"Lead No": "1001", "Summary": "first call"},
            {"Lead No": "1001", "Summary": "second call"},
            {"Lead No": "1002", "Summary": "unmapped"},
            {"Lead No": "1003", "Summary": "mapped to same client"},
            {"Lead No": "", "Summary": "no lead"},
        ]).to_csv(calls / "calls.csv", index=False)
        pd.DataFrame([{"Summary": "no identifier"}]).to_csv(
            calls / "bad.csv", index=False
        )
        leads_path = root / "Leads.csv"
        pd.DataFrame([
            {"Lead Number": "1001", "Client Code": "CLIENT-A", "Modified On": "2026-08-01"},
            {"Lead Number": "1001", "Client Code": "OLD-A", "Modified On": "2026-07-01"},
            {"Lead Number": "1002", "Client Code": "", "Modified On": "2026-08-01"},
            {"Lead Number": "1003", "Client Code": "CLIENT-A", "Modified On": "2026-08-02"},
        ]).to_csv(leads_path, index=False)

        call_leads, file_audit = extract_call_leads(calls)
        assert len(call_leads) == 3
        assert int(call_leads.loc[call_leads["Lead Number"] == "1001", "Call Count"].iloc[0]) == 2
        assert "Skipped" in file_audit.loc[file_audit["Source File"] == "bad.csv", "Status"].iloc[0]

        mapping = map_leads_to_clients(call_leads, leads_path)
        first = mapping[mapping["Lead Number"] == "1001"].iloc[0]
        assert first["Client Code"] == "CLIENT-A"
        assert first["Match Status"] == "Matched - multiple client codes"
        assert mapping.loc[mapping["Lead Number"] == "1002", "Client Code"].iloc[0] == ""

        metrics = pd.DataFrame([{
            "client_code": "CLIENT-A", "client_name": "Client A",
            "opening_date": pd.Timestamp("2026-01-02"), "account_status": "Active",
            "funds_received_till_date": 50000, "funds_as_of_date": pd.Timestamp("2026-08-04"),
            "total_stock": 125000, "margin_date": pd.Timestamp("2026-08-04"),
            "first_trade_date": pd.Timestamp("2026-01-05"),
            "last_trade_date": pd.Timestamp("2026-08-03"), "total_orders": 20,
            "executed_orders": 15, "executed_order_pct": 0.75,
            "orders_as_of_date": pd.Timestamp("2026-08-04"),
        }])
        detail, summary, audit, _, run_summary = build_reports(
            mapping, metrics, file_audit, leads_path, calls
        )
        assert len(detail) == 2  # both valid lead-client pairs are retained for AI matching
        assert detail["Client Code"].nunique() == 1
        assert len(summary) == 1  # business summary remains one row per client
        assert summary.iloc[0]["Lead Numbers"] == "1001, 1003"
        assert summary.iloc[0]["Call Count"] == 3
        assert summary.iloc[0]["Executed Order %"] == 0.75
        assert audit.loc[audit["Lead Number"] == "1002", "Match Status"].iloc[0] == "Client Code missing in Leads.csv"
        assert "Executed Orders / Total Orders" in str(
            run_summary.loc[run_summary["Metric"] == "Executed Order % Rule", "Value"].iloc[0]
        )
        output = root / "Sarthi_Call_Analysis_Client_360.xlsx"
        write_excel(output, detail, summary, audit, file_audit, run_summary)
        assert output.is_file()
        book = pd.ExcelFile(output)
        assert book.sheet_names == [
            "Client Detail", "Client Summary", "Lead Mapping Audit", "File Audit", "Run Summary"
        ]
        written_detail = pd.read_excel(output, sheet_name="Client Detail")
        assert list(written_detail.columns) == list(detail.columns)
        assert len(written_detail) == 2
    print("PASS: Call Analysis lead scope, Leads.csv mapping, client summary, and AI eligibility")


if __name__ == "__main__":
    main()
