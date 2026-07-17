"""Compatibility helpers for CloakBrowser runtime quirks."""

from __future__ import annotations

import os
import platform
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

_CLOAKBROWSER_CACHE_ENV = "CLOAKBROWSER_CACHE_DIR"
_SCANSCI_CACHE_ENV = "SCANSCI_PDF_CLOAKBROWSER_CACHE_DIR"
_BUILTIN_CACHE_DIR = Path(__file__).resolve().parent / "_browsers" / "cloakbrowser"
_launch_cleanup_lock = threading.RLock()
_LaunchResult = TypeVar("_LaunchResult")


class _TrackedPlaywrightContextManager:
    """Record Playwright instances started while CloakBrowser is launching."""

    def __init__(self, manager: Any, started: list[Any]) -> None:
        self._manager = manager
        self._started = started

    def start(self) -> Any:
        playwright = self._manager.start()
        self._started.append(playwright)
        return playwright

    def __enter__(self) -> Any:
        playwright = self._manager.__enter__()
        self._started.append(playwright)
        return playwright

    def __exit__(self, *args: Any) -> Any:
        return self._manager.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._manager, name)


def launch_with_driver_cleanup(
    launch_callable: Callable[..., _LaunchResult],
    *args: Any,
    **kwargs: Any,
) -> _LaunchResult:
    """Stop Playwright when CloakBrowser fails after starting its driver.

    CloakBrowser owns the driver after a successful launch and stops it from the
    returned browser/context ``close()`` method. CloakBrowser 0.4.10 does not
    stop the driver when Chromium launch or post-launch patching raises.
    """
    import playwright.sync_api as playwright_sync_api

    with _launch_cleanup_lock:
        original_sync_playwright = playwright_sync_api.sync_playwright
        started: list[Any] = []

        def tracked_sync_playwright(*factory_args: Any, **factory_kwargs: Any) -> Any:
            manager = original_sync_playwright(*factory_args, **factory_kwargs)
            return _TrackedPlaywrightContextManager(manager, started)

        playwright_sync_api.sync_playwright = tracked_sync_playwright
        try:
            return launch_callable(*args, **kwargs)
        except BaseException:
            for playwright in reversed(started):
                try:
                    playwright.stop()
                except BaseException:
                    pass
            raise
        finally:
            playwright_sync_api.sync_playwright = original_sync_playwright


def configure_builtin_cloakbrowser(
    cache_dir: str | os.PathLike[str] | None = None,
    *,
    create_dir: bool = True,
) -> Path:
    """Point CloakBrowser at ScanSci-PDF's project-managed browser cache."""
    existing = os.environ.get(_CLOAKBROWSER_CACHE_ENV)
    if existing:
        return Path(existing)

    target = Path(cache_dir or os.environ.get(_SCANSCI_CACHE_ENV, "") or _BUILTIN_CACHE_DIR)
    target = target.expanduser().resolve()
    if create_dir:
        target.mkdir(parents=True, exist_ok=True)
    os.environ[_CLOAKBROWSER_CACHE_ENV] = str(target)
    return target


def prepare_cloakbrowser_runtime(config_module: Any | None = None) -> Path:
    """Configure ScanSci-PDF's CloakBrowser runtime before importing launch APIs."""
    cache_dir = configure_builtin_cloakbrowser()
    ensure_cloakbrowser_platform_compatible(config_module)
    return cache_dir


def ensure_cloakbrowser_platform_compatible(config_module: Any | None = None) -> bool:
    """Patch CloakBrowser platform detection when Windows reports no machine."""
    if platform.system() != "Windows" or platform.machine():
        return False

    try:
        config = config_module
        if config is None:
            from cloakbrowser import config as config  # type: ignore[no-redef]
    except Exception:
        return False

    supported = getattr(config, "SUPPORTED_PLATFORMS", None)
    if not isinstance(supported, dict):
        return False

    if ("Windows", "") in supported:
        return False

    is_64bit_windows = bool(os.environ.get("ProgramFiles(x86)")) or bool(
        os.environ.get("PROCESSOR_ARCHITEW6432")
    )
    if not is_64bit_windows:
        return False

    supported[("Windows", "")] = supported.get(("Windows", "AMD64"), "windows-x64")
    return True
