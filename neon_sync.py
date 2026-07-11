# -*- coding: utf-8 -*-
r"""
Neon catalog sync + Neon-URL loader.

The URL is read from your Streamlit secrets file
(D:\PMD-Desktop-main\.streamlit\secrets.toml) — whatever the key is called. The
loader scans the TOML for the first value that looks like a Postgres URL, so it
works whether the key is neon_url, NEON_DATABASE_URL, database_url, or nested
under [neon] / [connections.*]. Falls back to the NEON_DATABASE_URL env var.

channel_binding=require is stripped (it blocks psycopg).
"""
from __future__ import annotations

import os
from pathlib import Path

import dump_flows as df

SECRETS_PATH = Path(r"D:\PMD-Desktop-main\.streamlit\secrets.toml")

_SELECT = "SELECT key, name, handler, active, sort_order, max_files FROM dump_types ORDER BY sort_order, key"


def _clean_url(url: str) -> str:
    url = (url or "").replace("channel_binding=require", "")
    return url.replace("&&", "&").rstrip("&?").replace("?&", "?")


def _walk(obj):
    """Yield every string value in a nested dict/list."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    elif isinstance(obj, str):
        yield obj


def load_neon_url(secrets_path: Path = SECRETS_PATH) -> str:
    """Return the Neon Postgres URL, or '' if not found."""
    env = os.environ.get("NEON_DATABASE_URL", "").strip()
    if env:
        return _clean_url(env)
    try:
        try:
            import tomllib  # py311+
            data = tomllib.loads(Path(secrets_path).read_text(encoding="utf-8"))
        except ModuleNotFoundError:
            import tomli  # py<311
            data = tomli.loads(Path(secrets_path).read_text(encoding="utf-8"))
    except Exception:
        return ""
    for val in _walk(data):
        if val.startswith("postgres://") or val.startswith("postgresql://"):
            return _clean_url(val)
    return ""


def fetch_catalog(neon_url: str) -> list:
    if not neon_url:
        return []
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("psycopg not installed. pip install 'psycopg[binary]'")
    rows = []
    with psycopg.connect(neon_url) as conn:
        with conn.cursor() as cur:
            cur.execute(_SELECT)
            cols = [d.name for d in cur.description]
            for rec in cur.fetchall():
                rows.append(dict(zip(cols, rec)))
    return rows


def sync(db_path: Path, neon_url: str | None = None) -> dict:
    url = neon_url or load_neon_url()
    if not url:
        return {"error": "No Neon URL found in secrets.toml or NEON_DATABASE_URL."}
    try:
        rows = fetch_catalog(url)
    except Exception as e:
        return {"error": str(e)}
    created, updated = df.sync_catalog(rows, db_path=db_path)
    return {"created": created, "updated": updated, "total": len(rows)}


if __name__ == "__main__":
    import sys
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else df.DEFAULT_DB
    print("neon url found:", bool(load_neon_url()))
    print(sync(db))
