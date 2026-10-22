"""Operations that redirect traffic or choose an executable ask first.

From item 2 of #71: this server is driven by agents, so one misjudged tool call
could install an executable or repoint every request at another endpoint. The
gate is deliberately narrow -- the keys that decide *where traffic goes* and
*which binary runs* -- because a confirmation on every write makes the common
case clumsy without buying anything.
"""

from __future__ import annotations

import json

import pytest

from scansci_pdf import embedded_tor, server


def _json(payload: str) -> dict:
    return json.loads(payload)


@pytest.fixture
def applied(monkeypatch):
    """Capture config writes instead of touching the real config file."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(server, "update_config", lambda key, value: calls.append((key, value)))
    return calls


# --------------------------------------------------------------------------- #
# config keys that move the trust boundary
# --------------------------------------------------------------------------- #


# The gate is deliberately narrow: where traffic goes, and which binary runs.
# Everything else stays a one-call write.
GATED_KEYS = (
    "browser_executable",
    "browser_static_proxy",
    "camofox_url",
    "ezproxy_login_url",
    "flaresolverr_url",
    "instsci_base_url",
    "network_proxy",
    "proxy_pool",
    "tor_binary_path",
    "tor_proxy",
    "vpnsci_base_url",
)


def test_the_gated_set_is_exactly_these_keys():
    """Written out so a reviewer can see the boundary, and so widening it is a
    deliberate edit rather than an accident."""
    assert set(server.TRUST_BOUNDARY_CONFIG_KEYS) == set(GATED_KEYS)


@pytest.mark.parametrize("key", GATED_KEYS)
def test_trust_boundary_keys_require_confirmation(key, applied):
    result = _json(server._config_set_impl(key, "http://127.0.0.1:1"))

    assert result["success"] is False
    assert result["requires_confirmation"] is True
    assert applied == [], "nothing may be written before the confirmation"


@pytest.mark.parametrize("key", GATED_KEYS)
def test_trust_boundary_keys_apply_once_confirmed(key, applied):
    result = _json(server._config_set_impl(key, "http://127.0.0.1:1", confirmed=True))

    assert result["success"] is True
    assert applied == [(key, "http://127.0.0.1:1")]


def test_ordinary_keys_still_apply_without_confirmation(applied):
    result = _json(server._config_set_impl("email", "me@example.com"))

    assert result["success"] is True
    assert applied == [("email", "me@example.com")]


# --------------------------------------------------------------------------- #
# installing and starting Tor
# --------------------------------------------------------------------------- #


def test_tor_install_requires_confirmation(monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(server, "scansci_pdf_tor_install", lambda: called.append(1) or "{}")

    result = _json(server.scansci_pdf_tor(action="install"))

    assert result["requires_confirmation"] is True
    assert called == []


def test_tor_install_runs_when_confirmed(monkeypatch):
    monkeypatch.setattr(server, "scansci_pdf_tor_install", lambda: '{"installed": true}')

    assert _json(server.scansci_pdf_tor(action="install", confirmed=True)) == {"installed": True}


def test_tor_start_requires_confirmation_when_it_would_download(monkeypatch):
    """`start` installs on demand, so gating only action="install" would leave the
    same download reachable through the start path."""
    called: list[int] = []
    monkeypatch.setattr(server, "_tor_is_installed", lambda: False)
    monkeypatch.setattr(
        server, "scansci_pdf_tor_start", lambda use_bridges=False: called.append(1) or "{}"
    )

    result = _json(server.scansci_pdf_tor(action="start"))

    assert result["requires_confirmation"] is True
    assert called == []


def test_tor_start_proceeds_when_tor_is_already_present(monkeypatch):
    monkeypatch.setattr(server, "_tor_is_installed", lambda: True)
    monkeypatch.setattr(server, "scansci_pdf_tor_start", lambda use_bridges=False: '{"running": true}')

    assert _json(server.scansci_pdf_tor(action="start")) == {"running": True}


def test_tor_stop_is_not_gated(monkeypatch):
    monkeypatch.setattr(server, "scansci_pdf_tor_stop", lambda: '{"stopped": true}')

    assert _json(server.scansci_pdf_tor(action="stop")) == {"stopped": True}


def test_unknown_install_state_is_treated_as_not_installed(monkeypatch):
    """Counting "can't tell" as installed would let a download through silently."""
    monkeypatch.setattr(embedded_tor, "tor_installed", lambda config: (_ for _ in ()).throw(RuntimeError("boom")))

    assert server._tor_is_installed() is False


# --------------------------------------------------------------------------- #
# an explicit tor binary wins, and a bad pin does not fall through
# --------------------------------------------------------------------------- #


def test_pinned_tor_binary_is_used(tmp_path, monkeypatch):
    pinned = tmp_path / "tor"
    pinned.write_bytes(b"#!/bin/sh\n")
    monkeypatch.setattr(embedded_tor, "_tor_dir", lambda config: tmp_path / "embedded")

    assert embedded_tor._tor_binary({"tor_binary_path": str(pinned)}) == pinned


def test_a_pin_that_does_not_exist_does_not_silently_become_another_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(embedded_tor, "_tor_dir", lambda config: tmp_path / "embedded")

    assert embedded_tor._tor_binary({"tor_binary_path": str(tmp_path / "missing")}) is None


def test_tor_installed_reports_the_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(embedded_tor, "_tor_dir", lambda config: tmp_path / "embedded")

    assert embedded_tor.tor_installed({"tor_binary_path": str(tmp_path / "missing")}) is False

    pinned = tmp_path / "tor"
    pinned.write_bytes(b"#!/bin/sh\n")
    assert embedded_tor.tor_installed({"tor_binary_path": str(pinned)}) is True
