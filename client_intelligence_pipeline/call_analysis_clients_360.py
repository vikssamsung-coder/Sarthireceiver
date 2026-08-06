#!/usr/bin/env python3
"""Build a lead-driven Client 360 report and optionally run Client Intelligence.

Population rule
---------------
1. Read every supported file in ``01_Input/Call_Analysis``.
2. Extract unique Lead Numbers from those call-analysis rows.
3. Match those Lead Numbers to ``D:\\Sarthi\\Leads\\Leads.csv``.
4. Keep only mappings having both a Lead Number and Client Code for the
   operational Client 360 population. Unmatched leads remain in the audit sheet.
5. Enrich the mapped clients from Sarthi CDP lifetime/current meta tables.
6. When ``--run-intelligence`` is supplied, run the normal Client Intelligence
   pipeline. Because this workbook is written immediately before that run, it is
   the active Client 360 and AI eligibility is limited to this exact population.

Examples
--------
python call_analysis_clients_360.py --run-intelligence --max-ai-calls 5000
python call_analysis_clients_360.py --skip-ai --run-intelligence
python call_analysis_clients_360.py --call-input "D:\\Customer Final Evaluation\\01_Input\\Call_Analysis"
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd


DEFAULT_ROOT = Path(r"D:\Customer Final Evaluation")
DEFAULT_LEADS = Path(r"D:\Sarthi\Leads\Leads.csv")
SUPPORTED = {".csv", ".xlsx", ".xlsm"}

LEAD_ALIASES = ("Lead Number", "Lead No", "Lead Code", "Prospect ID")
CLIENT_ALIASES = ("Client Code", "LD Client Code", "Terminal Code")
FRESHNESS_ALIASES = (
    "Modified On", "Last Activity Date", "Updated On", "Account Opened Date",
    "LD Account Opened Date", "Created On", "Lead Created On",
)

DETAIL_COLUMNS = [
    "Client Code", "Lead Number", "Client Name", "Opening Date", "Account Status",
    "Call Count", "Call Source Files", "Funds Received Till Date", "Funds As Of Date",
    "Total Stock", "Margin Date", "First Trade Date", "Last Trade Date",
    "Total Orders", "Executed Orders", "Executed Order %", "Orders As Of Date",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build Client 360 only for lead numbers present in Call Analysis input."
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--call-input", type=Path)
    parser.add_argument("--leads", type=Path, default=DEFAULT_LEADS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-intelligence", action="store_true")
    parser.add_argument("--skip-ai", action="store_true")
    parser.add_argument("--max-ai-calls", type=int)
    return parser.parse_args()


def norm_header(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def clean_id(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip().upper()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def find_column(columns: Iterable[object], aliases: Iterable[str]) -> object | None:
    lookup = {norm_header(column): column for column in columns}
    for alias in aliases:
        if norm_header(alias) in lookup:
            return lookup[norm_header(alias)]
    return None


def read_csv_flexible(path: Path) -> pd.DataFrame:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
        try:
            return pd.read_csv(path, dtype=str, low_memory=False, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise RuntimeError(f"Unable to decode {path}: {last_error}")


def read_tabular(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".csv":
        return read_csv_flexible(path)
    book = pd.ExcelFile(path)
    if not book.sheet_names:
        return pd.DataFrame()
    return pd.read_excel(path, sheet_name=book.sheet_names[0], dtype=str)


def extract_call_leads(call_folder: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    if not call_folder.is_dir():
        raise FileNotFoundError(f"Call Analysis folder not found: {call_folder}")
    files = sorted(
        path for path in call_folder.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED
        and not path.name.startswith("~$")
    )
    if not files:
        raise RuntimeError(f"No Call Analysis CSV/XLSX files found in: {call_folder}")

    records: list[dict[str, object]] = []
    file_audit: list[dict[str, object]] = []
    for path in files:
        try:
            frame = read_tabular(path)
            lead_col = find_column(frame.columns, LEAD_ALIASES)
            if lead_col is None:
                file_audit.append({
                    "Source File": path.name, "Rows Read": len(frame),
                    "Lead Rows": 0, "Unique Leads": 0,
                    "Status": "Skipped - Lead Number column missing",
                })
                continue
            leads = frame[lead_col].map(clean_id)
            valid = leads[leads != ""]
            for lead in valid:
                records.append({"Lead Number": lead, "Source File": path.name})
            file_audit.append({
                "Source File": path.name, "Rows Read": len(frame),
                "Lead Rows": len(valid), "Unique Leads": valid.nunique(),
                "Status": "Read",
            })
        except Exception as exc:
            file_audit.append({
                "Source File": path.name, "Rows Read": 0, "Lead Rows": 0,
                "Unique Leads": 0, "Status": f"Error - {exc}",
            })

    raw = pd.DataFrame(records, columns=["Lead Number", "Source File"])
    if raw.empty:
        raise RuntimeError("No valid Lead Numbers were found in the Call Analysis inputs.")
    grouped = (
        raw.groupby("Lead Number", as_index=False)
        .agg(
            **{
                "Call Count": ("Lead Number", "size"),
                "Call Source Files": (
                    "Source File", lambda values: ", ".join(sorted(set(values)))
                ),
            }
        )
    )
    return grouped, pd.DataFrame(file_audit)


def map_leads_to_clients(call_leads: pd.DataFrame, leads_path: Path) -> pd.DataFrame:
    if not leads_path.is_file():
        raise FileNotFoundError(f"Leads.csv not found: {leads_path}")
    leads = read_csv_flexible(leads_path)
    lead_col = find_column(leads.columns, LEAD_ALIASES)
    client_col = find_column(leads.columns, CLIENT_ALIASES)
    if lead_col is None or client_col is None:
        raise RuntimeError(
            "Leads.csv must contain Lead Number and Client Code/Terminal Code columns."
        )
    freshness_col = find_column(leads.columns, FRESHNESS_ALIASES)
    mapped = pd.DataFrame({
        "Lead Number": leads[lead_col].map(clean_id),
        "Client Code": leads[client_col].map(clean_id),
        "_row": range(len(leads)),
    })
    mapped["_freshness"] = (
        pd.to_datetime(leads[freshness_col], errors="coerce")
        if freshness_col is not None else pd.NaT
    )
    wanted = set(call_leads["Lead Number"])
    mapped = mapped[mapped["Lead Number"].isin(wanted)].copy()

    output: list[dict[str, object]] = []
    for row in call_leads.itertuples(index=False):
        # itertuples renames spaced columns; direct positional access is stable here.
        lead = clean_id(row[0])
        candidates = mapped[mapped["Lead Number"] == lead]
        client_candidates = sorted(set(candidates["Client Code"]) - {""})
        if candidates.empty:
            status, client = "Lead not found in Leads.csv", ""
        elif not client_candidates:
            status, client = "Client Code missing in Leads.csv", ""
        else:
            valid = candidates[candidates["Client Code"] != ""].sort_values(
                ["_freshness", "_row"], ascending=[False, False], na_position="last"
            )
            client = clean_id(valid.iloc[0]["Client Code"])
            status = "Matched" if len(client_candidates) == 1 else "Matched - multiple client codes"
        output.append({
            "Lead Number": lead,
            "Client Code": client,
            "Call Count": int(row[1]),
            "Call Source Files": row[2],
            "Match Status": status,
            "Client Code Candidates": ", ".join(client_candidates),
        })
    return pd.DataFrame(output)


def read_sql(connection, sql: str, params: tuple = ()) -> pd.DataFrame:
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        columns = [item[0] for item in cursor.description] if cursor.description else []
    return pd.DataFrame(rows, columns=columns)


def query_by_client_chunks(
    connection, sql_template: str, client_codes: list[str], chunk_size: int = 750
) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for start in range(0, len(client_codes), chunk_size):
        chunk = client_codes[start:start + chunk_size]
        placeholders = ",".join(["%s"] * len(chunk))
        parts.append(read_sql(connection, sql_template.format(codes=placeholders), tuple(chunk)))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def extract_client_metrics(connection, client_codes: list[str]) -> pd.DataFrame:
    codes = sorted({clean_id(code) for code in client_codes if clean_id(code)})
    if not codes:
        return pd.DataFrame(columns=DETAIL_COLUMNS)

    base = query_by_client_chunks(connection, """
        SELECT client_code, client_name, opening_date, status AS account_status
        FROM client_master
        WHERE client_code IN ({codes})
    """, codes)
    funds = query_by_client_chunks(connection, """
        SELECT client_code, as_of_date AS funds_as_of_date,
               lifetime_receipt_amt AS funds_received_till_date
        FROM funds_client_meta
        WHERE client_code IN ({codes})
    """, codes)
    orders = query_by_client_chunks(connection, """
        SELECT client_code, as_of_date AS orders_as_of_date, first_trade_date,
               last_trade_date, lifetime_total_orders AS total_orders,
               lifetime_executed_orders AS executed_orders
        FROM orders_client_meta
        WHERE client_code IN ({codes})
    """, codes)
    margin = query_by_client_chunks(connection, """
        SELECT m.client_code, m.report_date AS margin_date, m.total_stock
        FROM margin_sheet m
        JOIN (
            SELECT client_code, MAX(report_date) AS latest_date
            FROM margin_sheet
            WHERE client_code IN ({codes})
            GROUP BY client_code
        ) latest
          ON latest.client_code=m.client_code AND latest.latest_date=m.report_date
    """, codes)

    for frame in (base, funds, orders, margin):
        if "client_code" in frame:
            frame["client_code"] = frame["client_code"].map(clean_id)
            frame.drop_duplicates("client_code", keep="last", inplace=True)
    metrics = pd.DataFrame({"client_code": codes})
    for frame in (base, funds, margin, orders):
        metrics = metrics.merge(frame, on="client_code", how="left")
    for column in ("funds_received_till_date", "total_stock", "total_orders", "executed_orders"):
        metrics[column] = pd.to_numeric(metrics.get(column), errors="coerce").fillna(0)
    metrics["executed_order_pct"] = (
        metrics["executed_orders"].div(metrics["total_orders"].where(metrics["total_orders"] > 0))
        .fillna(0)
    )
    return metrics


def build_reports(
    mapping: pd.DataFrame, metrics: pd.DataFrame, file_audit: pd.DataFrame,
    leads_path: Path, call_folder: Path,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    matched = mapping[(mapping["Client Code"] != "") & mapping["Match Status"].str.startswith("Matched")].copy()
    detail = matched.merge(metrics, left_on="Client Code", right_on="client_code", how="left")
    detail = detail.rename(columns={
        "client_name": "Client Name", "opening_date": "Opening Date",
        "account_status": "Account Status",
        "funds_received_till_date": "Funds Received Till Date",
        "funds_as_of_date": "Funds As Of Date", "total_stock": "Total Stock",
        "margin_date": "Margin Date", "first_trade_date": "First Trade Date",
        "last_trade_date": "Last Trade Date", "total_orders": "Total Orders",
        "executed_orders": "Executed Orders", "executed_order_pct": "Executed Order %",
        "orders_as_of_date": "Orders As Of Date",
    })
    for column in ("Funds Received Till Date", "Total Stock", "Total Orders", "Executed Orders", "Executed Order %"):
        if column in detail:
            detail[column] = pd.to_numeric(detail[column], errors="coerce").fillna(0)
    for column in DETAIL_COLUMNS:
        if column not in detail:
            detail[column] = ""
    detail = detail[DETAIL_COLUMNS].sort_values(["Client Code", "Lead Number"]).reset_index(drop=True)

    summary_rows: list[dict[str, object]] = []
    for client_code, rows in detail.groupby("Client Code", sort=True):
        first = rows.iloc[0]
        record = {column: first[column] for column in DETAIL_COLUMNS if column not in {
            "Lead Number", "Call Count", "Call Source Files"
        }}
        record["Lead Numbers"] = ", ".join(sorted(set(rows["Lead Number"])))
        record["Call Count"] = int(rows["Call Count"].sum())
        record["Call Source Files"] = ", ".join(sorted({
            name.strip() for value in rows["Call Source Files"] for name in str(value).split(",") if name.strip()
        }))
        summary_rows.append(record)
    client_summary = pd.DataFrame(summary_rows)
    preferred = [
        "Client Code", "Lead Numbers", "Client Name", "Opening Date", "Account Status",
        "Call Count", "Call Source Files", "Funds Received Till Date", "Funds As Of Date",
        "Total Stock", "Margin Date", "First Trade Date", "Last Trade Date",
        "Total Orders", "Executed Orders", "Executed Order %", "Orders As Of Date",
    ]
    client_summary = client_summary.reindex(columns=preferred)

    run_summary = pd.DataFrame([
        ("Call Analysis Folder", str(call_folder)),
        ("Leads File", str(leads_path)),
        ("Call Files Read", int((file_audit["Status"] == "Read").sum())),
        ("Unique Lead Numbers in Calls", len(mapping)),
        ("Matched Lead Numbers", len(detail)),
        ("Unmatched Lead Numbers", int((~mapping["Match Status"].str.startswith("Matched")).sum())),
        ("Unique Client Codes", detail["Client Code"].nunique()),
        ("Clients With Funds Received", int((client_summary["Funds Received Till Date"] > 0).sum())),
        ("Clients With Executed Orders", int((client_summary["Executed Orders"] > 0).sum())),
        ("Intelligence Eligibility Rule", "Both Lead Number and Client Code must be present and matched"),
        ("Executed Order % Rule", "Executed Orders / Total Orders; zero when Total Orders is zero"),
        ("Margin Rule", "Latest available margin_sheet row per client"),
    ], columns=["Metric", "Value"])
    return detail, client_summary, mapping, file_audit, run_summary


def write_excel(
    output: Path, detail: pd.DataFrame, client_summary: pd.DataFrame,
    mapping: pd.DataFrame, file_audit: pd.DataFrame, run_summary: pd.DataFrame,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output, engine="xlsxwriter", date_format="dd-mmm-yyyy",
                        datetime_format="dd-mmm-yyyy hh:mm") as writer:
        sheets = {
            "Client Detail": detail,
            "Client Summary": client_summary,
            "Lead Mapping Audit": mapping,
            "File Audit": file_audit,
            "Run Summary": run_summary,
        }
        header = writer.book.add_format({
            "bold": True, "font_color": "#FFFFFF", "bg_color": "#17365D",
            "border": 1, "align": "center", "valign": "vcenter", "text_wrap": True,
        })
        money = writer.book.add_format({"num_format": "#,##0.00"})
        integer = writer.book.add_format({"num_format": "#,##0"})
        percent = writer.book.add_format({"num_format": "0.0%"})
        date_format = writer.book.add_format({"num_format": "dd-mmm-yyyy"})
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
            ws = writer.sheets[name]
            ws.freeze_panes(1, 0)
            if len(frame.columns):
                ws.autofilter(0, 0, len(frame), len(frame.columns) - 1)
            ws.set_row(0, 34)
            for index, column in enumerate(frame.columns):
                ws.write(0, index, column, header)
                longest = max(len(str(column)), min(55, int(frame[column].fillna("").astype(str).str.len().max()) if len(frame) else 0))
                ws.set_column(index, index, min(max(longest + 2, 12), 44))
            for column in ("Funds Received Till Date", "Total Stock"):
                if column in frame:
                    index = frame.columns.get_loc(column)
                    ws.set_column(index, index, 18, money)
            for column in ("Call Count", "Total Orders", "Executed Orders"):
                if column in frame:
                    index = frame.columns.get_loc(column)
                    ws.set_column(index, index, 14, integer)
            if "Executed Order %" in frame:
                index = frame.columns.get_loc("Executed Order %")
                ws.set_column(index, index, 16, percent)
            for column in ("Opening Date", "Funds As Of Date", "Margin Date", "First Trade Date", "Last Trade Date", "Orders As Of Date"):
                if column in frame:
                    index = frame.columns.get_loc(column)
                    ws.set_column(index, index, 15, date_format)


def run_intelligence(args: argparse.Namespace) -> int:
    pipeline = Path(__file__).resolve().with_name("run_pipeline.py")
    command = [sys.executable, "-u", str(pipeline), "--root", str(args.root)]
    if args.skip_ai:
        command.append("--skip-ai")
    elif args.max_ai_calls:
        command += ["--max-ai-calls", str(max(1, args.max_ai_calls))]
    print(f"Running Client Intelligence: {command}")
    return subprocess.run(command, cwd=str(pipeline.parent), check=False).returncode


def main() -> int:
    args = parse_args()
    call_folder = args.call_input or args.root / "01_Input" / "Call_Analysis"
    output = args.output or args.root / "01_Input" / "Client_360" / "Sarthi_Call_Analysis_Client_360.xlsx"

    print("=" * 88)
    print("BIGUL · SARTHI · CALL ANALYSIS CLIENT 360")
    print("=" * 88)
    print(f"Call input : {call_folder}")
    print(f"Leads file : {args.leads}")
    call_leads, file_audit = extract_call_leads(call_folder)
    mapping = map_leads_to_clients(call_leads, args.leads)
    client_codes = mapping.loc[mapping["Client Code"] != "", "Client Code"].tolist()
    if not client_codes:
        raise RuntimeError("No Call Analysis Lead Number mapped to a Client Code in Leads.csv.")

    try:
        import pymysql
        from sarthi_new_clients_360_extract import get_db_config
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "MySQL dependency is missing. Install the project requirements before running."
        ) from exc
    connection = pymysql.connect(**get_db_config())
    try:
        metrics = extract_client_metrics(connection, client_codes)
    finally:
        connection.close()
    reports = build_reports(mapping, metrics, file_audit, args.leads, call_folder)
    write_excel(output, *reports)
    detail = reports[0]
    print(f"Unique call leads : {len(mapping):,}")
    print(f"Matched leads     : {len(detail):,}")
    print(f"Unique clients   : {detail['Client Code'].nunique():,}")
    print(f"Output           : {output}")
    if args.run_intelligence:
        return run_intelligence(args)
    print("DONE")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
