---
name: scansci-pdf
description: >
  Use this skill when the user wants to search academic literature, validate DOI or
  arXiv identifiers, export citations, organize paper lists, or retrieve papers
  through configured open-access, publisher, or authorized institutional routes.
---

# ScanSci PDF

Use the bundled `scansci-pdf` MCP server for paper discovery, metadata, citations,
download queues, access diagnostics, and result reporting. The repository's full
workflow reference is `../../skill/SKILL.md`; read it when the task needs detailed
tool routing or configuration guidance.

## Route by intent

- Search or identify papers: `scansci_pdf_search`, `scansci_pdf_verify_identifiers`,
  and `scansci_pdf_paper_metadata`.
- Resolve open-access locations: `scansci_pdf_resolve_oa`.
- Download one paper: `scansci_pdf_download` with an explicit strategy when the
  user has a source preference.
- Process a list: `scansci_pdf_parse_list`, `scansci_pdf_resolve_and_download`, or
  `scansci_pdf_batch_download`.
- Export citations: `scansci_pdf_citation` or `scansci_pdf_import_bib`.
- Diagnose setup or access: `scansci_pdf_setup_check`,
  `scansci_pdf_health_check`, and `scansci_pdf_network_diagnose`.

## Operating boundaries

- Preserve the user's requested source strategy and report the actual source in
  every download result.
- Prefer open-access, publisher API, and institution-authorized routes when no
  source preference is given.
- Ask before opening an interactive login, importing cookies, or changing access
  configuration.
- Treat API keys, cookies, proxy credentials, and institution details as secrets;
  never repeat them in the response.
- Gray-source and anti-bot routes require the user's explicit choice and must be
  used only where the user has the right to do so and applicable rules permit it.
