# -*- coding: utf-8 -*-
r"""
sarthi_service.py — the background services, in ONE process.

Supervises two children:
  sarthi_receiver.py --watch --interval 60    (Outlook poller)
  mis_poller.py      --watch --interval 60    (requests + schedules + queue worker)

WHY CHILDREN, NOT THREADS: pywin32 COM wants its own process, and a COM hang in
the receiver must not freeze the MIS side. Separate processes keep the failure
modes separate — which was the whole point of splitting them — while giving one
thing to launch and supervise.

Each child is restarted if it dies. If one crash-loops it is given up on and the
OTHER KEEPS RUNNING — a broken report can never take mail processing down.

Normally started FOR you by app.py (see service_manager.py). Can also be run
directly:

    python sarthi_service.py                both
    python sarthi_service.py --no-mis       receiver only
    python sarthi_service.py --no-receiver  MIS only
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESTART_DELAY = 15          # seconds before relaunching a dead child
MAX_RAPID_DEATHS = 5        # give up if it dies this many times inside...
RAPID_WINDOW = 120          # ...this many seconds

_lock = threading.Lock()


def say(tag, msg):
    with _lock:
        print(f"{datetime.now():%H:%M:%S} [{tag:<8}] {msg}", flush=True)


class Child:
    def __init__(self, tag, args):
        self.tag = tag
        self.args = args
        self.proc = None
        self.stop = False
        self.deaths = []

    def _pump(self):
        for raw in self.proc.stdout:
            line = raw.rstrip()
            if line:
                say(self.tag, line)

    def _rapid(self):
        now = time.time()
        self.deaths = [t for t in self.deaths if now - t < RAPID_WINDOW]
        return len(self.deaths)

    def run(self):
        if not (HERE / self.args[0]).is_file():
            say(self.tag, f"MISSING {self.args[0]} — not starting")
            return

        while not self.stop:
            say(self.tag, "starting: " + " ".join(self.args))
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, "-u"] + self.args, cwd=str(HERE),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1)
            except Exception as e:
                say(self.tag, f"could not start: {e}")
                return

            try:
                self._pump()
            except Exception:
                pass

            rc = self.proc.wait()
            if self.stop:
                say(self.tag, "stopped")
                return

            self.deaths.append(time.time())
            n = self._rapid()
            say(self.tag, f"exited (code {rc}) — death {n} in {RAPID_WINDOW}s")

            if n >= MAX_RAPID_DEATHS:
                say(self.tag, "GIVING UP — dying too fast. Fix it, then restart the "
                              "service. The other loop keeps running.")
                return

            say(self.tag, f"restarting in {RESTART_DELAY}s")
            for _ in range(RESTART_DELAY):
                if self.stop:
                    return
                time.sleep(1)

    def kill(self):
        self.stop = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


def main(argv):
    ap = argparse.ArgumentParser(description="Sarthi services — receiver + MIS")
    ap.add_argument("--no-receiver", action="store_true")
    ap.add_argument("--no-mis", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    a = ap.parse_args(argv)

    children = []
    if not a.no_receiver:
        children.append(Child("receiver", ["sarthi_receiver.py", "--watch",
                                           "--interval", str(a.interval)]))
    if not a.no_mis:
        children.append(Child("mis", ["mis_poller.py", "--watch",
                                      "--interval", str(a.interval)]))
    if not children:
        print("nothing to run")
        return 1

    say("service", "up — " + ", ".join(c.tag for c in children))

    threads = []
    for c in children:
        t = threading.Thread(target=c.run, daemon=True, name=c.tag)
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
        say("service", "all loops gave up — nothing left to supervise")
        return 1
    except KeyboardInterrupt:
        say("service", "stopping...")
        for c in children:
            c.kill()
        for t in threads:
            t.join(timeout=5)
        say("service", "stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
