"""Institutional login via camoufox (stealth browser). Replaces Selenium for WebVPN/CARSI/EZProxy login."""

from __future__ import annotations

import json
import time
import atexit
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger()


class PersistentBrowser:
    """Keeps a Camoufox browser alive across multiple operations.

    Login once, reuse the same browser for all subsequent downloads.
    The WebVPN session stays valid because the browser instance never closes.
    """

    def __init__(self):
        self._camofox = None
        self._browser = None
        self._context = None
        self._page = None
        self._cookies_saved = False

    @property
    def is_alive(self) -> bool:
        if self._browser is None:
            return False
        try:
            # Test if browser is still responsive
            self._page.url  # noqa: B018
            return True
        except Exception:
            self._cleanup()
            return False

    def get_page(self, config: dict[str, Any] | None = None):
        """Get or create the browser page. Returns (context, page)."""
        if self.is_alive:
            return self._context, self._page
        return self._start(config)

    def _start(self, config: dict[str, Any] | None = None):
        """Start a new Camoufox browser instance. Restores saved state if available."""
        try:
            from camoufox.sync_api import Camoufox
            from camoufox.addons import DefaultAddons
        except ImportError:
            raise RuntimeError("camoufox not installed")

        log.info("   [camofox] Starting persistent browser...")
        self._camofox = Camoufox(headless=False, exclude_addons=[DefaultAddons.UBO])
        self._browser = self._camofox.start()
        self._context = self._browser.new_context()
        self._page = self._context.new_page()

        # Restore saved state (cookies + localStorage)
        if config:
            self._restore_state(config)

        return self._context, self._page

    def _restore_state(self, config: dict[str, Any]):
        """Restore saved cookies and localStorage into the browser."""
        from .config import DATA_DIR
        cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
        state_file = cache_dir / "browser_state.json"
        if not state_file.exists():
            return
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            log.info("   [camofox] browser_state.json corrupted, starting fresh")
            return

        # Restore cookies
        cookies = state.get("cookies", [])
        if cookies:
            try:
                self._context.add_cookies(cookies)
                log.info(f"   [camofox] Restored {len(cookies)} cookies")
            except Exception as e:
                log.info(f"   [camofox] Cookie restore warning: {e}")

        # Restore localStorage (need to navigate to each domain first)
        storage = state.get("localStorage", {})
        for origin, items in storage.items():
            try:
                self._page.goto(origin, wait_until="commit", timeout=10000)
                for key, value in items.items():
                    self._page.evaluate(f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)})")
            except Exception:
                pass

        log.info(f"   [camofox] Browser state restored")

    def save_cookies(self, config: dict[str, Any]):
        """Save current browser state (cookies + localStorage) to disk."""
        if not self._context:
            return
        try:
            from .config import DATA_DIR
            cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
            cache_dir.mkdir(parents=True, exist_ok=True)

            cookies = self._context.cookies()

            # Collect localStorage from all pages
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

            # Save combined state
            state = {"cookies": cookies, "localStorage": localStorage}
            state_file = cache_dir / "browser_state.json"
            state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

            # Also save legacy formats for compatibility
            cookie_file = cache_dir / "vpnsci-cookies.json"
            cookie_data = [
                {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
                for c in cookies
            ]
            cookie_file.write_text(json.dumps(cookie_data, indent=2, ensure_ascii=False), encoding="utf-8")

            netscape_file = cache_dir / "vpnsci-cookies.txt"
            from .browser_cookies import cookies_to_netscape
            netscape_file.write_text(cookies_to_netscape(cookies), encoding="utf-8")

            self._cookies_saved = True
            log.info(f"   [camofox] Saved {len(cookies)} cookies + {len(localStorage)} localStorage origins")
        except Exception as e:
            log.info(f"   [camofox] Failed to save state: {e}")

    def _cleanup(self):
        """Close browser gracefully."""
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._camofox:
                self._camofox.__exit__(None, None, None)
        except Exception:
            pass
        self._camofox = None
        self._browser = None
        self._context = None
        self._page = None

    def close(self):
        """Explicitly close the browser."""
        self._cleanup()
        log.info("   [camofox] Persistent browser closed")


# Module-level singleton
_browser = PersistentBrowser()
atexit.register(_browser.close)


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
    """Save cookies in Netscape format (camofox-browser import compatible)."""
    from .browser_cookies import cookies_to_netscape
    cookie_file.write_text(cookies_to_netscape(cookies), encoding="utf-8")


def _import_to_camofox_browser(cookie_file: Path, config: dict[str, Any]) -> int:
    """Import cookies into camofox-browser. Returns count imported."""
    try:
        from .camofox import import_cookies, is_available
        if not is_available(config):
            log.info("   [camofox] camofox-browser not running, skipping auto-import")
            return 0
        count = import_cookies(cookie_file, config)
        log.info(f"   [camofox] Imported {count} cookies into camofox-browser")
        return count
    except Exception as exc:
        log.info(f"   [camofox] Could not auto-import to camofox-browser: {exc}")
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
) -> bool | tuple[bool, Any, Any, Any]:
    """Open a visible camoufox browser for interactive login.

    Args:
        url: Login URL to open.
        config: scansci-pdf config dict.
        cookie_file: Path to save captured cookies (JSON).
        detect_login: Optional callable(browser_context, page) -> bool for custom login detection.
        max_wait: Max seconds to wait for login.
        auto_import: Whether to auto-import cookies into camofox-browser.
        keep_alive: If True, return (True, camofox, context, page) without closing browser.

    Returns:
        True if login succeeded, or (True, camofox, context, page) if keep_alive.
    """
    try:
        from camoufox.sync_api import Camoufox
        from camoufox.addons import DefaultAddons
    except ImportError:
        log.info("   [camofox] camoufox not installed. Run: pip install camoufox")
        return (False, None, None, None) if keep_alive else False

    log.info(f"   [camofox] Opening stealth browser: {url}")
    print(f"\n  请在浏览器中登录 ({url})")
    print("  程序会自动检测登录完成...\n")

    try:
        camofox_inst = Camoufox(headless=False, exclude_addons=[DefaultAddons.UBO])
        browser = camofox_inst.start()
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            log.info(f"   [camofox] Page load warning: {exc}")
            print("  页面加载超时，但仍可手动登录。")

        # Poll for login completion
        elapsed = 0
        while elapsed < max_wait:
            time.sleep(3)
            elapsed += 3

            try:
                current_url = page.url
            except Exception:
                log.info("   [camofox] Browser closed by user.")
                if not keep_alive:
                    try:
                        browser.close()
                        camofox_inst.__exit__(None, None, None)
                    except Exception:
                        pass
                return (False, None, None, None) if keep_alive else False

            # Custom detection
            if detect_login and detect_login(context, page):
                cookies = context.cookies()
                _save_cookies_json(cookies, cookie_file)
                netscape_path = cookie_file.with_suffix(".txt")
                _save_cookies_netscape(cookies, netscape_path)
                log.info(f"   [camofox] Login successful! Saved {len(cookies)} cookies.")
                print(f"  登录成功！Cookie 已保存至 {cookie_file}")
                if auto_import:
                    _import_to_camofox_browser(netscape_path, config)
                if keep_alive:
                    return True, camofox_inst, context, page
                browser.close()
                camofox_inst.__exit__(None, None, None)
                return True

            # Default detection: check if URL changed away from login pages
            url_lower = current_url.lower()
            if "login" not in url_lower and "cas" not in url_lower and "sso" not in url_lower:
                # Check if we have meaningful cookies
                cookies = context.cookies()
                if len(cookies) > 3:
                    _save_cookies_json(cookies, cookie_file)
                    netscape_path = cookie_file.with_suffix(".txt")
                    _save_cookies_netscape(cookies, netscape_path)
                    log.info(f"   [camofox] Login successful! Saved {len(cookies)} cookies.")
                    print(f"  登录成功！Cookie 已保存至 {cookie_file}")
                    if auto_import:
                        _import_to_camofox_browser(netscape_path, config)
                    if keep_alive:
                        return True, camofox_inst, context, page
                    browser.close()
                    camofox_inst.__exit__(None, None, None)
                    return True

        print("  登录超时。")
        if not keep_alive:
            try:
                browser.close()
                camofox_inst.__exit__(None, None, None)
            except Exception:
                pass
        return (False, None, None, None) if keep_alive else False

    except Exception as exc:
        log.info(f"   [camofox] Login error: {exc}")
        print(f"  登录出错: {exc}")
        return (False, None, None, None) if keep_alive else False


def webvpn_login(config: dict[str, Any]) -> bool:
    """Login to WebVPN via camoufox browser."""
    from .sources.vpnsci import _get_webvpn_base
    base = _get_webvpn_base(config)
    if not base:
        log.info("   [WebVPN] No base URL configured")
        return False

    from .config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    cookie_file = cache_dir / "vpnsci_cookies.json"

    return open_login_browser(base, config, cookie_file=cookie_file, max_wait=600)


def carsi_login(publisher: str, config: dict[str, Any], *, login_url: str, domains: list[str]) -> bool:
    """Login to CARSI institutional access via camoufox browser."""
    from .config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    cookie_file = cache_dir / f"carsi_{publisher}_cookies.json"

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
    """Login to EZProxy via camoufox browser."""
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
