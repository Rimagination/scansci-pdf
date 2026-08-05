"""Core commands: doctor, setup, login."""

import typer
from rich.table import Table

from ._utils import (
    app, console, load_config, save_config,
    _doctor_checks, _apply_school_config, _access_url,
    _school_type_label, _show_setup_check, _setup_logging,
)


def register_commands(app_: typer.Typer) -> None:
    """Register main commands on the Typer app."""
    app_.command("doctor")(doctor)
    app_.command()(setup)
    app_.command()(login)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------
def setup(
    school: str = typer.Option("", "--school", help="Set institution by school name or partial match."),
    email: str = typer.Option("", "--email", help="Set email for Open Access metadata services."),
    output_dir: str = typer.Option("", "--output-dir", help="Set the default PDF output directory."),
    federated: bool = typer.Option(True, "--federated/--no-federated", help="Enable browser federated institutional login."),
    federated_school: str = typer.Option("", "--federated-school", help="Override the school name shown in publisher login pages."),
    check: bool = typer.Option(False, "--check", help="Check environment without changing configuration."),
):
    """One-step environment setup for institutional paper downloads."""
    from pathlib import Path
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


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------
def login(
    force: bool = typer.Option(False, "--force", "-f", help="Force re-login even if session is valid."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose logging."),
):
    """Initialize or refresh institutional access session."""
    from ..fetcher import PaperFetcher

    _setup_logging(verbose)
    config = load_config()
    fetcher = PaperFetcher(config)
    console.print("[bold]Checking institutional access session...[/bold]")
    if fetcher.auth.login(force=force):
        console.print("[green]Institutional access session is active.[/green]")
    else:
        console.print("[red]Failed to authenticate institutional access.[/red]")
        raise typer.Exit(1)
