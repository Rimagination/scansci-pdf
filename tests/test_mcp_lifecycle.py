"""MCP-LIFECYCLE-01: the stdio server must exit when stdin closes (EOF).

Regression: a client that sends initialize + tools/list and then closes its
stdin must not leave the server process hanging forever.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _spawn_server():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    # Binary pipes: Windows console encoding produces GBK bytes on stderr for
    # Chinese log lines; decoding happens manually with errors="replace".
    return subprocess.Popen(
        [sys.executable, "-m", "scansci_pdf.main", "run"],
        cwd=str(REPO),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _rpc(method: str, params: dict, msg_id: int | None = None) -> str:
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params:
        body["params"] = params
    return json.dumps(body, ensure_ascii=False)


def test_stdio_server_exits_on_eof():
    proc = _spawn_server()
    try:
        payload = (
            "\n".join(
                [
                    _rpc("initialize", {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "lifecycle-test", "version": "0"},
                    }, msg_id=1),
                    _rpc("notifications/initialized", {}),
                    _rpc("tools/list", {}, msg_id=2),
                ]
            )
            + "\n"
        )
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.close()

        t0 = time.monotonic()
        out_bytes, err_bytes = proc.communicate(timeout=20)
        elapsed = time.monotonic() - t0
        out = out_bytes.decode("utf-8", errors="replace")
        err = err_bytes.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err_bytes = proc.communicate()
        err = err_bytes.decode("utf-8", errors="replace")
        raise AssertionError(
            f"MCP stdio server did not exit after stdin EOF (stderr: {err[-500:]})"
        ) from None

    assert proc.returncode == 0, f"exit={proc.returncode}, stderr={err[-800:]}"
    assert elapsed < 15, f"exit took {elapsed:.1f}s"
    # tools/list response is present in stdout
    assert '"tools"' in out or '"jsonrpc"' in out