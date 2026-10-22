"""Certificate verification must not depend on unrelated environment state.

`http_utils` read "some HTTP proxy variable is set" as "the network is trusted"
and turned verification off for every request on the shared retry path, warning
suppressed. On Windows those variables are commonly machine-wide, so a default
install performed no certificate validation at all while institutional cookies
and publisher API keys travelled over it. Importing the package also set
``NO_PROXY=*`` process-wide, which disabled proxy routing for every other library
sharing the process — and an MCP server commonly shares its process (#70).

Self-signed campus connectors are the one case that legitimately needs an
exception; they now opt in through ``institutional_allow_self_signed``.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PROXY_ENV = {
    "HTTP_PROXY": "http://127.0.0.1:7897",
    "HTTPS_PROXY": "http://127.0.0.1:7897",
    "http_proxy": "http://127.0.0.1:7897",
    "https_proxy": "http://127.0.0.1:7897",
}


def _run_in_subprocess(code: str, env_overrides: dict[str, str]) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src")
    env.update(env_overrides)
    proc = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr[-500:]
    return proc.stdout.strip()


# --------------------------------------------------------------------------- #
# 1. verification stays on regardless of proxy variables
# --------------------------------------------------------------------------- #


def test_shared_retry_path_keeps_verification_on_with_proxy_env(monkeypatch):
    from scansci_pdf import http_utils

    for key, value in PROXY_ENV.items():
        monkeypatch.setenv(key, value)
    reloaded = importlib.reload(http_utils)
    try:
        assert reloaded._SSL_VERIFY is True
    finally:
        importlib.reload(http_utils)


def test_institutional_retry_path_keeps_verification_on_with_proxy_env(monkeypatch):
    """The institutional path is a shim over the canonical module.

    Reloading the shim would only re-execute the shim, and the value it
    reads is the canonical module's already-computed one -- the test would
    prove nothing. The decision has to be re-made in the canonical module,
    which is where the code that makes it lives; the shim then has to hand
    back that same value.
    """
    from scansci_pdf import http_utils
    from scansci_pdf.institutional import http_utils as institutional_http_utils

    for key, value in PROXY_ENV.items():
        monkeypatch.setenv(key, value)
    try:
        importlib.reload(http_utils)
        assert institutional_http_utils._SSL_VERIFY is True
        assert http_utils._SSL_VERIFY is True
    finally:
        importlib.reload(http_utils)


def test_request_with_retry_forwards_verify_true(monkeypatch):
    from scansci_pdf import http_utils

    captured: dict = {}

    class _Response:
        status_code = 200

    def fake_request(method, url, **kwargs):
        captured.update(kwargs)
        return _Response()

    for key, value in PROXY_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(http_utils.requests, "request", fake_request)

    http_utils.request_with_retry("GET", "https://example.org/x")

    assert captured["verify"] is True


# --------------------------------------------------------------------------- #
# 2. self-signed campus connectors are an explicit opt-in
# --------------------------------------------------------------------------- #


def test_self_signed_is_off_by_default():
    from scansci_pdf.network import allow_self_signed

    assert allow_self_signed({}) is False
    assert allow_self_signed(None) is False


def test_self_signed_can_be_opted_into():
    from scansci_pdf.network import allow_self_signed

    assert allow_self_signed({"institutional_allow_self_signed": True}) is True


# --------------------------------------------------------------------------- #
# 3. importing the package no longer rewrites process-global proxy state
# --------------------------------------------------------------------------- #


def test_import_preserves_an_existing_no_proxy():
    out = _run_in_subprocess(
        "import os, scansci_pdf; print(os.environ.get('NO_PROXY'))",
        {"NO_PROXY": "keep-me", "SSL_CERT_FILE": ""},
    )
    assert out == "keep-me"


def test_import_does_not_invent_no_proxy_when_none_was_set():
    out = _run_in_subprocess(
        "import os, scansci_pdf; print(os.environ.get('NO_PROXY') or 'unset')",
        {"NO_PROXY": "", "no_proxy": "", "SSL_CERT_FILE": ""},
    )
    assert out == "unset"


def test_the_ide_proxy_workaround_still_applies_where_it_was_written_for(tmp_path):
    """The ZCode situation — its proxy CA in SSL_CERT_FILE — keeps the workaround."""
    cert = tmp_path / "root-ca-cert.pem"
    cert.write_bytes(b"not a real certificate\n")

    out = _run_in_subprocess(
        "import os, scansci_pdf; print(os.environ.get('NO_PROXY'))",
        {"NO_PROXY": "", "SSL_CERT_FILE": str(cert)},
    )
    assert out == "*"
