"""CLI interface for InstSci."""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# Fix Windows console encoding for Unicode output
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config, save_config
from .fetcher import PaperFetcher
from .schools import get_school, list_schools, search_schools
from .sources import semantic_scholar

app = typer.Typer(
    name="scansci-pdf",
    help="Fetch academic papers via institutional access, Open Access, or arXiv.",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _ensure_email(config: dict):
    """Prompt user to set email if not configured (needed for Unpaywall)."""
    if not config.get("email", ""):
        console.print("[yellow]Email not configured (needed for Unpaywall OA detection).[/yellow]")
        email = typer.prompt("Enter your email address")
        config["email"] = email
        save_config(config)
        console.print(f"[green]Email saved: {email}[/green]")


def _school_type_label(school_type: str) -> str:
    return {
        "webvpn": "CampusPortal",
        "easyconnect": "CampusConnector",
        "atrust": "CampusConnector",
        "ezproxy": "LibraryPortal",
    }.get(school_type, school_type)


def _read_dois(path: Path) -> list[str]:
    """Read DOIs from a text file, one per line, skipping comments and blanks."""
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _print_batch_summary(summary: dict, *, extra_lines: list[str] | None = None) -> None:
    """Print a standard batch download summary."""
    console.print(
        f"[bold]Done:[/bold] {summary['success']}/{summary['count']} verified PDFs, "
        f"{summary.get('unverified', 0)} unverified PDFs."
    )
    console.print(f"[dim]PDF dir: {summary['pdf_dir']}[/dim]")
    console.print(f"[dim]Manifest: {summary['manifest']}[/dim]")
    # Auto-stop banner: make clear the batch was halted, not finished cleanly.
    if summary.get("auto_stopped"):
        n = summary.get("ip_blocked_count", 0)
        console.print(
            f"[yellow bold]⚠ 已自动停止：[/yellow bold]连续检测到 IP 被出版商封禁"
            f"（{n} 篇返回 ip_blocked），剩余任务已取消。"
        )
        console.print(
            "[yellow]应对：调低 batch_workers、增大 request_delay_min/max，"
            "或参考 README「被封 IP」章节。[/yellow]"
        )
    for line in extra_lines or []:
        console.print(line)


def _installed_package_version(name: str) -> str:
    import importlib.metadata
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


# Minimum recommended cloakbrowser version. cloakbrowser iterates on
# fingerprint patches and anti-detection fixes very frequently; older versions
# fall behind bot-detection heuristics and start failing silently. This is a
# soft floor surfaced by `doctor`/`check`/`browser-doctor` (offline, no network),
# not a hard install requirement. Bump it whenever you pin a new floor in
# pyproject.toml.
CLOAKBROWSER_RECOMMENDED_MIN = "0.4.11"


def _parse_version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in (v or "").split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _cloakbrowser_version_status() -> tuple[str, str]:
    """Return (status, detail) for the installed cloakbrowser.

    status is one of: not_installed / outdated / ok. detail carries the version
    (and an upgrade hint when outdated). Kept offline: anti-detection tooling
    version checks should not phone home.
    """
    version = _installed_package_version("cloakbrowser")
    if not version:
        return ("not_installed", "not installed (pip install 'scansci-pdf[cloakbrowser]')")
    if _parse_version_tuple(version) < _parse_version_tuple(CLOAKBROWSER_RECOMMENDED_MIN):
        hint = f"{version} (outdated, recommend >={CLOAKBROWSER_RECOMMENDED_MIN}: pip install -U cloakbrowser)"
        return ("outdated", hint)
    return ("ok", version)


def _doctor_checks() -> list[tuple[str, str, str]]:
    import shutil
    checks: list[tuple[str, str, str]] = [
        ("Python runtime", "ok", sys.executable),
    ]
    for command in ("scansci-pdf", "scansci-pdf-mcp"):
        path = shutil.which(command)
        checks.append((command, "ok" if path else "warning", path or "not found on PATH"))
    # scansci-pdf + pymupdf: simple installed/missing check
    for package in ("scansci-pdf", "pymupdf"):
        version = _installed_package_version(package)
        checks.append((f"package: {package}", "ok" if version else "warning", version or "not installed"))
    # cloakbrowser: also flag stale versions (anti-detection patches age fast)
    cb_status, cb_detail = _cloakbrowser_version_status()
    checks.append(("package: cloakbrowser", cb_status if cb_status != "not_installed" else "warning", cb_detail))
    try:
        from .cloakbrowser_compat import configure_builtin_cloakbrowser
        cache_dir = configure_builtin_cloakbrowser(create_dir=False)
        status = "ok" if cache_dir.exists() else "warning"
        detail = str(cache_dir) if cache_dir.exists() else f"not downloaded yet: {cache_dir}"
    except Exception as exc:
        status = "warning"
        detail = f"cache check failed: {exc}"
    checks.append(("CloakBrowser cache", status, detail))
    return checks


@app.command("doctor")
def doctor():
    """Inspect ScanSci-PDF runtime, dependencies, and browser cache."""
    table = Table(title="ScanSci-PDF Doctor")
    table.add_column("Item", width=24)
    table.add_column("Status", width=10)
    table.add_column("Detail", overflow="fold")
    styles = {"ok": "green", "warning": "yellow", "info": "cyan", "outdated": "yellow", "not_installed": "yellow"}
    for label, status, detail in _doctor_checks():
        style = styles.get(status, "white")
        table.add_row(label, f"[{style}]{status}[/{style}]", detail)
    console.print(table)


def _apply_school_config(cfg: dict, school: str):
    entry = get_school(school)
    cfg["instsci_school"] = entry.name
    if entry.school_type == "ezproxy":
        cfg["ezproxy_login_url"] = entry.host
        cfg["instsci_base_url"] = ""
    else:
        cfg["instsci_base_url"] = entry.host
        cfg["ezproxy_login_url"] = ""
    return entry


def _access_url(cfg: dict) -> str:
    return cfg.get("ezproxy_login_url", "") or cfg.get("instsci_base_url", "")


def _configured_subscription_institution(cfg: dict) -> str:
    """Return the configured subscription institution search text, if any."""
    return (cfg.get("carsi_idp_name", "") or cfg.get("instsci_school", "") or "").strip()


def _resolve_subscription_institution(
    cfg: dict,
    institution: str,
    *,
    prompt: bool = True,
) -> str:
    """Resolve institution text without hard-coding any school as the default."""
    explicit = institution.strip()
    if explicit:
        return explicit

    configured = _configured_subscription_institution(cfg)
    if configured:
        return configured

    if not prompt:
        console.print(
            "[red]Subscription institution is required.[/red] "
            "Pass --institution or run: instsci setup --school \"Your Institution\""
        )
        raise typer.Exit(1)

    console.print(
        "[yellow]Subscription institution is required for closed-access publisher PDFs.[/yellow]"
    )
    console.print(
        "[dim]Use the institution that owns your subscription, e.g. the name shown in "
        "OpenAthens/Shibboleth/CARSI login pages.[/dim]"
    )
    value = typer.prompt("Subscription institution").strip()
    if not value:
        console.print("[red]Subscription institution cannot be empty.[/red]")
        raise typer.Exit(1)

    cfg["carsi_enabled"] = True
    cfg["carsi_idp_name"] = value
    save_config(cfg)
    return value


def _path_status(path_value: str) -> tuple[str, str]:
    if not path_value:
        return "missing", ""
    path = Path(path_value)
    return ("ok" if path.exists() else "missing", str(path))


def _show_setup_check(cfg: dict) -> bool:
    checks: list[tuple[str, str, str]] = []
    checks.append(("School", "ok" if cfg.get("instsci_school", "") else "missing", cfg.get("instsci_school", "") or "set with --school"))
    checks.append(("Access URL", "ok" if _access_url(cfg) else "missing", _access_url(cfg) or "derived from --school"))
    federated_ready = (not cfg.get("carsi_enabled", False)) or bool(cfg.get("carsi_idp_name", ""))
    checks.append((
        "Federated login",
        "ok" if federated_ready else "missing",
        cfg.get("carsi_idp_name", "") or ("disabled" if not cfg.get("carsi_enabled", False) else "set with --federated-school"),
    ))
    for label, path_value in [
        ("Output dir", cfg.get("output_dir", "")),
        ("Cache dir", cfg.get("cache_dir", "")),
        ("Chrome profile", cfg.get("chrome_profile_dir", "")),
        ("Session dir", cfg.get("carsi_cookie_dir", "")),
    ]:
        status, detail = _path_status(path_value)
        checks.append((label, status, detail))

    table = Table(title="InstSci Environment Check")
    table.add_column("Item", width=18)
    table.add_column("Status", width=10)
    table.add_column("Detail", overflow="fold")
    ready = True
    for label, status, detail in checks:
        if status != "ok":
            ready = False
        style = "green" if status == "ok" else "yellow"
        table.add_row(label, f"[{style}]{status}[/{style}]", detail)
    console.print(table)
    return ready


@app.command()
def setup(
    school: str = typer.Option("", "--school", help="Set institution by school name or partial match."),
    email: str = typer.Option("", "--email", help="Set email for Open Access metadata services."),
    output_dir: str = typer.Option("", "--output-dir", help="Set the default PDF output directory."),
    federated: bool = typer.Option(True, "--federated/--no-federated", help="Enable browser federated institutional login."),
    federated_school: str = typer.Option("", "--federated-school", help="Override the school name shown in publisher login pages."),
    check: bool = typer.Option(False, "--check", help="Check environment without changing configuration."),
):
    """One-step environment setup for institutional paper downloads."""
    cfg = load_config()
    changed = False
    school_entry = None

    has_setter = any([school, email, output_dir, federated_school]) or not federated
    if check and not has_setter:
        if not _show_setup_check(cfg):
            raise typer.Exit(2)
        return

    if school:
        try:
            school_entry = _apply_school_config(cfg, school)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        changed = True

    if email:
        cfg["email"] = email
        changed = True

    if output_dir:
        cfg["output_dir"] = output_dir
        changed = True

    if federated and (school or federated_school or cfg.get("instsci_school", "")):
        cfg["carsi_enabled"] = True
        if federated_school:
            cfg["carsi_idp_name"] = federated_school
        elif school_entry is not None:
            cfg["carsi_idp_name"] = school_entry.name
        elif cfg.get("instsci_school", "") and not cfg.get("carsi_idp_name", ""):
            cfg["carsi_idp_name"] = cfg.get("instsci_school", "")
        changed = True
    elif not federated:
        cfg["carsi_enabled"] = False
        changed = True

    for d in [cfg.get("output_dir", ""), cfg.get("cache_dir", ""), cfg.get("carsi_cookie_dir", "")]:
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)
    if changed:
        save_config(cfg)

    ready = bool(cfg.get("instsci_school", "") and _access_url(cfg) and ((not cfg.get("carsi_enabled", False)) or cfg.get("carsi_idp_name", "")))
    if ready:
        console.print("[green]Environment ready.[/green]")
    else:
        console.print("[yellow]Environment prepared, but institution access is incomplete.[/yellow]")
    if school_entry is not None:
        type_label = _school_type_label(school_entry.school_type)
        console.print(f"  School:       {school_entry.name} ({type_label})")
        console.print(f"  Access URL:   {_access_url(cfg)}")
        if school_entry.school_type in {"easyconnect", "atrust"}:
            console.print("[yellow]This school needs a local campus connector before downloading.[/yellow]")
            console.print("  Set it with: [cyan]instsci config-cmd --connector-url socks5://127.0.0.1:1080[/cyan]")
    _output_dir = cfg.get("output_dir", "")
    _browser_dir = cfg.get("chrome_profile_dir", "")
    _sessions_dir = cfg.get("carsi_cookie_dir", "")
    console.print(f"  Output dir:   {_output_dir}")
    console.print(f"  Browser dir:  {_browser_dir}")
    console.print(f"  Sessions dir: {_sessions_dir}")
    console.print("[dim]Next: instsci papers dois.txt --publisher auto[/dim]")
    console.print("[dim]If SSO, 2FA, or CAPTCHA appears, complete it once in the opened browser window.[/dim]")

    if (check or not ready) and not _show_setup_check(cfg):
        raise typer.Exit(2)


@app.command()
def login(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login even if session is valid."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Initialize or refresh institutional access session."""
    _setup_logging(verbose)
    config = load_config()
    fetcher = PaperFetcher(config)

    console.print("[bold]Checking institutional access session...[/bold]")
    if fetcher.auth.login(force=force):
        console.print("[green]Institutional access session is active.[/green]")
    else:
        console.print("[red]Failed to authenticate institutional access.[/red]")
        raise typer.Exit(1)


@app.command()
def fetch(
    identifier: str = typer.Argument(help="DOI or URL of the paper to fetch."),
    output: str = typer.Option("", "--output", "-o", help="Output directory for PDFs."),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, markdown, text."),
    text_only: bool = typer.Option(False, "--text-only", "-t", help="Output only plain text (minimal tokens)."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Fetch a single paper by DOI or URL."""
    _setup_logging(verbose)
    config = load_config()
    _ensure_email(config)
    if output:
        config["output_dir"] = output

    fetcher = PaperFetcher(config)
    try:
        console.print(f"[bold]Fetching:[/bold] {identifier}")
        result = fetcher.fetch_with_result(identifier, use_cache=not no_cache)
        paper = result.paper

        if result.status != "success":
            console.print(f"[yellow]Status: {result.status} ({result.reason or result.quality})[/yellow]")
            if result.next_action:
                console.print(f"[yellow]Next: {result.next_action.message}[/yellow]")
                if result.next_action.command:
                    console.print(f"[dim]{result.next_action.command}[/dim]")

        if text_only:
            console.print(result.to_text())
        elif format == "markdown":
            console.print(result.to_markdown())
        elif format == "text":
            console.print(result.to_text())
        else:
            console.print(result.to_json())

        if paper.pdf_path:
            console.print(f"\n[dim]PDF saved to: {paper.pdf_path}[/dim]")
        console.print(f"[dim]Source: {paper.source}[/dim]")

    finally:
        fetcher.close()


@app.command()
def batch(
    file: Path = typer.Argument(help="File containing DOIs (one per line)."),
    output: str = typer.Option("", "--output", "-o", help="Output directory."),
    format: str = typer.Option("json", "--format", "-f", help="Output format: json, markdown, text."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Fetch multiple papers from a file of DOIs."""
    _setup_logging(verbose)

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    dois = _read_dois(file)

    if not dois:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)

    console.print(f"[bold]Found {len(dois)} DOIs to fetch.[/bold]")

    config = load_config()
    if output:
        config["output_dir"] = output

    fetcher = PaperFetcher(config)
    results_dir = Path(config.get("output_dir", ""))
    results_dir.mkdir(parents=True, exist_ok=True)

    succeeded = 0
    failed = 0

    try:
        for i, doi in enumerate(dois, 1):
            console.print(f"\n[bold][{i}/{len(dois)}][/bold] Fetching: {doi}")
            try:
                paper = fetcher.fetch(doi)
                if paper.full_text:
                    succeeded += 1
                    # Save result
                    safe_name = doi.replace("/", "_").replace(":", "_")
                    if format == "markdown":
                        out_file = results_dir / f"{safe_name}.md"
                        out_file.write_text(paper.to_markdown(), encoding="utf-8")
                    elif format == "text":
                        out_file = results_dir / f"{safe_name}.txt"
                        out_file.write_text(paper.to_text(), encoding="utf-8")
                    else:
                        out_file = results_dir / f"{safe_name}.json"
                        out_file.write_text(paper.to_json(), encoding="utf-8")
                    console.print(f"  [green]OK[/green] → {out_file.name}")
                else:
                    failed += 1
                    console.print("  [yellow]No full text extracted[/yellow]")
            except Exception as e:
                failed += 1
                console.print(f"  [red]Error: {e}[/red]")

        console.print(f"\n[bold]Done:[/bold] {succeeded} succeeded, {failed} failed out of {len(dois)}.")

    finally:
        fetcher.close()


@app.command("est-batch")
def est_batch(
    year: int = typer.Option(2026, "--year", help="Publication year."),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of EST articles."),
    output: str = typer.Option("", "--output", "-o", help="Run output directory."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    institution: str = typer.Option("", "--institution", help="Subscription institution search text. Omit to use configured institution or prompt."),
    login_timeout: int = typer.Option(900, "--login-timeout", help="Seconds to wait for manual SSO/2FA completion."),
    pdf_timeout: int = typer.Option(60, "--pdf-timeout", help="Seconds to wait for each candidate PDF navigation."),
    post_login_hold: int = typer.Option(0, "--post-login-hold", help="Seconds to keep the authorized article page open before PDF capture."),
    post_run_hold: int = typer.Option(0, "--post-run-hold", help="Seconds to keep the browser page open after capture or failure."),
    target_verified: int = typer.Option(0, "--target-verified", help="Stop after this many verified PDFs. Zero disables early stop."),
    attempt_cache: str = typer.Option("", "--attempt-cache", help="JSONL attempt cache path. Defaults to attempts.jsonl in the run directory."),
    skip_attempted: bool = typer.Option(False, "--skip-attempted", help="Skip DOIs already present in the attempt cache."),
):
    """Download recent Environmental Science & Technology articles through ACS/CloakBrowser."""
    from .acs_batch import ACSCloakBatchDownloader, fetch_est_records

    cfg = load_config()
    institution = _resolve_subscription_institution(cfg, institution)
    run_dir = Path(output) if output else Path("downloads") / f"est_{year}_{limit}" / f"acs_cloak_{datetime.now():%Y%m%d_%H%M%S}"
    console.print(f"[bold]Fetching EST metadata:[/bold] year={year}, limit={limit}")
    records = fetch_est_records(year=year, limit=limit, email=cfg.get("email", ""))
    if not records:
        console.print("[red]No EST records found.[/red]")
        raise typer.Exit(1)

    console.print(f"[green]Found {len(records)} DOI records.[/green]")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    console.print("[dim]If a CloakBrowser window stops on SSO or 2FA, complete it there and leave the window open.[/dim]")

    downloader = ACSCloakBatchDownloader(
        cfg,
        institution_query=institution,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
        post_login_hold_sec=post_login_hold,
        post_run_hold_sec=post_run_hold,
    )
    summary = downloader.run_records(
        records,
        run_dir,
        retry_failed=retry_failed,
        target_verified=target_verified or None,
        attempt_cache=attempt_cache or None,
        skip_attempted=skip_attempted,
    )
    _print_batch_summary(summary, extra_lines=[
        f"[dim]Attempt cache: {summary['attempt_cache']}[/dim]",
    ])
    if summary["missing"] or summary.get("unverified", 0):
        console.print("[yellow]Some items failed or were unverified; see the run manifest and diagnostics folders.[/yellow]")
        raise typer.Exit(2)


@app.command("publisher-batch")
def publisher_batch(
    file: Path = typer.Argument(help="File containing DOI values (one per line)."),
    publisher: str = typer.Option("acs", "--publisher", "-p", help="Publisher profile key, e.g. acs, elsevier, wiley, or ieee."),
    output: str = typer.Option("", "--output", "-o", help="Run output directory."),
    browser_profile: str = typer.Option("", "--browser-profile", help="Override the persistent CloakBrowser profile directory."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    institution: str = typer.Option("", "--institution", help="Subscription institution search text. Omit to use configured institution or prompt."),
    login_timeout: int = typer.Option(900, "--login-timeout", help="Seconds to wait for manual SSO/2FA completion."),
    pdf_timeout: int = typer.Option(60, "--pdf-timeout", help="Seconds to wait for each candidate PDF navigation."),
    target_verified: int = typer.Option(0, "--target-verified", help="Stop after this many verified PDFs. Zero disables early stop."),
    attempt_cache: str = typer.Option("", "--attempt-cache", help="JSONL attempt cache path. Defaults to attempts.jsonl in the run directory."),
    skip_attempted: bool = typer.Option(False, "--skip-attempted", help="Skip DOIs already present in the attempt cache."),
):
    """Download a DOI list through a named publisher profile and CloakBrowser."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    dois = _read_dois(file)
    if not dois:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)
    records = [PaperRecord(doi=doi) for doi in dois]

    try:
        profile = get_publisher_profile(publisher)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    cfg = load_config()
    if browser_profile:
        cfg["chrome_profile_dir"] = browser_profile
    institution = _resolve_subscription_institution(cfg, institution)
    profile_key = publisher.strip().lower().replace(" ", "-")
    run_dir = Path(output) if output else Path("downloads") / f"{profile_key}_{len(records)}" / f"cloak_{datetime.now():%Y%m%d_%H%M%S}"
    console.print(f"[bold]Publisher profile:[/bold] {profile.name}")
    console.print(f"[bold]Found {len(records)} DOI records.[/bold]")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    _bp = cfg.get("chrome_profile_dir", "")
    console.print(f"[bold]Browser profile:[/bold] {_bp}")
    console.print("[dim]If a CloakBrowser window stops on SSO or 2FA, complete it there and leave the window open.[/dim]")

    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
    )
    summary = downloader.run_records(
        records,
        run_dir,
        retry_failed=retry_failed,
        target_verified=target_verified or None,
        attempt_cache=attempt_cache or None,
        skip_attempted=skip_attempted,
    )
    _print_batch_summary(summary, extra_lines=[
        f"[dim]Attempt cache: {summary['attempt_cache']}[/dim]",
    ])
    if summary["missing"] or summary.get("unverified", 0):
        console.print("[yellow]Some items failed or were unverified; see the run manifest and diagnostics folders.[/yellow]")
        raise typer.Exit(2)


@app.command("papers")
def papers(
    file: Path = typer.Argument(help="File containing DOI values (one per line)."),
    publisher: str = typer.Option("auto", "--publisher", "-p", help="Publisher profile, or 'auto' to infer from DOI prefixes."),
    output: str = typer.Option("", "--output", "-o", help="Run output directory."),
    browser_profile: str = typer.Option("", "--browser-profile", help="Override the persistent CloakBrowser profile directory."),
    institution: str = typer.Option("", "--institution", help="Subscription institution search text. Omit to use configured institution or prompt."),
    login_timeout: int = typer.Option(900, "--login-timeout", help="Seconds to wait for manual SSO/CAPTCHA completion."),
    pdf_timeout: int = typer.Option(90, "--pdf-timeout", help="Seconds to wait for each PDF navigation."),
    post_login_hold: int = typer.Option(0, "--post-login-hold", help="Seconds to keep the authorized article page open before PDF capture."),
    post_run_hold: int = typer.Option(0, "--post-run-hold", help="Seconds to keep the browser page open after capture or failure."),
    retry_failed: bool = typer.Option(True, "--retry/--no-retry", help="Retry transient failures in a fresh browser context."),
    concurrency: int = typer.Option(0, "--concurrency", "-j", min=0, max=4, help="Parallel browser workers (0 = use config max_browser_workers, default 2)."),
    broker: bool = typer.Option(True, "--broker/--no-broker", help="Use the long-lived publisher session broker by default."),
    broker_ttl: int = typer.Option(86400, "--broker-ttl", help="Seconds to keep an auto-started broker alive."),
):
    """Recommended browser workflow for closed-access publisher PDFs."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile, infer_publisher_profile, list_publisher_profiles

    if not file.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    dois = _read_dois(file)
    if not dois:
        console.print("[yellow]No DOIs found in file.[/yellow]")
        raise typer.Exit(0)
    records = [PaperRecord(doi=doi) for doi in dois]

    if publisher.strip().lower() == "auto":
        inferred = [infer_publisher_profile(record.doi) for record in records]
        profiles = {profile for profile in inferred if profile is not None}
        if len(profiles) != 1 or len(profiles) != len(set(inferred)):
            console.print("[red]Could not infer one publisher for all DOIs.[/red]")
            console.print(f"[yellow]Use --publisher with one of: {', '.join(list_publisher_profiles())}.[/yellow]")
            raise typer.Exit(1)
        profile = profiles.pop()
    else:
        try:
            profile = get_publisher_profile(publisher)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc

    cfg = load_config()
    if browser_profile:
        cfg["chrome_profile_dir"] = browser_profile
    institution = _resolve_subscription_institution(cfg, institution)
    profile_key = profile.name.lower().replace(" ", "-")
    run_dir = Path(output) if output else Path("downloads") / f"papers_{profile_key}_{len(records)}" / f"browser_{datetime.now():%Y%m%d_%H%M%S}"

    console.print(f"[bold]Recommended route:[/bold] browser-based publisher workflow ({profile.name})")
    console.print("[dim]Complete SSO, 2FA, or CAPTCHA in the opened browser window; InstSci continues automatically.[/dim]")
    console.print(f"[bold]Found {len(records)} DOI records.[/bold]")
    console.print(f"[bold]Output:[/bold] {run_dir}")
    _bp = cfg.get("chrome_profile_dir", "")
    console.print(f"[bold]Browser profile:[/bold] {_bp}")

    profile_key_arg = publisher.strip().lower().replace(" ", "-")
    broker_publisher = profile_key_arg if profile_key_arg != "auto" else profile.name.lower().replace(" ", "-")
    if broker:
        from . import session_broker

        if not session_broker.broker_is_running(broker_publisher):
            console.print(f"[dim]Starting publisher session broker: {broker_publisher}[/dim]")
            session_broker.start_broker_process(
                publisher=broker_publisher,
                browser_profile=cfg.get("chrome_profile_dir", ""),
                institution=institution,
                ttl_seconds=broker_ttl,
                cwd=Path.cwd(),
            )
            deadline = time.time() + 30
            while time.time() < deadline and not session_broker.broker_is_running(broker_publisher):
                time.sleep(1)
        if session_broker.broker_is_running(broker_publisher):
            console.print(f"[bold]Session broker:[/bold] running ({broker_publisher})")
            timeout_seconds = max(
                120,
                login_timeout + len(records) * (pdf_timeout + post_login_hold + post_run_hold + 60),
            )
            summary = session_broker.submit_broker_job(
                publisher=broker_publisher,
                records=[{"doi": record.doi, "title": record.title, "published": record.published, "url": record.url} for record in records],
                output_dir=str(run_dir),
                institution=institution,
                login_timeout=login_timeout,
                pdf_timeout=pdf_timeout,
                post_login_hold=post_login_hold,
                post_run_hold=post_run_hold,
                timeout_seconds=timeout_seconds,
            )
            _print_batch_summary(summary)
            if summary["missing"] or summary.get("unverified", 0):
                console.print("[yellow]Some items need manual CAPTCHA/login attention; rerun the same command after completing it.[/yellow]")
                raise typer.Exit(2)
            return
        console.print("[yellow]Session broker did not start; falling back to one-shot browser workflow.[/yellow]")

    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=pdf_timeout,
        post_login_hold_sec=post_login_hold,
        post_run_hold_sec=post_run_hold,
    )
    effective_concurrency = concurrency or cfg.get("max_browser_workers", 2)
    summary = downloader.run_records(
        records,
        run_dir,
        retry_failed=retry_failed,
        concurrency=effective_concurrency,
    )
    _print_batch_summary(summary)
    if summary["missing"] or summary.get("unverified", 0):
        console.print("[yellow]Some items need manual CAPTCHA/login attention; rerun the same command after completing it.[/yellow]")
        raise typer.Exit(2)


@app.command("session-broker-status")
def session_broker_status(
    publisher: str = typer.Option("elsevier", "--publisher", "-p", help="Publisher broker key."),
):
    """Show a long-lived publisher browser session broker."""
    from . import session_broker

    state = session_broker.load_broker_state(publisher)
    running = session_broker.broker_is_running(publisher)
    table = Table(title="InstSci Session Broker")
    table.add_column("Publisher")
    table.add_column("Status")
    table.add_column("PID")
    table.add_column("Profile", overflow="fold")
    table.add_column("Queue", overflow="fold")
    table.add_row(
        publisher,
        "running" if running else "stopped",
        str(state.get("pid", "")) if state else "",
        str(state.get("profile_dir", "")) if state else "",
        str(state.get("queue_dir", "")) if state else "",
    )
    console.print(table)


@app.command("session-broker-stop")
def session_broker_stop(
    publisher: str = typer.Option("elsevier", "--publisher", "-p", help="Publisher broker key."),
):
    """Ask a long-lived publisher broker to stop."""
    from . import session_broker

    session_broker.broker_stop_path(publisher).parent.mkdir(parents=True, exist_ok=True)
    session_broker.broker_stop_path(publisher).write_text("stop", encoding="utf-8")
    console.print(f"[green]Stop requested for broker:[/green] {publisher}")


@app.command("session-broker-run", hidden=True)
def session_broker_run(
    publisher: str = typer.Option(..., "--publisher", "-p"),
    browser_profile: str = typer.Option("", "--browser-profile"),
    institution: str = typer.Option("", "--institution"),
    ttl: int = typer.Option(86400, "--ttl"),
):
    """Run the long-lived broker loop. Internal command."""
    from .publisher_batch import PaperRecord, PublisherBatchDownloader
    from .publisher_profiles import get_publisher_profile
    from .session_broker import BrokerState, broker_dir, broker_stop_path, write_broker_state

    cfg = load_config()
    if browser_profile:
        cfg["chrome_profile_dir"] = browser_profile
    institution = _resolve_subscription_institution(cfg, institution, prompt=False)
    profile = get_publisher_profile(publisher)
    root = broker_dir(publisher)
    queue_dir = root / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    state = BrokerState(
        publisher=publisher,
        profile_dir=cfg.get("chrome_profile_dir", ""),
        pid=os.getpid(),
        queue_dir=str(queue_dir),
        started_at=datetime.now().isoformat(timespec="seconds"),
        ttl_seconds=ttl,
        heartbeat_at=datetime.now().isoformat(timespec="seconds"),
    )
    write_broker_state(state)
    downloader = PublisherBatchDownloader(
        cfg,
        profile=profile,
        institution_query=institution,
        login_timeout_sec=900,
        pdf_timeout_sec=90,
    )
    context = downloader._launch_context()
    deadline = time.time() + max(1, ttl)
    try:
        while time.time() < deadline and not broker_stop_path(publisher).exists():
            state.heartbeat_at = datetime.now().isoformat(timespec="seconds")
            write_broker_state(state)
            jobs = sorted(queue_dir.glob("*.json"))
            for job_path in jobs:
                if job_path.name.endswith(".done.json"):
                    continue
                try:
                    job = json.loads(job_path.read_text(encoding="utf-8"))
                    run_dir = Path(str(job["output_dir"]))
                    primary_dir = run_dir / "primary"
                    primary_dir.mkdir(parents=True, exist_ok=True)
                    job_downloader = PublisherBatchDownloader(
                        cfg,
                        profile=profile,
                        institution_query=str(job.get("institution") or institution),
                        login_timeout_sec=int(job.get("login_timeout") or 900),
                        pdf_timeout_sec=int(job.get("pdf_timeout") or 90),
                        post_login_hold_sec=int(job.get("post_login_hold") or 0),
                        post_run_hold_sec=int(job.get("post_run_hold") or 0),
                    )
                    records = [PaperRecord(**record) for record in job.get("records", [])]
                    results = []
                    for record in records:
                        results.append(job_downloader.fetch_one(context, record, primary_dir))
                        job_downloader._write_results(primary_dir / "summary_partial.json", results)
                    job_downloader._write_results(primary_dir / "summary.json", results)
                    summary = job_downloader._write_complete_artifacts(records, results, run_dir)
                    summary["publisher"] = profile.name
                    summary["broker"] = True
                    summary["browser_profile_dir"] = cfg.get("chrome_profile_dir", "")
                    (run_dir / "summary.json").write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    (queue_dir / f"{job['id']}.done.json").write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception as exc:
                    payload = {"count": 0, "success": 0, "missing": 1, "unverified": 0, "error": f"{type(exc).__name__}: {exc}"}
                    done_name = f"{job_path.stem}.done.json"
                    (queue_dir / done_name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                finally:
                    job_path.unlink(missing_ok=True)
            time.sleep(2)
    finally:
        try:
            context.close()
        except Exception:
            pass


@app.command("session-doctor")
def session_doctor(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher profile key to include publisher domains."),
    browser_profile: str = typer.Option("", "--browser-profile", help="Inspect one browser profile instead of known candidates."),
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
):
    """Inspect local browser profiles for institution/publisher session presence."""
    from .profile_health import DEFAULT_SESSION_DOMAINS, candidate_profile_dirs, inspect_browser_profile
    from .publisher_profiles import get_publisher_profile

    cfg = load_config()
    profile = None
    domains = list(DEFAULT_SESSION_DOMAINS)
    if publisher:
        try:
            profile = get_publisher_profile(publisher)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from exc
        domains.extend(profile.base_domains)
    domains = list(dict.fromkeys(domain for domain in domains if domain))

    profiles = [Path(browser_profile)] if browser_profile else candidate_profile_dirs(cfg, workspace=Path.cwd())
    reports = [inspect_browser_profile(path, domains) for path in profiles]

    table = Table(title="InstSci Browser Session Doctor")
    table.add_column("Profile", overflow="fold")
    table.add_column("Exists", width=8)
    table.add_column("Session Hosts", overflow="fold")
    table.add_column("Latest Expiry", overflow="fold")
    table.add_column("Notes", overflow="fold")
    for report in reports:
        present = []
        expiries = []
        seen_hosts: set[str] = set()
        for domain, info in report["domains"].items():
            latest = str(info.get("latest_expires_at") or "")
            if latest:
                expiries.append(f"{domain}: {latest}")
            for host in info.get("hosts", []):
                host_name = str(host.get("host") or "")
                if host_name in seen_hosts:
                    continue
                seen_hosts.add(host_name)
                count = int(host.get("cookie_count") or 0)
                if count:
                    session_count = int(host.get("session_cookie_count") or 0)
                    suffix = f", session={session_count}" if session_count else ""
                    present.append(f"{host_name}({count}{suffix})")
        notes = report.get("error") or ("cookie DB missing" if report["exists"] and not report["cookies_db_exists"] else "")
        table.add_row(
            report["profile_dir"],
            "yes" if report["exists"] else "no",
            ", ".join(present) or "-",
            ", ".join(expiries) or "-",
            notes,
        )
    console.print(table)

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "publisher": profile.name if profile else "",
            "domains": domains,
            "reports": reports,
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Report: {output_path}[/dim]")


@app.command("publisher-doctor")
def publisher_doctor(
    publisher: str = typer.Option("all", "--publisher", "-p", help="Publisher profile key, or 'all'."),
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
    probe_pdf: bool = typer.Option(True, "--probe-pdf/--no-probe-pdf", help="Probe PDF candidate URLs without saving files."),
    max_candidates: int = typer.Option(4, "--max-candidates", min=0, max=10, help="Maximum PDF candidates to probe per publisher."),
    timeout: int = typer.Option(20, "--timeout", min=3, max=120, help="Network timeout in seconds."),
):
    """HTTP preflight to verify reusable publisher PDF routes.

    Browser-backed InstSci workflows are authoritative for publisher PDF
    capability verdicts; this command only checks route templates and blockers.
    """
    from .publisher_access import verify_publishers
    from .publisher_profiles import list_publisher_profiles

    keys = list_publisher_profiles() if publisher.strip().lower() == "all" else [publisher.strip()]
    console.print(f"[bold]Verifying publisher access assets:[/bold] {', '.join(keys)}")
    console.print(
        "[yellow]HTTP preflight only:[/yellow] use the built-in browser workflow "
        "for final publisher PDF capability verdicts."
    )
    results = verify_publishers(
        keys,
        probe_pdf=probe_pdf,
        max_candidates=max_candidates,
        timeout=timeout,
    )

    table = Table(title="Publisher Access Verification")
    table.add_column("Publisher", width=18)
    table.add_column("Landing", width=8)
    table.add_column("PDF Links", width=9, justify="right")
    table.add_column("Observed", width=22)
    table.add_column("Final Host", overflow="fold")
    needs_attention = False
    for result in results:
        if result["landing_status"] == 404 or not result["pdf_candidates"]:
            needs_attention = True
        table.add_row(
            result["profile_key"],
            str(result["landing_status"]),
            str(len(result["pdf_candidates"])),
            result["observed_access"],
            urlparse(result["landing_url"]).hostname or result["landing_url"],
        )
    console.print(table)

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Report: {output_path}[/dim]")

    if needs_attention:
        raise typer.Exit(2)


@app.command("identity-policy")
def identity_policy(
    output: str = typer.Option("", "--output", "-o", help="Optional JSON report path."),
):
    """Show the institutional identity routing policy for publisher PDFs."""
    from .publisher_access import load_institutional_identity_policy

    policy = load_institutional_identity_policy()
    console.print("[bold]InstSci Institutional Identity Policy[/bold]")
    console.print(f"Default mode: [cyan]{policy['default_mode']}[/cyan]")
    console.print(f"Default identity: [cyan]{policy['default_identity']}[/cyan]")
    required = "required" if policy["subscription_institution"]["required_for_closed_access"] else "optional"
    console.print(f"Subscription institution: [cyan]{required}[/cyan]")
    console.print(f"Preferred off-campus access: [cyan]{policy['preferred_off_campus_access']}[/cyan]")
    console.print(f"Final PDF verdict requires: [cyan]{policy['final_pdf_verdict_requires']}[/cyan]")

    table = Table(title="Identity Route Order")
    table.add_column("Order", width=5, justify="right")
    table.add_column("Identity", width=22)
    table.add_column("Role", overflow="fold")
    table.add_column("Global default", width=14)
    for index, identity_key in enumerate(policy["identity_order"], 1):
        section_key = "webvpn" if identity_key == "webvpn_broker" else identity_key
        identity = policy["identities"].get(section_key, {})
        table.add_row(
            str(index),
            identity_key,
            str(identity.get("recommended_role", "")).replace("_", " "),
            "yes" if identity.get("global_default") else "no",
        )
    console.print(table)

    webvpn = policy["identities"]["webvpn"]
    console.print(
        "[yellow]WebVPN is optional:[/yellow] "
        f"{webvpn['persistence_limits']['cookie_store']['notes']}"
    )
    console.print(
        "[yellow]Use visible CloakBrowser:[/yellow] "
        "keep the same live context for SSO, CAPTCHA, Cloudflare, and PDF-token flows."
    )

    if output:
        output_path = Path(output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Report: {output_path}[/dim]")


@app.command()
def search(
    query: str = typer.Argument(help="Search query."),
    limit: int = typer.Option(10, "--limit", "-n", help="Maximum results."),
    year: str = typer.Option("", "--year", "-y", help="Year range, e.g., '2020-2024' or '2020-'."),
    do_fetch: bool = typer.Option(False, "--fetch", help="Also fetch full text for results with DOIs."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Search for papers via Semantic Scholar."""
    _setup_logging(verbose)

    console.print(f"[bold]Searching:[/bold] {query}")
    results = semantic_scholar.search(query, limit=limit, year_range=year or None)

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        raise typer.Exit(0)

    # Display results in a table
    table = Table(title=f"Search Results ({len(results)})")
    table.add_column("#", style="dim", width=3)
    table.add_column("Year", width=5)
    table.add_column("Title", max_width=60)
    table.add_column("Authors", max_width=30)
    table.add_column("DOI", max_width=25)
    table.add_column("Cites", width=5, justify="right")

    for i, r in enumerate(results, 1):
        authors_str = ", ".join(r.authors[:3])
        if len(r.authors) > 3:
            authors_str += " et al."
        table.add_row(
            str(i),
            str(r.year or ""),
            r.title[:60],
            authors_str[:30],
            r.doi[:25] if r.doi else r.arxiv_id[:25] if r.arxiv_id else "",
            str(r.citation_count),
        )

    console.print(table)

    # Optionally fetch full texts
    if do_fetch:
        fetchable = [r for r in results if r.doi or r.arxiv_id]
        if fetchable:
            console.print(f"\n[bold]Fetching {len(fetchable)} papers...[/bold]")
            config = load_config()
            fetcher = PaperFetcher(config)
            try:
                for r in fetchable:
                    identifier = r.doi or f"arxiv:{r.arxiv_id}"
                    console.print(f"  Fetching: {identifier}")
                    try:
                        paper = fetcher.fetch(identifier)
                        status = "[green]OK[/green]" if paper.full_text else "[yellow]No text[/yellow]"
                        console.print(f"    {status}")
                    except Exception as e:
                        console.print(f"    [red]Error: {e}[/red]")
            finally:
                fetcher.close()


@app.command()
def cache(
    action: str = typer.Argument(help="Action: 'clear' to remove cached results."),
):
    """Manage the paper cache."""
    if action == "clear":
        config = load_config()
        fetcher = PaperFetcher(config)
        fetcher.clear_cache()
        console.print("[green]Cache cleared.[/green]")
    else:
        console.print(f"[red]Unknown action: {action}. Use 'clear'.[/red]")
        raise typer.Exit(1)


@app.command()
def schools(
    query: str = typer.Argument("", help="Search query (name, province, or host). Omit to list all."),
):
    """List or search supported universities."""
    if query:
        results = search_schools(query)
    else:
        results = list_schools()

    if not results:
        console.print(f"[yellow]No schools found matching '{query}'.[/yellow]")
        raise typer.Exit(0)

    table = Table(title=f"Supported Schools ({len(results)})")
    table.add_column("#", style="dim", width=4)
    table.add_column("Province", width=10)
    table.add_column("School", max_width=25)
    table.add_column("Type", width=12)
    table.add_column("Host", max_width=40)
    table.add_column("Custom Key", width=5, justify="center")

    from .schools import WEBVPN_DEFAULT_KEY
    for i, s in enumerate(results, 1):
        has_custom = "Y" if s.key != WEBVPN_DEFAULT_KEY else ""
        table.add_row(str(i), s.province, s.name, _school_type_label(s.school_type), s.host, has_custom)

    console.print(table)


@app.command()
def config_cmd(
    show: bool = typer.Option(True, "--show", help="Show current config."),
    set_email: str = typer.Option("", "--email", help="Set email for Unpaywall API."),
    set_output: str = typer.Option("", "--output-dir", help="Set default output directory."),
    set_access_url: str = typer.Option("", "--access-url", help="Set institutional access gateway URL."),
    set_webvpn_url: str = typer.Option("", "--webvpn-url", help="Legacy gateway URL option.", hidden=True),
    set_school: str = typer.Option("", "--school", help="Set school (use 'instsci schools' to list)."),
    set_connector_url: str = typer.Option("", "--connector-url", help="Set local SOCKS5 connector URL for EasyConnect."),
    set_proxy_url: str = typer.Option("", "--proxy-url", help="Legacy local connector URL option.", hidden=True),
    set_elsevier_key: str = typer.Option("", "--elsevier-api-key", help="Set Elsevier API key."),
    set_elsevier_token: str = typer.Option("", "--elsevier-inst-token", help="Set Elsevier institutional token."),
    set_flaresolverr: str = typer.Option("", "--flaresolverr-url", help="Set FlareSolverr URL."),
    set_static_proxy: str = typer.Option("", "--static-proxy", help="Set browser static proxy (e.g. socks5://1.2.3.4:1080)."),
    set_remote_port: int = typer.Option(-1, "--remote-assist-port", help="Set remote assist HTTP port (0=disabled)."),
    set_max_workers: int = typer.Option(-1, "--max-browser-workers", help="Set max parallel browser workers."),
    set_federated_enable: bool = typer.Option(False, "--federated-enable", help="Enable federated institutional auth."),
    set_federated_disable: bool = typer.Option(False, "--federated-disable", help="Disable federated institutional auth."),
    set_federated_school: str = typer.Option("", "--federated-school", help="Set school name for federated login."),
    set_carsi_enable: bool = typer.Option(False, "--carsi-enable", help="Legacy federated auth option.", hidden=True),
    set_carsi_disable: bool = typer.Option(False, "--carsi-disable", help="Legacy federated auth option.", hidden=True),
    set_carsi_school: str = typer.Option("", "--carsi-school", help="Legacy federated school option.", hidden=True),
):
    """View or update configuration."""
    cfg = load_config()
    changed = False

    if set_email:
        cfg["email"] = set_email
        changed = True
        console.print(f"[green]Email set to: {set_email}[/green]")

    if set_output:
        cfg["output_dir"] = set_output
        changed = True
        console.print(f"[green]Output dir set to: {set_output}[/green]")

    access_url = set_access_url or set_webvpn_url
    if access_url:
        cfg["instsci_base_url"] = access_url.rstrip("/")
        changed = True
        console.print(f"[green]Institutional access URL set to: {access_url}[/green]")

    if set_school:
        try:
            entry = _apply_school_config(cfg, set_school)
            changed = True
            type_label = _school_type_label(entry.school_type)
            console.print(f"[green]School set to: {entry.name} ({type_label}, {entry.host})[/green]")
            if entry.school_type == "easyconnect":
                console.print("[yellow]This school uses a local campus connector. Please:[/yellow]")
                console.print("  1. Connect via zju-connect: [cyan]zju-connect -server {0}[/cyan]".format(entry.host))
                console.print("  2. Set connector: [cyan]instsci config-cmd --connector-url socks5://127.0.0.1:1080[/cyan]")
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)

    connector_url = set_connector_url or set_proxy_url
    if connector_url:
        cfg["network_proxy"] = connector_url
        changed = True
        console.print(f"[green]Connector URL set to: {connector_url}[/green]")

    if set_elsevier_key:
        cfg["elsevier_api_key"] = set_elsevier_key
        changed = True
        console.print("[green]Elsevier API key saved.[/green]")

    if set_elsevier_token:
        cfg["elsevier_insttoken"] = set_elsevier_token
        changed = True
        console.print("[green]Elsevier institutional token saved.[/green]")

    if set_flaresolverr:
        cfg["flaresolverr_url"] = set_flaresolverr.rstrip("/")
        changed = True
        console.print(f"[green]FlareSolverr URL set to: {set_flaresolverr}[/green]")

    federated_enable = set_federated_enable or set_carsi_enable
    federated_disable = set_federated_disable or set_carsi_disable
    federated_school = set_federated_school or set_carsi_school

    if federated_enable:
        cfg["carsi_enabled"] = True
        changed = True
        console.print("[green]Federated institutional auth enabled.[/green]")

    if federated_disable:
        cfg["carsi_enabled"] = False
        changed = True
        console.print("[yellow]Federated institutional auth disabled.[/yellow]")

    if federated_school:
        cfg["carsi_idp_name"] = federated_school
        changed = True
        console.print(f"[green]Federated login school set to: {federated_school}[/green]")

    if set_static_proxy:
        cfg["browser_static_proxy"] = set_static_proxy
        changed = True
        console.print(f"[green]Browser static proxy set to: {set_static_proxy}[/green]")

    if set_remote_port >= 0:
        cfg["remote_assist_port"] = set_remote_port
        changed = True
        status = f"port {set_remote_port}" if set_remote_port > 0 else "disabled"
        console.print(f"[green]Remote assist: {status}[/green]")

    if set_max_workers >= 1:
        cfg["max_browser_workers"] = set_max_workers
        changed = True
        console.print(f"[green]Max browser workers set to: {set_max_workers}[/green]")

    if changed:
        save_config(cfg)

    has_setter = any([set_email, set_output, set_access_url, set_webvpn_url, set_school,
                      set_connector_url, set_proxy_url,
                       set_elsevier_key, set_elsevier_token, set_flaresolverr,
                       set_federated_enable, set_federated_disable, set_federated_school,
                       set_carsi_enable, set_carsi_disable, set_carsi_school,
                       set_static_proxy, set_remote_port >= 0, set_max_workers >= 1])
    if show and not has_setter:
        # Determine school type
        try:
            from .schools import get_school as _get_school
            school_entry = _get_school(cfg.get("instsci_school", ""))
            school_type = school_entry.school_type
        except ValueError:
            school_type = "unknown"

        _school = cfg.get("instsci_school", "")
        _conn = cfg.get("network_proxy", "") or "(not set)"
        _email = cfg.get("email", "")
        _eak = cfg.get("elsevier_api_key", "")
        _eit = cfg.get("elsevier_insttoken", "")
        _fsu = cfg.get("flaresolverr_url", "")
        _fe = cfg.get("carsi_enabled", False)
        _fs = cfg.get("carsi_idp_name", "") or "(not set)"
        _od = cfg.get("output_dir", "")
        _cd = cfg.get("cache_dir", "")
        _cp = cfg.get("cookie_path", "")
        console.print("[bold]Current configuration:[/bold]")
        console.print(f"  School:            {_school} ({school_type})")
        console.print(f"  Access URL:        {_access_url(cfg)}")
        console.print(f"  Connector URL:     {_conn}")
        console.print(f"  Email:             {_email}")
        console.print(f"  Elsevier API key:  {'****' if _eak else '(not set)'}")
        console.print(f"  Elsevier inst tok: {'****' if _eit else '(not set)'}")
        console.print(f"  FlareSolverr URL:  {_fsu}")
        console.print(f"  Federated login:   {'Yes' if _fe else 'No'}")
        console.print(f"  Federated school:  {_fs}")
        console.print(f"  Output dir:        {_od}")
        console.print(f"  Cache dir:         {_cd}")
        console.print(f"  Cookie path:       {_cp}")
        _sp = cfg.get("browser_static_proxy", "")
        _rp = cfg.get("remote_assist_port", 0)
        _mw = cfg.get("max_browser_workers", 2)
        console.print(f"  Static proxy:      {_sp or '(not set)'}")
        console.print(f"  Remote assist:     {f'port {_rp}' if _rp else 'disabled'}")
        console.print(f"  Browser workers:   {_mw}")


def _run_federated_login(
    publisher: str,
    url: str,
    force: bool,
    verbose: bool,
) -> None:
    """Run the federated institutional login flow."""
    _setup_logging(verbose)
    config = load_config()

    if not config.get("carsi_enabled", ""):
        console.print("[red]Federated login is not enabled. Run: instsci config-cmd --federated-enable --federated-school \"你的学校名\"[/red]")
        raise typer.Exit(1)

    if not config.get("carsi_idp_name", ""):
        console.print("[red]Federated login school not set. Run: instsci config-cmd --federated-school \"你的学校名\"[/red]")
        raise typer.Exit(1)

    if not publisher and url:
        from .sources.carsi import detect_publisher
        publisher = detect_publisher(url) or ""

    if not publisher:
        console.print("[yellow]Available publishers:[/yellow]")
        console.print("  sciencedirect, springer, wiley, ieee, tandfonline, nature")
        publisher = typer.prompt("Enter publisher name")

    from .sources.carsi import CARSIClient
    carsi = CARSIClient(config)
    try:
        console.print(f"[bold]Federated login for: {publisher}[/bold]")
        console.print(f"[dim]School: {config.get('carsi_idp_name', '')}[/dim]")
        if carsi.login(publisher, force=force):
            console.print("[green]Federated access session established![/green]")
        else:
            console.print("[red]Federated login failed.[/red]")
            raise typer.Exit(1)
    finally:
        carsi.close()


@app.command("federated-login")
def federated_login(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher (sciencedirect, springer, wiley, ieee, tandfonline, nature). Omit to pick from article URL."),
    url: str = typer.Option("", "--url", "-u", help="Article URL to auto-detect publisher."),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Authenticate via federated institutional login."""
    _run_federated_login(publisher, url, force, verbose)


@app.command("carsi-login", hidden=True)
def carsi_login(
    publisher: str = typer.Option("", "--publisher", "-p", help="Publisher (sciencedirect, springer, wiley, ieee, tandfonline, nature). Omit to pick from article URL."),
    url: str = typer.Option("", "--url", "-u", help="Article URL to auto-detect publisher."),
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Legacy alias for federated-login."""
    _run_federated_login(publisher, url, force, verbose)


@app.command()
def elsevier_setup(
    api_key: str = typer.Option("", "--api-key", help="Elsevier API key (32-char hex)."),
    inst_token: str = typer.Option("", "--inst-token", help="Elsevier institutional token."),
    validate: bool = typer.Option(False, "--validate", help="Validate an existing API key."),
):
    """Set up Elsevier API key for ScienceDirect full-text retrieval.

    Get a free key at: https://dev.elsevier.com/
    """
    cfg = load_config()

    if api_key:
        cfg["elsevier_api_key"] = api_key
        save_config(cfg)
        console.print("[green]Elsevier API key saved.[/green]")

    if inst_token:
        cfg["elsevier_insttoken"] = inst_token
        save_config(cfg)
        console.print("[green]Elsevier institutional token saved.[/green]")

    key = cfg.get("elsevier_api_key", "")
    if not key:
        console.print("[yellow]No Elsevier API key configured.[/yellow]")
        console.print()
        console.print("To get a free API key:")
        console.print("  1. Go to [cyan]https://dev.elsevier.com/[/cyan]")
        console.print("  2. Register/sign in, then open My API Key / API Key Settings")
        console.print("  3. Create an API key; choose ScienceDirect / Article Retrieval if asked")
        console.print("  4. Run: [cyan]scansci-pdf elsevier-setup --api-key YOUR_KEY --validate[/cyan]")
        console.print()
        console.print("Closed full text still depends on institutional subscription/IP entitlement.")
        console.print("If your library provides an Elsevier institutional token:")
        console.print("  [cyan]scansci-pdf elsevier-setup --api-key KEY --inst-token TOKEN[/cyan]")
        raise typer.Exit(0)

    if validate:
        console.print("Validating Elsevier API key...")
        import requests
        try:
            resp = requests.get(
                "https://api.elsevier.com/content/serial/title",
                headers={"X-ELS-APIKey": key, "Accept": "application/json"},
                params={"issn": "0043-1354"},  # Water Research
                timeout=15,
            )
            if resp.status_code == 200:
                console.print("[green]API key is valid![/green]")
                data = resp.json()
                titles = data.get("search-results", {}).get("entry", [])
                if titles:
                    console.print(f"  Test query returned: {titles[0].get('dc:title', 'N/A')[:60]}")
            elif resp.status_code == 401:
                console.print("[red]API key is invalid (HTTP 401).[/red]")
            else:
                console.print(f"[yellow]Unexpected response: HTTP {resp.status_code}[/yellow]")
        except Exception as e:
            console.print(f"[red]Validation failed: {e}[/red]")

        # Check Article Retrieval XML access. Closed object-PDF entitlement is tested by normal download.
        console.print()
        console.print("Testing Article Retrieval XML access...")
        try:
            resp = requests.get(
                "https://api.elsevier.com/content/article/doi/10.1016/j.nicl.2021.102600",
                headers={"X-ELS-APIKey": key, "Accept": "application/xml"},
                params={"view": "FULL"},
                timeout=30,
            )
            ct = resp.headers.get("content-type", "")
            if resp.status_code == 200 and "xml" in ct:
                console.print(f"[green]Article Retrieval XML: YES ({len(resp.content)} bytes)[/green]")
                console.print(
                    "  Closed Elsevier PDFs use: view=FULL XML -> attachment-eid -> "
                    "Content Object API /object/eid/{eid}."
                )
            else:
                console.print(f"[yellow]Article Retrieval XML: HTTP {resp.status_code} ({ct[:40]})[/yellow]")
        except Exception as e:
            console.print(f"[red]Article Retrieval XML test failed: {e}[/red]")

    console.print()
    console.print(f"  API Key:        {key[:8]}...{key[-4:]}" if len(key) > 12 else f"  API Key:        {key}")
    _it = cfg.get("elsevier_insttoken", "") or "(not set)"
    console.print(f"  Inst Token:     {_it}")


if __name__ == "__main__":
    app()
