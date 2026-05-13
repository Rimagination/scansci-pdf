"""CLI entrypoint for ScanSci PDF server."""

from __future__ import annotations

from enum import Enum

import typer

app = typer.Typer(help="ScanSci PDF server")


class ServerMode(str, Enum):
    STDIO = "stdio"
    HTTP = "streamable_http"


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
        from .browser_cookies import extract_via_camofox
        target_url = url or "https://www.sciencedirect.com/"
        result = extract_via_camofox(config, url=target_url)
        if result["success"]:
            print(f"  {result['message']}")
        else:
            print(f"  {result.get('message') or result.get('error', 'Failed')}")
            raise typer.Exit(1)
    elif login_type == "webvpn":
        from .camofox_login import webvpn_login
        success = webvpn_login(config)
        raise typer.Exit(0 if success else 1)
    elif login_type == "ezproxy":
        from .camofox_login import ezproxy_login
        success = ezproxy_login(config)
        raise typer.Exit(0 if success else 1)
    elif login_type == "custom":
        if not url:
            print("  Error: --url is required for login_type=custom")
            raise typer.Exit(1)
        from .camofox_login import open_login_browser
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
    output: str = typer.Option("", help="Output directory"),
    no_bibtex: bool = typer.Option(False, help="Skip BibTeX citation"),
) -> None:
    """Download a paper with zero configuration. Just give a DOI."""
    from .sources import download
    result = download(
        identifier, output or None,
        scihub_enabled=True, use_tor=True, use_vpnsci=True,
        bibtex=not no_bibtex, strategy="fastest",
    )
    if result.get("success"):
        print(f"  OK: {result.get('file', '')}")
        print(f"  Source: {result.get('source', '?')}")
    else:
        print(f"  FAILED: {result.get('error', 'unknown')}")
        print(f"  Hint: 运行 scansci-pdf login 配置机构代理，或检查网络连接")


@app.command("camofox-status")
def camofox_status() -> None:
    """Check camofox-browser availability."""
    import json as _json
    from .config import load_config
    from .camofox import is_available
    config = load_config()
    url = config.get("camofox_url", "http://localhost:9377")
    enabled = config.get("camofox_enabled", True)
    available = is_available(config) if enabled else False
    print(f"  camofox-browser: {'running' if available else 'unreachable'}")
    print(f"  URL: {url}")
    print(f"  Enabled: {enabled}")


@app.command("import-cookies")
def import_cookies_cmd(cookie_file: str = typer.Argument(help="Netscape-format cookie file path")) -> None:
    """Import Netscape cookies into camofox-browser."""
    from .config import load_config
    from .camofox import import_cookies, is_available

    config = load_config()
    if not is_available(config):
        print("Error: camofox-browser is not running")
        raise typer.Exit(1)
    try:
        count = import_cookies(cookie_file, config)
        print(f"Imported {count} cookies from {cookie_file}")
    except Exception as exc:
        print(f"Error: {exc}")
        raise typer.Exit(1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
