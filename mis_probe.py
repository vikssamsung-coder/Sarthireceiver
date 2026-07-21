# -*- coding: utf-8 -*-
r"""
mis_probe.py — run ONE report's steps and show, per step, exactly what it
printed and whether it emitted the OUTPUT= line the engine needs.

Use this when a report "runs fine" but nothing gets emailed — almost always
because no step printed:  OUTPUT=<absolute path>

    python mis_probe.py partner_dashboard
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import dump_flows as df
import mis_flows as mf

OUTPUT_PREFIX = "OUTPUT="


def main():
    if len(sys.argv) < 2:
        print("usage: python mis_probe.py <report_key>")
        reports = mf.list_mis_types(db_path=mf.DEFAULT_DB)
        if reports:
            print("known reports:", ", ".join(t["key"] for t in reports))
        return 1

    key = sys.argv[1]
    t = mf.get_mis_type(key, db_path=mf.DEFAULT_DB)
    if not t:
        print(f"no such report: {key}")
        return 1

    out_folder = t.get("out_folder") or ""
    context = {
        "report_key": key, "report_name": t.get("name") or key,
        "params": "", "out_folder": out_folder, "requester_email": "",
        "user_key": "", "req_id": "", "trigger": "probe", "run_id": "0",
        "today": mf._today(), "output_path": "",
    }
    steps = mf.build_mis_steps(key, context, db_path=mf.DEFAULT_DB)
    if not steps:
        print(f"{key} has no enabled steps.")
        return 1

    print(f"probing {key} — {len(steps)} step(s)\n")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    final_output = ""
    for i, s in enumerate(steps, 1):
        target = s["target_path"]
        print(f"── step {i}: {s['step_name']}  ({target})")
        if not Path(target).is_file():
            print("   ✗ script not found\n")
            continue

        cmd = ([target] + s["args"]) if s["kind"] == "bat" \
            else [sys.executable, target] + s["args"]
        try:
            p = subprocess.run(cmd, cwd=s["working_dir"] or str(Path(target).parent),
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", env=env, timeout=1800,
                               shell=(s["kind"] == "bat"))
        except Exception as e:
            print(f"   ✗ failed to launch: {e}\n")
            continue

        out_lines = (p.stdout or "").splitlines()
        outputs = [ln.strip()[len(OUTPUT_PREFIX):].strip().strip('"')
                   for ln in out_lines if ln.strip().startswith(OUTPUT_PREFIX)]

        print(f"   exit code : {p.returncode}")
        print(f"   stdout    : {len(out_lines)} line(s)")
        for ln in out_lines[-8:]:
            print(f"      | {ln}")
        if p.stderr and p.stderr.strip():
            print("   stderr (last 4):")
            for ln in p.stderr.strip().splitlines()[-4:]:
                print(f"      ! {ln}")

        if outputs:
            final_output = outputs[-1]
            exists = Path(final_output).is_file()
            print(f"   OUTPUT=   : {final_output}  {'(exists)' if exists else '(MISSING FILE!)'}")
        else:
            print("   OUTPUT=   : ✗ none printed")
        print()

    print("=" * 60)
    if final_output:
        print(f"engine would email: {final_output}")
        if not Path(final_output).is_file():
            print("...but that file does not exist — the run would FAIL.")
    else:
        print("NO step printed OUTPUT=<path>.  The engine has nothing to email,")
        print("so this report FAILS even though the scripts run.")
        print()
        print("FIX: in whichever script writes the final file, add ONE line at the")
        print("very end, after the file is saved:")
        print()
        print('     print(f"OUTPUT={os.path.abspath(final_path)}")')
        print()
        print("Only the LAST OUTPUT= line in the whole run is used, so put it in")
        print("the last step (the one that produces the deliverable).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
