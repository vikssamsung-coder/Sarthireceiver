# -*- coding: utf-8 -*-
r"""
outlook_com.py — the ONE gate to Outlook.

Outlook exposes a SINGLE-INSTANCE COM automation server. Before MIS, only the
receiver talked to it, so a bare Dispatch("Outlook.Application") was fine. Now
TWO processes (receiver poll + MIS mailer) can reach for it, and when they
overlap the server throws:

    (-2146959355, 'Server execution failed')   [0x80080005]

...for BOTH — which is exactly the "worked until MIS was added, now even the
receiver/extractor break" symptom.

This module fixes it two ways:

  1. Per-process COM apartment — pythoncom.CoInitialize() / CoUninitialize()
     around every use, so the two processes don't corrupt a shared apartment.

  2. A cross-process file lock — receiver and MIS take turns; they never call
     into Outlook at the same instant. The lock lives beside the registry so
     both processes agree on it without knowing about each other.

Usage:

    from outlook_com import outlook_app, outlook_namespace

    with outlook_app() as app:          # for sending (MIS)
        mail = app.CreateItem(0)
        ...

    with outlook_namespace() as ns:     # for reading (receiver)
        inbox = ns.GetDefaultFolder(6)
        ...
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

import dump_flows as df

LOCK_PATH = Path(df.DEFAULT_DB).parent / "outlook_com.lock"
LOCK_TIMEOUT = int(os.environ.get("SARTHI_OUTLOOK_LOCK_TIMEOUT", "300"))  # 5 min
LOCK_STALE = int(os.environ.get("SARTHI_OUTLOOK_LOCK_STALE", "600"))      # 10 min


# ---------------------------------------------------------------------------
# cross-process lock (Windows-safe: exclusive create, no fcntl needed)
# ---------------------------------------------------------------------------
@contextmanager
def _cross_process_lock(who: str):
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = None
    deadline = time.time() + LOCK_TIMEOUT
    while True:
        # break a stale lock (a process that died holding it)
        try:
            if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime > LOCK_STALE:
                LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{who} {os.getpid()} {time.time():.0f}".encode())
            break
        except FileExistsError:
            if time.time() > deadline:
                raise TimeoutError(
                    f"could not acquire Outlook lock within {LOCK_TIMEOUT}s "
                    f"(held by another process). Lock: {LOCK_PATH}")
            time.sleep(0.5)
    try:
        yield
    finally:
        try:
            if fd is not None:
                os.close(fd)
            LOCK_PATH.unlink(missing_ok=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# COM context managers
# ---------------------------------------------------------------------------
@contextmanager
def _com_apartment():
    """Initialise a COM apartment for THIS thread, tear it down after. Without
    this, a background thread/process gets 'CoInitialize has not been called'
    or corrupts a shared apartment."""
    try:
        import pythoncom
    except ImportError:
        # not on Windows / pywin32 missing — let the caller's Dispatch raise
        yield
        return
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


@contextmanager
def outlook_app(who: str = "app"):
    """Serialized, apartment-safe Outlook.Application. Use for sending mail."""
    import win32com.client
    with _cross_process_lock(who), _com_apartment():
        app = win32com.client.Dispatch("Outlook.Application")
        yield app


@contextmanager
def outlook_namespace(who: str = "receiver"):
    """Serialized, apartment-safe MAPI namespace. Use for reading the inbox."""
    import win32com.client
    with _cross_process_lock(who), _com_apartment():
        ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        yield ns
