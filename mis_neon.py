# -*- coding: utf-8 -*-
r"""
The Neon side of the MIS Builder.

Two jobs:
  1. report_requests — find work, CLAIM it, mark done/failed.
  2. Recipients      — expand mis_report_access principals into emails.

The Neon URL comes from neon_sync.load_neon_url(), which already reads
D:\PMD-Desktop-main\.streamlit\secrets.toml and strips channel_binding. That
logic lives THERE and is not duplicated here.

Confirmed Neon schema:
  report_requests(req_id, user_key, requester_email, report_key, report_name,
                  params, source, status, created_at)
  mis_report_access(report_key, principal_type, principal)
  mis_types(key, name, params_hint, handler, active, sort_order, updated_at)
  users(user_key, name, role, department, login_role, password, active, email)

An access row names a PRINCIPAL, not an email — it must be expanded against
users. Handled principal_types: user / user_key, role (matches role OR
login_role), department / dept, email, all / everyone / *.

admin_emails (the deny-by-default fallback) is read from the same secrets.toml:

    admin_emails = "vikrant@bigul.co, rinku@bigul.co"     # or a TOML list

Self-check:  python mis_neon.py
"""
from __future__ import annotations

import os
from pathlib import Path

import neon_sync

_ADMIN_KEYS = ("admin_emails", "ADMIN_EMAILS", "mis_admin_emails")


def load_neon_url() -> str:
    """Delegate. One reader, one channel_binding strip."""
    return neon_sync.load_neon_url()


def _secrets() -> dict:
    try:
        try:
            import tomllib
            return tomllib.loads(Path(neon_sync.SECRETS_PATH).read_text(encoding="utf-8"))
        except ModuleNotFoundError:
            import tomli
            return tomli.loads(Path(neon_sync.SECRETS_PATH).read_text(encoding="utf-8"))
    except Exception:
        return {}


def admin_emails() -> list:
    """Who gets a report that has NO access rows. Deny by default."""
    def _pull(d):
        for k in _ADMIN_KEYS:
            v = d.get(k)
            if isinstance(v, list):
                return [str(e).strip() for e in v if str(e).strip()]
            if isinstance(v, str) and v.strip():
                return [e.strip() for e in v.split(",") if e.strip()]
        return []

    sec = _secrets()
    hit = _pull(sec)
    if hit:
        return hit
    for v in sec.values():
        if isinstance(v, dict):
            hit = _pull(v)
            if hit:
                return hit
    env = os.environ.get("SARTHI_ADMIN_EMAILS", "")
    return [e.strip() for e in env.split(",") if e.strip()]


def _conn():
    try:
        import psycopg
    except ImportError:
        raise RuntimeError("psycopg not installed. pip install 'psycopg[binary]'")
    url = load_neon_url()
    if not url:
        raise RuntimeError(f"No Neon URL in {neon_sync.SECRETS_PATH} or NEON_DATABASE_URL")
    return psycopg.connect(url, connect_timeout=15)


# ---------------------------------------------------------------------------
# report_requests
# ---------------------------------------------------------------------------
def fetch_requested() -> list:
    sql = ("SELECT req_id, user_key, requester_email, report_key, report_name, params "
           "FROM report_requests WHERE status='requested' ORDER BY created_at")
    with _conn() as con, con.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def claim_request(req_id: str) -> bool:
    """Claim before working. Proceed ONLY if exactly 1 row changed — this is what
    makes a second poller safe."""
    with _conn() as con, con.cursor() as cur:
        cur.execute("UPDATE report_requests SET status='processing' "
                    "WHERE req_id=%s AND status='requested'", (req_id,))
        return cur.rowcount == 1


def set_request_status(req_id: str, status: str) -> None:
    if status not in ("done", "failed", "requested", "processing"):
        raise ValueError(f"bad status: {status}")
    with _conn() as con, con.cursor() as cur:
        cur.execute("UPDATE report_requests SET status=%s WHERE req_id=%s",
                    (status, req_id))


# ---------------------------------------------------------------------------
# mis_types catalog sync (Neon -> local), mirroring neon_sync.sync
# ---------------------------------------------------------------------------
def fetch_mis_catalog() -> list:
    sql = ("SELECT key, name, handler, active, sort_order, params_hint "
           "FROM mis_types ORDER BY sort_order, key")
    with _conn() as con, con.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def sync_catalog(db_path) -> dict:
    """Pull the shared report catalog. ONLY key/name/handler/enabled/sort_order
    come from Neon — out_folder, schedule and dump triggers are LOCAL to this
    box and a sync never clobbers them."""
    import mis_flows as mf
    try:
        rows = fetch_mis_catalog()
    except Exception as e:
        return {"error": str(e)}

    def _truthy(v):
        return str(v or "").strip().lower() in ("1", "true", "t", "yes", "y", "active")

    created = updated = 0
    for r in rows:
        key = str(r.get("key") or "").strip()
        if not key:
            continue
        cur = mf.get_mis_type(key, db_path=db_path)
        mf.upsert_mis_type(
            key,
            r.get("name") or key,
            enabled=1 if _truthy(r.get("active")) else 0,
            sort_order=r.get("sort_order") or 100,
            out_folder=(cur or {}).get("out_folder"),          # keep local
            handler=r.get("handler"),
            source="neon",
            schedule_time=(cur or {}).get("schedule_time"),    # keep local
            schedule_days=(cur or {}).get("schedule_days") or "1111100",
            trigger=mf.get_trigger(cur) if cur else None,      # keep local
            db_path=db_path)
        if cur:
            updated += 1
        else:
            created += 1
    return {"created": created, "updated": updated, "total": len(rows)}


def params_hints() -> dict:
    """report_key -> params_hint, so the UI can show what a report expects."""
    try:
        return {r["key"]: (r.get("params_hint") or "") for r in fetch_mis_catalog()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Recipients — principal expansion, deny by default
# ---------------------------------------------------------------------------
_ACCESS_ROWS = ("SELECT LOWER(COALESCE(principal_type,'')), COALESCE(principal,'') "
                "FROM mis_report_access WHERE report_key=%s")

# u.active is cast to text: it may be boolean or int, and a bare `WHERE u.active`
# breaks on the int flavour.
_EXPAND = """
SELECT DISTINCT u.email
FROM mis_report_access a
JOIN users u ON (
        (LOWER(a.principal_type) IN ('user','user_key')
         AND LOWER(u.user_key) = LOWER(a.principal))
     OR (LOWER(a.principal_type) = 'role'
         AND (LOWER(COALESCE(u.role,'')) = LOWER(a.principal)
           OR LOWER(COALESCE(u.login_role,'')) = LOWER(a.principal)))
     OR (LOWER(a.principal_type) IN ('department','dept')
         AND LOWER(COALESCE(u.department,'')) = LOWER(a.principal))
     OR (LOWER(a.principal_type) = 'email'
         AND LOWER(COALESCE(u.email,'')) = LOWER(a.principal))
     OR (LOWER(a.principal_type) IN ('all','everyone','*'))
)
WHERE a.report_key = %s
  AND COALESCE(u.email,'') <> ''
  AND (u.active IS NULL OR LOWER(u.active::text) NOT IN ('false','f','0','no'))
"""

KNOWN_PRINCIPALS = {"user", "user_key", "role", "department", "dept", "email",
                    "all", "everyone", "*"}


def access_rows(report_key: str) -> list:
    with _conn() as con, con.cursor() as cur:
        cur.execute(_ACCESS_ROWS, (report_key,))
        return [(t, p) for t, p in cur.fetchall()]


def resolve_recipients(report_key: str) -> dict:
    """
    {"emails": [...], "source": "access"|"admin"|"none", "rules": [...], "error": str|None}

    No access rows        -> admin_emails (deny by default).
    Rows matching NOBODY  -> ERROR, not an admin fallback. A rule that matches
                             no one is a typo, not an instruction to reroute the
                             report; surfacing it beats silently sending it
                             somewhere else for six months.
    No admins configured  -> empty + error. The poller fails the run rather than
                             reporting success on a report nobody received.
    """
    admins = admin_emails()
    try:
        rules = access_rows(report_key)
    except Exception as e:
        return {"emails": [], "source": "none", "rules": [],
                "error": f"cannot read mis_report_access: {e}"}

    if not rules:
        if admins:
            return {"emails": admins, "source": "admin", "rules": [], "error": None}
        return {"emails": [], "source": "admin", "rules": [],
                "error": (f"no access rows for '{report_key}' and no admin_emails "
                          f"in {neon_sync.SECRETS_PATH}")}

    try:
        with _conn() as con, con.cursor() as cur:
            cur.execute(_EXPAND, (report_key,))
            emails = sorted({r[0].strip() for r in cur.fetchall()
                             if r and r[0] and "@" in r[0]})
    except Exception as e:
        return {"emails": [], "source": "none", "rules": rules,
                "error": f"principal expansion failed: {e}"}

    if emails:
        return {"emails": emails, "source": "access", "rules": rules, "error": None}

    return {"emails": [], "source": "none", "rules": rules,
            "error": (f"{len(rules)} access rule(s) on '{report_key}' match no active "
                      f"user with an email: "
                      + "; ".join(f"{t}={p}" for t, p in rules))}


def ping():
    try:
        with _conn() as con, con.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return None
    except Exception as e:
        return str(e)


def audit() -> None:
    with _conn() as con, con.cursor() as cur:
        cur.execute("SELECT DISTINCT LOWER(COALESCE(principal_type,'')) "
                    "FROM mis_report_access ORDER BY 1")
        kinds = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT key, name FROM mis_types ORDER BY sort_order, key")
        reports = cur.fetchall()

    print("\nprincipal_type values in use:", ", ".join(kinds) or "(none)")
    unknown = [k for k in kinds if k and k not in KNOWN_PRINCIPALS]
    if unknown:
        print("  !! NOT HANDLED by the expander:", ", ".join(unknown))
        print("     Those reports will resolve to NOBODY. Extend _EXPAND.")

    print("\nrecipients per report:")
    for key, _name in reports:
        r = resolve_recipients(key)
        if r["error"]:
            print(f"  {key:<22} PROBLEM  {r['error']}")
        else:
            print(f"  {key:<22} [{r['source']}] {', '.join(r['emails'])}")


if __name__ == "__main__":
    print("secrets      :", neon_sync.SECRETS_PATH)
    u = load_neon_url()
    print("neon url     :", u.split("@")[-1] if u else "NOT FOUND")
    print("channel_bind :", "STILL PRESENT — BUG" if "channel_binding" in u else "stripped")
    print("admin emails :", ", ".join(admin_emails()) or "NONE — add admin_emails to secrets.toml")
    err = ping()
    print("connection   :", "OK" if err is None else f"FAILED — {err}")
    if err is None:
        audit()
