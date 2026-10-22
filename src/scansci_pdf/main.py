"""CLI entrypoint for ScanSci PDF server."""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer

app = typer.Typer(help="ScanSci PDF server")


class ServerMode(str, Enum):
    STDIO = "stdio"
    HTTP = "streamable_http"


@app.command("run")
def run_server(
    mode: ServerMode = typer.Option(ServerMode.STDIO, help="Transport mode"),
    host: str = typer.Option("127.0.0.1", help="HTTP host (default localhost; bind 0.0.0.0 only behind auth)"),
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


@app.command("login")
def login(
    login_type: str = typer.Option("cookies", help="Login type: cookies, webvpn, carsi, ezproxy, custom"),
    url: str = typer.Option("", help="URL to open (for cookies/custom type)"),
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
        success = ezproxy_login(config)
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
    strategy: str = typer.Option("", help="Override download strategy: fastest, grey_only(all 3 grey sources), scihub_only(Sci-Hub only), scihub_first, oa_first, legal_only"),
    si: bool = typer.Option(False, "--si", help="Also fetch Supplementary Information attachments"),
    md: bool = typer.Option(False, "--md", help="Also export the PDF as agent-ready markdown"),
) -> None:
    """Download a paper with zero configuration. Just give a DOI."""
    from .sources import download
    from .config import load_config, update_config

    result = download(
        identifier, output,
        scihub_enabled=True, use_tor=True, use_vpnsci=True,
        bibtex=not no_bibtex,
        strategy=strategy if strategy else None,
    )
    if result.get("success"):
        print(f"  OK: {result.get('file', '')}")
        print(f"  Source: {result.get('source', '?')}")
        if md:
            from .md_export import pdf_to_markdown

            md_path = pdf_to_markdown(result["file"])
            print(f"  Markdown: {md_path}")
        if si:
            from .supplementary import fetch_supplementary

            out_dir = str(Path(result.get("file", output)).parent) if result.get("file") else output
            si_files = fetch_supplementary(identifier, out_dir, load_config())
            print(f"  SI files: {len(si_files)}")
            for f in si_files:
                print(f"    {f}")
    else:
        reason = (result.get("reason") or result.get("error")
                  or result.get("error_type") or "unknown")
        print(f"  FAILED: {reason}")
        source_failures = result.get("source_failures") or []
        if source_failures:
            print(f"  渠道明细 ({len(source_failures)} 个渠道失败，部分可能只是临时不可用):")
            for f in source_failures:
                note = f" — {f['reason']}" if f.get("reason") else ""
                print(f"    {f.get('source', '?')}: {f.get('error_type') or 'failed'}{note}")
        hint = result.get('agent_hint', '')
        if hint:
            print(f"  Hint: {hint}")
        else:
            print(f"  Hint: 运行 scansci-pdf login 配置机构代理，或检查网络连接")


@app.command("browser-status")
def browser_status() -> None:
    """Check browser backend availability and which browser kernel is in effect."""
    from .config import load_config
    from .browser_engine import is_available
    from .browser_backend import browser_info
    import importlib.metadata as _md

    config = load_config()
    available = is_available(config)
    print(f"  Browser backend: {'available' if available else 'not installed'}")
    if not available:
        return
    info = browser_info(config)
    print(f"  backend: {info.get('backend', '?')}")
    for pkg in ("patchright", "cloakbrowser"):
        try:
            print(f"  {pkg} package: {_md.version(pkg)}")
        except Exception:
            pass
    if info.get("binary"):
        print(f"  browser kernel: {info['binary']} (version {info.get('version') or '?'})")
    else:
        print(f"  browser kernel: {info.get('binary') or '?'}")


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

@app.command("progress")
def progress_cmd() -> None:
    """Show the frosted-glass floating progress bar for running download tasks."""
    from .progress_bar import main as _progress_bar_main

    _progress_bar_main()


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


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file then replace) so a crash mid-write
    never leaves a truncated batch report."""
    tmp = path.with_suffix(str(path.suffix) + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _write_retry_file(output: str, results: list) -> None:
    """Persist failed identifiers as retry.txt so 'batch --retry' can pick them up."""
    try:
        from .pipeline import collect_failures

        failed = collect_failures(results)
        if failed:
            p = Path(output) / "retry.txt"
            p.write_text("\n".join(failed) + "\n", encoding="utf-8")
            print(f"  {len(failed)} failed → retry list: {p}")
    except Exception:
        pass


@app.command("batch")
def batch_fetch_cmd(
    input_file: str = typer.Argument(help="File with one DOI/URL per line, a queue TSV, a csv/xlsx table, APA or BibTeX"),
    output: str = typer.Option(".", help="Output directory (default: current directory)"),
    format: str = typer.Option("json", help="Output format: json, text"),
    scihub: bool = typer.Option(False, "--scihub", help="Use Sci-Hub racing engine (includes grey sources) instead of institutional cascade"),
    runs_dir: str = typer.Option("", "--runs-dir", help="ScanSci Find run directory: use its download queue as input and its preprint arXiv IDs as fallbacks for failed DOIs"),
    lanes: bool = typer.Option(True, "--lanes/--no-lanes", help="Channel-lane scheduling (default on): pretriage + Elsevier API/OA/MDPI fast lane -> grey racing -> institutional cascade. --no-lanes falls back to per-item racing"),
    retry: str = typer.Option("", "--retry", help="Retry failed identifiers from a previous batch_results.json"),
) -> None:
    """Batch fetch papers. Default: institutional cascade. Use --scihub for grey-source racing."""
    import json as _json
    from .config import load_config as _load_config

    fallbacks: dict[str, list[str]] | None = None
    entries = None
    if retry:
        from .pipeline import collect_failures

        prev = _json.loads(Path(retry).read_text(encoding="utf-8"))
        dois = collect_failures(prev)
        print(f"  Retrying {len(dois)} failed identifiers from {retry}")
    elif runs_dir:
        from .discovery import build_download_queue, build_preprint_fallbacks
        run_identifiers = build_download_queue(runs_dir)
        fallbacks = build_preprint_fallbacks(runs_dir) or None
        if not run_identifiers:
            print(f"  No identifiers found in {runs_dir}/download_queue.json")
            return
        dois = run_identifiers
        print(f"  Using ScanSci Find queue from {runs_dir}: {len(dois)} identifiers")
    else:
        from .pipeline import load_job

        entries = load_job(input_file)
        dois = [e.identifier for e in entries if e.identifier and not e.unresolved]
        unresolved = sum(1 for e in entries if e.unresolved)
        if unresolved:
            print(f"  {unresolved} 行无法识别为 DOI/arXiv，已跳过（可用标题检索补全后重试）")
    if not dois:
        print("  No DOIs/URLs found in input file.")
        return

    # Channel-lane scheduling (default; --no-lanes falls back to per-item
    # racing). Grey-oriented strategies (scihub_only/grey_only/scihub_first)
    # express a lane-ORDER preference the fast->grey->institutional schedule
    # would invert, and an explicit --scihub asks for the racing engine —
    # both stay on racing.
    from .pipeline import collect_failures, grey_allowed, run_lanes

    _cfg = _load_config()
    _grey_oriented = (
        scihub
        or _cfg.get("download_strategy", "fastest") in ("scihub_only", "grey_only", "scihub_first")
    )
    use_lanes = lanes and entries is not None and not _grey_oriented
    if use_lanes:
        cfg = _load_config()
        try:
            # Lane scheduling only changes the schedule, never the source
            # authorization: allow_grey is derived from the user's config.
            results = run_lanes(entries, output, config=cfg, allow_grey=grey_allowed(cfg))
        except Exception as exc:
            print(f"\n  Lane scheduling error: {exc}")
            results = [
                {"doi": e.identifier, "success": False, "error": f"lane error: {exc}"}
                for e in entries if not e.unresolved
            ]
            partial_path = Path(output) / "batch_results.partial.json"
            _atomic_write_json(partial_path, results)
            print(f"  Partial results saved to: {partial_path}")
            return
        ok = sum(1 for r in results if r.get("success"))
        print(f"\n  Lane results: {ok}/{len(results)} succeeded")
        if format == "json":
            _write_retry_file(output, results)
            out_path = Path(output) / "batch_results.json"
            _atomic_write_json(out_path, results)
            print(f"  Results saved to: {out_path}")
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
        results = batch_download(dois, output_dir=output, scihub_enabled=True, fallbacks=fallbacks)
        # Verify file existence for each "success" result before writing report
        _verify_batch_results(results, output)
        if format == "json":
            _write_retry_file(output, results)
            out_path = Path(output) / "batch_results.json"
            _atomic_write_json(out_path, results)
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
        _atomic_write_json(out_path, results)
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


@app.command("sciverse-setup")
def sciverse_setup(
    token: str = typer.Option("", "--token", help="Sciverse API token (https://sciverse.space/tokens)"),
    test: bool = typer.Option(True, "--test/--no-test", help="Validate the token after saving"),
    open_console: bool = typer.Option(True, "--open/--no-open", help="Open the token console when unconfigured"),
) -> None:
    """Configure Sciverse API access (semantic evidence + metadata search + relations)."""
    import webbrowser

    from .config import load_config, update_config
    from .sources.sciverse import TOKEN_URL, is_configured, key_preview, validate_key

    config = load_config()
    supplied = str(token or "").strip()

    if supplied:
        update_config("sciverse_api_key", supplied)
        config = load_config()
        print(f"  Sciverse API token: saved ({key_preview(supplied)})")
    elif is_configured(config):
        print(f"  Sciverse API token: set ({key_preview(config.get('sciverse_api_key'))})")
    else:
        print("  Sciverse API token: (not set)")
        if open_console:
            try:
                webbrowser.open(TOKEN_URL)
                print(f"  已打开控制台: {TOKEN_URL}")
            except Exception:
                pass
        print("\n  配置步骤:")
        print(f"  1. 打开 {TOKEN_URL} 并登录/注册 Sciverse")
        print("  2. 在「密钥」页面创建 API Token")
        print("  3. 运行: scansci-pdf sciverse-setup --token <token> --test")
        print("\n  能力: 语义证据片段 / 元数据检索 / 引文图谱 / 图表下载（可选，不影响现有下载）")
        print("  配额紧张时可关闭 meta 路: scansci-pdf config-cmd sciverse_meta_search_enabled false")
        return

    if not config.get("sciverse_enabled", True):
        print("  ⚠️ sciverse_enabled=false，当前不会调用 Sciverse")
        print("  开启: scansci-pdf config-cmd sciverse_enabled true")
        return

    if not test:
        print("  跳过验证（--no-test）")
        return

    validation = validate_key(config)
    status = validation.get("status")
    if status == "ok":
        print(f"  ✅ 验证通过: meta-catalog 可用（{validation.get('fields', 0)} 个字段）")
        print("  Sciverse 已启用：检索会自动并入语义证据与元数据。")
        print("  配额紧张时可关闭 meta 路: scansci-pdf config-cmd sciverse_meta_search_enabled false")
    elif status == "invalid":
        print("  ❌ 验证失败: Token 无效或已过期（HTTP 401）")
        print("  请重新创建 Token: https://sciverse.space/tokens")
    elif status == "forbidden":
        print("  ⚠️ Token 有效但缺少 meta-catalog 权限（HTTP 403）")
        print("  可尝试 scansci-pdf sciverse-search 验证语义检索权限，或联系 Sciverse 开通字段权限。")
    elif status == "rate_limited":
        print("  ⚠️ 触发配额（HTTP 429）。Token 本身可能有效，请稍后重试验证。")
    else:
        print(f"  ⚠️ 验证请求失败: {validation.get('message', '未知错误')}")
        if validation.get("action"):
            print(f"  action: {validation['action']}")


@app.command("elsevier-setup")
def elsevier_setup(
    api_key: str = typer.Option("", help="Elsevier API key"),
    inst_token: str = typer.Option("", help="Elsevier institutional token（可选，通常无需配置：校园网 + API key 即可）"),
) -> None:
    """Configure Elsevier API access for direct full-text retrieval."""
    from .config import load_config, save_config

    config = load_config()
    changed = False

    if api_key:
        config["elsevier_api_key"] = api_key
        changed = True
        print(f"  Elsevier API key: saved")
        # 配置即验证：自检样本自动跑一遍，当场给出 key 画像
        try:
            from .elsevier_check import check_key_profile

            _rows, profile, advice = check_key_profile(config)
            print(f"  key 画像: {profile}")
            print(f"  建议: {advice}")
        except Exception as e:
            print(f"  自检失败（可稍后运行 elsevier-check）: {e}")
    if inst_token:
        config["elsevier_insttoken"] = inst_token
        changed = True
        print(f"  Elsevier inst token: saved")

    if not changed:
        has_key = bool(config.get("elsevier_api_key"))
        has_token = bool(config.get("elsevier_insttoken"))
        print(f"  Elsevier API key:   {'set' if has_key else '(not set)'}")
        print(f"  Elsevier inst token: {'set' if has_token else '(not set, 通常不需要)'}")
        print(f"\n  Usage: scansci-pdf elsevier-setup --api-key YOUR_KEY")
        print(f"\n  申请 key：机构邮箱注册 Elsevier 账号，并在校园网/学校 VPN 下创建 key"
              f"（门户靠注册时的邮箱域 + 出口 IP 识别机构，个人邮箱/校外创建绑不上机构）。")
        print(f"  创建后当场跑 elsevier-check：无 key 基线 406、大刊样本 200 即绑定成功。")
        print(f"  提示：insttoken 通常不需要——API key + 校园网/机构网络出口即可（仅拿不到校园出口时申请）。")
        print(f"  NOT_ENTITLED 表示未连校园网或学校未订阅该刊，不是缺 insttoken。")
        print(f"  403 'Requestor configuration settings insufficient'：出口不在机构注册段（配 network_proxy 走校园出口），"
              f"或机构未给该 key 开 API 全文权限（联系机构 Elsevier 管理员）。")


@app.command("elsevier-check")
def elsevier_check_cmd(
    doi: str = typer.Option("", "--doi", help="单篇 DOI 逐篇双路由探测"),
    file: str = typer.Option("", "--file", help="DOI 清单（每行一个，逐篇探测）"),
    json_out: bool = typer.Option(False, "--json", help="输出机读 JSON"),
) -> None:
    """Elsevier API key 权限自检与逐篇探测（纯查询，零下载）。"""
    from rich.console import Console
    from rich.table import Table

    console = Console()

    from . import progress_reporter as _pr
    from .config import load_config
    from .elsevier_check import check_dois, check_key_profile

    config = load_config()
    console.print("[bold]Elsevier key 权限自检[/bold]")
    profile_rows, profile, advice = check_key_profile(config)
    t = Table(title="key 自检样本")
    for col in ("DOI", "样本类型", "代理路由", "直连路由", "判定"):
        t.add_column(col)
    for r in profile_rows:
        t.add_row(r["doi"][:34], r["sample_type"], r["proxy"], r["direct"], r["verdict"])
    console.print(t)
    console.print(f"[bold]key 画像:[/bold] {profile} — {advice}")

    dois: list[str] = []
    if doi:
        dois.append(doi.strip())
    elif file:
        from pathlib import Path as _Path

        p = _Path(file)
        if p.exists():
            dois = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        else:
            console.print(f"[red]清单不存在: {file}[/red]")
    if dois:
        _pr.start_task("Elsevier权限检测", total=len(dois))
        rows = check_dois(dois, config, progress=_pr)
        _pr.finish()
        t2 = Table(title="用户样本双路由探测")
        for col in ("DOI", "代理路由", "直连路由", "判定", "建议通道"):
            t2.add_column(col)
        for r in rows:
            t2.add_row(r["doi"][:34], r["proxy"], r["direct"], r["verdict"], r["route_advice"])
        console.print(t2)
        if json_out:
            import json as _json

            console.print(_json.dumps({"profile": profile, "advice": advice,
                                       "rows": profile_rows + rows},
                                      ensure_ascii=False, indent=2))


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
    from .institutional.publisher_batch import PaperRecord, PublisherBatchDownloader
    from .institutional.publisher_profiles import _PROFILE_ALIASES, list_publisher_profiles
    from .sources.publishers import get_publisher

    key = (publisher or "").strip().lower()
    if not key and dois:
        key = get_publisher(dois[0]).lower()
    profile = _PROFILE_ALIASES.get(key)
    if profile is None:
        raise SystemExit(
            f"Unknown/unresolved publisher '{publisher or key or '(auto)'}'. "
            f"Pass --publisher with one of: {', '.join(list_publisher_profiles())}")
    # This path drives a manual SSO/2FA login in the browser, so the window
    # has to stay visible: browser_headless in the config must not win here.
    downloader = PublisherBatchDownloader(
        config, profile=profile, require_interactive_browser=True
    )
    records = [PaperRecord(doi=d) for d in dois]
    summary = downloader.run_records(records, output)
    results = summary.get("results", []) if isinstance(summary, dict) else []

    def _ok(r: Any) -> bool:
        if isinstance(r, dict):
            return bool(r.get("ok"))
        return bool(getattr(r, "ok", False))

    success = sum(1 for r in results if _ok(r))
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
    out: Annotated[str, typer.Option("--out", help="Write results to a channel-annotated queue file (feed to 'batch --lanes')")] = "",
) -> None:
    """Search academic papers (OpenAlex/Crossref/S2; NCPSSD for Chinese; Sciverse when configured).

    Results include DOI, title, authors, year, and citation count.
    Use the DOIs with 'scansci-pdf get' or 'scansci-pdf batch' to download.

    Examples:
      scansci-pdf search "carbon cycle" --limit 10 --sort cited_by_count
      scansci-pdf search --author "Fang Jingyun" --limit 10 --sort cited_by_count
      scansci-pdf search --author-id A5102961214 --limit 10 --sort cited_by_count
    """
    import json as _json
    from .search import search_papers_v2 as search_papers

    if author or author_id:
        # Author-based search
        results = search_papers(
            limit=limit, year_from=year_from, year_to=year_to, sort=sort,
            author=author if author else None,
            author_id=author_id if author_id else None,
        )
        # Keep the resolved author match for the JSON payload; the human
        # readable line is only shown in non-JSON mode.
        author_match = None
        if results and results[0].get("_author_match"):
            author_match = results[0].pop("_author_match")
    else:
        sort_key = sort if sort else None
        results = search_papers(query, limit=limit, year_from=year_from, year_to=year_to, sort=sort_key)
        author_match = None

    if out:
        from .pipeline import QueueEntry, predict_channel, write_queue

        qe = [
            QueueEntry(
                identifier=r.get("doi") or r.get("arxiv_id") or "",
                channel=predict_channel(r.get("doi") or ""),
                title=str(r.get("title", "")),
            )
            for r in results
            if r.get("doi") or r.get("arxiv_id")
        ]
        p = write_queue(qe, out)
        print(f"  Queue written: {p} ({len(qe)} entries)")

    hint = None
    try:
        from .config import load_config
        from .sources.sciverse import is_configured
        _cfg = load_config()
        if not is_configured(_cfg) and _cfg.get("sciverse_enabled", True):
            hint = (
                "未配置 Sciverse（sciverse_api_key）；语义证据/元数据/引文图谱未启用。"
                "运行 scansci-pdf sciverse-setup --token <token> --test 启用（可选）。"
            )
    except Exception:
        pass

    if json_output:
        payload = {"results": results}
        if author_match is not None:
            payload["author_match"] = author_match
        if hint:
            payload["config_hint"] = {
                "service": "sciverse",
                "status": "not_configured",
                "config_key": "sciverse_api_key",
                "message": hint,
                "action": "scansci-pdf sciverse-setup --token <token> --test",
            }
        print(_json.dumps(payload, indent=2, ensure_ascii=True))
    else:
        if author_match is not None:
            print(f"  Author: {author_match['name']} (ID:{author_match['id']}, works:{author_match['works_count']}, cited:{author_match['cited_by_count']})")
        for i, r in enumerate(results, 1):
            authors = ", ".join(r.get("authors", [])[:3] or [])
            cited = r.get("cited_by_count", 0)
            print(f"{i:2d}. {r.get('title', '?')[:80]}")
            print(f"    {authors}  ({r.get('year', '?')})  cited={cited}  doi:{r.get('doi', '?')}")
            if i < len(results):
                print()
        if hint:
            print(f"\n  提示: {hint}")


def _sciverse_config_or_exit():
    from .config import load_config
    from .sources.sciverse import is_configured

    config = load_config()
    if not is_configured(config):
        print("Error: sciverse_api_key is not configured.")
        print("  1. Create a key at https://sciverse.space/tokens")
        print("  2. scansci-pdf config-cmd sciverse_api_key <token>")
        raise typer.Exit(1)
    return config


def _parse_meta_filters_json(filters_json: str) -> list[dict[str, Any]]:
    if not filters_json.strip():
        return []
    try:
        parsed = json.loads(filters_json)
    except json.JSONDecodeError as exc:
        print(f"Error: invalid --filters-json: {exc}")
        raise typer.Exit(2) from None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not all(
        isinstance(item, dict) and item.get("field") for item in parsed
    ):
        print("Error: --filters-json must be a JSON array of {field, operator, value}")
        raise typer.Exit(2)
    return parsed


@app.command("sciverse-search")
def sciverse_search_cmd(
    query: str = typer.Argument(..., help="Natural-language research question"),
    top_k: int = typer.Option(10, help="Chunks to return (1-100)"),
    sub_queries: int = typer.Option(0, help="Query-rewrite count (0-4; higher recall/latency)"),
    year_from: int = typer.Option(None, help="Start year"),
    year_to: int = typer.Option(None, help="End year"),
    lang: str = typer.Option("", help="Document language (en/zh/...)"),
    venue: str = typer.Option("", help="Normalized venue name (e.g. Nature)"),
    venue_type: str = typer.Option("", help="journal/conference/book series/repository"),
    topic: str = typer.Option("", help="Primary topic name"),
    topic_domain: str = typer.Option("", help="Physical/Social/Health/Life Sciences"),
    citation_min: int = typer.Option(None, help="Minimum citation count"),
    citation_max: int = typer.Option(None, help="Maximum citation count"),
    doc_ids: str = typer.Option("", "--doc-ids", help="Comma-separated doc_id hard scope"),
    filters_json: str = typer.Option("", help="Advanced Sciverse filters JSON (merged last)"),
    enrich_dois: bool = typer.Option(False, "--enrich-dois", help="Resolve DOI/unique_id for hits (one extra meta-search call)"),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON payload"),
) -> None:
    """Semantic evidence search via Sciverse agentic-search (needs sciverse_api_key).

    Returns citable chunks with doc_id/chunk_id/offset/page_no. Read more
    context with 'scansci-pdf sciverse-content <doc_id> --offset N'.
    Use --enrich-dois when the hits will be fed into download/selection.

    Setup:
      scansci-pdf config-cmd sciverse_api_key <token>   # https://sciverse.space/tokens
    """
    from .sources.sciverse import (
        SciverseError,
        agentic_search,
        build_filters,
        enrich_hits_with_metadata,
    )

    config = _sciverse_config_or_exit()

    extra = None
    if filters_json.strip():
        try:
            extra = json.loads(filters_json)
        except json.JSONDecodeError as exc:
            print(f"Error: invalid --filters-json: {exc}")
            raise typer.Exit(2) from None
        if not isinstance(extra, dict):
            print("Error: --filters-json must be a JSON object")
            raise typer.Exit(2)

    filters = build_filters(
        lang=lang,
        year_from=year_from,
        year_to=year_to,
        venue=venue,
        venue_type=venue_type,
        topic=topic,
        topic_domain=topic_domain,
        citation_min=citation_min,
        citation_max=citation_max,
        doc_ids=[part.strip() for part in doc_ids.split(",") if part.strip()],
        extra=extra,
    )
    try:
        payload = agentic_search(
            query, config=config, top_k=top_k, sub_queries=sub_queries, filters=filters
        )
    except SciverseError as exc:
        print(f"Error: {exc}")
        if exc.action:
            print(f"  action: {exc.action}")
        raise typer.Exit(1) from None

    hits = payload.get("hits") if isinstance(payload.get("hits"), list) else []
    doi_before = sum(1 for hit in hits if hit.get("doi"))
    if enrich_dois and hits:
        hits = enrich_hits_with_metadata(hits, config)
    doi_added = sum(1 for hit in hits if hit.get("doi")) > doi_before
    if json_output:
        print(json.dumps(
            {"count": len(hits), "doi_enriched": doi_added, "hits": hits},
            indent=2, ensure_ascii=False,
        ))
        return
    if not hits:
        print("No results. Broadening the query or relaxing filters can help (EMPTY_RESULT).")
        return
    print(f"Sciverse agentic-search: {len(hits)} evidence chunk(s)\n")
    for i, hit in enumerate(hits, 1):
        title = str(hit.get("title") or "Untitled")
        year = hit.get("publication_published_year") or ""
        venue_name = hit.get("publication_venue_name_unified") or ""
        meta = "  ".join(str(part) for part in (year, venue_name) if part)
        print(f"{i:2d}. {title[:90]}")
        if meta:
            print(f"    {meta}")
        loc = []
        if hit.get("page_no") is not None:
            loc.append(f"page {hit['page_no']}")
        if hit.get("offset") is not None:
            loc.append(f"offset {hit['offset']}")
        if hit.get("score") is not None:
            loc.append(f"score {hit['score']}")
        if hit.get("doi"):
            loc.append(f"doi {hit['doi']}")
        loc.append(f"doc_id {hit.get('doc_id') or '?'}")
        print(f"    {'  '.join(loc)}")
        chunk = str(hit.get("chunk") or "").strip().replace("\n", " ")
        if chunk:
            print(f"    {chunk[:300]}{'…' if len(chunk) > 300 else ''}")
        print()


@app.command("sciverse-content")
def sciverse_content_cmd(
    doc_id: str = typer.Argument(..., help="doc_id returned by sciverse-search"),
    offset: int = typer.Option(None, help="Character offset (Unicode code points); omit for full text"),
    limit: int = typer.Option(700, help="Max characters when offset is given"),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON payload"),
) -> None:
    """Read Sciverse full text for a doc_id (paged via next_offset/more)."""
    from .sources.sciverse import SciverseError, content

    config = _sciverse_config_or_exit()
    try:
        payload = content(doc_id, config=config, offset=offset, limit=limit)
    except SciverseError as exc:
        print(f"Error: {exc}")
        if exc.action:
            print(f"  action: {exc.action}")
        raise typer.Exit(1) from None

    if json_output:
        print(json.dumps({"doc_id": doc_id, **payload}, indent=2, ensure_ascii=False))
        return
    print(str(payload.get("text") or ""))
    if payload.get("more"):
        print(
            f"\n[more=true next_offset={payload.get('next_offset')} "
            f"chars_returned={payload.get('chars_returned')}]"
        )


@app.command("sciverse-meta-search")
def sciverse_meta_search_cmd(
    query: str = typer.Argument("", help="Keywords; omit when using filters only"),
    collection: str = typer.Option("papers", help="papers/authors/sources"),
    page: int = typer.Option(1, help="Page number (>=1)"),
    page_size: int = typer.Option(25, help="Results per page (1-200)"),
    year_from: int = typer.Option(None, help="Start year"),
    year_to: int = typer.Option(None, help="End year"),
    lang: str = typer.Option("", help="Document language"),
    venue: str = typer.Option("", help="Normalized venue name"),
    venue_type: str = typer.Option("", help="journal/conference/book series/repository"),
    topic: str = typer.Option("", help="Primary topic display name"),
    topic_domain: str = typer.Option("", help="Primary topic domain display name"),
    author: str = typer.Option("", help="Author name (fuzzy match)"),
    doi: str = typer.Option("", help="Exact DOI"),
    citation_min: int = typer.Option(None, help="Minimum citation count"),
    citation_max: int = typer.Option(None, help="Maximum citation count"),
    doc_ids: str = typer.Option("", "--doc-ids", help="Comma-separated doc_id scope"),
    filters_json: str = typer.Option("", help="JSON array of {field, operator, value}"),
    sort: str = typer.Option("", help="Sort field[:asc|desc], e.g. citation_count:desc"),
    fields: str = typer.Option("", help="Comma-separated projection fields"),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON payload"),
) -> None:
    """Structured metadata search via Sciverse meta-search (returns DOI/unique_id)."""
    from .sources.sciverse import (
        SciverseError,
        build_meta_filters,
        build_meta_sort,
        meta_search,
    )

    config = _sciverse_config_or_exit()
    extra_filters = _parse_meta_filters_json(filters_json)
    projection = [part.strip() for part in fields.split(",") if part.strip()]
    try:
        payload = meta_search(
            query,
            config=config,
            collection=collection,
            filters=build_meta_filters(
                year_from=year_from,
                year_to=year_to,
                lang=lang,
                venue=venue,
                venue_type=venue_type,
                author=author,
                topic=topic,
                topic_domain=topic_domain,
                doi=doi,
                doc_ids=[part.strip() for part in doc_ids.split(",") if part.strip()],
                citation_min=citation_min,
                citation_max=citation_max,
                extra=extra_filters,
            ),
            sort=build_meta_sort(sort) if sort else None,
            fields=projection or None,
            page=page,
            page_size=page_size,
        )
    except SciverseError as exc:
        print(f"Error: {exc}")
        if exc.action:
            print(f"  action: {exc.action}")
        raise typer.Exit(1) from None

    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    total = payload.get("total_count") or payload.get("total") or len(results)
    total_label = f">={total}" if payload.get("total_count_capped") else str(total)
    print(f"Sciverse meta-search: {len(results)} result(s), total={total_label}\n")
    for i, row in enumerate(results, 1):
        title = str(row.get("title") or "Untitled")
        authors = row.get("author")
        if isinstance(authors, list):
            names = [
                item.get("name") if isinstance(item, dict) else str(item)
                for item in authors[:3]
            ]
            author_text = ", ".join(str(name) for name in names if name)
        else:
            author_text = str(authors or "")
        year = row.get("publication_published_year") or ""
        venue_name = row.get("publication_venue_name_unified") or ""
        cited = row.get("citation_count") or 0
        print(f"{i:2d}. {title[:90]}")
        meta = "  ".join(str(part) for part in (author_text, year, venue_name) if part)
        if meta:
            print(f"    {meta}")
        print(f"    cited={cited}  doi:{row.get('doi') or '?'}")
        ids = [
            f"{key}={row[key]}"
            for key in ("unique_id", "doc_id")
            if row.get(key)
        ]
        if ids:
            print(f"    {'  '.join(ids)}")
        print()
    if payload.get("next_cursor"):
        print(f"[next_cursor={payload['next_cursor']}]")


@app.command("sciverse-relations")
def sciverse_relations_cmd(
    unique_id: str = typer.Argument(..., help="unique_id from sciverse-meta-search"),
    relation: str = typer.Option("REFERENCES", help="CITATIONS/REFERENCES/RELATED_WORKS"),
    page: int = typer.Option(1, help="Page number (>=1)"),
    page_size: int = typer.Option(25, help="Items per page (1-200)"),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON payload"),
) -> None:
    """Page through a paper's citations, references, or related works."""
    from .sources.sciverse import SciverseError, paper_relations

    config = _sciverse_config_or_exit()
    try:
        payload = paper_relations(
            unique_id,
            relation=relation,
            page=page,
            page_size=page_size,
            config=config,
        )
    except SciverseError as exc:
        print(f"Error: {exc}")
        if exc.action:
            print(f"  action: {exc.action}")
        raise typer.Exit(1) from None

    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    total = payload.get("total_count") or len(items)
    print(f"{relation.upper()}: {len(items)} item(s), total={total}\n")
    for i, item in enumerate(items, 1):
        print(f"{i:2d}. {item.get('title') or 'Untitled'}")
        print(f"    id:{item.get('id') or '?'}  type:{item.get('id_type') or '?'}")
    pages = payload.get("total_pages")
    if pages:
        print(f"\n[page={payload.get('page')}/{pages}]")


@app.command("sciverse-resource")
def sciverse_resource_cmd(
    file_name: str = typer.Argument(..., help="Relative resource path from content Markdown"),
    output_dir: str = typer.Option("", "--output-dir", help="Directory (or file path) to save into"),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON payload"),
) -> None:
    """Download a Sciverse figure/table/attachment binary to disk."""
    from .sources.sciverse import SciverseError, save_resource

    config = _sciverse_config_or_exit()
    try:
        saved = save_resource(file_name, output_dir or "", config=config)
    except SciverseError as exc:
        print(f"Error: {exc}")
        if exc.action:
            print(f"  action: {exc.action}")
        raise typer.Exit(1) from None
    if json_output:
        print(json.dumps(saved, indent=2, ensure_ascii=False))
        return
    print(f"Saved: {saved['path']}")
    print(f"  type: {saved['content_type']}  size: {saved['size']} bytes")


@app.command("sciverse-catalog")
def sciverse_catalog_cmd(
    collection: str = typer.Option("papers", help="papers/authors/sources"),
    samples: bool = typer.Option(False, "--samples", help="Include sample values for enum fields"),
    json_output: bool = typer.Option(False, "--json", help="Print the raw JSON payload"),
) -> None:
    """Show the Sciverse meta-search field/operator catalog."""
    from .sources.sciverse import SciverseError, meta_catalog

    config = _sciverse_config_or_exit()
    try:
        payload = meta_catalog(
            collection=collection,
            include_sample_values=samples,
            config=config,
        )
    except SciverseError as exc:
        print(f"Error: {exc}")
        if exc.action:
            print(f"  action: {exc.action}")
        raise typer.Exit(1) from None

    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
    print(f"Collection: {collection}  fields: {len(fields)}")
    operators = payload.get("filter_operators") or []
    if operators:
        print(f"Operators: {', '.join(str(op) for op in operators)}")
    default_fields = payload.get("default_fields") or []
    if default_fields:
        print(f"Default fields: {', '.join(str(field) for field in default_fields)}")
    print()
    for field in fields:
        if not isinstance(field, dict):
            continue
        flags = []
        if field.get("filterable"):
            flags.append("filter")
        if field.get("sortable"):
            flags.append("sort")
        if field.get("searchable"):
            flags.append("search")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        print(f"- {field.get('name')}: {field.get('type')}{suffix}")


@app.command("plan")
def plan_cmd(
    query: str = typer.Argument(help="Research topic"),
    domain: str = typer.Option("general", "--domain", help="Domain profile (general/medicine/computer_science/chinese_general/...)"),
    depth: str = typer.Option("standard", "--depth", help="Depth: quick/standard/systematic"),
    question: str = typer.Option("", "--question", help="Research question"),
    year_from: int = typer.Option(None, "--year-from"),
    year_to: int = typer.Option(None, "--year-to"),
) -> None:
    """Build an auditable search protocol without running a search (ScanSci Find)."""
    from .discovery import find_cli_available, plan
    if not find_cli_available():
        print("Error: scansci-find CLI not available. Install it, e.g.: pip install -e D:\\Projects\\active\\scansci-find")
        raise typer.Exit(1)
    print(json.dumps(plan(query, domain=domain, depth=depth, question=question,
                          year_from=year_from, year_to=year_to), ensure_ascii=False, indent=2))


@app.command("estimate")
def estimate_cmd(
    query: str = typer.Argument(help="Research topic"),
    domain: str = typer.Option("general", "--domain"),
    depth: str = typer.Option("standard", "--depth"),
    question: str = typer.Option("", "--question"),
    year_from: int = typer.Option(None, "--year-from"),
    year_to: int = typer.Option(None, "--year-to"),
) -> None:
    """Estimate result volume before spending a full search budget (ScanSci Find)."""
    from .discovery import find_cli_available, estimate
    if not find_cli_available():
        print("Error: scansci-find CLI not available. Install it, e.g.: pip install -e D:\\Projects\\active\\scansci-find")
        raise typer.Exit(1)
    print(json.dumps(estimate(query, domain=domain, depth=depth, question=question,
                              year_from=year_from, year_to=year_to), ensure_ascii=False, indent=2))


@app.command("smoke")
def smoke_cmd(
    query: str = typer.Argument(help="Research topic"),
    domain: str = typer.Option("general", "--domain"),
    records: int = typer.Option(4, "--records", help="Records per source (default 4)"),
) -> None:
    """Fetch a few records per source and validate the candidate contract (ScanSci Find)."""
    from .discovery import find_cli_available, smoke
    if not find_cli_available():
        print("Error: scansci-find CLI not available. Install it, e.g.: pip install -e D:\\Projects\\active\\scansci-find")
        raise typer.Exit(1)
    print(json.dumps(smoke(query, domain=domain, records_per_source=records), ensure_ascii=False, indent=2))


@app.command("calibrate")
def calibrate_cmd(
    query: str = typer.Argument(help="Research topic"),
    domain: str = typer.Option("general", "--domain"),
    depth: str = typer.Option("standard", "--depth"),
    sample_size: int = typer.Option(100, "--sample-size"),
) -> None:
    """Run a bounded calibration sample before a high-recall search (ScanSci Find)."""
    from .discovery import find_cli_available, calibrate
    if not find_cli_available():
        print("Error: scansci-find CLI not available. Install it, e.g.: pip install -e D:\\Projects\\active\\scansci-find")
        raise typer.Exit(1)
    print(json.dumps(calibrate(query, domain=domain, depth=depth, sample_size=sample_size), ensure_ascii=False, indent=2))


@app.command("verify")
def verify_cmd(
    input_file: str = typer.Argument(help="Candidates JSON file (from discovery search)"),
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Verify DOI/PMID/arXiv identifiers against authoritative APIs (ScanSci Find)."""
    from .discovery import find_cli_available, verify
    if not find_cli_available():
        print("Error: scansci-find CLI not available. Install it, e.g.: pip install -e D:\\Projects\\active\\scansci-find")
        raise typer.Exit(1)
    print(json.dumps(verify(input_file, limit=limit), ensure_ascii=False, indent=2))


@app.command("resolve-oa")
def resolve_oa_cmd(
    input_file: str = typer.Argument(help="Candidates JSON file (from discovery search)"),
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Resolve DOI open-access locations through Unpaywall (ScanSci Find)."""
    from .discovery import find_cli_available, resolve_oa
    if not find_cli_available():
        print("Error: scansci-find CLI not available. Install it, e.g.: pip install -e D:\\Projects\\active\\scansci-find")
        raise typer.Exit(1)
    print(json.dumps(resolve_oa(input_file, limit=limit), ensure_ascii=False, indent=2))


@app.command("build-queue")
def build_queue_cmd(
    input_path: str = typer.Argument(help="Discovery output dir (with download_queue.json) or candidates JSON file"),
    out: str = typer.Option("", "--out", help="Write identifier list to file (one per line)"),
) -> None:
    """Build a download identifier queue from ScanSci Find output — feed to 'scansci-pdf batch'."""
    from pathlib import Path as _Path
    from .discovery import build_download_queue
    import json as _json

    p = _Path(input_path)
    if p.is_dir():
        identifiers = build_download_queue(p)
    else:
        try:
            candidates = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            print(f"Error: cannot read {input_path}")
            raise typer.Exit(1)
        if isinstance(candidates, dict):
            candidates = candidates.get("candidates", [])
        identifiers = []
        for c in candidates:
            for key in ("doi", "arxiv_id"):
                if c.get(key):
                    identifiers.append(str(c[key]))
                    break
    # Resolve any bare titles (no DOI/arXiv) through the built-in resolver
    unresolved = [i for i in identifiers if i]
    for i in unresolved:
        print(i)
    if out:
        _Path(out).write_text("\n".join(unresolved) + "\n", encoding="utf-8")
        print(f"\nWrote {len(unresolved)} identifiers to {out} (use: scansci-pdf batch {out})")
    else:
        print(f"\n{len(unresolved)} identifiers ready (use: scansci-pdf batch <file>)")


@app.command("find")
def find_cmd(
    query: str = typer.Argument(help="Research topic"),
    out: str = typer.Option(..., "--out", help="Output directory for ScanSci Find artifacts"),
    domain: str = typer.Option("general", "--domain"),
    depth: str = typer.Option("quick", "--depth", help="quick/standard/systematic"),
    limit: int = typer.Option(20, "--limit"),
    expand_citations: bool = typer.Option(False, "--expand-citations"),
    citation_rounds: int = typer.Option(None, "--citation-rounds"),
    citation_source: str = typer.Option("semantic", "--citation-source"),
    verify_identifiers: bool = typer.Option(False, "--verify-identifiers"),
    resolve_oa: bool = typer.Option(False, "--resolve-oa"),
    find_preprints: bool = typer.Option(False, "--find-preprints", help="Find lawful preprint copies of paywalled candidates"),
    code_links: bool = typer.Option(False, "--code-links", help="Attach Papers with Code code-availability metadata"),
    sort: str = typer.Option("", "--sort", help="relevance | recency | citations"),
    year_from: int = typer.Option(None, "--year-from"),
    year_to: int = typer.Option(None, "--year-to"),
) -> None:
    """Full discovery search via ScanSci Find's 13-source engine.

    Writes candidates.json / download_queue.json / coverage & PRISMA reports
    into --out. Follow up with 'scansci-pdf build-queue <dir>' then
    'scansci-pdf batch <queue file>' to download.
    """
    from .discovery import build_download_queue, find_cli_available, search
    if not find_cli_available():
        print("Error: scansci-find CLI not available. Install it, e.g.: pip install -e D:\\Projects\\active\\scansci-find")
        raise typer.Exit(1)
    payload = search(
        query, out, domain=domain, depth=depth, limit=limit,
        expand_citations=expand_citations, citation_source=citation_source,
        citation_rounds=citation_rounds,
        verify_identifiers=verify_identifiers, resolve_oa=resolve_oa,
        find_preprints=find_preprints, code_links=code_links, sort=sort,
        year_from=year_from, year_to=year_to,
    )
    queue = build_download_queue(out)
    print(f"  Query: {query}")
    print(f"  Domain: {domain} | Depth: {depth} | Total candidates: {payload.get('total', 0)}")
    print(f"  Download queue: {len(queue)} identifiers")
    print(f"  Artifacts: {out}")
    if queue:
        print(f"  Next: scansci-pdf build-queue {out} --out queue.txt && scansci-pdf batch queue.txt")


@app.command("manifest")
def manifest_cmd(
    input_file: str = typer.Argument(help="oa_manifest.json produced by scansci-find"),
    output: str = typer.Option(".", help="Output directory"),
    scihub: bool = typer.Option(False, "--scihub", help="Allow grey sources for non-eligible entries"),
    use_tor: bool = typer.Option(False, "--use-tor"),
    use_vpnsci: bool = typer.Option(True, "--use-vpnsci", help="Enable WebVPN/institutional phase for needs_institution entries"),
) -> None:
    """Download per a ScanSci Find oa_manifest.

    Eligible entries (open_pdf/preprint with a PDF URL) are fetched directly
    from the manifest URL; needs_institution entries go through the racing
    engine with the institutional phase enabled. Writes download_results.json
    so scansci-find reconcile can write results back into the candidate set.
    """
    import json as _json
    from pathlib import Path as _Path

    from .identifiers import safe_filename
    from .pdf_utils import download_pdf, is_pdf_file
    from .sources import _update_doi_index, download

    payload = _json.loads(_Path(input_file).read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(entries, list):
        print("  Invalid manifest: expected a list of entries or {'entries': [...]}")
        raise typer.Exit(1)
    config = load_config()
    out_dir = _Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    direct_hits = 0
    for entry in entries:
        identifier = str(entry.get("identifier") or "").strip()
        if not identifier:
            continue
        status = str(entry.get("download_status") or "")
        pdf_url = str(entry.get("pdf_url") or "").strip()
        if status == "eligible" and pdf_url:
            out_path = out_dir / f"{safe_filename(identifier)}.pdf"
            result = download_pdf(pdf_url, out_path, config, source="oa_manifest")
            if result and result.get("success") and is_pdf_file(out_path):
                result["identifier"] = identifier
                result["doi"] = identifier
                result["cached"] = False
                _update_doi_index(out_dir, identifier, out_path, source="oa_manifest", strategy=config.get("download_strategy", ""), config=config)
                results.append(result)
                direct_hits += 1
                continue
            print(f"  Direct PDF failed for {identifier}, falling back to engine")
        result = download(identifier, out_dir, scihub_enabled=scihub, use_tor=use_tor, use_vpnsci=use_vpnsci)
        results.append(result)

    succeeded = sum(1 for r in results if r and r.get("success"))
    report = {
        "total": len(results),
        "direct_downloads": direct_hits,
        "succeeded": succeeded,
        "failed": len(results) - succeeded,
        "results": results,
    }
    (out_dir / "download_results.json").write_text(_json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(_json.dumps(report, indent=2, ensure_ascii=False))


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
    # Console-safe printing: replace characters the target encoding cannot
    # represent (e.g. GBK consoles with non-ASCII author names) instead of
    # crashing on UnicodeEncodeError.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    app()


if __name__ == "__main__":
    main()
