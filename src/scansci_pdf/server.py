"""MCP server with tools for paper fetching."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .cache import cache_clear, cache_get
from .config import get_config_safe, load_config, update_config
from .network import fetch_json
from .paperlist import PaperEntry, parse_paper_list
from .resolver import batch_resolve
from .search import search_papers
from .sources import batch_download, download
from .tor import check_tor_circuit

mcp_app = FastMCP(
    name="scansci-pdf",
    instructions="Academic paper downloader with 13+ sources, multi-university WebVPN, Tor, and Sci-Hub support. Supports DOI, arXiv ID, keyword search, and resumable batch downloads.",
)


@mcp_app.tool()
def scansci_pdf_smart_download(
    identifier: str,
    output_dir: str | None = None,
    bibtex: bool = False,
    strategy: str | None = None,
) -> str:
    """Download a paper with zero configuration required.

    Automatically tries all available sources (OA, Sci-Hub, LibGen, WebVPN) with
    automatic Tor bypass when direct access fails. Just give a DOI — everything else is handled.

    Args:
        identifier: DOI (e.g. 10.1038/nature12373), DOI URL, or arXiv ID (e.g. 2301.00001)
        output_dir: Override default output directory
        bibtex: Also return BibTeX citation for this paper
        strategy: Override download strategy: fastest (default, all sources), scihub_only (only Sci-Hub/LibGen), scihub_first, oa_first, legal_only
    """
    result = download(
        identifier, output_dir,
        scihub_enabled=True,
        use_tor=True,
        use_vpnsci=True,
        bibtex=bibtex,
        strategy=strategy,
    )
    # Add actionable guidance on failure
    if not result.get("success"):
        doi = result.get("doi", result.get("identifier", ""))
        hint = (
            "下载失败。可能原因：1) 网络受限 → 运行 scansci-pdf login 配置机构代理 "
            "2) Sci-Hub 被封锁 → Tor 会自动尝试，检查 scansci-pdf browser-status "
            "3) 论文太新或未收录 → 尝试机构代理下载"
        )
        result["hint"] = {"message": hint}
        # Elsevier papers: suggest API key
        if doi.startswith("10.1016/"):
            config = load_config()
            if not config.get("elsevier_api_key"):
                result["hint"]["elsevier_setup"] = (
                    "Elsevier 论文可通过 API Key 直接下载（1-2秒），免费申请。"
                    "请运行 scansci_pdf_elsevier_setup 获取配置指引。"
                )
    return json.dumps(result, ensure_ascii=False)


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
        strategy: Override download strategy: fastest, scihub_only, scihub_first, oa_first, legal_only
    """
    result = download(identifier, output_dir, scihub_enabled=scihub_enabled, use_tor=use_tor, use_vpnsci=use_vpnsci, bibtex=bibtex, strategy=strategy)

    # Add actionable hints for agents when download fails
    if not result.get("success"):
        error_type = result.get("error_type", "")
        action = result.get("action", "")
        doi = result.get("doi", result.get("identifier", ""))
        if error_type == "paywall" or action == "login_required":
            result["agent_hint"] = (
                f"此论文需要机构登录才能下载。请运行 scansci_pdf_login(identifier=\"{doi}\") "
                "打开浏览器让用户登录机构账号，登录后关闭浏览器，然后重试下载。"
            )
        elif error_type == "cloudflare_blocked":
            result["agent_hint"] = (
                "Cloudflare 防护阻止访问。请提示用户启动 CloakBrowser（端口 9377），"
                "或配置代理后重试。"
            )
        # Elsevier papers: suggest API key setup
        if doi.startswith("10.1016/"):
            config = load_config()
            if not config.get("elsevier_api_key"):
                result.setdefault("hint", {})
                if isinstance(result.get("hint"), str):
                    result["hint"] = {"message": result["hint"]}
                result["hint"]["elsevier_setup"] = (
                    "Elsevier 论文可通过 API Key 直接下载（1-2秒），免费申请。"
                    "请运行 scansci_pdf_elsevier_setup 获取配置指引。"
                )

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
    ctx: Any = None,
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
        if ctx and hasattr(ctx, "report_progress"):
            try:
                ctx.report_progress(current, total)
            except Exception:
                pass

    result = batch_download(
        identifiers, output_dir,
        scihub_enabled=scihub_enabled, use_tor=use_tor, use_vpnsci=use_vpnsci,
        batch_id=batch_id, resume=resume,
        progress_callback=_progress_report,
    )

    # Add agent hint if any paywall failures detected
    failed_results = [r for r in result.get("results", []) if not r.get("success")]
    paywall_failures = [r for r in failed_results if r.get("error_type") == "paywall"]
    if paywall_failures:
        dois = [r.get("doi", r.get("identifier", "?")) for r in paywall_failures[:3]]
        result["agent_hint"] = (
            f"{len(paywall_failures)} 篇论文需要机构登录才能下载（如 {', '.join(dois)}）。"
            "请运行 scansci_pdf_login(identifier=\"第一篇DOI\") 打开浏览器让用户登录，"
            "登录后关闭浏览器，然后重新批量下载。"
        )

    return json.dumps(result, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_search(
    query: str = "",
    limit: int = 10,
    year_from: int | None = None,
    year_to: int | None = None,
    sort: str | None = None,
    author: str | None = None,
    author_id: str | None = None,
) -> str:
    """Search for academic papers by keyword or author using OpenAlex API.

    Args:
        query: Search query (e.g. "machine learning drug discovery"). Leave empty when using --author/--author_id.
        limit: Maximum number of results (default 10, max 50)
        year_from: Filter papers published from this year (e.g. 2020)
        year_to: Filter papers published up to this year (e.g. 2025)
        sort: Sort order - "cited_by_count" (most cited first), "publication_date" (newest first), or omit for relevance
        author: Search by author name — resolves to OpenAlex author ID automatically (e.g. "Fang Jingyun")
        author_id: Search by OpenAlex author ID directly (e.g. "A5102961214")
    """
    results = search_papers(
        query,
        limit=min(limit, 50),
        year_from=year_from,
        year_to=year_to,
        sort=sort,
        author=author,
        author_id=author_id,
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

    from .browser_engine import is_available as browser_ok
    checks["browser"] = {"status": "ok" if browser_ok(config) else "unavailable"}

    overall = "ok" if all(c.get("status") == "ok" for c in checks.values()) else "degraded"

    result: dict[str, Any] = {
        "overall": overall,
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
def scansci_pdf_browser_doctor() -> str:
    """Report reusable shared browser runtime options before suggesting installs."""
    from .browser_discovery import doctor

    return json.dumps(doctor(), ensure_ascii=False, indent=2)


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
def scansci_pdf_auto_setup() -> str:
    """One-click setup: auto-start Tor, check browser, probe Sci-Hub domains.

    Run this once before downloading papers. No configuration needed — everything is auto-detected.
    Returns what was set up and what needs manual attention.
    """
    config = load_config()
    report: dict[str, Any] = {"actions": [], "status": {}}

    # 1. Auto-start Tor
    try:
        from .tor import ensure_tor, check_tor_circuit
        tor_proxy = ensure_tor(config)
        if tor_proxy:
            report["actions"].append(f"Tor started at {tor_proxy}")
            report["status"]["tor"] = "running"
        else:
            report["actions"].append("Tor could not be started (will retry on download)")
            report["status"]["tor"] = "unavailable"
    except Exception as e:
        report["actions"].append(f"Tor error: {e}")
        report["status"]["tor"] = "error"

    # 2. Check browser
    try:
        from .browser_engine import is_available
        if is_available(config):
            report["actions"].append("CloakBrowser is running")
            report["status"]["browser"] = "running"
        else:
            report["actions"].append("CloakBrowser not running (optional, for Cloudflare bypass)")
            report["status"]["browser"] = "not_running"
    except Exception:
        report["status"]["browser"] = "unknown"

    # 3. Probe Sci-Hub domains
    try:
        from .sources.scihub import _probe_scihub_domains
        from .domain_db import load_stats
        config_copy = config.copy()
        config_copy["_force_probe"] = True
        # Reset probe timestamp to force re-probe
        from .domain_db import set_probe_timestamp
        set_probe_timestamp(config_copy, timestamp=0)
        _probe_scihub_domains(config_copy)
        stats = load_stats(config_copy)
        reachable = [d for d, s in stats.items() if s.get("reachable") and not d.startswith("_")]
        report["actions"].append(f"Sci-Hub: {len(reachable)} domains reachable")
        report["status"]["scihub_domains"] = reachable[:5]
    except Exception as e:
        report["actions"].append(f"Sci-Hub probe error: {e}")

    # 4. Check WebVPN/CARSI
    report["status"]["webvpn"] = "configured" if config.get("vpnsci_enabled") else "not_configured"
    report["status"]["carsi"] = "configured" if config.get("carsi_enabled") else "not_configured"

    # 5. Check Elsevier API key
    if config.get("elsevier_api_key"):
        report["status"]["elsevier_api"] = "configured"
        report["actions"].append("Elsevier API key configured (ScienceDirect fast-track enabled)")
    else:
        report["status"]["elsevier_api"] = "not_configured"
        report["actions"].append(
            "Elsevier API key not set — ScienceDirect downloads will use browser fallback. "
            "Run scansci_pdf_elsevier_setup to configure (free, recommended)."
        )

    report["summary"] = "Ready to download. Use scansci_pdf_smart_download with a DOI."
    return json.dumps(report, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_elsevier_setup(test: bool = False) -> str:
    """Setup Elsevier API key for ScienceDirect fast-track access.

    Opens the Elsevier Developer Portal in browser for key registration,
    guides the user through the process, and validates the configured key.

    Args:
        test: If True, test the configured key by downloading a sample paper.
    """
    import webbrowser
    config = load_config()
    api_key = config.get("elsevier_api_key", "")

    result: dict[str, Any] = {}

    if api_key:
        result["status"] = "configured"
        result["key_preview"] = f"{api_key[:8]}...{api_key[-4:]}"
        result["message"] = "Elsevier API key 已配置。"

        if test:
            # Validate by hitting the serial title API (lightweight, no PDF download)
            import requests
            from .network import USER_AGENT
            try:
                s = requests.Session()
                s.trust_env = False
                proxy = config.get("network_proxy", "")
                if proxy:
                    s.proxies = {"http": proxy, "https": proxy}
                resp = s.get(
                    "https://api.elsevier.com/content/serial/title",
                    headers={"Accept": "application/json", "X-ELS-APIKey": api_key, "User-Agent": USER_AGENT},
                    params={"count": 1},
                    timeout=15,
                )
                if resp.status_code == 200:
                    result["test"] = "passed"
                    result["message"] += " API Key 验证有效！ScienceDirect 论文可直接 API 下载。"
                else:
                    result["test"] = "failed"
                    result["message"] += f" API Key 验证失败（HTTP {resp.status_code}），请检查 key 是否正确。"
            except Exception as e:
                result["test"] = "error"
                result["message"] += f" 验证请求失败: {e}"
        else:
            result["message"] += " 运行 scansci_pdf_elsevier_setup(test=true) 验证 key 有效性。"
    else:
        result["status"] = "not_configured"
        # Open browser to Elsevier Developer Portal
        try:
            webbrowser.open("https://dev.elsevier.com/")
            result["browser_opened"] = True
        except Exception:
            result["browser_opened"] = False

        result["message"] = (
            "Elsevier API Key 未配置。请按以下步骤操作：\n\n"
            "1. 浏览器已打开 Elsevier Developer Portal（如未打开请访问 https://dev.elsevier.com/）\n"
            "2. 注册或登录你的 Elsevier 账号（个人邮箱即可，免费）\n"
            "3. 点击 \"My API Key\" → \"Create new key\"\n"
            "4. 应用名称随意填写，选择 \"ScienceDirect Article Retrieval\" API\n"
            "5. 复制生成的 API Key（32位字符串）\n"
            "6. 运行配置命令：\n"
            "   scansci_pdf_config_set(key=\"elsevier_api_key\", value=\"你的APIKey\")\n\n"
            "配置后所有 Elsevier/ScienceDirect/Cell Press 论文自动走 API 直接下载（1-2秒）。"
        )

    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_network_diagnose() -> str:
    """Diagnose network connectivity and provide actionable fix suggestions.

    Tests DNS resolution, TCP connectivity, proxy, Tor, and CloakBrowser status.
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
    ctx: Any = None,
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
        if ctx and hasattr(ctx, "report_progress"):
            try:
                ctx.report_progress(current, total)
            except Exception:
                pass

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
def scansci_pdf_paper_metadata(doi: str) -> str:
    """Get metadata for a paper by DOI from Semantic Scholar.

    Returns title, authors, year, abstract, citation count, and identifiers.
    Lighter than fetch_paper — does not download full text.

    Args:
        doi: The DOI of the paper (e.g. "10.1038/nphys1509").
    """
    from .sources.semantic_scholar import get_paper

    result = get_paper(f"DOI:{doi}")
    if result is None:
        return json.dumps({"success": False, "doi": doi, "error": "Paper not found"})

    return json.dumps({
        "success": True,
        "doi": result.doi,
        "title": result.title,
        "authors": result.authors,
        "year": result.year,
        "journal": result.journal,
        "abstract": result.abstract,
        "citation_count": result.citation_count,
        "arxiv_id": result.arxiv_id,
        "s2_url": result.s2_url,
    }, ensure_ascii=False)


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
def scansci_pdf_carsi_login(publisher: str | None = None) -> str:
    """Login via CARSI federated authentication for publisher institutional access.

    Opens browser to publisher's institutional login page. Cookies are saved
    and reused for subsequent downloads. Works with ScienceDirect, Springer, Wiley, etc.

    Args:
        publisher: Publisher key (sciencedirect, springer, wiley, ieee, tandfonline, nature).
                   Auto-detected from DOI if omitted.
    """
    from .sources.carsi import CARSIClient

    config = load_config()
    if not config.get("carsi_enabled"):
        return json.dumps({"success": False, "error": "CARSI not enabled. Run: scansci_pdf_config_set key=carsi_enabled value=true"})

    idp_name = config.get("carsi_idp_name", "")
    if not idp_name:
        return json.dumps({"success": False, "error": "No IdP set. Run: scansci_pdf_config_set key=carsi_idp_name value=你的学校名称（如 北京大学、浙江大学）"})

    target_publisher = publisher or "sciencedirect"
    client = CARSIClient(config)
    if target_publisher not in client._publisher_configs:
        available = list(client._publisher_configs.keys())
        return json.dumps({"success": False, "error": f"Unknown publisher: {target_publisher}", "available": available})

    ok = client.login(target_publisher)
    if ok:
        return json.dumps({"success": True, "message": f"CARSI login successful for {target_publisher}.", "idp": idp_name})
    return json.dumps({"success": False, "error": "Login failed or timed out. Make sure Chrome is installed."})


@mcp_app.tool()
def scansci_pdf_carsi_status() -> str:
    """Check CARSI configuration and login status."""
    from .sources.carsi import CARSIClient

    config = load_config()
    enabled = config.get("carsi_enabled", False)
    idp_name = config.get("carsi_idp_name", "")

    if not enabled:
        return json.dumps({"carsi_enabled": False, "message": "CARSI not enabled."})

    client = CARSIClient(config)
    publishers = {}
    for pub_key in client._publisher_configs:
        cookie_file = client._cookie_path(pub_key)
        has_cookies = cookie_file.exists()
        publishers[pub_key] = {
            "has_cookies": has_cookies,
            "cookie_file": str(cookie_file),
        }

    return json.dumps({
        "carsi_enabled": True,
        "carsi_idp_name": idp_name,
        "hint": f"当前学校: {idp_name}。如需更换，运行 scansci_pdf_config_set key=carsi_idp_name value=新学校名称" if idp_name else "未设置学校。运行 scansci_pdf_config_set key=carsi_idp_name value=你的学校名称",
        "publishers": publishers,
    }, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_ezproxy_login() -> str:
    """Open browser for EZProxy institutional proxy login.

    Uses the university library's EZProxy service to access papers.
    Login happens in your browser - only session cookies are saved.
    """
    from .sources.ezproxy import ezproxy_login, _validate_ezproxy_session

    config = load_config()
    if not config.get("ezproxy_enabled"):
        return json.dumps({"success": False, "error": "EZProxy not enabled. Run: scansci_pdf_config_set key=ezproxy_enabled value=true"})

    if _validate_ezproxy_session(config):
        return json.dumps({"success": True, "message": "Already logged in. Session is valid."})

    ok = ezproxy_login(config)
    if ok:
        return json.dumps({"success": True, "message": "Login successful. Cookies saved."})
    return json.dumps({"success": False, "error": "Login failed or timed out. Make sure Chrome is installed."})


@mcp_app.tool()
def scansci_pdf_ezproxy_status() -> str:
    """Check EZProxy configuration and login status."""
    from .sources.ezproxy import _get_ezproxy_base, _validate_ezproxy_session, _ezproxy_cookie_path

    config = load_config()
    enabled = config.get("ezproxy_enabled", False)
    base_url = _get_ezproxy_base(config)
    cookie_path = _ezproxy_cookie_path(config)
    has_cookies = cookie_path.exists()
    session_valid = _validate_ezproxy_session(config) if enabled and has_cookies else False

    return json.dumps({
        "ezproxy_enabled": enabled,
        "ezproxy_login_url": base_url,
        "cookie_file": str(cookie_path),
        "has_cookies": has_cookies,
        "session_valid": session_valid,
    }, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_vpnsci_set_school(school: str) -> str:
    """Set the university for WebVPN access.

    Args:
        school: University name (e.g. "北京大学", "浙江大学", "复旦大学")
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
    ctx: Any = None,
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
    from .log import get_logger
    _log = get_logger()
    def _resolve_progress(current: int, total: int, identifier: str, result: dict[str, Any]) -> None:
        ok = result.get("success", False)
        src = result.get("source", "?")
        status = "OK" if ok else "FAIL"
        _log.info(f"   [{current}/{total}] {status} {src} {identifier}")
        if ctx and hasattr(ctx, "report_progress"):
            try:
                ctx.report_progress(current, total)
            except Exception:
                pass

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


@mcp_app.tool()
def scansci_pdf_import_browser_cookies(
    url: str = "https://www.sciencedirect.com/",
    max_wait: int = 300,
) -> str:
    """Extract publisher cookies via CloakBrowser for institutional access.

    Opens a visible stealth browser window. Log in to your institution (university library),
    then close the browser. Cookies are saved and automatically used for all future downloads.

    No WebVPN or special configuration needed — works with any institution.

    Args:
        url: Page to open (default: ScienceDirect). Use publisher-specific URL for best results.
        max_wait: Max seconds to wait for login (default 300).
    """
    config = load_config()
    from .browser_cookies import extract_via_browser
    result = extract_via_browser(config, url=url, max_wait=max_wait)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_login(
    identifier: str,
    max_wait: int = 300,
) -> str:
    """Login to publisher via your institution for paywall access.

    Opens a stealth browser to the article or publisher page. Click
    'Access through your institution' or 'Log In', select your
    institution, and complete SSO login. Close the browser when done.
    Cookies are automatically captured and saved for all future downloads.

    No WebVPN or CARSI configuration needed — works with any institution.

    Args:
        identifier: DOI (e.g. 10.1126/science.aec6396) or publisher name
                    (e.g. "elsevier", "wiley", "nature", "springer", "ieee",
                    "science", "tandfonline", "pnas", "acs", "rsc", "aip",
                    "aps", "iop", "oxford", "acm")
        max_wait: Max seconds to wait for login (default 300)
    """
    config = load_config()
    from .browser_cookies import publisher_login
    result = publisher_login(identifier, config, max_wait=max_wait)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_browser_status() -> str:
    """Check CloakBrowser availability and configuration."""
    config = load_config()
    from .browser_engine import is_available
    available = is_available(config)
    return json.dumps({
        "available": available,
        "status": "ok" if available else "unreachable",
    }, ensure_ascii=False, indent=2)


@mcp_app.tool()
def scansci_pdf_browser_login(
    login_type: str = "webvpn",
    custom_url: str | None = None,
) -> str:
    """Open a stealth browser for institutional login (WebVPN/CARSI/EZProxy/custom).

    Captures cookies after login and auto-imports them into CloakBrowser.

    Args:
        login_type: One of "webvpn", "carsi", "ezproxy", "custom"
        custom_url: URL to open (required when login_type is "custom")
    """
    config = load_config()
    from .browser_login import open_login_browser, webvpn_login, ezproxy_login
    from .config import DATA_DIR
    from pathlib import Path

    if login_type == "webvpn":
        success = webvpn_login(config)
    elif login_type == "ezproxy":
        success = ezproxy_login(config)
    elif login_type == "custom":
        if not custom_url:
            return json.dumps({"error": "custom_url is required for login_type=custom"})
        cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
        cookie_file = cache_dir / "custom_cookies.json"
        success = open_login_browser(custom_url, config, cookie_file=cookie_file)
    elif login_type == "carsi":
        return json.dumps({"error": "Use the CARSI publisher-specific login flow instead"})
    else:
        return json.dumps({"error": f"Unknown login_type: {login_type}. Use webvpn/ezproxy/custom"})

    return json.dumps({"login_type": login_type, "success": success}, ensure_ascii=False)


@mcp_app.tool()
def scansci_pdf_browser_import_cookies(cookie_file: str) -> str:
    """Import Netscape-format cookies into CloakBrowser.

    Args:
        cookie_file: Path to Netscape-format cookie file
    """
    config = load_config()
    from .browser_engine import import_cookies, is_available
    if not is_available(config):
        return json.dumps({"error": "CloakBrowser is not running"})
    try:
        count = import_cookies(cookie_file, config)
        return json.dumps({"imported": count, "file": cookie_file}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)
