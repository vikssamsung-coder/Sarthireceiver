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
import json
import os
import shutil
import ssl
import tempfile
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
MANIFEST_NAME = ".sarthi_update_manifest.json"

# Refuse a partial or wrong repository archive before touching the installation.
REQUIRED_CODE_FILES = {
    Path("app.py"),
    Path("updater.py"),
    Path("sarthi_receiver.py"),
    Path("client_intelligence_pipeline/run_pipeline.py"),
    Path("client_intelligence_pipeline/phase2_intelligence.py"),
    Path("client_intelligence_pipeline/prompts/phase2_call_intelligence.md"),
}

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


def _is_managed_code_path(rel: Path) -> bool:
    """Return whether an archive path is safe application content to manage."""
    return (
        bool(rel.parts)
        and rel.name not in SKIP_NAMES
        and rel.name != MANIFEST_NAME
        and rel.suffix not in SKIP_EXTS
        and not any(part in SKIP_DIRS for part in rel.parts)
        and not rel.is_absolute()
        and ".." not in rel.parts
    )


def _archive_files(zf: zipfile.ZipFile, log=print) -> dict[Path, zipfile.ZipInfo]:
    """Build the complete safe repository-file map after stripping GitHub's root."""
    files: dict[Path, zipfile.ZipInfo] = {}
    for member in zf.infolist():
        if member.is_dir():
            continue
        parts = Path(member.filename).parts
        if len(parts) < 2:
            continue
        rel = Path(*parts[1:])
        if not _is_managed_code_path(rel):
            if rel.parts and (rel.is_absolute() or ".." in rel.parts):
                log(f"skipped unsafe archive path: {member.filename}")
            continue
        files[rel] = member
    missing = sorted(str(path) for path in REQUIRED_CODE_FILES.difference(files))
    if missing:
        raise ValueError(
            "GitHub archive is incomplete or is not the Sarthireceiver repository; "
            f"missing required code: {', '.join(missing)}"
        )
    return files


def _read_previous_manifest(dest_root: Path) -> set[Path]:
    path = dest_root / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return {
            Path(item) for item in payload.get("managed_files", [])
            if isinstance(item, str) and _is_managed_code_path(Path(item))
        }
    except (OSError, ValueError, TypeError):
        return set()


def _write_manifest(dest_root: Path, managed_files: set[Path]) -> None:
    payload = {
        "repository": f"{OWNER}/{REPO}",
        "branch": BRANCH,
        "managed_files": sorted(path.as_posix() for path in managed_files),
    }
    manifest = dest_root / MANIFEST_NAME
    temporary = manifest.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, manifest)


def update_from_github(dest_dir, token: str = "", log=print) -> list:
    """Replace all GitHub-managed app code while preserving local runtime data."""
    if not token:
        token = load_github_token()
    data = _fetch_zip(token)
    dest_root = Path(dest_dir).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    previous = _read_previous_manifest(dest_root)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        archive_files = _archive_files(zf, log=log)
        current = set(archive_files)

        # Fully stage and validate the download before modifying installed code.
        with tempfile.TemporaryDirectory(prefix="sarthi-update-") as stage_name:
            stage_root = Path(stage_name)
            for rel, member in archive_files.items():
                staged = stage_root / rel
                staged.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(staged, "wb") as out:
                    shutil.copyfileobj(src, out)

            written: list[str] = []
            for rel in sorted(current, key=lambda path: path.as_posix()):
                target = (dest_root / rel).resolve()
                target.relative_to(dest_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(stage_root / rel, target)
                written.append(str(rel))
                log(f"updated {rel}")

    # Remove only files that a previous updater run explicitly managed. Arbitrary
    # local files and all protected data/configuration paths remain untouched.
    for rel in sorted(previous - current, key=lambda path: path.as_posix()):
        target = (dest_root / rel).resolve()
        try:
            target.relative_to(dest_root)
        except ValueError:
            continue
        if target.is_file() and _is_managed_code_path(rel):
            target.unlink()
            log(f"removed obsolete {rel}")

    _write_manifest(dest_root, current)
    return written


if __name__ == "__main__":
    import sys
    dest = sys.argv[1] if len(sys.argv) > 1 else "."
    files = update_from_github(dest)
    print(f"Updated {len(files)} file(s).")
