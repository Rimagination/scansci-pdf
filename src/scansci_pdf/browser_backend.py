"""Unified browser backend: patchright (default), CloakBrowser, or Camoufox.

patchright is the Apache-2.0 open-source Playwright fork that patches the
detectable automation fingerprints (``--enable-automation`` removal,
``Runtime.enable`` leak, ``navigator.webdriver``, ...). Its documented best
practice is ``channel="chrome"`` + ``headless=False`` — i.e. driving the local
Google Chrome, whose kernel auto-updates. CloakBrowser's free tier is pinned
to Chromium 146 (newer 148/150 kernels are Pro-only), so it is kept as an
opt-in fallback. Camoufox is the last-resort fallback: an open-source
anti-detect Firefox (kernel Firefox 152), with a clean headless UA — no
``HeadlessChrome``/``HeadlessFirefox`` leak, verified at runtime.

All backends expose the same Playwright sync API surface (``launch``,
``launch_persistent_context``); the CloakBrowser-only ``humanize`` flag is
accepted everywhere and ignored on patchright.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BACKEND_PATCHRIGHT = "patchright"
BACKEND_CLOAKBROWSER = "cloakbrowser"
BACKEND_CAMOUFOX = "camoufox"
DEFAULT_BACKEND = BACKEND_PATCHRIGHT


# ---------------------------------------------------------------------------
# Availability / resolution
# ---------------------------------------------------------------------------

def is_available(name: str | None = None) -> bool:
    """Check whether a backend package is importable. None → resolved backend."""
    if name is None:
        name = DEFAULT_BACKEND
    try:
        if name == BACKEND_PATCHRIGHT:
            import patchright  # noqa: F401
            return True
        if name == BACKEND_CLOAKBROWSER:
            import cloakbrowser  # noqa: F401
            return True
        if name == BACKEND_CAMOUFOX:
            import camoufox  # noqa: F401
            return True
    except ImportError:
        return False
    return False


def resolve_backend(config: dict[str, Any] | None = None) -> str:
    """Resolve the effective backend from config, falling back gracefully.

    ``browser_backend`` config key (default "patchright"); an unknown value
    warns and resets to the default. Fallback chain:
    patchright → cloakbrowser → camoufox (last resort). A missing patchright
    falls to cloakbrowser; a missing — or below the kernel floor — cloakbrowser
    falls to camoufox when that is installed.
    """
    cfg = config or {}
    requested = str(cfg.get("browser_backend", DEFAULT_BACKEND) or DEFAULT_BACKEND).strip().lower()
    if requested not in (BACKEND_PATCHRIGHT, BACKEND_CLOAKBROWSER, BACKEND_CAMOUFOX):
        logger.warning("browser_backend: unknown backend '%s', using %s", requested, DEFAULT_BACKEND)
        requested = DEFAULT_BACKEND
    if requested == BACKEND_PATCHRIGHT and not is_available(BACKEND_PATCHRIGHT):
        logger.warning(
            "browser_backend: patchright not installed, falling back to cloakbrowser. "
            "Run: pip install patchright"
        )
        requested = BACKEND_CLOAKBROWSER
    if requested == BACKEND_CLOAKBROWSER and not is_available(BACKEND_CLOAKBROWSER):
        logger.warning(
            "browser_backend: cloakbrowser not installed, falling back to camoufox. "
            "Run: pip install cloakbrowser (or pip install camoufox)"
        )
        requested = BACKEND_CAMOUFOX
    if requested == BACKEND_CAMOUFOX and not is_available(BACKEND_CAMOUFOX):
        logger.warning(
            "browser_backend: camoufox not installed, falling back to cloakbrowser. "
            "Run: pip install camoufox"
        )
        requested = BACKEND_CLOAKBROWSER if is_available(BACKEND_CLOAKBROWSER) else BACKEND_PATCHRIGHT
    # A stale cloakbrowser kernel fails the hard gate at launch time; redirect
    # to camoufox when available instead of erroring out (escape hatch kept).
    if requested == BACKEND_CLOAKBROWSER and is_available(BACKEND_CAMOUFOX):
        v = _cloakbrowser_dist_version()
        if v is not None and v < CLOAKBROWSER_REQUIRED_MIN and not os.environ.get("SCANSCI_ALLOW_OLD_CLOAKBROWSER"):
            logger.warning(
                "browser_backend: cloakbrowser %s below required floor %s, using camoufox",
                ".".join(str(x) for x in v), ".".join(str(x) for x in CLOAKBROWSER_REQUIRED_MIN),
            )
            return BACKEND_CAMOUFOX
    return requested


# ---------------------------------------------------------------------------
# Local browser discovery (Chrome/Edge newer than bundled stealth Chromium 146)
# ---------------------------------------------------------------------------
# CloakBrowser's free tier is pinned to Chromium 146 (free builds stopped
# updating after 2026-05; newer 148/150 are Pro-only). Sites behind
# Cloudflare Turnstile repeatedly challenge the stale 146 fingerprint, so we
# prefer a local Chrome/Edge with a newer kernel.
# ---------------------------------------------------------------------------

_BUILTIN_CHROMIUM_MIN = (146, 0)  # bundled stealth Chromium; only swap when newer

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

_version_cache: dict[str, str] = {}


def _parse_version(v: str) -> tuple[int, ...]:
    """'150.0.7871.187' -> (150, 0, 7871, 187); (0,) when unparseable."""
    nums = [int(n) for n in re.findall(r"\d+", v or "")]
    return tuple(nums[:4]) if nums else (0,)


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
    _version_cache.update(result)
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
            result[path] = m.group(0) if m else "999.0.0.0"
        except Exception:
            result[path] = "999.0.0.0"
    return result


def find_local_browser() -> str | None:
    """Find a local Chrome/Edge newer than the bundled stealth Chromium 146.

    Returns the binary path, or None when the bundled Chromium should be kept.
    """
    versions = _probe_windows_versions() if os.name == "nt" else _probe_posix_versions()
    best: str | None = None
    best_version = _BUILTIN_CHROMIUM_MIN
    for path, version in versions.items():
        parsed = _parse_version(version)
        if parsed > best_version:
            best, best_version = path, parsed
    if best:
        logger.info(
            "browser_backend: using local browser %s (kernel %s) instead of bundled Chromium",
            best, best_version,
        )
    return best


# ---------------------------------------------------------------------------
# CloakBrowser kernel override (CLOAKBROWSER_BINARY_PATH protocol)
# ---------------------------------------------------------------------------

def resolve_browser_binary(config: dict[str, Any] | None = None) -> str | None:
    """Decide which browser binary CloakBrowser should launch.

    Priority: explicit config ``browser_executable`` > an externally pinned
    ``CLOAKBROWSER_BINARY_PATH`` env var (left untouched) > auto-detected
    local Chrome/Edge when ``browser_auto_upgrade`` is on (default).

    Returns a path to set as CLOAKBROWSER_BINARY_PATH, or None to keep the
    bundled stealth Chromium.
    """
    cfg = config or {}
    explicit = str(cfg.get("browser_executable", "") or "").strip()
    if explicit:
        if Path(explicit).exists():
            logger.info("browser_backend: using configured browser_executable: %s", explicit)
            return explicit
        logger.warning("browser_backend: browser_executable '%s' not found, falling back", explicit)
    env_path = os.environ.get("CLOAKBROWSER_BINARY_PATH", "").strip()
    if env_path and Path(env_path).exists():
        return None  # externally pinned — leave as-is
    if cfg.get("browser_auto_upgrade", True):
        return find_local_browser()
    return None


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
    """Launch via patchright (Playwright sync API, patched driver).

    Tries channel=chrome / executable_path first; if the local Chrome is
    missing, retries with the bundled chromium build (patchright install
    chromium). ``humanize`` is a no-op here.
    """
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

    # Headless Chrome's UA advertises ``HeadlessChrome``, a clear automation
    # signal to anti-bot checks. Inject a clean desktop UA at the context level
    # (renderer-level override — page-level JS UA spoofing is itself detected,
    # verified via sannysoft: JS override trips HEADCHR_UA, context-level UA is
    # "ok"). setdefault keeps an explicit caller-supplied user_agent.
    clean_ua = _clean_patchright_ua(browser, headless)
    if clean_ua:
        _original_new_context = browser.new_context

        def _new_context_with_clean_ua(*a: Any, **kw: Any) -> Any:
            kw.setdefault("user_agent", clean_ua)
            return _original_new_context(*a, **kw)

        browser.new_context = _new_context_with_clean_ua  # type: ignore[method-assign]

    # Stop the Playwright driver when the browser closes (mirrors CloakBrowser).
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
            f"patchright could not launch any browser: {last_error}. "
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


CLOAKBROWSER_REQUIRED_MIN = (0, 5, 9)  # keep in sync with the pyproject extra floor


def _cloakbrowser_dist_version() -> tuple[int, ...] | None:
    try:
        from importlib.metadata import version as _dist_version

        raw = _dist_version("cloakbrowser")
    except Exception:
        return None
    parts: list[int] = []
    for token in raw.split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _enforce_cloakbrowser_floor() -> None:
    """Hard gate: stale stealth kernels get flagged by publishers, so browser
    launches refuse to run on an outdated cloakbrowser. Escape hatch for
    offline/emergency use: SCANSCI_ALLOW_OLD_CLOAKBROWSER=1."""
    import os

    if os.environ.get("SCANSCI_ALLOW_OLD_CLOAKBROWSER"):
        return
    raw = None
    try:
        from importlib.metadata import version as _dist_version

        raw = _dist_version("cloakbrowser")
    except Exception:
        pass
    v = _cloakbrowser_dist_version()
    if v is None or v >= CLOAKBROWSER_REQUIRED_MIN:
        return
    required = ".".join(str(x) for x in CLOAKBROWSER_REQUIRED_MIN)
    raise RuntimeError(
        f"cloakbrowser {raw} is below the required minimum {required} — stale stealth "
        "kernels get flagged by publishers. Run: pip install -U cloakbrowser "
        "(escape hatch: set SCANSCI_ALLOW_OLD_CLOAKBROWSER=1)"
    )


def _launch_cloakbrowser(
    headless: bool,
    proxy: Any,
    args: list[str] | None,
    humanize: bool,
    **kwargs: Any,
) -> Any:
    _enforce_cloakbrowser_floor()
    from cloakbrowser import launch

    return launch(headless=headless, humanize=humanize, args=args, proxy=proxy, **kwargs)


def _launch_cloakbrowser_persistent(
    user_data_dir: str,
    headless: bool,
    proxy: Any,
    args: list[str] | None,
    humanize: bool,
    **kwargs: Any,
) -> Any:
    _enforce_cloakbrowser_floor()
    from cloakbrowser import launch_persistent_context

    return launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=headless,
        humanize=humanize,
        args=args,
        proxy=proxy,
        **kwargs,
    )


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
    driver lifecycle, mirroring the other backends.
    """
    from camoufox import DefaultAddons, NewBrowser
    from playwright.sync_api import sync_playwright

    opts = dict(kwargs)
    # The bundled uBlock addon downloads at build time and fails (or is
    # rate-limited) -> exclude it, it is not needed for download sessions.
    opts.setdefault("exclude_addons", [DefaultAddons.UBO])
    pw = sync_playwright().start()
    try:
        browser = NewBrowser(
            pw,
            headless=headless,
            humanize=humanize,
            args=args or [],
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
            args=args or [],
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
# Public entry points
# ---------------------------------------------------------------------------

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

    ``humanize`` is accepted for CloakBrowser compatibility and ignored on
    the patchright backend. Returns the same object as
    ``playwright.chromium.launch()`` / ``cloakbrowser.launch()``.
    """
    backend = resolve_backend(config)
    if backend == BACKEND_PATCHRIGHT:
        if humanize:
            logger.debug("browser_backend: humanize not supported by patchright, ignored")
        return _launch_patchright(config, headless=headless, proxy=proxy, args=args, **kwargs)
    if backend == BACKEND_CAMOUFOX:
        return _launch_camoufox(headless=headless, proxy=proxy, args=args, humanize=humanize, **kwargs)
    return _launch_cloakbrowser(headless=headless, proxy=proxy, args=args, humanize=humanize, **kwargs)


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
    if backend == BACKEND_PATCHRIGHT:
        if humanize:
            logger.debug("browser_backend: humanize not supported by patchright, ignored")
        return _launch_patchright_persistent(
            config, user_data_dir, headless=headless, proxy=proxy, args=args, **kwargs
        )
    if backend == BACKEND_CAMOUFOX:
        return _launch_camoufox_persistent(
            user_data_dir, headless=headless, proxy=proxy, args=args, humanize=humanize, **kwargs
        )
    return _launch_cloakbrowser_persistent(
        user_data_dir, headless=headless, proxy=proxy, args=args, humanize=humanize, **kwargs
    )


def browser_info(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Human-readable info about the current backend for diagnostics.

    Returns {"backend": ..., "binary": ..., "version": ...}.
    """
    cfg = config or {}
    backend = resolve_backend(cfg)
    info: dict[str, Any] = {"backend": backend, "binary": "", "version": ""}
    if backend == BACKEND_PATCHRIGHT:
        binary = ""
        explicit = str(cfg.get("browser_executable", "") or "").strip()
        if explicit and Path(explicit).exists():
            binary = explicit
        else:
            versions = _probe_windows_versions() if os.name == "nt" else _probe_posix_versions()
            for path, version in versions.items():
                if "chrome" in path.lower():
                    binary, info["version"] = path, version
                    break
            else:
                binary = "channel=chrome"
        info["binary"] = binary
        return info
    if backend == BACKEND_CAMOUFOX:
        try:
            from camoufox.pkgman import INSTALL_DIR, Version
            installed = Version.from_path()
            info["binary"] = str(INSTALL_DIR / "camoufox.exe") if os.name == "nt" else str(INSTALL_DIR)
            info["version"] = installed.full_string if installed.is_supported() else "unsupported"
        except Exception:
            info["binary"] = "camoufox"
            info["version"] = "?"
        return info
    # CloakBrowser backend
    binary = resolve_browser_binary(cfg)
    try:
        from cloakbrowser.config import get_chromium_version
        bundled = get_chromium_version()
    except Exception:
        bundled = "?"
    if binary:
        info["binary"] = binary
        info["version"] = f"local (bundled {bundled})"
    else:
        info["binary"] = "bundled stealth Chromium"
        info["version"] = bundled
    return info
