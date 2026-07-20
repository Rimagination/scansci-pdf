"""Institutional login via stealth browser. Replaces Selenium for WebVPN/CARSI/EZProxy login."""

from __future__ import annotations

import json
import time
import atexit
from pathlib import Path
from typing import Any

try:
    from cloakbrowser import launch
    _HAS_CLOAKBROWSER = True
except ImportError:
    launch = None  # type: ignore[assignment]
    _HAS_CLOAKBROWSER = False
from .log import get_logger
from .private_files import atomic_write_private

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

    @property
    def is_alive(self) -> bool:
        if self._browser is None:
            return False
        try:
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
        """Start a new browser instance. Restores saved state if available."""
        if not _HAS_CLOAKBROWSER:
            raise RuntimeError("cloakbrowser not installed. Run: pip install cloakbrowser")
        log.info("   [browser] Starting persistent browser...")
        self._browser = launch(
            headless=False, humanize=True,
            args=["--disable-features=CrossOriginOpenerPolicy"],
        )
        self._context = self._browser.new_context()
        self._page = self._context.new_page()

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
            log.info("   [browser] browser_state.json corrupted, starting fresh")
            return

        cookies = state.get("cookies", [])
        if cookies:
            try:
                self._context.add_cookies(cookies)
                log.info(f"   [browser] Restored {len(cookies)} cookies")
            except Exception as e:
                log.info(f"   [browser] Cookie restore warning: {e}")

        storage = state.get("localStorage", {})
        for origin, items in storage.items():
            try:
                self._page.goto(origin, wait_until="commit", timeout=10000)
                for key, value in items.items():
                    self._page.evaluate(f"localStorage.setItem({json.dumps(key)}, {json.dumps(value)})")
            except Exception as e:
                log.info(f"   [browser] localStorage restore failed for {origin}: {e}")

        log.info("   [browser] Browser state restored")

    def save_cookies(self, config: dict[str, Any]):
        """Save current browser state (cookies + localStorage) to disk."""
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
            atomic_write_private(
                state_file,
                json.dumps(state, indent=2, ensure_ascii=False),
            )

            cookie_file = cache_dir / "instsci-cookies.json"
            cookie_data = [
                {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
                for c in cookies
            ]
            atomic_write_private(
                cookie_file,
                json.dumps(cookie_data, indent=2, ensure_ascii=False),
            )

            netscape_file = cache_dir / "instsci-cookies.txt"
            from .browser_cookies import cookies_to_netscape
            atomic_write_private(netscape_file, cookies_to_netscape(cookies))

            self._cookies_saved = True
            log.info(f"   [browser] Saved {len(cookies)} cookies + {len(localStorage)} localStorage origins")
        except Exception as e:
            log.info(f"   [browser] Failed to save state: {e}")

    def _cleanup(self):
        """Close browser gracefully."""
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        self._browser = None
        self._context = None
        self._page = None

    def close(self):
        """Explicitly close the browser."""
        self._cleanup()
        log.info("   [browser] Persistent browser closed")


# Module-level singleton
_browser = PersistentBrowser()
# Logging streams may already be closed when atexit handlers run.
atexit.register(_browser._cleanup)


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
    atomic_write_private(
        cookie_file,
        json.dumps(cookie_data, indent=2, ensure_ascii=False),
    )


def _save_cookies_netscape(cookies: list[dict[str, Any]], cookie_file: Path) -> None:
    """Save cookies in Netscape format (CloakBrowser import compatible)."""
    from .browser_cookies import cookies_to_netscape
    atomic_write_private(cookie_file, cookies_to_netscape(cookies))


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
    manual_confirm: bool = False,
) -> bool | tuple[bool, Any, Any, Any]:
    """Open a visible stealth browser for interactive login.

    Args:
        url: Login URL to open.
        config: scansci-pdf config dict.
        cookie_file: Path to save captured cookies (JSON).
        detect_login: Optional callable(browser_context, page) -> bool for custom login detection.
        max_wait: Max seconds to wait for login.
        auto_import: Whether to auto-import cookies into CloakBrowser.
        keep_alive: If True, return (True, context, page) without closing browser.
        publisher: Publisher name for remote assist display.
        manual_confirm: Wait for Enter and save the whole browser context instead
            of relying on URL-based login detection.

    Returns:
        True if login succeeded, or (True, context, page) if keep_alive.
    """
    log.info(f"   [browser] Opening stealth browser: {url}")
    print(f"\n  请在浏览器中登录 ({url})")
    print("  程序会自动检测登录完成...\n")

    # Start remote assist if port is configured
    remote = None
    if int(config.get("remote_assist_port", 0)) > 0:
        from .remote_assist import RemoteAssist
        remote = RemoteAssist(config, publisher=publisher)
        remote.start()
        remote.update_url(url)

    if not _HAS_CLOAKBROWSER:
        log.info("   [browser] cloakbrowser not installed")
        return (False, None, None, None) if keep_alive else False

    try:
        browser = launch(headless=False, humanize=True,
                         args=["--disable-features=CrossOriginOpenerPolicy"])
        context = browser.new_context()
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            log.info(f"   [browser] Page load warning: {exc}")
            print("  页面加载超时，但仍可手动登录。")

        if manual_confirm:
            print("  登录完成后，请返回此终端并按 Enter 保存 Cookie。")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                if remote:
                    remote.stop()
                browser.close()
                return (False, None, None, None) if keep_alive else False

            cookies = context.cookies()
            if not cookies:
                print("  未检测到 Cookie；请确认登录已完成。")
                if remote:
                    remote.stop()
                browser.close()
                return (False, None, None, None) if keep_alive else False

            _save_cookies_json(cookies, cookie_file)
            netscape_path = cookie_file.with_suffix(".txt")
            _save_cookies_netscape(cookies, netscape_path)
            log.info(f"   [browser] Login manually confirmed. Saved {len(cookies)} cookies.")
            print(f"  登录状态已确认！Cookie 已保存至 {cookie_file}")
            if auto_import:
                _import_to_browser(netscape_path, config)
            if remote:
                remote.stop()
            if keep_alive:
                return True, context, page
            browser.close()
            return True

        elapsed = 0
        while elapsed < max_wait:
            time.sleep(3)
            elapsed += 3

            try:
                # Some institutional login flows finish in a new tab instead
                # of navigating the page that initiated authentication. Check
                # every live HTTP(S) page, newest first, so redirects such as
                # an EZProxy redirect can be detected without asking the user
                # to move the successful URL back into the original tab.
                context_pages = list(context.pages)
                if page not in context_pages:
                    context_pages.insert(0, page)

                live_pages: list[tuple[Any, str]] = []
                for candidate in reversed(context_pages):
                    try:
                        candidate_url = candidate.url
                    except Exception:
                        continue
                    if candidate_url.startswith(("http://", "https://")):
                        live_pages.append((candidate, candidate_url))

                if not live_pages:
                    raise RuntimeError("No live browser pages")

                current_url = live_pages[0][1]
                if remote:
                    remote.update_url(current_url)
            except Exception:
                log.info("   [browser] Browser closed by user.")
                if remote:
                    remote.stop()
                if not keep_alive:
                    try:
                        browser.close()
                    except Exception:
                        pass
                return (False, None, None, None) if keep_alive else False

            successful_page = None
            if detect_login:
                for candidate, _candidate_url in live_pages:
                    try:
                        if detect_login(context, candidate):
                            successful_page = candidate
                            break
                    except Exception:
                        continue

            if successful_page is not None:
                cookies = context.cookies()
                _save_cookies_json(cookies, cookie_file)
                netscape_path = cookie_file.with_suffix(".txt")
                _save_cookies_netscape(cookies, netscape_path)
                log.info(f"   [browser] Login successful! Saved {len(cookies)} cookies.")
                print(f"  登录成功！Cookie 已保存至 {cookie_file}")
                if auto_import:
                    _import_to_browser(netscape_path, config)
                if remote:
                    remote.stop()
                if keep_alive:
                    return True, context, successful_page
                browser.close()
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
                    if remote:
                        remote.stop()
                    if keep_alive:
                        return True, context, page
                    browser.close()
                    return True

        print("  登录超时。")
        if remote:
            remote.stop()
        if not keep_alive:
            try:
                browser.close()
            except Exception:
                pass
        return (False, None, None, None) if keep_alive else False

    except Exception as exc:
        log.info(f"   [browser] Login error: {exc}")
        print(f"  登录出错: {exc}")
        return (False, None, None, None) if keep_alive else False


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


def ezproxy_login(config: dict[str, Any], *, manual_confirm: bool = False) -> bool:
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
        manual_confirm=manual_confirm,
    )
