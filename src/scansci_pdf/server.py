"""MCP server with tools for paper fetching."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .cache import cache_clear, cache_get
from .config import get_config_safe, load_config, update_config
from .network import fetch_json
from .paperlist import PaperEntry, parse_paper_list
from .resolver import batch_resolve
from .search import search_papers
from .sources import batch_download, download, STRATEGIES
from .tor import check_tor_circuit

mcp_app = FastMCP(
    name="scansci-pdf",
    instructions=(
        "Academic paper downloader MCP server with 13+ sources, WebVPN/CARSI institutional access, "
        "Tor, Sci-Hub/LibGen fallback, citation export, and resumable batch downloads. "
        "Use scansci_pdf_search before downloading when the user provides only a title or broad topic. "
        "Ask the user to choose papers before batch-downloading search results. "
        "For paywalled papers, configure WebVPN with scansci_pdf_vpnsci_set_school, then scansci_pdf_vpnsci_login, "
        "then call download with use_vpnsci=true. For network failures, call scansci_pdf_network_diagnose."
    ),
)


def _safe_report_progress(ctx: Context | None, current: int, total: int, identifier: str, result: dict[str, Any]) -> None:
    """Report MCP progress from sync download callbacks without exposing ctx in tool schemas."""
    if ctx is None:
        return
    try:
        import anyio

        ok = result.get("success", False)
        source = result.get("source", "?")
        status = "OK" if ok else "FAIL"
        anyio.from_thread.run(
            ctx.report_progress,
            current,
            total,
            f"{status} {source} {identifier}",
        )
    except Exception:
        # Progress notifications are best-effort; never fail a download because
        # the client does not support progress or the callback is running outside
        # an anyio worker thread (for example in direct tests).
        pass


@mcp_app.tool()
def scansci_pdf_download(
    identifier: str,
    output_dir: str | None = None,
    scihub_enabled: bool | None = None,
    use_tor: bool = False,
    use_vpnsci: bool = False,
    bibtex: bool = False,
    strategy: str | None = None,
) -> str:
    """Download a single academic paper by DOI or arXiv ID.

    Args:
        identifier: DOI (e.g. 10.1038/nature12373), DOI URL, or arXiv ID (e.g. 2301.00001)
        output_dir: Override default output directory
        scihub_enabled: Enable/disable Sci-Hub for this download
        use_tor: Route through Tor SOCKS5 proxy for anonymity
        use_vpnsci: Try WebVPN institutional proxy as last resort (requires prior login via scansci_pdf_vpnsci_login)
        bibtex: Also return BibTeX citation for this paper
        strategy: Download strategy: "fastest" (default), "oa_first", "scihub_only", "legal_only"
    """
    result = download(identifier, output_dir, scihub_enabled=scihub_enabled, use_tor=use_tor, use_vpnsci=use_vpnsci, bibtex=bibtex, strategy=strategy)
    return json.dumps(result, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_batch_download(
    identifiers: list[str],
    output_dir: str | None = None,
    scihub_enabled: bool | None = None,
    use_tor: bool = False,
    use_vpnsci: bool = False,
    batch_id: str | None = None,
    resume: bool = True,
    ctx: Context | None = None,
) -> str:
    """Download multiple papers by DOI or arXiv ID.

    Args:
        identifiers: List of DOIs or arXiv IDs
        output_dir: Override default output directory
        scihub_enabled: Enable/disable Sci-Hub
        use_tor: Route Sci-Hub/LibGen through Tor
        use_vpnsci: Try WebVPN institutional proxy as last resort (requires prior login via scansci_pdf_vpnsci_login)
        batch_id: Unique ID for this batch (auto-generated if omitted). Used for resume support.
        resume: Skip items completed in a previous run (default true). Set false to re-download all.
    """
    from .log import get_logger
    _log = get_logger()

    def _progress_report(current: int, total: int, identifier: str, result: dict[str, Any]) -> None:
        ok = result.get("success", False)
        src = result.get("source", "?")
        status = "OK" if ok else "FAIL"
        _log.info(f"   [{current}/{total}] {status} {src} {identifier}")
        _safe_report_progress(ctx, current, total, identifier, result)

    result = batch_download(
        identifiers, output_dir,
        scihub_enabled=scihub_enabled, use_tor=use_tor, use_vpnsci=use_vpnsci,
        batch_id=batch_id, resume=resume,
        progress_callback=_progress_report,
    )
    return json.dumps(result, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_search(
    query: str,
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str | None = None,
) -> str:
    """Search for academic papers by keyword using OpenAlex API.

    Args:
        query: Search query (e.g. "machine learning drug discovery")
        limit: Maximum number of results (default 10, max 50)
        year_from: Filter papers published from this year (e.g. 2020)
        year_to: Filter papers published up to this year (e.g. 2025)
        sort: Sort order - "cited_by_count" (most cited first), "publication_date" (newest first), or omit for relevance
    """
    results = search_papers(
        query,
        limit=min(limit, 50),
        year_from=year_from,
        year_to=year_to,
        sort=sort,
    )
    return json.dumps({"results": results}, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_health_check(detailed: bool = False) -> str:
    """Check availability of all download sources with latency and status.

    Args:
        detailed: If true, include Sci-Hub domain stats from cache
    """
    config = load_config()
    probes = {
        "europepmc": "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:10.1038/nature12373&format=json&pageSize=1",
        "unpaywall": f"https://api.unpaywall.org/v2/10.1038/nature12373?email={config.get('email', 'test@example.com')}",
        "core": "https://api.core.ac.uk/v3/search/works?q=doi:%2210.1038/nature12373%22&limit=1",
        "semanticscholar": "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1038/nature12373?fields=openAccessPdf",
        "openalex": "https://api.openalex.org/works/doi:10.1038/nature12373",
        "crossref": "https://api.crossref.org/works/10.1038/nature12373",
    }
    checks: dict[str, Any] = {}
    for name, url in probes.items():
        t0 = time.time()
        try:
            resp = fetch_json(url, config)
            latency = round((time.time() - t0) * 1000)
            if resp:
                checks[name] = {"status": "ok", "latency_ms": latency}
            else:
                checks[name] = {"status": "error", "reason": "no response", "latency_ms": latency}
        except Exception as exc:
            latency = round((time.time() - t0) * 1000)
            checks[name] = {"status": "error", "reason": type(exc).__name__, "latency_ms": latency}

    tor_ok = check_tor_circuit()
    checks["tor"] = {"status": "ok" if tor_ok else "unavailable"}

    from .flaresolverr import is_available as flaresolverr_ok
    checks["flaresolverr"] = {"status": "ok" if flaresolverr_ok(config) else "unavailable"}

    overall = "ok" if all(c.get("status") == "ok" for c in checks.values()) else "degraded"

    result: dict[str, Any] = {
        "overall": overall,
        "strategy": config.get("download_strategy", "fastest"),
        "scihub_enabled": config.get("scihub_enabled", False),
        "checks": checks,
    }

    if detailed:
        from .domain_db import load_stats
        stats = load_stats(config)
        scihub_domains = []
        for domain, s in stats.items():
            if domain.startswith("_"):
                continue
            total = s.get("success", 0) + s.get("fail", 0)
            scihub_domains.append({
                "domain": domain,
                "success": s.get("success", 0),
                "fail": s.get("fail", 0),
                "rate": round(s.get("success", 0) / total * 100, 1) if total > 0 else 0,
                "avg_latency_ms": s.get("avg_latency_ms"),
                "reachable": s.get("reachable"),
            })
        scihub_domains.sort(key=lambda d: d["rate"], reverse=True)
        result["scihub_domains"] = scihub_domains[:10]

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_source_scores() -> str:
    """Show adaptive source health scores based on download history.

    Returns per-source success rate (EMA), latency, and attempts.
    Sources with low scores are deprioritized in download racing.
    """
    from .sources.scoring import get_all_scores
    scores = get_all_scores()
    if not scores:
        return json.dumps({"message": "No download history yet. Scores will build after first downloads."})
    # Sort by score descending
    sorted_scores = sorted(scores.items(), key=lambda x: x[1].get("success_ema", 0), reverse=True)
    result = []
    for source, data in sorted_scores:
        result.append({
            "source": source,
            "success_rate": round(data.get("success_ema", 0) * 100, 1),
            "avg_latency_ms": round(data.get("latency_ema", 0)),
            "attempts": data.get("attempts", 0),
            "last_error": data.get("last_error", ""),
        })
    return json.dumps({"sources": result}, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_network_diagnose() -> str:
    """Diagnose network connectivity and provide actionable fix suggestions.

    Tests DNS resolution, TCP connectivity, proxy, Tor, and FlareSolverr status.
    Returns specific configuration commands to fix detected issues.
    """
    from .sources.scoring import diagnose_network
    config = load_config()
    report = diagnose_network(config)
    return json.dumps(report, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_config_get() -> str:
    """Get current scansci-pdf configuration (sensitive values masked)."""
    return json.dumps(get_config_safe(), ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_config_set(key: str, value: str) -> str:
    """Update a scansci-pdf configuration setting.

    Args:
        key: Config key (e.g. "email", "scihub_enabled", "vpnsci_school", "vpnsci_enabled", "network_proxy", "batch_workers")
        value: New value (booleans as "true"/"false", numbers as strings)
    """
    try:
        update_config(key, value)
        return json.dumps({"success": True, "key": key, "value": value})
    except Exception as exc:
        return json.dumps({"success": False, "key": key, "error": str(exc)})


@mcp_app.tool()
def scansci_pdf_cache_clear(identifier: str | None = None) -> str:
    """Clear paper download cache.

    Args:
        identifier: Clear cache for specific paper. Omit to clear all cache.
    """
    config = load_config()
    cleared = cache_clear(identifier, config)
    return json.dumps({"cleared": cleared})


@mcp_app.tool()
def scansci_pdf_import_bib(
    bib_file: str,
    output_dir: str | None = None,
    scihub_enabled: bool | None = None,
    use_tor: bool = False,
    ctx: Context | None = None,
) -> str:
    """Import DOIs from a .bib file and download all papers.

    Args:
        bib_file: Path to .bib file
        output_dir: Override default output directory
        scihub_enabled: Enable/disable Sci-Hub
        use_tor: Route through Tor
    """
    from .bibparser import parse_bib_file
    from .log import get_logger
    _log = get_logger()

    entries = parse_bib_file(bib_file)
    if not entries:
        return json.dumps({"success": False, "error": "No entries with DOI found in .bib file"})

    identifiers = [e["doi"] for e in entries]

    def _bib_progress(current: int, total: int, identifier: str, result: dict[str, Any]) -> None:
        ok = result.get("success", False)
        src = result.get("source", "?")
        status = "OK" if ok else "FAIL"
        _log.info(f"   [{current}/{total}] {status} {src} {identifier}")
        _safe_report_progress(ctx, current, total, identifier, result)

    result = batch_download(identifiers, output_dir, scihub_enabled=scihub_enabled, use_tor=use_tor, progress_callback=_bib_progress)
    result["bib_entries"] = len(entries)
    result["bib_file"] = bib_file
    return json.dumps(result, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_citation(identifier: str, format: str = "bibtex") -> str:
    """Get citation for a paper in various formats.

    Args:
        identifier: DOI or arXiv ID
        format: Citation format: "bibtex", "ris", or "endnote"
    """
    from .identifiers import normalize_doi
    config = load_config()
    doi = normalize_doi(identifier)

    if format == "bibtex":
        from .bibtex import fetch_bibtex
        citation = fetch_bibtex(doi, config)
    elif format == "ris":
        from .citation import to_ris
        citation = to_ris(doi, config)
    elif format == "endnote":
        from .citation import to_endnote
        citation = to_endnote(doi, config)
    else:
        return json.dumps({"success": False, "error": f"Unknown format: {format}. Use bibtex, ris, or endnote"})

    if citation:
        return json.dumps({"success": True, "doi": doi, "format": format, "citation": citation})
    return json.dumps({"success": False, "doi": doi, "error": "Failed to fetch metadata"})


@mcp_app.tool()
def scansci_pdf_zotero_push(identifier: str) -> str:
    """Push a downloaded paper to Zotero.

    Args:
        identifier: DOI or arXiv ID of a previously downloaded paper
    """
    from .identifiers import normalize_doi
    from .zotero import push_to_zotero
    config = load_config()

    # Check if paper is in cache
    cached = cache_get(identifier, config)
    if not cached or not cached.get("success"):
        return json.dumps({"success": False, "error": "Paper not found in cache. Download it first."})

    doi = cached.get("doi", normalize_doi(identifier))
    pdf_path = Path(cached.get("file", "")) if cached.get("file") else None

    # Fetch metadata for better Zotero entry
    from .citation import fetch_metadata
    metadata = fetch_metadata(doi, config)

    result = push_to_zotero(doi, pdf_path, config, metadata)
    return json.dumps(result, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_vpnsci_login() -> str:
    """Open browser for WebVPN institutional proxy login (CAS authentication).

    Login happens in your browser - passwords never pass through this program.
    Only session cookies are saved. Run this before using use_vpnsci=true.
    """
    config = load_config()
    if not config.get("vpnsci_enabled"):
        return json.dumps({"success": False, "error": "WebVPN not enabled. Run: scansci_pdf_config_set key=vpnsci_enabled value=true"})

    from .sources.vpnsci import vpnsci_login, _validate_session, _get_webvpn_base
    if _validate_session(config):
        return json.dumps({"success": True, "message": "Already logged in. Session is valid."})

    base = _get_webvpn_base(config)
    if not base:
        return json.dumps({"success": False, "error": "No WebVPN URL. Set vpnsci_school or vpnsci_base_url."})

    ok = vpnsci_login(config)
    if ok:
        return json.dumps({"success": True, "message": "Login successful. Cookies saved."})
    return json.dumps({"success": False, "error": "Login failed or timed out. Make sure Chrome is installed."})


@mcp_app.tool()
def scansci_pdf_vpnsci_test(doi: str | None = None) -> str:
    """Test WebVPN connectivity by attempting to access a paper.

    Args:
        doi: DOI to test (default: 10.1038/nature12373)
    """
    from .sources.vpnsci import vpnsci_is_configured, _validate_session, convert_url, _get_webvpn_base
    config = load_config()
    test_doi = doi or "10.1038/nature12373"

    if not vpnsci_is_configured(config):
        return json.dumps({"success": False, "error": "WebVPN not configured. Set vpnsci_enabled=true and vpnsci_school."})

    if not _validate_session(config):
        return json.dumps({"success": False, "error": "No valid session. Run scansci_pdf_vpnsci_login first."})

    base = _get_webvpn_base(config)
    doi_url = f"https://doi.org/{test_doi}"
    proxy_url = convert_url(doi_url, base, config)
    return json.dumps({
        "success": True,
        "message": "Session is valid.",
        "test_url": proxy_url[:150] + "..." if len(proxy_url) > 150 else proxy_url,
    })


@mcp_app.tool()
def scansci_pdf_vpnsci_status() -> str:
    """Check WebVPN configuration and login status."""
    from .sources.vpnsci import vpnsci_is_configured, _validate_session, vpnsci_cookie_path, _get_webvpn_base
    config = load_config()

    enabled = config.get("vpnsci_enabled", False)
    school = config.get("vpnsci_school", "")
    base_url = _get_webvpn_base(config)
    cookie_path = vpnsci_cookie_path(config)
    has_cookies = cookie_path.exists()
    session_valid = _validate_session(config) if enabled and has_cookies else False

    return json.dumps({
        "vpnsci_enabled": enabled,
        "vpnsci_school": school,
        "vpnsci_base_url": base_url,
        "cookie_file": str(cookie_path),
        "has_cookies": has_cookies,
        "session_valid": session_valid,
    })


@mcp_app.tool()
def scansci_pdf_vpnsci_schools(query: str | None = None) -> str:
    """List or search supported WebVPN universities.

    Args:
        query: Search by name, province, or host. Omit to list all schools.
    """
    from .schools import list_schools, search_schools
    if query:
        results = search_schools(query)
    else:
        results = list_schools()

    schools = [{"name": s.name, "province": s.province, "host": s.host} for s in results[:50]]
    return json.dumps({"total": len(results), "showing": len(schools), "schools": schools}, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_vpnsci_set_school(school: str) -> str:
    """Set the university for WebVPN access.

    Args:
        school: University name (e.g. "清华大学", "北京大学", "浙江大学")
    """
    from .schools import get_school
    try:
        entry = get_school(school)
    except ValueError as e:
        return json.dumps({"success": False, "error": str(e)})

    update_config("vpnsci_school", entry.name)
    update_config("vpnsci_base_url", entry.host)
    update_config("vpnsci_enabled", "true")
    return json.dumps({
        "success": True,
        "school": entry.name,
        "province": entry.province,
        "host": entry.host,
    }, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_parse_list(file_path: str) -> str:
    """Parse a paper list file (APA references, BibTeX, or DOI list) and extract metadata.

    Returns structured entries with title, authors, year, DOI.
    Supports .md, .txt, .bib files. Auto-detects format.

    Args:
        file_path: Path to paper list file
    """
    try:
        entries = parse_paper_list(file_path)
    except FileNotFoundError as e:
        return json.dumps({"success": False, "error": str(e)})
    except Exception as e:
        return json.dumps({"success": False, "error": f"Parse error: {e}"})

    result = []
    for i, entry in enumerate(entries):
        result.append({
            "index": i + 1,
            "title": entry.title,
            "authors": entry.authors,
            "year": entry.year,
            "doi": entry.doi,
            "journal": entry.journal,
        })

    dois_found = sum(1 for e in entries if e.doi)
    return json.dumps({
        "success": True,
        "total": len(entries),
        "with_doi": dois_found,
        "without_doi": len(entries) - dois_found,
        "entries": result,
    }, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_resolve_and_download(
    file_path: str,
    output_dir: str | None = None,
    scihub_enabled: bool | None = None,
    use_tor: bool = False,
    use_vpnsci: bool = False,
    resolve_titles: bool = True,
    ctx: Context | None = None,
) -> str:
    """Parse paper list → fix DOI format → resolve missing DOIs by title search → batch download.

    Full pipeline: parses APA/BibTeX/DOI list, repairs unicode hyphens in DOIs,
    searches OpenAlex for papers without DOIs, then downloads all.

    Args:
        file_path: Path to paper list file (.md, .txt, .bib)
        output_dir: Override default output directory
        scihub_enabled: Enable/disable Sci-Hub
        use_tor: Route through Tor
        use_vpnsci: Try WebVPN institutional proxy as last resort
        resolve_titles: Search OpenAlex for papers without DOI (default true)
    """
    from .log import get_logger
    _log = get_logger()

    try:
        entries = parse_paper_list(file_path)
    except FileNotFoundError as e:
        return json.dumps({"success": False, "error": str(e)})
    except Exception as e:
        return json.dumps({"success": False, "error": f"Parse error: {e}"})

    if not entries:
        return json.dumps({"success": False, "error": "No entries found in file"})

    config = load_config()

    # Resolve missing DOIs by title search
    resolve_stats = {"total": len(entries), "already_has_doi": 0, "resolved_by_title": 0, "unresolvable": 0}
    if resolve_titles:
        result = batch_resolve(entries, config)
        entries = result["entries"]
        resolve_stats = result["stats"]

    # Collect DOIs for download
    dois = [e.doi for e in entries if e.doi]
    if not dois:
        return json.dumps({
            "success": False,
            "error": "No valid DOIs found after resolution",
            "resolve_stats": resolve_stats,
        })

    # Deduplicate
    seen = set()
    unique_dois = []
    for d in dois:
        if d not in seen:
            seen.add(d)
            unique_dois.append(d)

    # Download
    def _resolve_progress(current: int, total: int, identifier: str, result: dict[str, Any]) -> None:
        ok = result.get("success", False)
        src = result.get("source", "?")
        status = "OK" if ok else "FAIL"
        _log.info(f"   [{current}/{total}] {status} {src} {identifier}")
        _safe_report_progress(ctx, current, total, identifier, result)

    dl_result = batch_download(
        unique_dois, output_dir,
        scihub_enabled=scihub_enabled,
        use_tor=use_tor,
        use_vpnsci=use_vpnsci,
        progress_callback=_resolve_progress,
    )

    dl_result["parse_stats"] = {
        "total_entries": len(entries),
        "entries_with_doi": len(dois),
        "unique_dois": len(unique_dois),
    }
    dl_result["resolve_stats"] = resolve_stats

    return json.dumps(dl_result, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_setup_check() -> str:
    """Check system environment and return setup recommendations.

    Returns OS info, component status, and installation suggestions
    for missing dependencies. Use this to guide users through setup.
    """
    from .setup import setup_check
    result = setup_check()
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_tor_install() -> str:
    """Download and install Tor Expert Bundle to ~/.scansci-pdf/tor/.

    No Docker or system-wide installation needed. Tor binary is managed
    entirely within the scansci-pdf data directory.
    """
    config = load_config()
    from .tor import install_tor
    result = install_tor(config)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_tor_start(use_bridges: bool = False) -> str:
    """Start embedded Tor SOCKS5 proxy.

    Downloads Tor binary if not already installed. No Docker needed.
    After starting, use_tor=true in download tools will route through this proxy.

    Args:
        use_bridges: Use obfs4 bridges for restricted networks (e.g. behind firewall). Default false.
    """
    config = load_config()
    if use_bridges:
        update_config("tor_use_bridges", "true")
    update_config("use_tor_for_scihub", "true")

    from .tor import start_embedded_tor
    result = start_embedded_tor(config)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_tor_stop() -> str:
    """Stop the embedded Tor SOCKS5 proxy."""
    from .tor import stop_embedded_tor
    result = stop_embedded_tor()
    return json.dumps(result, ensure_ascii=False, indent=2)
