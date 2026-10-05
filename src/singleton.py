from __future__ import annotations

import atexit
import logging
import os
import re

log = logging.getLogger(__name__)

_MUTEX_HANDLE = None
_PID_PATH = None


def acquire_single_instance(name: str = "Local\\VoiceIA_Agent_Mutex") -> bool:
    """Return True if this process owns the singleton. False if another is running."""
    global _MUTEX_HANDLE, _PID_PATH

    # Pidfile first — catches duplicates when one process has pywin32 and another doesn't
    if not _acquire_pidfile(name):
        return False

    try:
        import win32api
        import win32event
        import winerror
    except ImportError:
        return True

    handle = win32event.CreateMutex(None, False, name)
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        log.error("Another instance is already running (%s) — exit", name)
        try:
            win32api.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass
        _release_pidfile()
        return False
    _MUTEX_HANDLE = handle

    def _release() -> None:
        global _MUTEX_HANDLE
        if _MUTEX_HANDLE is not None:
            try:
                win32api.CloseHandle(_MUTEX_HANDLE)
            except Exception:  # noqa: BLE001
                pass
            _MUTEX_HANDLE = None
        _release_pidfile()

    atexit.register(_release)
    return True


def _pid_filename(mutex_name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", mutex_name).strip("_").lower()
    return f"{slug}.pid"


def _acquire_pidfile(mutex_name: str) -> bool:
    global _PID_PATH
    from .config import ROOT

    path = ROOT / "outbox" / _pid_filename(mutex_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            old = int(path.read_text(encoding="utf-8").strip())
            if old != os.getpid() and _pid_alive(old):
                log.error("Another instance (pid=%s, %s) is already running — exit", old, path.name)
                return False
        except Exception:  # noqa: BLE001
            pass
    path.write_text(str(os.getpid()), encoding="utf-8")
    _PID_PATH = path
    return True


def _release_pidfile() -> None:
    global _PID_PATH
    path = _PID_PATH
    if path is None:
        return
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    _PID_PATH = None


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return exit_code.value == 259  # STILL_ACTIVE
    except Exception:  # noqa: BLE001
        return False
