"""
sarthi_service.py — ONE process, ONE window, both loops.

Supervises the Outlook receiver and the MIS poller as child processes. It does
not import either one, so it needs no knowledge of their internals and cannot
break them.

Why children and not threads: pywin32 COM wants its own process, and a COM hang
in the receiver must not freeze the MIS side. Separate processes keep the
failure modes separate — which was the point of splitting them — while giving
you a single window and a single thing to put in Task Scheduler.

Each child is restarted if it dies. Output is prefixed and interleaved.

    python sarthi_service.py              # both
    python sarthi_service.py --no-mis     # receiver only (old behaviour)
    python sarthi_service.py --no-receiver
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
RESTART_DELAY = 15          # seconds before relaunching a dead child
MAX_RAPID_RESTARTS = 5      # give up if it dies this many times inside...
RAPID_WINDOW = 120          # ...this many seconds

_print_lock = threading.Lock()


def say(tag: str, msg: str) -> None:
    with _print_lock:
        print("%s [%-8s] %s" % (datetime.now().strftime("%H:%M:%S"), tag, msg),
              flush=True)


class Child:
    def __init__(self, tag: str, args: List[str]):
        self.tag = tag
        self.args = args
        self.proc: Optional[subprocess.Popen] = None
        self.stop = False
        self.deaths: List[float] = []

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.rstrip()
            if line:
                say(self.tag, line)

    def _rapid_deaths(self) -> int:
        now = time.time()
        self.deaths = [t for t in self.deaths if now - t < RAPID_WINDOW]
        return len(self.deaths)

    def run(self) -> None:
        script = self.args[0]
        if not os.path.isfile(os.path.join(HERE, script)):
            say(self.tag, "MISSING %s — not starting" % script)
            return

        while not self.stop:
            cmd = [sys.executable, "-u"] + self.args
            say(self.tag, "starting: %s" % " ".join(self.args))
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=HERE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                )
            except Exception as e:
                say(self.tag, "could not start: %s" % e)
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
            n = self._rapid_deaths()
            say(self.tag, "exited (code %s) — death %d in %ds" % (rc, n, RAPID_WINDOW))

            if n >= MAX_RAPID_RESTARTS:
                say(self.tag, "GIVING UP — dying too fast. Fix it, then restart "
                              "the service. The other loop keeps running.")
                return

            say(self.tag, "restarting in %ss" % RESTART_DELAY)
            for _ in range(RESTART_DELAY):
                if self.stop:
                    return
                time.sleep(1)

    def kill(self) -> None:
        self.stop = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description="Sarthi service — receiver + MIS")
    ap.add_argument("--no-receiver", action="store_true")
    ap.add_argument("--no-mis", action="store_true")
    ap.add_argument("--mis-interval", type=int, default=60)
    ap.add_argument("--receiver-args", default="--watch",
                    help="args passed to sarthi_receiver.py")
    a = ap.parse_args(argv)

    children: List[Child] = []
    if not a.no_receiver:
        children.append(Child("receiver",
                              ["sarthi_receiver.py"] + a.receiver_args.split()))
    if not a.no_mis:
        children.append(Child("mis",
                              ["mis_poller.py", "--watch",
                               "--interval", str(a.mis_interval)]))

    if not children:
        print("nothing to run")
        return 1

    say("service", "Sarthi service up — %s" % ", ".join(c.tag for c in children))
    say("service", "Ctrl+C to stop everything")

    threads = []
    for c in children:
        t = threading.Thread(target=c.run, daemon=True, name=c.tag)
        t.start()
        threads.append(t)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1)
        say("service", "all loops have given up — nothing left to supervise")
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
