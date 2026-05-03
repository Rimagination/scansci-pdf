# ScanSci PDF

[![PyPI version](https://img.shields.io/pypi/v/scansci-pdf)](https://pypi.org/project/scansci-pdf/)
[![Python](https://img.shields.io/pypi/pyversions/scansci-pdf)](https://pypi.org/project/scansci-pdf/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-compatible-green)](https://modelcontextprotocol.io)

> MCP server for academic paper downloading — 13+ sources, 100+ university WebVPNs, parallel download engine

[English](#features) | [中文](#功能特性)

---

## Features

- **13+ download sources** — arXiv, Sci-Hub, LibGen, Unpaywall, OpenAlex, Semantic Scholar, DOAJ, EuropePMC, CORE, PMC, publisher direct links, and more
- **100+ university WebVPNs** — institutional proxy access for Chinese universities
- **Parallel racing engine** — multi-source concurrent download, first success wins
- **Smart list parsing** — APA citations, BibTeX, DOI lists with automatic DOI resolution
- **Auto-rename** — PDFs renamed to `AuthorYear_Title.pdf` format
- **Citation export** — BibTeX, RIS, EndNote formats
- **Zotero integration** — push downloaded papers directly to Zotero
- **Tor support** — anonymous access to Sci-Hub/LibGen via embedded Tor
- **Network diagnostics** — automatic detection of DNS blocks, proxy issues, and connectivity problems

---

## Quick Start

### Install

```bash
pip install scansci-pdf
```

### MCP Configuration

Add to Claude Desktop / Claude Code:

```json
{
  "mcpServers": {
    "scansci-pdf": {
      "command": "scansci-pdf",
      "args": ["run"]
    }
  }
}
```

<details>
<summary>HTTP mode</summary>

```bash
scansci-pdf run --mode streamable_http --host 0.0.0.0 --port 8000
```
</details>

<details>
<summary>Docker</summary>

```json
{
  "mcpServers": {
    "scansci-pdf": {
      "command": "docker",
      "args": ["compose", "-f", "path/to/docker-compose.yml", "run", "--rm", "scansci-pdf"]
    }
  }
}
```
</details>

### Check Environment

```bash
scansci-pdf check
```

---

## MCP Tools

### Paper Download

| Tool | Description |
|------|-------------|
| `scansci_pdf_download` | Download single paper (DOI or arXiv ID) |
| `scansci_pdf_batch_download` | Batch download multiple papers |
| `scansci_pdf_resolve_and_download` | Parse list → resolve DOIs → batch download |

### Search & Parse

| Tool | Description |
|------|-------------|
| `scansci_pdf_search` | Search papers by keyword (OpenAlex) |
| `scansci_pdf_parse_list` | Parse APA/BibTeX/DOI list file |

### Citation Management

| Tool | Description |
|------|-------------|
| `scansci_pdf_citation` | Get citation (BibTeX/RIS/EndNote) |
| `scansci_pdf_import_bib` | Import .bib file and download all papers |
| `scansci_pdf_zotero_push` | Push paper to Zotero |

### WebVPN

| Tool | Description |
|------|-------------|
| `scansci_pdf_vpnsci_login` | Browser CAS authentication |
| `scansci_pdf_vpnsci_test` | Test WebVPN connectivity |
| `scansci_pdf_vpnsci_status` | Check login status |
| `scansci_pdf_vpnsci_schools` | Search supported universities |
| `scansci_pdf_vpnsci_set_school` | Set current university |

### System

| Tool | Description |
|------|-------------|
| `scansci_pdf_health_check` | Check all source availability |
| `scansci_pdf_setup_check` | Detect environment and suggest installs |
| `scansci_pdf_config_get` / `config_set` | View/modify configuration |
| `scansci_pdf_cache_clear` | Clear download cache |
| `scansci_pdf_network_diagnose` | Network diagnostics (DNS, proxy, Tor, FlareSolverr) |

### Tor

| Tool | Description |
|------|-------------|
| `scansci_pdf_tor_install` | Auto-download Tor Expert Bundle |
| `scansci_pdf_tor_start` | Start embedded Tor SOCKS5 proxy |
| `scansci_pdf_tor_stop` | Stop Tor proxy |

---

## Download Strategies

| Strategy | Description |
|----------|-------------|
| `fastest` (default) | Multi-source parallel, fastest wins |
| `oa_first` | Open access first, Sci-Hub as fallback |
| `scihub_only` | Sci-Hub only |
| `legal_only` | Legal sources only (no Sci-Hub/LibGen) |

---

## WebVPN Setup

Access papers through Chinese university institutional proxy:

```
1. scansci_pdf_config_set(key="vpnsci_enabled", value="true")
2. scansci_pdf_vpnsci_set_school(school="清华大学")
3. scansci_pdf_vpnsci_login  →  browser opens CAS auth
4. scansci_pdf_vpnsci_test   →  verify connection
5. scansci_pdf_download(identifier="...", use_vpnsci=true)
```

Supports 100+ universities. Use `scansci_pdf_vpnsci_schools` to search.

---

## Configuration

Key settings (via `scansci_pdf_config_set`):

| Key | Default | Description |
|-----|---------|-------------|
| `scihub_enabled` | `true` | Enable Sci-Hub |
| `download_strategy` | `fastest` | Download strategy |
| `output_dir` | `~/.scansci-pdf/papers` | PDF output directory |
| `auto_rename` | `true` | Auto-rename PDFs |
| `network_proxy` | (empty) | HTTP/SOCKS proxy address |
| `batch_workers` | `10` | Batch download concurrency |
| `vpnsci_enabled` | `false` | Enable WebVPN |
| `use_tor_for_scihub` | `false` | Use Tor for Sci-Hub |

---

## Docker

```bash
docker compose up -d
```

| Service | Description | Port |
|---------|-------------|------|
| `scansci-pdf` | MCP server | 8000 |
| `tor` | Tor SOCKS5 proxy | 1080 |

Data persisted in Docker volume `scansci-pdf-data`.

---

## Tor Setup

Tor enables anonymous access to Sci-Hub/LibGen in regions where they are blocked.

```bash
# Auto-install Tor (~30MB download)
scansci_pdf_tor_install

# Start Tor proxy
scansci_pdf_tor_start

# Restricted networks (firewall blocks Tor) — use obfs4 bridges
scansci_pdf_tor_start(use_bridges=true)
```

Binary stored in `~/.scansci-pdf/tor/`, does not pollute system environment.

---

## Troubleshooting

**Sci-Hub download fails** — Run `scansci_pdf_health_check(detailed=true)` to check source status. Domain rotation is automatic.

**Tor connection fails** — Ensure Tor is running on `socks5h://127.0.0.1:1080`. For Docker deployment, Tor starts automatically.

**WebVPN login fails** — Requires Chrome/ChromeDriver. Login happens in your browser; passwords never pass through this tool.

**Download is slow** — Run `scansci_pdf_health_check(detailed=true)` to check source latency. If Sci-Hub is blocked in your network, try `legal_only` strategy.

**Network issues** — Run `scansci_pdf_network_diagnose` for a comprehensive connectivity report with actionable fix suggestions.

---

## Architecture

This project uses a layered architecture:

| Layer | Content | License |
|-------|---------|---------|
| Public | All `.py` source code, config, docs | Apache 2.0 |
| Protected | `_core/*.pyx` (Cython source) | Proprietary |
| Distributed | `_core/*.pyd` (compiled binaries) | Shipped via PyPI |

GitHub clones use pure Python fallbacks (same functionality, slightly lower performance). PyPI installs automatically get compiled extensions.

---

## Contributing

Contributions are welcome! Please open an issue or submit a pull request.

### Contributors

<a href="https://github.com/qwlei328-maker"><img src="https://avatars.githubusercontent.com/u/257463305?v=4" width="50" height="50" alt="qwlei328-maker" title="Natasha"/></a>
<a href="https://github.com/jingqingqiu1"><img src="https://avatars.githubusercontent.com/u/87510394?v=4" width="50" height="50" alt="jingqingqiu1" title="jingqingqiu1"/></a>
<a href="https://github.com/minqifeng"><img src="https://avatars.githubusercontent.com/u/61303605?v=4" width="50" height="50" alt="minqifeng" title="minqifeng"/></a>

---

## License

[Apache License 2.0](LICENSE)

Exception: Compiled Cython extensions in `src/scansci_pdf/_core/` (*.pyd/*.so) are distributed as pre-compiled binaries via PyPI only. Their Cython source (*.pyx) is proprietary and not included in this repository.
