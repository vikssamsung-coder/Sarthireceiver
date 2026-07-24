"""Resolve OneDrive/SharePoint email links to local files.

Private company links are resolved from a locally synced OneDrive root first.
Public/"anyone" links may be downloaded over HTTPS. Authentication is
deliberately not attempted here: a private link that is not locally synced
fails with a clear message instead of saving a Microsoft sign-in page as data.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

MAX_DOWNLOAD_BYTES = int(os.environ.get("SARTHI_CLOUD_MAX_MB", "250")) * 1024 * 1024
DATA_EXTENSIONS = {
    ".csv", ".xlsx", ".xlsm", ".xls", ".xlsb", ".txt", ".json",
    ".zip", ".gz", ".pdf", ".tsv",
}


def is_trusted_cloud_url(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme.lower() == "https"
        and (host in {"1drv.ms", "onedrive.live.com"}
             or host.endswith(".sharepoint.com"))
    )


def _cloud_path(url: str) -> str:
    """Return the decoded cloud path, including SharePoint's common ?id= form."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    candidate = (query.get("id") or query.get("RootFolder") or [""])[0]
    return unquote(candidate or parts.path)


def _relative_documents_path(url: str) -> Path | None:
    bits = [b for b in _cloud_path(url).replace("\\", "/").split("/") if b]
    lowered = [b.lower() for b in bits]
    for marker in ("documents", "shared documents"):
        if marker in lowered:
            pos = lowered.index(marker)
            rest = bits[pos + 1:]
            if rest:
                return Path(*rest)
    return None


def _safe_existing(root: Path, relative: Path) -> Path | None:
    try:
        root_resolved = root.resolve()
        candidate = (root / relative).resolve()
        candidate.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


def resolve_synced_file(url: str, cloud_root: str = "") -> Path | None:
    """Map a SharePoint Documents URL into a local OneDrive sync root."""
    root_text = (cloud_root or os.environ.get("SARTHI_ONEDRIVE_ROOT", "")).strip()
    if not root_text:
        return None
    root = Path(root_text)
    if not root.is_dir():
        return None

    relative = _relative_documents_path(url)
    if relative:
        found = _safe_existing(root, relative)
        if found:
            return found

    # Some configured roots already point at the shared subfolder. In that
    # case an exact filename fallback is useful, but never guess if ambiguous.
    name = Path(_cloud_path(url).rstrip("/")).name
    if not name:
        return None
    direct = _safe_existing(root, Path(name))
    if direct:
        return direct
    matches = [p for p in root.rglob(name) if p.is_file()]
    return matches[0] if len(matches) == 1 else None


def _download_url(url: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["download"] = ["1"]
    encoded = "&".join(
        f"{key}={value}" for key, values in query.items() for value in values
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, encoded, parts.fragment))


def _safe_name(value: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", unquote(value or "")).strip(" .")
    return value[:180]


def _response_filename(response) -> str:
    disposition = response.headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8''|[\"']?)([^\"';]+)", disposition,
                      flags=re.IGNORECASE)
    return unquote(match.group(1)) if match else ""


def download_public_file(url: str, destination: Path,
                         suggested_name: str = "") -> Path:
    """Download an unauthenticated sharing link and reject login/HTML pages."""
    destination.mkdir(parents=True, exist_ok=True)
    req = Request(_download_url(url), headers={"User-Agent": "SarthiReceiver/1.0"})
    with urlopen(req, timeout=60) as response:
        content_type = (response.headers.get_content_type() or "").lower()
        final_path = _cloud_path(response.geturl())
        choices = [
            suggested_name,
            _response_filename(response),
            Path(final_path.rstrip("/")).name,
        ]
        name = next(
            (_safe_name(value) for value in choices
             if Path(_safe_name(value)).suffix.lower() in DATA_EXTENSIONS),
            "",
        )
        if not name or Path(name).suffix.lower() not in DATA_EXTENSIONS:
            raise ValueError("cloud link does not expose a supported data filename")
        if content_type in {"text/html", "application/xhtml+xml"}:
            raise PermissionError(
                "private OneDrive link requires the file to be available under "
                "the configured local synced OneDrive root")

        target = destination / name
        fd, temp_name = tempfile.mkstemp(prefix="sarthi_cloud_", suffix=".part",
                                         dir=str(destination))
        total = 0
        try:
            with os.fdopen(fd, "wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("cloud file exceeds SARTHI_CLOUD_MAX_MB")
                    handle.write(chunk)
            Path(temp_name).replace(target)
        except Exception:
            Path(temp_name).unlink(missing_ok=True)
            raise
    return target


def materialize(url: str, cloud_root: str = "", suggested_name: str = "",
                destination: Path | None = None) -> Path:
    if not is_trusted_cloud_url(url):
        raise ValueError("untrusted cloud-link domain")
    synced = resolve_synced_file(url, cloud_root)
    if synced:
        return synced
    target_dir = destination or Path(tempfile.gettempdir()) / "sarthi_cloud_links"
    return download_public_file(url, target_dir, suggested_name=suggested_name)
