"""Long-lived publisher browser session broker.

The broker keeps one CloakBrowser context alive per publisher/profile and
accepts DOI batch jobs through a small file queue. It intentionally stores no
cookie values in the broker state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

from ..config import DATA_DIR as DEFAULT_BASE_DIR


BROKER_ROOT = DEFAULT_BASE_DIR / "brokers"


@dataclass
class BrokerState:
    publisher: str
    profile_dir: str
    pid: int
    queue_dir: str
    started_at: str
    ttl_seconds: int
    heartbeat_at: str = ""


def broker_key(publisher: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in publisher.strip().lower())


def broker_dir(publisher: str) -> Path:
    return BROKER_ROOT / broker_key(publisher)


def broker_state_path(publisher: str) -> Path:
    return broker_dir(publisher) / "state.json"


def broker_stop_path(publisher: str) -> Path:
    return broker_dir(publisher) / "stop"


def load_broker_state(publisher: str) -> dict[str, Any] | None:
    path = broker_state_path(publisher)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_broker_state(state: BrokerState) -> None:
    path = broker_state_path(state.publisher)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8")


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def broker_is_running(publisher: str) -> bool:
    state = load_broker_state(publisher)
    if not state:
        return False
    return pid_is_running(int(state.get("pid") or 0))


def start_broker_process(
    *,
    publisher: str,
    browser_profile: str,
    institution: str,
    ttl_seconds: int,
    cwd: str | Path,
) -> subprocess.Popen[Any]:
    root = broker_dir(publisher)
    root.mkdir(parents=True, exist_ok=True)
    broker_stop_path(publisher).unlink(missing_ok=True)
    stdout = root / "broker.out.log"
    stderr = root / "broker.err.log"
    args = [
        sys.executable,
        "-m",
        "instsci.cli",  # TODO: port session-broker-run to scansci_pdf CLI
        "session-broker-run",
        "--publisher",
        publisher,
        "--browser-profile",
        browser_profile,
        "--institution",
        institution,
        "--ttl",
        str(ttl_seconds),
    ]
    return subprocess.Popen(
        args,
        cwd=str(cwd),
        stdout=stdout.open("a", encoding="utf-8"),
        stderr=stderr.open("a", encoding="utf-8"),
        stdin=subprocess.DEVNULL,
    )


def submit_broker_job(
    *,
    publisher: str,
    records: list[dict[str, str]],
    output_dir: str,
    institution: str,
    login_timeout: int,
    pdf_timeout: int,
    post_login_hold: int,
    post_run_hold: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    state = load_broker_state(publisher)
    if not state:
        raise RuntimeError(f"No broker state for {publisher}")
    queue_dir = Path(str(state["queue_dir"]))
    queue_dir.mkdir(parents=True, exist_ok=True)
    job_id = uuid4().hex
    job_path = queue_dir / f"{job_id}.json"
    done_path = queue_dir / f"{job_id}.done.json"
    job = {
        "id": job_id,
        "publisher": publisher,
        "records": records,
        "output_dir": output_dir,
        "institution": institution,
        "login_timeout": login_timeout,
        "pdf_timeout": pdf_timeout,
        "post_login_hold": post_login_hold,
        "post_run_hold": post_run_hold,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    deadline = time.time() + max(1, timeout_seconds)
    while time.time() < deadline:
        if done_path.exists():
            return json.loads(done_path.read_text(encoding="utf-8"))
        if not broker_is_running(publisher):
            raise RuntimeError(f"Broker for {publisher} stopped before job completed")
        time.sleep(2)
    raise TimeoutError(f"Broker job timed out after {timeout_seconds}s: {job_id}")
