"""CLI entrypoint for ScanSci PDF server."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

import typer

app = typer.Typer(help="ScanSci PDF server")


class ServerMode(str, Enum):
    STDIO = "stdio"
    HTTP = "streamable_http"
    WEB = "web"


@app.command("run")
def run_server(
    mode: ServerMode = typer.Option(ServerMode.STDIO, help="Transport mode"),
    host: str = typer.Option("0.0.0.0", help="HTTP host"),
    port: int = typer.Option(8000, help="HTTP port"),
) -> None:
    """Start the ScanSci PDF server."""
    from .deps import print_status
    from .log import get_logger
    log = get_logger()

    # Check dependencies before starting
    print_status()

    from .server import mcp_app

    if mode == ServerMode.STDIO:
        log.info("Starting in stdio mode")
        mcp_app.run(transport="stdio")
    elif mode == ServerMode.WEB:
        try:
            import uvicorn
            from .web import app as web_app
        except ModuleNotFoundError as e:
            typer.echo(f"  Missing dependency: {e.name}. Install with: pip install 'scansci-pdf[web]'")
            raise typer.Exit(1)
        log.info(f"Starting web UI on http://{host}:{port}")
        uvicorn.run(web_app, host=host, port=port)
    else:
        import uvicorn
        log.info(f"Starting HTTP server on {host}:{port}")
        asgi_app = mcp_app.streamable_http_app()
        uvicorn.run(asgi_app, host=host, port=port)


@app.command("check")
def check_deps() -> None:
    """Check dependency status."""
    from .deps import print_status
    print_status()


@app.command("web")
def web_server(
    host: str = typer.Option("0.0.0.0", help="Web server host"),
    port: int = typer.Option(8080, help="Web server port"),
) -> None:
    """Start the ScanSci PDF web UI for browser-based paper downloading."""
    try:
        import uvicorn
        from .web import app as web_app
    except ModuleNotFoundError as e:
        typer.echo(f"  Missing dependency: {e.name}. Install with: pip install 'scansci-pdf[web]'")
        raise typer.Exit(1)
    print(f"  Starting ScanSci PDF Web UI on http://{host}:{port}")
    print(f"  Open http://localhost:{port} in your browser")
    uvicorn.run(web_app, host=host, port=port)


@app.command("login")
def login(
    login_type: str = typer.Option("cookies", help="Login type: cookies, webvpn, carsi, ezproxy, custom"),
    url: str = typer.Option("", help="URL to open (for cookies/custom type)"),
    manual_confirm: bool = typer.Option(
        False,
        "--manual-confirm",
        help="After logging in, press Enter in the terminal to save browser cookies.",
    ),
) -> None:
    """Log in to your institution via stealth browser. Cookies are saved for all future downloads."""
    from .config import load_config
    config = load_config()

    if login_type == "cookies":
        from .browser_cookies import extract_via_browser
        target_url = url or "https://www.sciencedirect.com/"
        result = extract_via_browser(config, url=target_url)
        if result["success"]:
            print(f"  {result['message']}")
        else:
            print(f"  {result.get('message') or result.get('error', 'Failed')}")
            raise typer.Exit(1)
    elif login_type == "webvpn":
        from .browser_login import webvpn_login
        success = webvpn_login(config)
        raise typer.Exit(0 if success else 1)
    elif login_type == "ezproxy":
        from .browser_login import ezproxy_login
        success = ezproxy_login(config, manual_confirm=manual_confirm)
        raise typer.Exit(0 if success else 1)
    elif login_type == "custom":
        if not url:
            print("  Error: --url is required for login_type=custom")
            raise typer.Exit(1)
        from .browser_login import open_login_browser
        from .config import DATA_DIR
        cookie_file = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / "custom_cookies.json"
        success = open_login_browser(url, config, cookie_file=cookie_file)
        raise typer.Exit(0 if success else 1)
    else:
        print(f"  Unknown login type: {login_type}")
        raise typer.Exit(1)


@app.command("get")
def get_paper(
    identifier: str = typer.Argument(help="DOI or arXiv ID"),
    output: str = typer.Option(".", help="Output directory (default: current directory)"),
    no_bibtex: bool = typer.Option(False, help="Skip BibTeX citation"),
    strategy: str = typer.Option("", help="Override download strategy: fastest, grey_only, scihub_only, scihub_first, oa_first, legal_only, ezproxy_only"),
    ezproxy_only: bool = typer.Option(
        False,
        "--ezproxy-only",
        help="Skip every other source and download directly through configured EZProxy.",
    ),
) -> None:
    """Download a paper with zero configuration. Just give a DOI."""
    from .sources import download
    from .config import load_config, update_config

    if ezproxy_only and strategy and strategy != "ezproxy_only":
        raise typer.BadParameter("--ezproxy-only cannot be combined with another --strategy")
    effective_strategy = "ezproxy_only" if ezproxy_only else (strategy or None)

    result = download(
        identifier, output,
        scihub_enabled=True, use_tor=True, use_vpnsci=True,
        bibtex=not no_bibtex,
        strategy=effective_strategy,
        ezproxy_interactive=True,
    )
    if result.get("success"):
        print(f"  OK: {result.get('file', '')}")
        print(f"  Source: {result.get('source', '?')}")
    else:
        failure_message = result.get("error") or result.get("reason") or "unknown"
        print(f"  FAILED: {failure_message}")
        hint = result.get('agent_hint', '')
        if hint:
            print(f"  Hint: {hint}")
        else:
            print(f"  Hint: 运行 scansci-pdf login 配置机构代理，或检查网络连接")


@app.command("browser-status")
def browser_status() -> None:
    """Check CloakBrowser availability."""
    from .config import load_config
    from .browser_engine import is_available
    config = load_config()
    available = is_available(config)
    print(f"  CloakBrowser: {'available' if available else 'not installed'}")


@app.command("browser-doctor")
def browser_doctor_cmd() -> None:
    """Report reusable shared browser runtime options without installing anything."""
    import json as _json

    from .browser_discovery import doctor

    print(_json.dumps(doctor(), ensure_ascii=False))


@app.command("import-cookies")
def import_cookies_cmd(cookie_file: str = typer.Argument(help="Netscape-format cookie file path")) -> None:
    """Import Netscape cookies into browser context."""
    from .config import load_config
    from .browser_engine import import_cookies, is_available

    config = load_config()
    if not is_available(config):
        print("Error: CloakBrowser not available")
        raise typer.Exit(1)
    try:
        count = import_cookies(cookie_file, config)
        print(f"Imported {count} cookies from {cookie_file}")
    except Exception as exc:
        print(f"Error: {exc}")
        raise typer.Exit(1)


@app.command("coverage")
def coverage_report(
    input_file: str = typer.Argument(help="File with one DOI per line"),
    json_output: str = typer.Option("", help="Save JSON coverage report to file"),
    no_browser: bool = typer.Option(False, help="Disable browser-based sources"),
) -> None:
    """Dry-run coverage audit: test DOI routing without downloading PDFs.

    Reports which sources would be attempted for each DOI and how
    publishers map to source tiers.
    """
    from pathlib import Path
    from .config import load_config
    from .sources.publishers import get_publisher, get_publisher_fast_sources, DOI_PREFIX_TO_PUBLISHER

    config = load_config()
    if no_browser:
        config["browser_headless"] = True
        config["vpnsci_enabled"] = False
        config["carsi_enabled"] = False

    dois = [line.strip() for line in Path(input_file).read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")]

    by_publisher: dict[str, dict[str, int]] = {}
    items: list[dict] = []

    for doi in dois:
        publisher = get_publisher(doi) or "Unknown"
        sources = get_publisher_fast_sources(doi)
        source_names = [name for _, name in sources]

        tier_info = {"doi": doi, "publisher": publisher, "sources": source_names}

        if not publisher:
            tier_info["status"] = "not_routed"
            tier_info["action"] = f"No publisher mapping for DOI prefix. Add to DOI_PREFIX_TO_PUBLISHER."
        elif not sources:
            tier_info["status"] = "no_sources"
            tier_info["action"] = f"Publisher '{publisher}' has no registered sources."
        else:
            tier_info["status"] = "routed"

        items.append(tier_info)

        if publisher not in by_publisher:
            by_publisher[publisher] = {"count": 0, "routed": 0, "not_routed": 0}
        by_publisher[publisher]["count"] += 1
        if tier_info["status"] == "routed":
            by_publisher[publisher]["routed"] += 1
        else:
            by_publisher[publisher]["not_routed"] += 1

    report = {
        "total": len(dois),
        "by_publisher": by_publisher,
        "items": items,
    }

    # Print summary
    print(f"\n  Coverage Report: {len(dois)} DOIs")
    print(f"  {'='*50}")
    for pub, stats in sorted(by_publisher.items(), key=lambda x: -x[1]["count"]):
        pct = stats["routed"] / stats["count"] * 100 if stats["count"] else 0
        print(f"  {pub:25s}  {stats['count']:3d} DOIs  {stats['routed']:3d} routed  {stats['not_routed']:3d} gaps  ({pct:.0f}%)")

    routed = sum(s["routed"] for s in by_publisher.values())
    not_routed = sum(s["not_routed"] for s in by_publisher.values())
    print(f"  {'='*50}")
    print(f"  {'TOTAL':25s}  {len(dois):3d} DOIs  {routed:3d} routed  {not_routed:3d} gaps  ({routed/len(dois)*100:.0f}%)\n")

    if not_routed > 0:
        print("  Unrouted DOIs (no publisher mapping):")
        for item in items:
            if item["status"] != "routed":
                print(f"    {item['doi']:45s}  {item['action']}")
        print()

    if json_output:
        import json as _json
        Path(json_output).write_text(_json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  JSON report saved to: {json_output}")


# ── Institutional access commands ─────────────────────────────────────────────

@app.command("setup")
def setup_school(
    school: str = typer.Argument("", help="School name to configure"),
    show: bool = typer.Option(False, "--show", help="Show current configuration"),
) -> None:
    """Configure institutional access (WebVPN/EZproxy/CARSI)."""
    from .config import load_config, save_config

    config = load_config()

    if show:
        print(f"  School:           {config.get('vpnsci_school', '(not set)')}")
        print(f"  WebVPN base URL:  {config.get('vpnsci_base_url', '(not set)')}")
        print(f"  EZproxy URL:      {config.get('ezproxy_login_url', '(not set)')}")
        print(f"  CARSI enabled:    {config.get('carsi_enabled', False)}")
        print(f"  CARSI IdP:        {config.get('carsi_idp_name', '(not set)')}")
        print(f"  Elsevier API key: {'set' if config.get('elsevier_api_key') else '(not set)'}")
        print(f"  Elsevier inst:    {'set' if config.get('elsevier_insttoken') else '(not set)'}")
        print(f"  Proxy:            {config.get('network_proxy', '(not set)')}")
        return

    if not school:
        from .schools import list_schools
        schools = list_schools()
        print(f"  Available schools ({len(schools)} total):\n")
        for s in schools[:30]:
            print(f"    {s.name:30s}  [{s.school_type}]  {s.host}")
        if len(schools) > 30:
            print(f"\n  ... and {len(schools) - 30} more. Use: scansci-pdf schools <query>")
        return

    from .schools import search_schools
    matches = search_schools(school)
    if not matches:
        print(f"  No school matching '{school}' found.")
        print(f"  Run 'scansci-pdf setup' to list available schools.")
        raise typer.Exit(1)

    chosen = matches[0]
    config["vpnsci_school"] = chosen.name
    config["vpnsci_base_url"] = chosen.host
    save_config(config)
    print(f"  Configured: {chosen.name}")
    print(f"  Type:       {chosen.school_type}")
    print(f"  Gateway:    {chosen.host}")
    if len(matches) > 1:
        others = ", ".join(m.name for m in matches[1:5])
        print(f"  Other matches: {others}")


@app.command("schools")
def list_schools_cmd(
    query: str = typer.Argument("", help="Search query (name, province, or host)"),
) -> None:
    """List or search available schools/institutions."""
    from .schools import list_schools, search_schools

    if query:
        results = search_schools(query)
        if not results:
            print(f"  No schools matching '{query}'.")
            return
        print(f"  Found {len(results)} school(s):\n")
        for s in results:
            print(f"    {s.name:30s}  [{s.school_type}]  {s.host}")
    else:
        schools = list_schools()
        print(f"  Available schools ({len(schools)} total):\n")
        for s in schools:
            print(f"    {s.name:30s}  [{s.school_type}]  {s.host}")


@app.command("fetch")
def fetch_paper_cmd(
    identifier: str = typer.Argument(help="DOI or article URL"),
    output: str = typer.Option(".", help="Output directory (default: current directory)"),
    format: str = typer.Option("markdown", help="Output format: markdown, json, text"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Skip cache"),
) -> None:
    """Fetch a paper via 7-step institutional cascade.
    
    Cascade: cache → OA → Elsevier API → DOI resolve → CARSI → publisher → browser → gateway.
    """
    from .institutional.config_adapter import ConfigAdapter
    from .institutional.fetcher import PaperFetcher

    config = ConfigAdapter.load()
    config._config["output_dir"] = output

    fetcher = PaperFetcher(config)
    result = fetcher.fetch_with_result(identifier, use_cache=not no_cache)

    if format == "json":
        print(result.to_json())
    elif format == "text":
        print(result.to_text())
    else:
        print(result.to_markdown(include_pdf_path=True))

    fetcher.close()


@app.command("batch")
def batch_fetch_cmd(
    input_file: str = typer.Argument(help="File with one DOI/URL per line"),
    output: str = typer.Option(".", help="Output directory (default: current directory)"),
    format: str = typer.Option("json", help="Output format: json, text"),
    scihub: bool = typer.Option(False, "--scihub", help="Use Sci-Hub racing engine (includes grey sources) instead of institutional cascade"),
) -> None:
    """Batch fetch papers. Default: institutional cascade. Use --scihub for grey-source racing."""
    import json as _json
    from .config import load_config as _load_config

    dois = [
        line.strip() for line in Path(input_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not dois:
        print("  No DOIs/URLs found in input file.")
        return

    # Auto-detect: if download_strategy is grey/scihub-oriented, switch to racing engine
    _cfg = _load_config()
    _strategy = _cfg.get("download_strategy", "fastest")
    _auto_scihub = scihub or _strategy in ("scihub_only", "grey_only", "scihub_first")
    if not scihub and _auto_scihub:
        print(f"  Auto-switching to Sci-Hub racing engine (download_strategy={_strategy})")

    if _auto_scihub:
        # Use the source-racing engine (includes Sci-Hub/SciBban/LibGen)
        from .sources import batch_download
        results = batch_download(dois, output_dir=output, scihub_enabled=True)
        # Verify file existence for each "success" result before writing report
        _verify_batch_results(results, output)
        if format == "json":
            out_path = Path(output) / "batch_results.json"
            out_path.write_text(_json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\n  Results saved to: {out_path}")
        return

    # Default: institutional cascade (PaperFetcher)
    from .institutional.config_adapter import ConfigAdapter
    from .institutional.fetcher import PaperFetcher

    config = ConfigAdapter.load()
    config._config["output_dir"] = output

    fetcher = PaperFetcher(config)
    results = []

    for i, doi in enumerate(dois, 1):
        print(f"  [{i}/{len(dois)}] {doi}")
        try:
            result = fetcher.fetch_with_result(doi)
            result_dict = result.to_dict()
            # Verify file actually exists on disk for success status
            if result_dict.get("status") == "success" or result_dict.get("success"):
                pdf_path = result_dict.get("file") or result_dict.get("pdf_path", "")
                if pdf_path and not Path(pdf_path).exists():
                    result_dict["status"] = "error"
                    result_dict["error"] = "PDF file not found on disk (may have been saved elsewhere)"
            results.append(result_dict)
            status = result.status
            quality = result.quality
            print(f"         → {status} ({quality})")
        except Exception as e:
            results.append({"doi": doi, "error": str(e)})
            print(f"         → error: {e}")

    fetcher.close()

    if format == "json":
        out_path = Path(output) / "batch_results.json"
        out_path.write_text(_json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n  Results saved to: {out_path}")


def _verify_batch_results(results: dict, output_dir: str) -> None:
    """Verify that 'success' results have actual files on disk; fix stale entries."""
    output_path = Path(output_dir)
    for r in results.get("results", []):
        if r.get("success"):
            file_path = r.get("file", "")
            if file_path and not Path(file_path).exists():
                # Check if file exists under a different name in the output dir
                doi = r.get("doi", "")
                if doi:
                    from .identifiers import safe_filename
                    safe = safe_filename(doi)
                    found = list(output_path.glob(f"{safe}*.pdf"))
                    if found:
                        r["file"] = str(found[0])
                    else:
                        r["success"] = False
                        r["error"] = "File missing from disk"
                        results["succeeded"] = max(0, results.get("succeeded", 1) - 1)
                        results["failed"] = results.get("failed", 0) + 1


@app.command("elsevier-setup")
def elsevier_setup(
    api_key: str = typer.Option("", help="Elsevier API key"),
    inst_token: str = typer.Option("", help="Elsevier institutional token"),
) -> None:
    """Configure Elsevier API access for direct full-text retrieval."""
    from .config import load_config, save_config

    config = load_config()
    changed = False

    if api_key:
        config["elsevier_api_key"] = api_key
        changed = True
        print(f"  Elsevier API key: saved")
    if inst_token:
        config["elsevier_insttoken"] = inst_token
        changed = True
        print(f"  Elsevier inst token: saved")

    if not changed:
        has_key = bool(config.get("elsevier_api_key"))
        has_token = bool(config.get("elsevier_insttoken"))
        print(f"  Elsevier API key:   {'set' if has_key else '(not set)'}")
        print(f"  Elsevier inst token: {'set' if has_token else '(not set)'}")
        print(f"\n  Usage: scansci-pdf elsevier-setup --api-key YOUR_KEY --inst-token YOUR_TOKEN")


@app.command("session-doctor")
def session_doctor() -> None:
    """Diagnose browser profile sessions and cookie health."""
    from .institutional.config_adapter import ConfigAdapter
    from .institutional.profile_health import candidate_profile_dirs, inspect_browser_profile

    config = ConfigAdapter.load()
    profiles = candidate_profile_dirs(config.chrome_profile_dir)

    domains = [
        "sciencedirect.com", "springer.com", "nature.com", "wiley.com",
        "acs.org", "rsc.org", "ieeexplore.ieee.org", "openathens.net",
    ]

    print("  Browser Profile Diagnostics\n")
    for profile_dir in profiles:
        report = inspect_browser_profile(profile_dir, domains)
        exists = report["exists"]
        cookies_db = report["cookies_db_exists"]
        print(f"  Profile: {report['profile_dir']}")
        print(f"    Exists:     {exists}")
        print(f"    Cookies DB: {cookies_db}")

        if report["error"]:
            print(f"    Error:      {report['error']}")

        for domain, info in report.get("domains", {}).items():
            total = info["cookie_count"]
            if total > 0:
                print(f"    {domain:25s}  {total:3d} cookies  (session={info['session_cookie_count']}, persistent={info['persistent_cookie_count']}, expired={info['expired_cookie_count']})")
        print()


@app.command("federated-login")
def federated_login(
    publisher: str = typer.Argument(help="Publisher key (e.g. sciencedirect, springer, wiley)"),
    force: bool = typer.Option(False, "--force", help="Force re-login"),
) -> None:
    """Log in to a publisher via CARSI/Shibboleth federation."""
    from .sources.carsi import CARSIClient
    from .config import load_config

    config = load_config()
    client = CARSIClient(config)

    success = client.login(publisher, force=force)
    if success:
        print(f"  Login successful for {publisher}.")
    else:
        print(f"  Login failed for {publisher}.")
        raise typer.Exit(1)
    client.close()


@app.command("publisher-batch")
def publisher_batch_cmd(
    input_file: str = typer.Argument(help="File with one DOI per line"),
    publisher: str = typer.Option("", help="Publisher key (auto-detected if omitted)"),
    output: str = typer.Option(".", help="Output directory (default: current directory)"),
    max_workers: str = typer.Option("1", help="Number of parallel workers"),
) -> None:
    """Batch download papers via publisher-specific workflows."""
    from .config import load_config

    dois = [
        line.strip() for line in Path(input_file).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if not dois:
        print("  No DOIs found in input file.")
        return

    config = load_config()
    config["output_dir"] = output

    print(f"  Batch: {len(dois)} DOIs")
    print(f"  Publisher: {publisher or 'auto-detect'}")
    print()

    # Use the existing publisher batch infrastructure
    from .institutional.publisher_batch import PublisherBatchDownloader
    downloader = PublisherBatchDownloader(config)
    results = downloader.run(dois, publisher=publisher)

    success = sum(1 for r in results if r.get("success"))
    print(f"\n  Results: {success}/{len(dois)} downloaded")


@app.command("search")
def search_cmd(
    query: str = typer.Argument("", help="Search query (keywords, author, title)"),
    limit: int = typer.Option(10, help="Max results"),
    year_from: int = typer.Option(None, help="Start year"),
    year_to: int = typer.Option(None, help="End year"),
    sort: str = typer.Option("", help="Sort: cited_by_count, publication_date"),
    json_output: bool = typer.Option(True, help="Output as JSON (default)"),
    author: str = typer.Option("", "--author", help="Search by author name (resolves to OpenAlex author ID)"),
    author_id: str = typer.Option("", "--author-id", help="Search by OpenAlex author ID directly"),
) -> None:
    """Search academic papers via OpenAlex, Semantic Scholar, and Crossref.

    Results include DOI, title, authors, year, and citation count.
    Use the DOIs with 'scansci-pdf get' or 'scansci-pdf batch' to download.

    Examples:
      scansci-pdf search "carbon cycle" --limit 10 --sort cited_by_count
      scansci-pdf search --author "Fang Jingyun" --limit 10 --sort cited_by_count
      scansci-pdf search --author-id A5102961214 --limit 10 --sort cited_by_count
    """
    import json as _json
    from .search import search_papers

    if author or author_id:
        # Author-based search
        results = search_papers(
            limit=limit, year_from=year_from, year_to=year_to, sort=sort,
            author=author if author else None,
            author_id=author_id if author_id else None,
        )
        # Show author match info
        if results and results[0].get("_author_match"):
            match = results[0].pop("_author_match")
            print(f"  Author: {match['name']} (ID:{match['id']}, works:{match['works_count']}, cited:{match['cited_by_count']})")
    else:
        sort_key = sort if sort else None
        results = search_papers(query, limit=limit, year_from=year_from, year_to=year_to, sort=sort_key)

    if json_output:
        print(_json.dumps({"results": results}, indent=2, ensure_ascii=False))
    else:
        for i, r in enumerate(results, 1):
            authors = ", ".join(r.get("authors", [])[:3] or [])
            cited = r.get("cited_by_count", 0)
            print(f"{i:2d}. {r.get('title', '?')[:80]}")
            print(f"    {authors}  ({r.get('year', '?')})  cited={cited}  doi:{r.get('doi', '?')}")
            if i < len(results):
                print()


@app.command("config-cmd")
def config_show(
    key: str = typer.Argument("", help="Config key to show/set"),
    value: str = typer.Argument("", help="Value to set"),
) -> None:
    """Show or set configuration values."""
    from .config import load_config, save_config, update_config

    config = load_config()

    if not key:
        # Show all config
        for k, v in sorted(config.items()):
            if "key" in k.lower() or "token" in k.lower() or "secret" in k.lower() or "password" in k.lower():
                v = "***" if v else "(not set)"
            print(f"  {k:30s} = {v}")
        return

    if not value:
        v = config.get(key, "(not set)")
        if "key" in key.lower() or "token" in key.lower():
            v = "***" if v and v != "(not set)" else "(not set)"
        print(f"  {key} = {v}")
        return

    # Use update_config for proper type coercion and validation
    try:
        update_config(key, value)
        print(f"  Set {key} = {value}")
    except ValueError as e:
        print(f"  Error: {e}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
