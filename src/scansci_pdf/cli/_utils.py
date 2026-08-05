"""CLI shared utilities and app singleton."""

import json
import logging
import os
import sys
from pathlib import Path

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

from ..config import load_config, save_config

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
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _print_batch_summary(summary: dict, *, extra_lines: list[str] | None = None) -> None:
    console.print(
        f"[bold]Done:[/bold] {summary['success']}/{summary['count']} verified PDFs, "
        f"{summary.get('unverified', 0)} unverified PDFs."
    )
    console.print(f"[dim]PDF dir: {summary['pdf_dir']}[/dim]")
    console.print(f"[dim]Manifest: {summary['manifest']}[/dim]")
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


CLOAKBROWSER_RECOMMENDED_MIN = "0.4.11"


def _parse_version_tuple(v: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in (v or "").split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _cloakbrowser_version_status() -> tuple[str, str]:
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
    for package in ("scansci-pdf", "pymupdf"):
        version = _installed_package_version(package)
        checks.append((f"package: {package}", "ok" if version else "warning", version or "not installed"))
    cb_status, cb_detail = _cloakbrowser_version_status()
    checks.append(("package: cloakbrowser", cb_status if cb_status != "not_installed" else "warning", cb_detail))
    try:
        from ..cloakbrowser_compat import configure_builtin_cloakbrowser
        cache_dir = configure_builtin_cloakbrowser(create_dir=False)
        status = "ok" if cache_dir.exists() else "warning"
        detail = str(cache_dir) if cache_dir.exists() else f"not downloaded yet: {cache_dir}"
    except Exception as exc:
        status = "warning"
        detail = f"cache check failed: {exc}"
    checks.append(("CloakBrowser cache", status, detail))
    return checks


def _apply_school_config(cfg: dict, school: str):
    from ..schools import get_school
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
    return (cfg.get("carsi_idp_name", "") or cfg.get("instsci_school", "") or "").strip()


def _resolve_subscription_institution(
    cfg: dict,
    institution: str,
    *,
    prompt: bool = True,
) -> str:
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


def _run_federated_login(publisher: str, url: str, force: bool, verbose: bool) -> None:
    _setup_logging(verbose)
    config = load_config()

    if not config.get("carsi_enabled", ""):
        console.print("[red]Federated login is not enabled. Run: instsci config-cmd --federated-enable --federated-school \"你的学校名\"[/red]")
        raise typer.Exit(1)
    if not config.get("carsi_idp_name", ""):
        console.print("[red]Federated login school not set. Run: instsci config-cmd --federated-school \"你的学校名\"[/red]")
        raise typer.Exit(1)
    if not publisher and url:
        from ..sources.carsi import detect_publisher
        publisher = detect_publisher(url) or ""
    if not publisher:
        console.print("[yellow]Available publishers:[/yellow]")
        console.print("  sciencedirect, springer, wiley, ieee, tandfonline, nature")
        publisher = typer.prompt("Enter publisher name")

    from ..sources.carsi import CARSIClient
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
