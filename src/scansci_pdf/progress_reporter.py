"""Task progress reporting via a well-known JSON file.

Writers (batch / lanes / fetch) report per-paper progress here; readers (the
frosted-glass progress bar, ``scansci-pdf progress``) watch the file. The two
are decoupled on purpose: a headless background task stays GUI-free while the
floating bar makes its progress visible on the desktop.

File: <data_dir>/progress/current.json — atomically replaced (tmp + rename)
on every update, so readers never see a torn write.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {}
_STARTED_MONO: float = 0.0

STALE_SECONDS = 120  # reader shows "idle" when no update for this long


def _progress_dir() -> Path:
    data_dir = Path(os.environ.get("SCANSCI_PDF_DATA_DIR", str(Path.home() / ".scansci-pdf")))
    return data_dir / "progress"


def progress_path() -> Path:
    return _progress_dir() / "current.json"


def _atomic_write(payload: dict[str, Any]) -> None:
    path = progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pid_alive(pid: int) -> bool:
    """Windows-safe liveness probe. NEVER use os.kill(pid, 0) on Windows —
    any signal value there calls TerminateProcess and would murder the bar."""
    if pid <= 0:
        return False
    try:
        if os.name != "nt":
            os.kill(pid, 0)
            return True
        import ctypes

        SYNCHRONIZE = 0x00100000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return False
        kernel32.CloseHandle(int(handle))
        return True
    except Exception:
        return False


def ensure_progress_bar(config: dict[str, Any] | None = None) -> None:
    """Spawn the floating progress bar once (no-op when one is already up).

    Called whenever a task starts, so users always get visibility without
    launching anything by hand. The bar is an independent process; a stale
    pid lock (dead bar) is reclaimed. Disable via config
    ``progress_bar_auto: false`` or env ``SCANSCI_PROGRESS_BAR=0``.
    """
    if os.environ.get("SCANSCI_PROGRESS_BAR") == "0":
        return
    if os.environ.get("SCANSCI_PROGRESS_BAR_CHILD") == "1":
        return
    if config is not None and config.get("progress_bar_auto") is False:
        return
    lock = _progress_dir() / "bar.pid"
    try:
        if lock.exists():
            pid = int(lock.read_text(encoding="utf-8").strip() or 0)
            if _pid_alive(pid):
                return  # a bar is already up
            # else: stale lock (bar died) - reclaim below
    except (ValueError, OSError):
        return
    try:
        import subprocess
        import sys

        kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}
        env = {**os.environ, "SCANSCI_PROGRESS_BAR_CHILD": "1"}
        subprocess.Popen(
            [sys.executable, "-m", "scansci_pdf.progress_bar"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, **kwargs,
        )
    except Exception:
        pass


def start_task(task: str, total: int, **extra: Any) -> None:
    """Begin (or restart) the reported task with a known total."""
    global _STATE, _STARTED_MONO
    try:
        from .config import load_config

        ensure_progress_bar(load_config())
    except Exception:
        try:
            ensure_progress_bar()
        except Exception:
            pass
    with _LOCK:
        _STARTED_MONO = time.monotonic()
        _STATE = {
            "task": task,
            "total": int(total),
            "done": 0,
            "success": 0,
            "failed": 0,
            "current": "",
            "phase": "",
            "status": "running",
            "attention": {},
            "started_at": datetime.now().isoformat(timespec="seconds"),
            **extra,
        }
        _STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            _atomic_write(dict(_STATE))
        except Exception:
            pass


def update(**kw: Any) -> None:
    """Merge fields into the current report (done/success/failed/current/...)."""
    with _LOCK:
        if not _STATE:
            return
        _STATE.update(kw)
        _STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            _atomic_write(dict(_STATE))
        except Exception:
            pass


def advance(ok: bool, current: str = "", phase: str = "") -> None:
    """Record one finished item (the hot path for per-paper lanes)."""
    with _LOCK:
        if not _STATE:
            return
        _STATE["done"] = int(_STATE.get("done", 0)) + 1
        _STATE["success"] = int(_STATE.get("success", 0)) + (1 if ok else 0)
        _STATE["failed"] = int(_STATE.get("failed", 0)) + (0 if ok else 1)
        if current:
            _STATE["current"] = current
        if phase:
            _STATE["phase"] = phase
        _STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            _atomic_write(dict(_STATE))
        except Exception:
            pass


def set_attention(key: str, message: str, *, current: str = "", phase: str = "") -> None:
    """Show a manual-action reminder without stopping other workers."""
    with _LOCK:
        if not _STATE:
            return
        attention = _STATE.setdefault("attention", {})
        if not isinstance(attention, dict):
            attention = {}
            _STATE["attention"] = attention
        attention[str(key)] = {
            "message": str(message),
            "current": str(current or ""),
            "phase": str(phase or ""),
            "since": datetime.now().isoformat(timespec="seconds"),
        }
        _STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            _atomic_write(dict(_STATE))
        except Exception:
            pass


def set_output_dir(path) -> None:
    """Record the task's output folder — the progress bar shows it as a
    clickable link so users can jump straight to the downloads."""
    with _LOCK:
        if not _STATE:
            return
        _STATE["output_dir"] = str(path)
        _STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            _atomic_write(dict(_STATE))
        except Exception:
            pass


def clear_attention(key: str) -> None:
    """Remove one manual-action reminder after its browser page clears."""
    with _LOCK:
        if not _STATE:
            return
        attention = _STATE.get("attention")
        if not isinstance(attention, dict):
            return
        attention.pop(str(key), None)
        _STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            _atomic_write(dict(_STATE))
        except Exception:
            pass


def finish() -> None:
    """Mark the task done (the bar keeps the final state briefly, then idles)."""
    with _LOCK:
        if not _STATE:
            return
        _STATE["status"] = "done"
        _STATE["attention"] = {}
        _STATE["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            _atomic_write(dict(_STATE))
        except Exception:
            pass


def elapsed_sec() -> float:
    return max(0.0, time.monotonic() - _STARTED_MONO)


def read_state() -> dict[str, Any] | None:
    """Reader side: latest report, or None when absent/stale."""
    try:
        raw = progress_path().read_text(encoding="utf-8")
        state = json.loads(raw)
    except Exception:
        return None
    if not isinstance(state, dict):
        return None
    if state.get("status") == "done":
        return state  # finished states stay visible a while (reader decides)
    try:
        ts = datetime.fromisoformat(str(state.get("updated_at")))
        age = (datetime.now() - ts).total_seconds()
        if age > STALE_SECONDS:
            return None
    except Exception:
        pass
    return state
