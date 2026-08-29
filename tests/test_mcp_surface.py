"""MCP surface contract: the tool diet (45 -> 17) must stay lean and dispatchable."""

from __future__ import annotations

import asyncio
import json

from scansci_pdf.server import (
    mcp_app,
    scansci_pdf_config,
    scansci_pdf_find,
    scansci_pdf_login,
)

EXPECTED = {
    "scansci_pdf_batch_download",
    "scansci_pdf_cache_clear",
    "scansci_pdf_channel_status",
    "scansci_pdf_citation",
    "scansci_pdf_config",
    "scansci_pdf_diagnostics",
    "scansci_pdf_download",
    "scansci_pdf_elsevier_setup",
    "scansci_pdf_expand_citations",
    "scansci_pdf_find",
    "scansci_pdf_login",
    "scansci_pdf_parse_list",
    "scansci_pdf_prepare_queue",
    "scansci_pdf_schools",
    "scansci_pdf_search",
    "scansci_pdf_tor",
    "scansci_pdf_zotero_push",
}


def test_tool_surface_matches_expected():
    tools = asyncio.run(mcp_app.list_tools())
    assert {t.name for t in tools} == EXPECTED
    assert len(tools) == 17


def test_tool_descriptions_are_diet_sized():
    tools = asyncio.run(mcp_app.list_tools())
    for t in tools:
        assert len(t.description or "") <= 200, (
            f"{t.name}: description grew to {len(t.description)} chars — keep it short, details belong in skills"
        )


def test_login_dispatch_unknown_kind_lists_allowed():
    out = json.loads(scansci_pdf_login(kind="nope"))
    assert "error" in out and set(out["allowed"]) == {
        "publisher", "webvpn", "carsi", "ezproxy", "custom", "cookie_import",
    }


def test_find_dispatch_unknown_action_lists_allowed():
    out = json.loads(scansci_pdf_find(action="nope"))
    assert "error" in out and set(out["allowed"]) == {"plan", "estimate", "smoke", "calibrate"}


def test_config_read_mode_returns_masked_dict():
    out = json.loads(scansci_pdf_config())
    assert isinstance(out, dict) and out  # masked full config
