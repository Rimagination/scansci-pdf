"""Unified browser backend: patchright (default), Camoufox, or ego-lite.

patchright is the Apache-2.0 open-source Playwright fork that patches the
detectable automation fingerprints (``--enable-automation`` removal,
``Runtime.enable`` leak, ``navigator.webdriver``, ...). Its documented best
practice is ``channel="chrome"`` + ``headless=False`` — i.e. driving the local
Google Chrome, whose kernel auto-updates. Camoufox is the open-source
anti-detect Firefox fallback (kernel Firefox 152), with a clean headless UA.
ego-lite is an external Chromium app (macOS/Windows) attached over CDP; it is
opt-in only and never launched implicitly.

All backends expose the same Playwright sync API surface (``launch``,
``launch_persistent_context``). The ``humanize`` flag is supported by Camoufox
and ignored on patchright/ego-lite.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BACKEND_PATCHRIGHT = "patchright"
BACKEND_CAMOUFOX = "camoufox"
BACKEND_EGO_LITE = "ego-lite"
DEFAULT_BACKEND = BACKEND_PATCHRIGHT
BACKENDS = (BACKEND_PATCHRIGHT, BACKEND_CAMOUFOX, BACKEND_EGO_LITE)
EGO_LITE_DEFAULT_PORTS = (9351, 9222)


# ---------------------------------------------------------------------------
# Availability / resolution
# ---------------------------------------------------------------------------

def is_available(name: str | None = None, config: dict[str, Any] | None = None) -> bool:
    """Check whether a backend package is importable. None → resolved backend."""
    if name is None:
        name = resolve_backend(config)
    try:
        if name == BACKEND_PATCHRIGHT:
            import patchright  # noqa: F401
            return True
        if name == BACKEND_CAMOUFOX:
            import camoufox  # noqa: F401
            return True
        if name == BACKEND_EGO_LITE:
            if platform.system() == "Linux":
                return False
            configured = str((config or {}).get("ego_lite_cdp_url", "") or "").strip()
            if configured:
                if not configured.startswith(("http://", "https://")):
                    configured = "http://" + configured
                return _probe_cdp(configured.rstrip("/"))
            if ego_lite_cdp_url(config):
                return True
            exe = _ego_lite_executable(config)
            return bool(exe and (Path(exe).exists() or shutil.which(exe)))
    except ImportError:
        return False
    return False


def is_any_available(config: dict[str, Any] | None = None) -> bool:
    """True when the resolved backend can actually be launched right now."""
    return is_available(resolve_backend(config), config)


def resolve_backend(config: dict[str, Any] | None = None) -> str:
    """Resolve the effective backend from config, falling back gracefully.

    ``browser_backend`` config key (default "patchright"); an unknown value
    warns and resets to the default. Fallback chain:
    patchright → camoufox. If neither is installed the requested/default name
    is returned and the launch site raises a clear install hint.
    """
    cfg = config or {}
    requested = str(cfg.get("browser_backend", DEFAULT_BACKEND) or DEFAULT_BACKEND).strip().lower()
    if requested not in BACKENDS:
        logger.warning("browser_backend: unknown backend '%s', using %s", requested, DEFAULT_BACKEND)
        requested = DEFAULT_BACKEND
    if requested == BACKEND_EGO_LITE:
        # Explicit-only: fail at launch with setup instructions instead of
        # silently driving a different engine.
        return BACKEND_EGO_LITE
    if requested == BACKEND_PATCHRIGHT and not is_available(BACKEND_PATCHRIGHT):
        logger.warning(
            "browser_backend: patchright not installed, falling back to camoufox. "
            "Run: pip install patchright"
        )
        requested = BACKEND_CAMOUFOX
    if requested == BACKEND_CAMOUFOX and not is_available(BACKEND_CAMOUFOX):
        logger.warning(
            "browser_backend: camoufox not installed, falling back to patchright. "
            "Run: pip install 'scansci-pdf[camoufox]'"
        )
        requested = BACKEND_PATCHRIGHT
    return requested


# ---------------------------------------------------------------------------
# Local browser discovery (diagnostics only)
# ---------------------------------------------------------------------------

_SYSTEM_BROWSER_PATHS_WIN = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

_SYSTEM_BROWSER_NAMES_POSIX = [
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "msedge",
]


def _probe_windows_versions() -> dict[str, str]:
    """One PowerShell call: {path: version} for every existing system browser."""
    paths = ",".join(f"'{p}'" for p in _SYSTEM_BROWSER_PATHS_WIN)
    script = (
        f"$paths = @({paths}); "
        "foreach ($p in $paths) { if (Test-Path $p) { "
        'Write-Output ("$p`t" + (Get-Item $p).VersionInfo.ProductVersion) } }'
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return {}
    result: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if "\t" in line:
            path, version = line.split("\t", 1)
            result[path.strip()] = version.strip()
    return result


def _probe_posix_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for name in _SYSTEM_BROWSER_NAMES_POSIX:
        path = shutil.which(name)
        if not path:
            continue
        try:
            out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=5).stdout or ""
            m = re.search(r"\d+\.\d+\.\d+\.\d+", out)
            result[path] = m.group(0) if m else "?"
        except Exception:
            result[path] = "?"
    return result


# ---------------------------------------------------------------------------
# patchright launch helpers
# ---------------------------------------------------------------------------

def _patchright_browser_kwargs(config: dict[str, Any] | None) -> dict[str, Any]:
    """Browser selection for the patchright backend.

    Explicit ``browser_executable`` wins; otherwise channel="chrome" (local
    Google Chrome, official best practice). Returns kwargs for launch().
    """
    cfg = config or {}
    explicit = str(cfg.get("browser_executable", "") or "").strip()
    if explicit:
        if Path(explicit).exists():
            return {"executable_path": explicit}
        logger.warning("browser_backend: browser_executable '%s' not found, using channel=chrome", explicit)
    return {"channel": "chrome"}


_DESKTOP_UA_TEMPLATE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)


def _clean_patchright_ua(browser: Any, headless: bool) -> str | None:
    """Drop the ``HeadlessChrome/...`` UA prefix that headless Chrome sends.

    Returns a desktop-UA string for the launched kernel's real major version
    (so the UA always matches the actual binary), or None when the version
    cannot be read. Only headless launches need this: non-headless Chrome
    already sends the clean desktop UA.
    """
    if not headless:
        return None
    try:
        major = str(browser.version).split(".")[0]
        if not major.isdigit():
            return None
        return _DESKTOP_UA_TEMPLATE.format(major=major)
    except Exception:
        return None


def _launch_patchright(
    config: dict[str, Any] | None,
    headless: bool,
    proxy: Any,
    args: list[str] | None,
    **kwargs: Any,
) -> Any:
    """Launch via patchright (Playwright sync API, patched driver)."""
    from patchright.sync_api import sync_playwright

    browser_kwargs = _patchright_browser_kwargs(config)
    pw = sync_playwright().start()
    last_error: Exception | None = None
    for attempt in (browser_kwargs, {}):  # preferred → bundled chromium fallback
        try:
            browser = pw.chromium.launch(
                headless=headless,
                args=args or [],
                proxy=proxy,
                **attempt,
                **kwargs,
            )
            break
        except Exception as exc:  # noqa: BLE001 - try the fallback binary
            last_error = exc
    else:
        pw.stop()
        raise RuntimeError(
            f"patchright could not launch any browser: {last_error}. "
            "Install Google Chrome or run: patchright install chromium"
        ) from last_error

    clean_ua = _clean_patchright_ua(browser, headless)
    if clean_ua:
        original_new_context = browser.new_context

        def _new_context_with_clean_ua(*a: Any, **kw: Any) -> Any:
            kw.setdefault("user_agent", clean_ua)
            return original_new_context(*a, **kw)

        browser.new_context = _new_context_with_clean_ua  # type: ignore[method-assign]

    original_close = browser.close

    def _close_with_cleanup() -> None:
        try:
            original_close()
        finally:
            try:
                pw.stop()
            except Exception:
                pass

    browser.close = _close_with_cleanup  # type: ignore[method-assign]
    return browser


def _launch_patchright_persistent(
    config: dict[str, Any] | None,
    user_data_dir: str,
    headless: bool,
    proxy: Any,
    args: list[str] | None,
    **kwargs: Any,
) -> Any:
    """Persistent context via patchright, with the same binary fallbacks."""
    from patchright.sync_api import sync_playwright

    browser_kwargs = _patchright_browser_kwargs(config)
    pw = sync_playwright().start()
    last_error: Exception | None = None
    for attempt in (browser_kwargs, {}):
        try:
            context = pw.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=headless,
                args=args or [],
                proxy=proxy,
                **attempt,
                **kwargs,
            )
            break
        except Exception as exc:  # noqa: BLE001 - try the fallback binary
            last_error = exc
    else:
        pw.stop()
        raise RuntimeError(
            f"patchright could not launch a persistent context: {last_error}. "
            "Install Google Chrome or run: patchright install chromium"
        ) from last_error

    original_close = context.close

    def _close_with_cleanup() -> None:
        try:
            original_close()
        finally:
            try:
                pw.stop()
            except Exception:
                pass

    context.close = _close_with_cleanup  # type: ignore[method-assign]
    return context


# ---------------------------------------------------------------------------
# Camoufox
# ---------------------------------------------------------------------------

def _launch_camoufox(
    headless: bool,
    proxy: Any,
    args: list[str] | None,
    humanize: bool,
    **kwargs: Any,
) -> Any:
    """Launch via Camoufox (anti-detect Firefox, Playwright API).

    Returns a vanilla ``playwright.sync_api.Browser`` so the rest of the stack
    (contexts, cookies, close) works unchanged. Handles its own Playwright
    driver lifecycle, mirroring the other backend.

    Callers pass Chromium-style flags; Camoufox is a Firefox fork that
    configures its own anti-fingerprinting, so caller args are dropped.
    """
    if args:
        logger.debug("browser_backend: camoufox ignores %d caller arg(s) (Chromium flags)", len(args))
    from camoufox import DefaultAddons, NewBrowser
    from playwright.sync_api import sync_playwright

    opts = dict(kwargs)
    opts.setdefault("exclude_addons", [DefaultAddons.UBO])
    pw = sync_playwright().start()
    try:
        browser = NewBrowser(
            pw,
            headless=headless,
            humanize=humanize,
            args=[],
            proxy=proxy,
            **opts,
        )
    except Exception:
        pw.stop()
        raise

    original_close = browser.close

    def _close_with_cleanup() -> None:
        try:
            original_close()
        finally:
            try:
                pw.stop()
            except Exception:
                pass

    browser.close = _close_with_cleanup  # type: ignore[method-assign]
    return browser


def _launch_camoufox_persistent(
    user_data_dir: str,
    headless: bool,
    proxy: Any,
    args: list[str] | None,
    humanize: bool,
    **kwargs: Any,
) -> Any:
    """Persistent context via Camoufox (returns a BrowserContext)."""
    if args:
        logger.debug("browser_backend: camoufox ignores %d caller arg(s) (Chromium flags)", len(args))
    from camoufox import DefaultAddons, NewBrowser
    from playwright.sync_api import sync_playwright

    opts = dict(kwargs)
    opts.setdefault("exclude_addons", [DefaultAddons.UBO])
    pw = sync_playwright().start()
    try:
        context = NewBrowser(
            pw,
            persistent_context=True,
            user_data_dir=user_data_dir,
            headless=headless,
            humanize=humanize,
            args=[],
            proxy=proxy,
            **opts,
        )
    except Exception:
        pw.stop()
        raise

    original_close = context.close

    def _close_with_cleanup() -> None:
        try:
            original_close()
        finally:
            try:
                pw.stop()
            except Exception:
                pass

    context.close = _close_with_cleanup  # type: ignore[method-assign]
    return context


# ---------------------------------------------------------------------------
# ego lite (CDP attach)
# ---------------------------------------------------------------------------

def ego_lite_cdp_url(config: dict[str, Any] | None = None) -> str:
    """Return the configured or auto-probed ego lite CDP URL, or ""."""
    cfg = config or {}
    configured = str(cfg.get("ego_lite_cdp_url", "") or "").strip()
    if configured:
        if not configured.startswith(("http://", "https://")):
            configured = "http://" + configured
        return configured.rstrip("/")
    for port in EGO_LITE_DEFAULT_PORTS:
        url = f"http://127.0.0.1:{port}"
        if _probe_cdp(url):
            return url
    return ""


def _probe_cdp(url: str, timeout: float = 1.5) -> bool:
    try:
        import requests

        session = requests.Session()
        session.trust_env = False  # localhost CDP must not go through ALL_PROXY
        return session.get(f"{url}/json/version", timeout=timeout).status_code == 200
    except Exception:
        return False


def _ego_lite_executable(config: dict[str, Any] | None = None) -> str:
    cfg = config or {}
    configured = str(cfg.get("ego_lite_executable", "") or "").strip()
    if configured:
        return configured
    for name in ("ego-browser", "ego", "ego lite", "ego-lite"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _ego_lite_start(
    config: dict[str, Any] | None,
    *,
    headless: bool,
    launch_if_needed: bool,
) -> tuple[Any, Any, subprocess.Popen[Any] | None, str]:
    if platform.system() == "Linux":
        raise RuntimeError(
            "ego-lite does not ship a Linux build. Use patchright/camoufox or "
            "connect to a remote ego-lite instance from macOS/Windows."
        )

    cfg = config or {}
    url = ego_lite_cdp_url(cfg)
    process: subprocess.Popen[Any] | None = None

    if not url and launch_if_needed:
        executable = _ego_lite_executable(cfg)
        if not executable:
            raise RuntimeError(
                "ego-lite is not running and no executable is configured. "
                "Start ego lite with --remote-debugging-port=9351 (or enable the "
                "CDP port in its settings), or set ego_lite_executable."
            )
        port = EGO_LITE_DEFAULT_PORTS[0]
        configured = str(cfg.get("ego_lite_cdp_url", "") or "").strip()
        if configured.rstrip("/").rsplit("/", 1)[-1].isdigit():
            port = int(configured.rstrip("/").rsplit("/", 1)[-1])
        profile = str(
            cfg.get("ego_lite_user_data_dir", "")
            or (Path.home() / ".scansci-pdf" / "ego-lite-profile")
        )
        Path(profile).mkdir(parents=True, exist_ok=True)
        command = [executable, f"--remote-debugging-port={port}", f"--user-data-dir={profile}"]
        if headless:
            command.append("--headless=new")
        logger.info("browser_backend: starting ego lite: %s", " ".join(command))
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        deadline = time.time() + float(cfg.get("ego_lite_launch_timeout", 20.0) or 20.0)
        while time.time() < deadline:
            if _probe_cdp(f"http://127.0.0.1:{port}"):
                url = f"http://127.0.0.1:{port}"
                break
            if process.poll() is not None:
                raise RuntimeError(
                    f"ego lite exited during startup (code {process.returncode})."
                )
            time.sleep(0.5)

    if not url:
        raise RuntimeError(
            "ego-lite is not reachable over CDP. Start ego lite with "
            "--remote-debugging-port=9351 (or enable the CDP port in Settings), "
            "then set ego_lite_cdp_url."
        )

    try:
        try:
            from patchright.sync_api import sync_playwright
        except ImportError:
            from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(url)
    except Exception as exc:
        if process is not None:
            process.terminate()
        raise RuntimeError(f"could not connect to ego lite at {url}: {exc}") from exc
    return pw, browser, process, url


def _release_ego_lite(pw: Any, process: subprocess.Popen[Any] | None) -> None:
    """Stop the Playwright client without closing the user's ego lite app."""
    try:
        pw.stop()
    except Exception:
        pass
    if process is not None:
        try:
            process.terminate()
        except Exception:
            pass


def _launch_ego_lite(
    config: dict[str, Any] | None,
    *,
    headless: bool,
    launch_if_needed: bool,
    **kwargs: Any,
) -> Any:
    pw, browser, process, url = _ego_lite_start(
        config, headless=headless, launch_if_needed=launch_if_needed
    )

    def _close_with_cleanup() -> None:
        # Do not call browser.close(): over CDP that can close the user's app.
        _release_ego_lite(pw, process)

    existing = list(getattr(browser, "contexts", []) or [])
    if existing:
        # The caller usually follows with browser.new_context(); reuse the
        # user's logged-in context instead of creating an empty incognito one.
        # Closing it would also disconnect the user's tab, so make close a no-op
        # exactly like the persistent path does.
        reused = existing[0]

        def _noop_close() -> None:
            return None

        reused.close = _noop_close  # type: ignore[method-assign]
        browser.new_context = lambda *a, **k: reused  # type: ignore[method-assign]

    browser.close = _close_with_cleanup  # type: ignore[method-assign]
    browser._scansci_ego_lite = {"url": url, "process": process}  # type: ignore[attr-defined]
    return browser


def _launch_ego_lite_persistent(
    config: dict[str, Any] | None,
    *,
    headless: bool,
    launch_if_needed: bool,
    **kwargs: Any,
) -> Any:
    pw, browser, process, url = _ego_lite_start(
        config, headless=headless, launch_if_needed=launch_if_needed
    )
    existing = list(getattr(browser, "contexts", []) or [])
    if existing:
        context = existing[0]
        owned = False
    else:
        context = browser.new_context()
        owned = True

    original_close = context.close

    def _close_with_cleanup() -> None:
        if owned:
            try:
                original_close()
            except Exception:
                pass
        # Reused contexts belong to the user; only disconnect the client.
        _release_ego_lite(pw, process)

    context.close = _close_with_cleanup  # type: ignore[method-assign]
    context._scansci_ego_lite = {  # type: ignore[attr-defined]
        "url": url,
        "process": process,
        "owned": owned,
    }
    return context


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def _proxy_from_config(config: dict[str, Any] | None) -> Any:
    """Playwright proxy dict from config, or None."""
    cfg = config or {}
    proxy = (str(cfg.get("browser_static_proxy", "") or "").strip()
             or str(cfg.get("network_proxy", "") or "").strip())
    return {"server": proxy} if proxy else None


def launch(
    *,
    headless: bool = True,
    proxy: Any = None,
    args: list[str] | None = None,
    humanize: bool = True,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Launch the resolved backend's stealth browser (Playwright sync API).

    ``humanize`` is supported by Camoufox and ignored on patchright.
    """
    backend = resolve_backend(config)
    if backend == BACKEND_EGO_LITE:
        return _launch_ego_lite(
            config,
            headless=headless,
            launch_if_needed=bool((config or {}).get("ego_lite_auto_launch", False)),
            **kwargs,
        )
    if backend == BACKEND_CAMOUFOX and proxy is None:
        proxy = _proxy_from_config(config)
    if backend == BACKEND_PATCHRIGHT:
        if humanize:
            logger.debug("browser_backend: humanize is not supported by patchright, ignored")
        return _launch_patchright(config, headless=headless, proxy=proxy, args=args, **kwargs)
    return _launch_camoufox(headless=headless, proxy=proxy, args=args, humanize=humanize, **kwargs)


def launch_persistent_context(
    user_data_dir: str,
    *,
    headless: bool = True,
    proxy: Any = None,
    args: list[str] | None = None,
    humanize: bool = True,
    config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Launch the resolved backend with a persistent profile.

    Same contract as ``playwright.chromium.launch_persistent_context()``.
    """
    backend = resolve_backend(config)
    if backend == BACKEND_EGO_LITE:
        return _launch_ego_lite_persistent(
            config,
            headless=headless,
            launch_if_needed=bool((config or {}).get("ego_lite_auto_launch", False)),
            **kwargs,
        )
    if backend == BACKEND_CAMOUFOX and proxy is None:
        proxy = _proxy_from_config(config)
    if backend == BACKEND_PATCHRIGHT:
        if humanize:
            logger.debug("browser_backend: humanize is not supported by patchright, ignored")
        return _launch_patchright_persistent(
            config, user_data_dir, headless=headless, proxy=proxy, args=args, **kwargs
        )
    return _launch_camoufox_persistent(
        user_data_dir, headless=headless, proxy=proxy, args=args, humanize=humanize, **kwargs
    )


def browser_info(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Human-readable info about the current backend for diagnostics."""
    cfg = config or {}
    backend = resolve_backend(cfg)
    info: dict[str, Any] = {"backend": backend, "binary": "", "version": ""}
    if backend == BACKEND_EGO_LITE:
        info["cdp_url"] = ego_lite_cdp_url(cfg)
        info["binary"] = _ego_lite_executable(cfg) or "ego-lite (external app)"
        info["version"] = "ego-lite (CDP)"
        return info
    if backend == BACKEND_PATCHRIGHT:
        explicit = str(cfg.get("browser_executable", "") or "").strip()
        if explicit and Path(explicit).exists():
            info["binary"] = explicit
        else:
            versions = _probe_windows_versions() if os.name == "nt" else _probe_posix_versions()
            for path, version in versions.items():
                if "chrome" in path.lower():
                    info["binary"], info["version"] = path, version
                    break
            else:
                info["binary"] = "channel=chrome"
        return info
    try:
        from camoufox.pkgman import INSTALL_DIR, Version
        installed = Version.from_path()
        info["binary"] = str(INSTALL_DIR / "camoufox.exe") if os.name == "nt" else str(INSTALL_DIR)
        info["version"] = installed.full_string if installed.is_supported() else "unsupported"
    except Exception:
        info["binary"] = "camoufox"
        info["version"] = "?"
    return info
