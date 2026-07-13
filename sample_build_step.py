"""
sample_build_step.py — the step-script contract, in one page.

Copy this shape for every MIS build script.

RULES:
  1. Take what you need from argv. The engine renders args from the step's
     template; an empty placeholder drops itself AND its flag, so optional
     args are safe.
  2. Write your file wherever you like (--out is the type's out_folder).
  3. Print, as the LAST line on stdout:
         OUTPUT=<absolute path>
     No OUTPUT= line -> the engine FAILS the run. It will not guess.
  4. Exit non-zero on failure. The engine stops the sequence there.

Register it as a step with args like:
    --params {params} --out {out_folder} --report {report_key}
"""
import argparse
import os
import sys
from datetime import datetime


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output folder ({out_folder})")
    ap.add_argument("--params", default="", help="free text from the requester")
    ap.add_argument("--report", default="", help="{report_key}")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)

    # ---- build the report here ----
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(a.out, "%s_%s.csv" % (a.report or "report", stamp))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("report,params,built_at\n")
        fh.write("%s,%s,%s\n" % (a.report, a.params.replace(",", ";"), stamp))
    # --------------------------------

    print("rows written: 1")
    print("OUTPUT=%s" % os.path.abspath(path))   # <-- THE CONTRACT
    return 0


if __name__ == "__main__":
    sys.exit(main())
