"""Technical docs must list every tool exposed by the canonical MCP surface."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from scansci_pdf.server import mcp_app

ROOT = Path(__file__).resolve().parents[1]


def _documented_tools(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r"`(scansci_pdf_[a-z_]+)`", text))


def _exposed_tools() -> set[str]:
    return {tool.name for tool in asyncio.run(mcp_app.list_tools())}


def test_reference_lists_every_mcp_tool():
    missing = _exposed_tools() - _documented_tools(ROOT / "docs" / "REFERENCE.md")
    assert not missing, f"docs/REFERENCE.md is missing MCP tools: {sorted(missing)}"


def test_skill_lists_every_mcp_tool():
    missing = _exposed_tools() - _documented_tools(ROOT / "skill" / "SKILL.md")
    assert not missing, f"skill/SKILL.md is missing MCP tools: {sorted(missing)}"
