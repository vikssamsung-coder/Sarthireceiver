# -*- coding: utf-8 -*-
r"""
A real MIS build step that consumes the files the receiver has ALREADY saved.
No new ingestion, no re-download.

How it finds your files, in order:
  1. flow_runs.saved_path — the exact file the receiver saved for the latest
     SUCCESSFUL run of that dump type on --on-date. This is the authoritative
     answer: it is the file your flow actually processed.
  2. Failing that, the newest .csv/.xlsx in the dump type's save_folder, read
     from dump_types — never hardcoded.

  >>>  YOUR BUSINESS LOGIC GOES IN build_report().  <<<
  I have not invented your column names. As shipped it profiles whatever the
  dumps actually contain, so it runs correctly on day one and you replace the
  middle.

Contract: prints OUTPUT=<abs path> as its last line. Exits non-zero on failure.

Register as a step with args:
    --dumps orderbook,trial_balance --out {out_folder} --report {report_key} --params {params}
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, date
from pathlib import Path

import pandas as pd

import dump_flows as df

OK_STATUS = ("success", "ok", "done", "completed")
READABLE = (".csv", ".xlsx", ".xls", ".txt")


def _conn(db_path):
    c = sqlite3.connect(str(db_path), timeout=30)
    c.row_factory = sqlite3.Row
    return c


def from_flow_runs(dump_key, on_date, db_path):
    """The exact file the receiver processed."""
    qs = ",".join("?" * len(OK_STATUS))
    try:
        with _conn(db_path) as c:
            r = c.execute(
                f"SELECT saved_path FROM flow_runs WHERE dump_type=? "
                f"AND LOWER(status) IN ({qs}) "
                f"AND substr(COALESCE(finished_at,started_at),1,10)=? "
                f"ORDER BY id DESC LIMIT 1",
                (dump_key, *OK_STATUS, on_date)).fetchone()
    except Exception:
        return None
    p = (r["saved_path"] if r else "") or ""
    return p if p and Path(p).is_file() else None


def newest_in_folder(folder):
    if not folder or not Path(folder).is_dir():
        return None
    cands = []
    for p in Path(folder).rglob("*"):
        if p.is_file() and p.suffix.lower() in READABLE and not p.name.startswith("~$"):
            try:
                cands.append((p.stat().st_mtime, str(p)))
            except OSError:
                pass
    if not cands:
        return None
    cands.sort(reverse=True)
    return cands[0][1]


def resolve_dump_file(dump_key, on_date, db_path):
    """Returns (path, how_it_was_found)."""
    p = from_flow_runs(dump_key, on_date, db_path)
    if p:
        return p, "flow_runs"
    folder = df.get_save_folder(dump_key, db_path=db_path)
    p = newest_in_folder(folder)
    if p:
        return p, "newest in save_folder"
    return None, f"not found (save_folder: {folder or 'unset'})"


def load(path) -> pd.DataFrame:
    ext = Path(path).suffix.lower()
    if ext in (".xlsx", ".xls"):
        return pd.read_excel(path)
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc, low_memory=False)
        except UnicodeDecodeError:
            continue
        except pd.errors.ParserError:
            return pd.read_csv(path, encoding=enc, low_memory=False,
                               sep=None, engine="python")
    raise ValueError(f"could not decode {path}")


def _safe_sheet(name, used):
    s = "".join(ch for ch in str(name) if ch not in "[]:*?/\\")[:28] or "sheet"
    base, i = s, 1
    while s in used:
        s = f"{base[:26]}_{i}"
        i += 1
    used.add(s)
    return s


# ===========================================================================
# >>> YOUR BUSINESS LOGIC HERE <<<
# frames = {"orderbook": DataFrame, "trial_balance": DataFrame, ...}
# Return {sheet_name: DataFrame}. Whatever you return is written as sheets.
# ===========================================================================
def build_report(frames: dict, params: str) -> dict:
    sheets = {}
    for key, d in frames.items():
        sheets[key] = d
        num = d.select_dtypes("number")
        if not num.empty:
            sheets[f"{key}_totals"] = pd.DataFrame({
                "column": list(num.columns),
                "sum": [num[c].sum() for c in num.columns],
                "mean": [round(float(num[c].mean()), 2) for c in num.columns],
                "non_null": [int(num[c].notna().sum()) for c in num.columns],
            })
    return sheets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dumps", required=True, help="comma-separated dump_types keys")
    ap.add_argument("--out", required=True, help="{out_folder}")
    ap.add_argument("--report", default="mis", help="{report_key}")
    ap.add_argument("--params", default="", help="{params}")
    ap.add_argument("--on-date", default="", help="YYYY-MM-DD, default today")
    ap.add_argument("--db", default=str(df.DEFAULT_DB))
    ap.add_argument("--allow-missing", action="store_true",
                    help="build with whatever landed instead of failing")
    a = ap.parse_args()

    on_date = a.on_date or date.today().isoformat()
    keys = [k.strip() for k in a.dumps.split(",") if k.strip()]
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    frames, summary, missing = {}, [], []

    for k in keys:
        path, how = resolve_dump_file(k, on_date, a.db)
        if not path:
            print(f"MISSING {k}: {how}")
            missing.append(k)
            summary.append({"dump": k, "source": "", "found_via": how,
                            "rows": 0, "columns": 0, "file_time": ""})
            continue
        try:
            d = load(path)
        except Exception as e:
            print(f"READ FAILED {k} ({path}): {e}")
            missing.append(k)
            continue

        frames[k] = d
        summary.append({
            "dump": k, "source": Path(path).name, "found_via": how,
            "rows": len(d), "columns": len(d.columns),
            "file_time": datetime.fromtimestamp(
                Path(path).stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
        print(f"loaded {k}: {len(d)} rows x {len(d.columns)} cols  <- {path} ({how})")

    if missing and not a.allow_missing:
        print(f"ERROR: dumps not available for {on_date}: {', '.join(missing)}")
        return 2
    if not frames:
        print("ERROR: nothing to build — no dump files resolved")
        return 2

    sheets = build_report(frames, a.params)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_path = (out_dir / f"{a.report}_{stamp}.xlsx").resolve()

    meta = pd.DataFrame([
        {"field": "report", "value": a.report},
        {"field": "as_of", "value": on_date},
        {"field": "params", "value": a.params or "(none)"},
        {"field": "built_at", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    ])
    used = set()
    with pd.ExcelWriter(out_path, engine="openpyxl") as xw:
        meta.to_excel(xw, sheet_name=_safe_sheet("Summary", used), index=False)
        pd.DataFrame(summary).to_excel(xw, sheet_name="Summary", index=False,
                                       startrow=len(meta) + 2)
        for name, d in sheets.items():
            d.to_excel(xw, sheet_name=_safe_sheet(name, used), index=False)

    print("sheets: " + ", ".join(sheets.keys()))
    print(f"OUTPUT={out_path}")            # <-- THE CONTRACT
    return 0


if __name__ == "__main__":
    sys.exit(main())
