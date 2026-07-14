# -*- coding: utf-8 -*-
r"""
service_manager.py — background services, started by the app.

Launching `streamlit run app.py` should be the ONLY thing you do. This starts
sarthi_service.py (Outlook receiver + MIS poller) behind it and keeps it alive.

WHY A DETACHED CHILD, NOT A THREAD:
  Streamlit re-runs the whole script on every widget click. A thread started in
  app.py would be spawned again on every rerun, and killed whenever the script
  is re-executed. And Outlook COM wants its own process. So: one detached child,
  guarded by a PID lock file, checked cheaply on each rerun.

The lock records pid + the exact command. Before trusting a pid we confirm the
process is alive AND is actually ours — Windows recycles pids, and killing some
unrelated python.exe because it inherited pid 8412 would be a genuinely bad day.

The child survives the app closing (that is the point: the receiver and the
schedule keep running). Reopening the app finds it and does not restart it.
Stop it from the Services screen.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import dump_flows as df

SERVICE = "sarthi_service.py"
HERE = Path(__file__).resolve().parent
STATE_DIR = Path(df.DEFAULT_DB).parent
LOCK = STATE_DIR / "sarthi_service.lock"
LOGFILE = STATE_DIR / "sarthi_service.log"

_last_check = {"at": 0.0, "result": None}
CHECK_EVERY_SEC = 5          # rerun-cheap: don't shell out on every click

IS_WIN = os.name == "nt"


# ---------------------------------------------------------------------------
# process identity
# ---------------------------------------------------------------------------
def _cmdline(pid: int) -> str:
    """Best-effort command line for a pid. '' if the process is gone."""
    try:
        import psutil
        return " ".join(psutil.Process(pid).cmdline())
    except ImportError:
        pass
    except Exception:
        return ""

    if IS_WIN:
        for cmd in (
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\").CommandLine"],
        ):
            try:
                out = subprocess.run(cmd, capture_output=True, text=True,
                                     timeout=10).stdout
                if out and out.strip():
                    return out
            except Exception:
                continue
        return ""

    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore").replace("\0", " ")
    except Exception:
        return ""


def _alive_and_ours(pid: int) -> bool:
    """Alive AND actually our service — pids get recycled."""
    if not pid:
        return False
    cl = _cmdline(pid)
    if not cl:
        return False
    return SERVICE.lower() in cl.lower()


# ---------------------------------------------------------------------------
# lock file
# ---------------------------------------------------------------------------
def _read_lock() -> dict:
    try:
        return json.loads(LOCK.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_lock(pid: int) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(json.dumps({
            "pid": pid, "service": SERVICE, "dir": str(HERE),
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }), encoding="utf-8")
    except Exception:
        pass


def _clear_lock() -> None:
    try:
        LOCK.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------
def status(force: bool = False) -> dict:
    """{"running": bool, "pid": int|None, "started_at": str|None, "log": Path}"""
    now = time.time()
    if not force and _last_check["result"] is not None \
            and now - _last_check["at"] < CHECK_EVERY_SEC:
        return _last_check["result"]

    lk = _read_lock()
    pid = int(lk.get("pid") or 0)
    running = _alive_and_ours(pid)
    if pid and not running:
        _clear_lock()                    # stale lock: the box rebooted, or it crashed

    res = {"running": running, "pid": pid if running else None,
           "started_at": lk.get("started_at") if running else None,
           "log": LOGFILE}
    _last_check.update(at=now, result=res)
    return res


def ensure_running() -> dict:
    """Start the services if they aren't up. Safe to call on EVERY Streamlit rerun."""
    st = status()
    if st["running"]:
        return st

    script = HERE / SERVICE
    if not script.is_file():
        return {"running": False, "pid": None, "started_at": None,
                "log": LOGFILE, "error": f"{SERVICE} not found in {HERE}"}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        log = open(LOGFILE, "a", buffering=1, encoding="utf-8", errors="replace")
        log.write(f"\n===== started by app at "
                  f"{datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
    except Exception:
        log = subprocess.DEVNULL

    kwargs = {"cwd": str(HERE), "stdout": log, "stderr": subprocess.STDOUT,
              "stdin": subprocess.DEVNULL}
    if IS_WIN:
        # Detach: no console window, survives the app closing, and Ctrl+C in the
        # Streamlit terminal doesn't kill it.
        CREATE_NO_WINDOW = 0x08000000
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = (CREATE_NO_WINDOW | DETACHED_PROCESS
                                   | CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True

    try:
        p = subprocess.Popen([sys.executable, "-u", str(script)], **kwargs)
    except Exception as e:
        return {"running": False, "pid": None, "started_at": None,
                "log": LOGFILE, "error": str(e)}

    _write_lock(p.pid)
    time.sleep(1.0)                       # let it fail fast if it's going to
    st = status(force=True)
    if not st["running"]:
        st["error"] = ("the service exited immediately — see the log")
    return st


def stop() -> bool:
    st = status(force=True)
    if not st["running"]:
        _clear_lock()
        return True
    pid = st["pid"]
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            os.kill(pid, 15)
    except Exception:
        return False
    time.sleep(1.0)
    ok = not _alive_and_ours(pid)
    if ok:
        _clear_lock()
    status(force=True)
    return ok


def restart() -> dict:
    stop()
    return ensure_running()


def tail_log(lines: int = 80) -> str:
    try:
        content = LOGFILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except Exception:
        return "(no log yet)"


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "start":
        print(ensure_running())
    elif cmd == "stop":
        print("stopped" if stop() else "could not stop")
    elif cmd == "restart":
        print(restart())
    else:
        print(status(force=True))
        print("\n--- log ---")
        print(tail_log(30))
