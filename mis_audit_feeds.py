# -*- coding: utf-8 -*-
"""Show every dump type: steps, rules, save_folder — to spot duplicates and gaps.
    python mis_audit_feeds.py
"""
import json
import dump_flows as df

types = df.list_dump_types()
print(f"{len(types)} dump types\n")
print(f"{'key':<32} {'steps':<6} {'rules':<6} {'folder'}")
print("-"*90)
for t in types:
    key = t["key"]
    n_steps = len(df.get_steps(key))
    try:
        rules = json.loads(t.get("recognition_json") or '{"groups":[]}').get("groups", [])
        n_rules = sum(len(g.get("conditions", [])) for g in rules)
    except Exception:
        n_rules = 0
    flag = ""
    if n_steps == 0 and n_rules > 0:
        flag = "  <-- catches mail but NO STEPS (will fail)"
    elif n_steps == 0 and n_rules == 0:
        flag = "  <-- empty (no steps, no rules)"
    en = "" if t.get("enabled") else " [disabled]"
    print(f"{key:<32} {n_steps:<6} {n_rules:<6} {t.get('save_folder') or '-'}{en}{flag}")

# highlight likely duplicates (normalized key match)
print("\n--- possible duplicates (same feed, different key) ---")
norm = {}
for t in types:
    k = t["key"].lower().replace("_", " ").replace("-", " ").strip()
    norm.setdefault(k, []).append(t["key"])
for k, keys in norm.items():
    if len(keys) > 1:
        detail = []
        for kk in keys:
            detail.append(f"{kk} ({len(df.get_steps(kk))} steps)")
        print("  ", " == ".join(detail))

# show rules for each so you can compare
print("\n--- rules per feed (to compare duplicates) ---")
for t in types:
    try:
        groups = json.loads(t.get("recognition_json") or '{"groups":[]}').get("groups", [])
    except Exception:
        groups = []
    conds = []
    for g in groups:
        for c in g.get("conditions", []):
            v = c.get("value") or c.get("values") or ""
            conds.append(f"{c.get('field')}:{c.get('op')}:{v}")
    print(f"  {t['key']:<32} {'; '.join(conds) if conds else '(no rules)'}")
