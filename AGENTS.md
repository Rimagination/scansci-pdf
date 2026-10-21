# AGENTS.md

ScanSci PDF is an MCP server plus agent skills for academic literature search,
citation export, and authorized paper retrieval. This file is the entry point for
coding agents working in this repository; host-specific adapters live in the
directories listed below.

## What runs

- Python package `scansci-pdf` (`src/scansci_pdf`), Python 3.11+.
- CLI entry point: `scansci-pdf` (`scansci-pdf run` starts the stdio MCP server;
  `scansci-pdf run --mode streamable_http` serves HTTP).
- 18 MCP tools named `scansci_pdf_*`; see `.mcp.json` for the standard client
  configuration.

## Install for development

```bash
python -m pip install -e .
# Optional: institutional browsers / WebVPN crypto
python -m pip install -e ".[cloakbrowser,vpnsci]"
scansci-pdf check
```

## Host adapters

| Host | Files |
|---|---|
| Standard MCP clients | `.mcp.json` (`scansci-pdf run`, stdio) |
| Claude Code | `.claude/skills/` + the standard MCP configuration |
| Codex App | `.codex-plugin/plugin.json`, `.mcp.json`, `skills/` |
| ZCode | `zcode-plugin/` (marketplace package, skills, `/scansci` commands) |
| DeepSeek Harness | `dsh-plugin/` (bundle: MCP client row + runtime skill registration) |

## Skills

- `skill/SKILL.md` — canonical, full workflow reference for `scansci-pdf`.
- `skills/scansci-pdf/SKILL.md` — short host-neutral wrapper.
- `skills/scansci-sort/SKILL.md` — list triage and route planning.
- `zcode-plugin/skills/` — expanded ZCode editions.

Keep the MCP tool names and the source-authorization rules in these skills in
sync with the implementation.

## Guardrails

- Preserve the user's requested download strategy and report the actual source
  of every download.
- Never re-enable gray sources when the user chose `legal_only` or
  `scihub_enabled=false`; scheduling must not expand authorization.
- Institutional login happens only in the browser opened by
  `scansci_pdf_login`; cookies come from that context, and the egress IP is what
  the publisher sees.
- Treat API keys, cookies, proxy credentials, and institution details as secrets.
- Read `docs/PLAYBOOK.md` before changing anti-bot, mirror, or sniffing code.
