# -*- coding: utf-8 -*-
r"""
GitHub updater — the PMD way: download the code over HTTPS and overwrite the
local files. No git, no install. Uses only the Python standard library.

Public repo  -> no credentials needed (downloads from codeload).
Private repo -> pass a GitHub token (read from secrets.toml, key-agnostic).

Skips local data and secrets so an update never clobbers them.
"""
from __future__ import annotations

import io
import shutil
import ssl
import urllib.request
import zipfile
from pathlib import Path

OWNER = "vikssamsung-coder"
REPO = "Sarthireceiver"
BRANCH = "main"

CODELOAD = f"https://codeload.github.com/{OWNER}/{REPO}/zip/refs/heads/{BRANCH}"
API_ZIP = f"https://api.github.com/repos/{OWNER}/{REPO}/zipball/{BRANCH}"

SKIP_NAMES = {"secrets.toml"}
SKIP_EXTS = {".sqlite3", ".sqlite", ".pyc"}
SKIP_DIRS = {"__pycache__", ".git", ".streamlit"}

SECRETS_PATH = Path(r"D:\PMD-Desktop-main\.streamlit\secrets.toml")


def _walk_strings(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


def load_github_token(secrets_path: Path = SECRETS_PATH) -> str:
    """Find a GitHub token in secrets.toml (value starting ghp_ / github_pat_)."""
    try:
        try:
            import tomllib
            data = tomllib.loads(Path(secrets_path).read_text(encoding="utf-8"))
        except ModuleNotFoundError:
            import tomli
            data = tomli.loads(Path(secrets_path).read_text(encoding="utf-8"))
    except Exception:
        return ""
    for v in _walk_strings(data):
        if v.startswith("ghp_") or v.startswith("github_pat_"):
            return v
    return ""


def _download(url: str, token: str = "", timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "sarthi-updater"})
    if token:
        req.add_header("Authorization", f"token {token}")
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def _fetch_zip(token: str = "") -> bytes:
    # public repos: codeload needs no auth. If that fails and we have a token
    # (private repo), use the API zipball which honours the token.
    try:
        return _download(CODELOAD, token="")
    except Exception:
        if token:
            return _download(API_ZIP, token=token)
        raise


def update_from_github(dest_dir, token: str = "", log=print) -> list:
    """Download the repo and overwrite files in dest_dir. Returns files written."""
    if not token:
        token = load_github_token()
    data = _fetch_zip(token)
    zf = zipfile.ZipFile(io.BytesIO(data))

    dest_root = Path(dest_dir).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    written = []
    for m in zf.infolist():
        if m.is_dir():
            continue
        parts = Path(m.filename).parts
        if len(parts) < 2:          # strip GitHub's top folder (Repo-main/)
            continue
        rel = Path(*parts[1:])
        if rel.name in SKIP_NAMES or rel.suffix in SKIP_EXTS:
            continue
        if any(p in SKIP_DIRS for p in rel.parts):
            continue
        target = (dest_root / rel).resolve()
        try:
            target.relative_to(dest_root)
        except ValueError:
            log(f"skipped unsafe archive path: {m.filename}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(m) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        written.append(str(rel))
        log(f"updated {rel}")
    return written


if __name__ == "__main__":
    import sys
    dest = sys.argv[1] if len(sys.argv) > 1 else "."
    files = update_from_github(dest)
    print(f"Updated {len(files)} file(s).")
