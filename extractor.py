# -*- coding: utf-8 -*-
r"""
Dump extractor.

Incoming dumps may be .zip, .csv or .xlsx. This normalises them into the dump
type's save folder so the flow's scripts can read a real data file:

  - .zip   -> unzipped into the folder; the CSV/XLSX inside becomes the input
  - .csv   -> copied as-is
  - .xlsx  -> copied as-is (an xlsx is itself a zip, so we never explode it)

Detection is extension-first, with a magic-byte fallback for files that arrive
without a clean extension. Returns which file the scripts should read (primary)
plus the full list of data files placed.

Zip-slip safe: archive members are written only inside the destination folder.
No pandas / openpyxl needed — this places files, it doesn't parse them.
"""
from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

DATA_EXTS = {".csv", ".xlsx", ".xls", ".tsv", ".txt"}
PREFERRED = [".csv", ".xlsx", ".xls", ".tsv", ".txt"]  # primary pick order


def _looks_xlsx(path: Path) -> bool:
    """True if a PK zip is actually an Office file (xlsx/xls), not a plain archive."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
        return any(n.startswith("xl/") for n in names) or "[Content_Types].xml" in names
    except Exception:
        return False


def detect_kind(path: Path) -> str:
    """Return 'zip' | 'xlsx' | 'csv' | 'other'."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xls"):
        return "xlsx"
    if ext == ".zip":
        return "zip"
    if ext in (".csv", ".tsv", ".txt"):
        return "csv"
    try:
        head = path.read_bytes()[:4]
    except Exception:
        return "other"
    if head[:2] == b"PK":                       # a zip container
        return "xlsx" if _looks_xlsx(path) else "zip"
    return "csv"                                # default: treat as delimited text


def _safe_members(z: zipfile.ZipFile, dest: Path):
    """Yield (member, target) for members that stay inside dest (zip-slip guard)."""
    dest = dest.resolve()
    for m in z.infolist():
        if m.is_dir():
            continue
        target = (dest / m.filename).resolve()
        if str(target).startswith(str(dest)):
            yield m, target


def _pick_primary(files):
    """Choose the single file scripts should read from the extracted set."""
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    for ext in PREFERRED:
        for f in files:
            if f.suffix.lower() == ext:
                return f
    return files[0]


def extract_dump(src_path, dest_folder, log=print) -> dict:
    """Place the dump's data into dest_folder and report what's there.

    Returns {kind, primary (Path|None), files [Path...], saved_folder}.
    """
    src = Path(src_path)
    dest = Path(dest_folder)
    dest.mkdir(parents=True, exist_ok=True)
    kind = detect_kind(src)

    if kind == "zip":
        placed = []
        with zipfile.ZipFile(src) as z:
            for m, target in _safe_members(z, dest):
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as fsrc, open(target, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
                placed.append(target)
        data = [f for f in placed if f.suffix.lower() in DATA_EXTS] or placed
        primary = _pick_primary(data)
        log(f"extracted zip -> {len(placed)} file(s); input = {primary.name if primary else 'none'}")
        return {"kind": "zip", "primary": primary, "files": data, "saved_folder": dest}

    target = dest / src.name
    if target.resolve() != src.resolve():
        shutil.copy2(src, target)
        log(f"saved {kind} -> {target}")
    return {"kind": kind, "primary": target, "files": [target], "saved_folder": dest}
