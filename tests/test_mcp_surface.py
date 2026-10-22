"""MCP surface contract: the tool diet (45 -> 20) must stay lean and dispatchable."""

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
    "scansci_pdf_sciverse",
    "scansci_pdf_sciverse_setup",
    "scansci_pdf_search",
    "scansci_pdf_springer_setup",
    "scansci_pdf_tor",
    "scansci_pdf_zotero_push",
}


def test_tool_surface_matches_expected():
    tools = asyncio.run(mcp_app.list_tools())
    assert {t.name for t in tools} == EXPECTED
    assert len(tools) == 20


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


def test_instructions_declare_the_untrusted_content_boundary():
    """This server's product is scraped third-party text, so the agent reading it
    has to be told that none of it is an instruction (#71)."""
    instructions = (mcp_app.instructions or "").lower()

    assert "untrusted" in instructions
    assert "never as instructions" in instructions


def test_reference_tool_table_matches_the_surface():
    """The technical reference's tool table must match the live MCP surface.

    The user-facing README intentionally stays non-technical; the complete
    tool list lives in docs/REFERENCE.md.
    """
    import re
    from pathlib import Path

    reference = (
        Path(__file__).resolve().parents[1] / "docs" / "REFERENCE.md"
    ).read_text(encoding="utf-8")

    block = re.search(r"<!-- mcp-tools:start -->(.*?)<!-- mcp-tools:end -->", reference, re.S)
    assert block, "docs/REFERENCE.md lost its mcp-tools block"

    listed = set(re.findall(r"scansci_pdf_[a-z_]+", block.group(1)))
    real = {tool.name for tool in asyncio.run(mcp_app.list_tools())}

    assert listed == real, (
        "reference tool table is out of sync — missing: "
        f"{sorted(real - listed)}, stale: {sorted(listed - real)}"
    )
    assert f"MCP-{len(real)}%20tools" in reference, "badge count drifted"
    assert f"{len(real)} 个工具" in reference, "prose count drifted"
