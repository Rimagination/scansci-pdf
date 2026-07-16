"""Institutional login via stealth browser. Replaces Selenium for WebVPN/CARSI/EZProxy login."""

from __future__ import annotations

import json
import time
import atexit
import threading
from pathlib import Path
from typing import Any

from .cloakbrowser_compat import launch_with_driver_cleanup, prepare_cloakbrowser_runtime

try:
    prepare_cloakbrowser_runtime()
    from cloakbrowser import launch
    _HAS_CLOAKBROWSER = True
except Exception:
    launch = None  # type: ignore[assignment]
    _HAS_CLOAKBROWSER = False
from .log import get_logger

log = get_logger()


class PersistentBrowser:
    """Keeps a stealth browser alive across multiple operations.

    Login once, reuse the same browser for all subsequent downloads.
    The WebVPN session stays valid because the browser instance never closes.
    """

    def __init__(self):
        self._browser = None
        self._context = None
        self._page = None
        self._cookies_saved = False
        self._owner_thread: threading.Thread | None = None
        self._owner_thread_id: int | None = None
        self._browser_lease = None
        self._lifecycle_lock = threading.RLock()

    def _assert_owner_locked(self, operation: str) -> None:
        owner = self._owner_thread
        current = threading.get_ident()
        if owner is not None and owner is not threading.current_thread():
            raise RuntimeError(
                "PersistentBrowser is owned by another thread; "
                f"cannot {operation} Playwright resources from thread {current} "
                f"(owner={self._owner_thread_id})"
            )

    def _is_alive_locked(self) -> bool:
        if self._browser is None:
            return False
        try:
            self._page.url  # noqa: B018
            return True
        except Exception:
            if not self._cleanup_locked():
                raise RuntimeError(
                    "PersistentBrowser resources became stale and could not be closed"
                )
            return False

    @property
    def is_alive(self) -> bool:
        with self._lifecycle_lock:
            self._assert_owner_locked("inspect")
            return self._is_alive_locked()

    def get_page(self, config: dict[str, Any] | None = None):
        """Get or create the browser page. Returns (context, page)."""
        with self._lifecycle_lock:
            self._assert_owner_locked("reuse")
            if self._is_alive_locked():
                return self._context, self._page
            return self._start_locked(config)

    def _start(self, config: dict[str, Any] | None = None):
        """Start a new browser instance. Restores saved state if available."""
        with self._lifecycle_lock:
            self._assert_owner_locked("start")
            if self._is_alive_locked():
                return self._context, self._page
            return self._start_locked(config)

    def _start_locked(self, config: dict[str, Any] | None = None):
        """Start resources while the lifecycle lock is held by their owner."""
        if not _HAS_CLOAKBROWSER:
            raise RuntimeError("cloakbrowser not installed. Run: pip install cloakbrowser")
        log.info("   [browser] Starting persistent browser...")
        browser = None
        context = None
        page = None
        slot_lease = None
        try:
            from . import browser_engine
            slot_lease = browser_engine._retain_browser_slot(config)
            browser = launch_with_driver_cleanup(
                launch,
                headless=False, humanize=True,
                args=["--disable-features=CrossOriginOpenerPolicy"],
            )
            context = browser.new_context()
            page = context.new_page()
            if config:
                self._restore_state(config, context=context, page=page)
        except Exception:
            resources_closed = self._close_resources(page, context, browser)
            if resources_closed:
                if slot_lease is not None:
                    slot_lease.close()
                self._browser = None
                self._context = None
                self._page = None
                self._browser_lease = None
                self._owner_thread = None
                self._owner_thread_id = None
            else:
                # Preserve the exact handles and permit for an owner-thread
                # retry.  Launching a replacement here could exceed the cap.
                self._browser = browser
                self._context = context
                self._page = page
                self._browser_lease = slot_lease
                self._owner_thread = threading.current_thread()
                self._owner_thread_id = threading.get_ident()
            raise

        self._browser = browser
        self._context = context
        self._page = page
        self._browser_lease = slot_lease
        self._owner_thread = threading.current_thread()
        self._owner_thread_id = threading.get_ident()
        return context, page

    def _restore_state(
        self,
        config: dict[str, Any],
        *,
        context=None,
        page=None,
    ):
        """Restore saved cookies and localStorage into the browser."""
        from .config import DATA_DIR
        cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
        state_file = cache_dir / "browser_state.json"
        if not state_file.exists():
            return
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            log.info("   [browser] browser_state.json corrupted, starting fresh")
            return
        if not isinstance(state, dict):
            log.info("   [browser] browser_state.json has invalid structure, starting fresh")
            return

        if context is None:
            context = self._context
        if page is None:
            page = self._page

        cookies = state.get("cookies", [])
        if cookies and context is not None:
            try:
                context.add_cookies(cookies)
                log.info(f"   [browser] Restored {len(cookies)} cookies")
            except Exception as e:
                log.info(f"   [browser] Cookie restore warning: {e}")

        storage = state.get("localStorage", {})
        if not isinstance(storage, dict) or page is None:
            storage = {}
        for origin, items in storage.items():
            try:
                page.goto(origin, wait_until="commit", timeout=10000)
                for key, value in items.items():
                    page.evaluate(f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)})")
            except Exception as e:
                log.info(f"   [browser] localStorage restore failed for {origin}: {e}")

        log.info("   [browser] Browser state restored")

    def save_cookies(self, config: dict[str, Any]):
        """Save current browser state (cookies + localStorage) to disk."""
        with self._lifecycle_lock:
            self._assert_owner_locked("save")
            if not self._context:
                return
            try:
                from .config import DATA_DIR
                cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
                cache_dir.mkdir(parents=True, exist_ok=True)

                cookies = self._context.cookies()

                localStorage = {}
                for page in self._context.pages:
                    try:
                        url = page.url
                        if url.startswith("http"):
                            from urllib.parse import urlparse
                            origin = f"{urlparse(url).scheme}://{urlparse(url).hostname}"
                            items = page.evaluate("""
                                (() => {
                                    const items = {};
                                    for (let i = 0; i < localStorage.length; i++) {
                                        const key = localStorage.key(i);
                                        items[key] = localStorage.getItem(key);
                                    }
                                    return items;
                                })()
                            """)
                            if items:
                                localStorage[origin] = items
                    except Exception:
                        pass

                state = {"cookies": cookies, "localStorage": localStorage}
                state_file = cache_dir / "browser_state.json"
                state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

                cookie_file = cache_dir / "instsci-cookies.json"
                cookie_data = [
                    {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
                    for c in cookies
                ]
                cookie_file.write_text(json.dumps(cookie_data, indent=2, ensure_ascii=False), encoding="utf-8")

                netscape_file = cache_dir / "instsci-cookies.txt"
                from .browser_cookies import cookies_to_netscape
                netscape_file.write_text(cookies_to_netscape(cookies), encoding="utf-8")

                self._cookies_saved = True
                log.info(f"   [browser] Saved {len(cookies)} cookies + {len(localStorage)} localStorage origins")
            except Exception as e:
                log.info(f"   [browser] Failed to save state: {e}")

    def _close_resources(self, page, context, browser) -> bool:
        from . import browser_engine

        closed: dict[str, bool] = {
            "page": page is None,
            "context": context is None,
            "browser": browser is None,
        }
        for label, resource in (
            ("page", page),
            ("context", context),
            ("browser", browser),
        ):
            if resource is None:
                continue
            resource_closed, errors = browser_engine._close_resource_with_confirmation(
                resource,
                is_browser=label == "browser",
            )
            closed[label] = resource_closed
            if not resource_closed:
                log.info(
                    f"   [browser] Failed to close {label} after retry: "
                    + "; ".join(str(error) for error in errors)
                )

        if browser is not None and closed["browser"]:
            return True
        if context is not None and closed["context"]:
            return browser is None
        return browser is None and context is None and closed["page"]

    def _cleanup_locked(self):
        """Close resources while holding the lifecycle lock on the owner thread."""
        page = self._page
        context = self._context
        browser = self._browser
        if not self._close_resources(page, context, browser):
            return False
        lease = self._browser_lease
        self._browser = None
        self._context = None
        self._page = None
        self._browser_lease = None
        self._owner_thread = None
        self._owner_thread_id = None
        if lease is not None:
            lease.close()
        return True

    def _cleanup(self):
        """Close browser resources and detach ownership before calling drivers."""
        with self._lifecycle_lock:
            self._assert_owner_locked("close")
            if not self._cleanup_locked():
                raise RuntimeError(
                    "PersistentBrowser close could not be confirmed; "
                    "resources and global browser slot were retained"
                )

    def close(self):
        """Explicitly close the browser."""
        self._cleanup()
        log.info("   [browser] Persistent browser closed")

    def _close_at_exit(self) -> None:
        """Best-effort process-exit cleanup without crossing Playwright threads."""
        try:
            self._cleanup()
        except RuntimeError:
            # Logging handlers (including pytest's capture stream) may already
            # be closed when atexit callbacks run. Cleanup must stay silent.
            pass


# Module-level singleton
_browser = PersistentBrowser()
atexit.register(_browser._close_at_exit)


def get_browser(config: dict[str, Any] | None = None):
    """Get the persistent browser singleton. Returns (browser, context, page)."""
    context, page = _browser.get_page(config)
    return _browser, context, page


def save_browser_cookies(config: dict[str, Any]):
    """Save cookies from the persistent browser."""
    _browser.save_cookies(config)


def close_browser():
    """Close the persistent browser."""
    _browser.close()


def _save_cookies_json(cookies: list[dict[str, Any]], cookie_file: Path) -> None:
    """Save cookies in JSON format (scansci-pdf compatible)."""
    cookie_data = [
        {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
        for c in cookies
    ]
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    cookie_file.write_text(
        json.dumps(cookie_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _save_cookies_netscape(cookies: list[dict[str, Any]], cookie_file: Path) -> None:
    """Save cookies in Netscape format (CloakBrowser import compatible)."""
    from .browser_cookies import cookies_to_netscape
    cookie_file.write_text(cookies_to_netscape(cookies), encoding="utf-8")


def _import_to_browser(cookie_file: Path, config: dict[str, Any]) -> int:
    """Import cookies into CloakBrowser. Returns count imported."""
    try:
        from .browser_engine import import_cookies, is_available
        if not is_available(config):
            log.info("   [browser] CloakBrowser not running, skipping auto-import")
            return 0
        count = import_cookies(cookie_file, config)
        log.info(f"   [browser] Imported {count} cookies into CloakBrowser")
        return count
    except Exception as exc:
        log.info(f"   [browser] Could not auto-import to CloakBrowser: {exc}")
        return 0


def open_login_browser(
    url: str,
    config: dict[str, Any],
    *,
    cookie_file: Path,
    detect_login: Any = None,
    max_wait: int = 300,
    auto_import: bool = True,
    keep_alive: bool = False,
    publisher: str = "",
) -> bool | tuple[bool, Any, Any, Any]:
    """Open a visible stealth browser for interactive login.

    Args:
        url: Login URL to open.
        config: scansci-pdf config dict.
        cookie_file: Path to save captured cookies (JSON).
        detect_login: Optional callable(browser_context, page) -> bool for custom login detection.
        max_wait: Max seconds to wait for login.
        auto_import: Whether to auto-import cookies into CloakBrowser.
        keep_alive: If True, return (True, browser, context, page) without closing browser.
        publisher: Publisher name for remote assist display.

    Returns:
        True if login succeeded, or (True, browser, context, page) if keep_alive.
    """
    log.info(f"   [browser] Opening stealth browser: {url}")
    print(f"\n  请在浏览器中登录 ({url})")
    print("  程序会自动检测登录完成...\n")

    if not _HAS_CLOAKBROWSER:
        log.info("   [browser] cloakbrowser not installed")
        return (False, None, None, None) if keep_alive else False

    remote = None
    browser = None
    slot_lease = None
    ownership_transferred = False
    try:
        if int(config.get("remote_assist_port", 0)) > 0:
            from .remote_assist import RemoteAssist
            remote = RemoteAssist(config, publisher=publisher)
            remote.start()
            remote.update_url(url)

        from . import browser_engine
        slot_lease = browser_engine._retain_browser_slot(config)
        raw_browser = launch_with_driver_cleanup(
            launch,
            headless=False,
            humanize=True,
            args=["--disable-features=CrossOriginOpenerPolicy"],
        )
        browser = browser_engine._LeasedBrowser(raw_browser, slot_lease)
        slot_lease = None
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            log.info(f"   [browser] Page load warning: {exc}")
            print("  页面加载超时，但仍可手动登录。")

        elapsed = 0
        while elapsed < max_wait:
            time.sleep(3)
            elapsed += 3

            try:
                current_url = page.url
                if remote:
                    remote.update_url(current_url)
            except Exception:
                log.info("   [browser] Browser closed by user.")
                return (False, None, None, None) if keep_alive else False

            if detect_login and detect_login(context, page):
                cookies = context.cookies()
                _save_cookies_json(cookies, cookie_file)
                netscape_path = cookie_file.with_suffix(".txt")
                _save_cookies_netscape(cookies, netscape_path)
                log.info(f"   [browser] Login successful! Saved {len(cookies)} cookies.")
                print(f"  登录成功！Cookie 已保存至 {cookie_file}")
                if auto_import:
                    _import_to_browser(netscape_path, config)
                if keep_alive:
                    ownership_transferred = True
                    return True, browser, context, page
                return True

            url_lower = current_url.lower()
            if "login" not in url_lower and "cas" not in url_lower and "sso" not in url_lower:
                cookies = context.cookies()
                if len(cookies) > 3:
                    _save_cookies_json(cookies, cookie_file)
                    netscape_path = cookie_file.with_suffix(".txt")
                    _save_cookies_netscape(cookies, netscape_path)
                    log.info(f"   [browser] Login successful! Saved {len(cookies)} cookies.")
                    print(f"  登录成功！Cookie 已保存至 {cookie_file}")
                    if auto_import:
                        _import_to_browser(netscape_path, config)
                    if keep_alive:
                        ownership_transferred = True
                        return True, browser, context, page
                    return True

        print("  登录超时。")
        return (False, None, None, None) if keep_alive else False

    except Exception as exc:
        log.info(f"   [browser] Login error: {exc}")
        print(f"  登录出错: {exc}")
        return (False, None, None, None) if keep_alive else False
    finally:
        if remote is not None:
            try:
                remote.stop()
            except Exception:
                pass
        if browser is not None and not ownership_transferred:
            try:
                browser.close()
            except Exception as exc:
                log.info(
                    "   [browser] Login browser close failed; retaining global slot: "
                    f"{exc}"
                )
        elif slot_lease is not None:
            slot_lease.close()


def webvpn_login(config: dict[str, Any]) -> bool:
    """Login to WebVPN via stealth browser."""
    from .sources.instsci import _get_webvpn_base
    base = _get_webvpn_base(config)
    if not base:
        log.info("   [WebVPN] No base URL configured")
        return False

    from .config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    cookie_file = cache_dir / "instsci_cookies.json"

    return open_login_browser(base, config, cookie_file=cookie_file, max_wait=600)


def carsi_login(publisher: str, config: dict[str, Any], *, login_url: str, domains: list[str]) -> bool:
    """Login to CARSI institutional access via stealth browser."""
    from .config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / "carsi_cookies"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cookie_file = cache_dir / f"{publisher}.json"

    def _detect(context: Any, page: Any) -> bool:
        try:
            current_url = page.url
            on_publisher = any(d in current_url for d in domains)
            on_login = any(x in current_url.lower() for x in ("login", "institutional", "wayf", "saml", "cas", "idp"))
            return on_publisher and not on_login
        except Exception:
            return False

    return open_login_browser(
        login_url,
        config,
        cookie_file=cookie_file,
        detect_login=_detect,
        max_wait=180,
    )


def ezproxy_login(config: dict[str, Any]) -> bool:
    """Login to EZProxy via stealth browser."""
    base = config.get("ezproxy_login_url", "")
    if not base:
        log.info("   [EZProxy] No ezproxy_login_url configured")
        return False

    from .config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    cookie_file = cache_dir / "ezproxy_cookies.json"

    login_url = base.replace("{url}", "https://www.sciencedirect.com")

    def _detect(context: Any, page: Any) -> bool:
        try:
            current_url = page.url
            return "libproxy" not in current_url.lower() and "login" not in current_url.lower()
        except Exception:
            return False

    return open_login_browser(
        login_url,
        config,
        cookie_file=cookie_file,
        detect_login=_detect,
        max_wait=180,
    )
