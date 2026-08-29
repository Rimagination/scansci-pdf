"""Campus institutional access authentication management using CloakBrowser."""

import binascii
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from Crypto.Cipher import AES

from .session_store import CookieStore

try:
    from .browser_backend import launch
    from .browser_backend import is_available as _browser_backend_available
    _HAS_CLOAKBROWSER = _browser_backend_available()
except ImportError:
    launch = None  # type: ignore[assignment]
    _HAS_CLOAKBROWSER = False

logger = logging.getLogger(__name__)

TEST_URL = "https://www.nature.com"
WEBVPN_DEFAULT_KEY = b"wrdvpnisthebest!"


def _get_cookie_path(config: dict) -> Path:
    """Resolve cookie file path from config."""
    explicit = config.get("instsci_cookie_file", "")
    if explicit:
        return Path(explicit)
    data_dir = Path(os.environ.get("SCANSCI_PDF_DATA_DIR", str(Path.home() / ".scansci-pdf")))
    return data_dir / "cookies" / "webvpn-cookies.json"


def _get_profile_dir(config: dict) -> Path:
    """Resolve Chrome profile directory from config."""
    explicit = config.get("chrome_profile_dir", "")
    if explicit:
        return Path(explicit)
    data_dir = Path(os.environ.get("SCANSCI_PDF_DATA_DIR", str(Path.home() / ".scansci-pdf")))
    return data_dir / "browser_profiles" / "webvpn"


class WebVPNAuth:
    """Manages campus access authentication and URL conversion.

    Supports Chinese university campus gateway systems.
    URL conversion uses AES-CFB encryption on the hostname.
    """

    def __init__(
        self,
        config: dict,
        key: bytes | None = None,
        iv: bytes | None = None,
    ):
        self.config = config
        self._encrypt_key = key or WEBVPN_DEFAULT_KEY
        self._encrypt_iv = iv or self._encrypt_key
        self._session: requests.Session | None = None
        self._browser = None
        self._context = None
        self._page = None
        base = config.get("instsci_base_url", "")
        self._webvpn_base = base.rstrip("/") if base else ""

    @property
    def browser_context(self):
        return self._context

    @property
    def browser_page(self):
        return self._page

    def _browser_launch_args(self) -> list[str]:
        return [
            "--no-proxy-server",
            "--disable-features=CrossOriginOpenerPolicy",
        ]

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
            if os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY") or \
               os.environ.get("http_proxy") or os.environ.get("https_proxy"):
                self._session.verify = False
            proxy = self.config.get("network_proxy", "")
            if proxy:
                self._session.proxies = {
                    "http": proxy,
                    "https": proxy,
                }
                logger.info("Using connector: %s", proxy)
        return self._session

    def convert_url(self, url: str) -> str:
        """Convert a regular URL to a campus gateway URL using AES-CFB encryption."""
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
        path = parsed.path
        query = parsed.query

        if not hostname:
            return url

        cipher = AES.new(self._encrypt_key, AES.MODE_CFB, self._encrypt_iv, segment_size=128)
        encrypted = cipher.encrypt(hostname.encode("utf-8"))

        encrypted_hex = binascii.hexlify(self._encrypt_iv).decode() + binascii.hexlify(encrypted).decode()

        scheme_part = scheme
        if port:
            scheme_part = f"{scheme}-{port}"

        result = f"{self._webvpn_base}/{scheme_part}/{encrypted_hex}{path}"
        if query:
            result += f"?{query}"
        return result

    def login(self, force: bool = False) -> bool:
        """Ensure we have a valid session."""
        proxy = self.config.get("network_proxy", "")
        if proxy:
            logger.info("Connector mode: skipping login (connector handles auth).")
            return True

        if not force and self._try_load_cookies():
            logger.info("Loaded saved cookies - session is valid.")
            return True

        logger.info("No valid session found. Opening browser for login...")
        return self._browser_login()

    def _try_load_cookies(self) -> bool:
        cookie_path = _get_cookie_path(self.config)
        if not CookieStore(cookie_path).load_into(self.session):
            logger.info("All saved cookies have expired.")
            return False
        return self._validate_session()

    def _validate_session(self) -> bool:
        proxy = self.config.get("network_proxy", "")
        if proxy:
            test_url = TEST_URL
        else:
            test_url = self.convert_url(TEST_URL)
        try:
            resp = self.session.get(test_url, timeout=15, allow_redirects=True)
            if "cas" in resp.url.lower() or "login" in resp.url.lower():
                logger.info("Session expired - redirected to login page.")
                return False
            if resp.status_code == 200:
                return True
        except requests.RequestException as e:
            logger.warning("Session validation failed: %s", e)
        return False

    def _browser_login(self) -> bool:
        if not _HAS_CLOAKBROWSER:
            logger.error("no browser backend available. Run: pip install patchright (or pip install cloakbrowser)")
            return False

        try:
            from .browser_backend import launch_persistent_context
            profile_dir = _get_profile_dir(self.config)
            profile_dir.mkdir(parents=True, exist_ok=True)
            self._context = launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False, humanize=True,
                args=self._browser_launch_args(),
            )
            self._browser = None
            self._page = self._context.new_page()
        except Exception as e:
            logger.error("Failed to start CloakBrowser: %s", e)
            return False

        if not self._webvpn_base:
            logger.error(
                "WebVPN base URL is empty — school gateway unknown. "
                "Run `scansci-pdf setup <学校全名>` first and check `scansci-pdf schools`."
            )
            return False

        self._page.goto(self._webvpn_base, wait_until="networkidle", timeout=30000)
        current_url = self._page.url
        logger.info("Session test: navigated to campus gateway, landed on %s", current_url[:80])

        parsed = urlparse(current_url)
        url_host = (parsed.hostname or "").lower()
        on_login_page = (
            "cas" in current_url.lower()
            or "login" in current_url.lower()
            or "/oauth/" in current_url.lower()
            or "/sso/" in current_url.lower()
            or "/wayf" in current_url.lower()
            or "/shibboleth" in current_url.lower()
        )
        is_idp = "id.tsinghua" in url_host or "idp." in url_host or "auth." in url_host

        if not on_login_page and not is_idp:
            logger.info("Persistent context has valid session! URL=%s", current_url[:60])
            self._save_browser_cookies()
            return True

        self._page.goto(self._webvpn_base, wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print(f"  Please log in at {self._webvpn_base}")
        print("  in the browser window that just opened.")
        print("  The tool will detect when login is complete.")
        print("=" * 60 + "\n")

        max_wait = 600
        poll_interval = 3
        elapsed = 0
        last_url = ""

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            try:
                if not self._context.pages:
                    logger.info("Browser closed by user.")
                    self._browser = None
                    self._context = None
                    self._page = None
                    return False

                current_url = self._page.url

                if current_url != last_url:
                    logger.info("Browser URL: %s", current_url)
                    last_url = current_url

                cookies = self._context.cookies()
                vpn_cookies = [
                    c for c in cookies
                    if "webvpn" in c.get("domain", "").lower()
                    and c.get("name", "").startswith("wengine_vpn_ticket")
                ]
                if vpn_cookies:
                    parsed_url = urlparse(current_url)
                    url_host = (parsed_url.hostname or "").lower()
                    on_webvpn = url_host and "webvpn" in url_host
                    if on_webvpn:
                        logger.info("Login confirmed: campus session cookie and gateway URL. URL=%s", current_url[:60])
                        self._save_browser_cookies()
                        print("\n  Login successful! Cookies saved. Browser kept alive for PDF download.\n")
                        return True

                on_login_page = (
                    "/login" in current_url.lower()
                    or "cas" in current_url.lower()
                    or "/oauth/" in current_url.lower()
                    or "/sso/" in current_url.lower()
                    or "/wayf" in current_url.lower()
                    or "/shibboleth" in current_url.lower()
                )
                if not on_login_page:
                    is_gateway = (
                        self._webvpn_base in current_url
                        or "otrust" in current_url.lower()
                        or "/portal/" in current_url.lower()
                    )
                    if is_gateway:
                        logger.info("Login detected via URL! (url=%s)", current_url[:60])
                        self._save_browser_cookies()
                        print("\n  Login successful! Cookies saved. Browser kept alive for PDF download.\n")
                        return True

            except Exception:
                logger.warning("Browser connection lost.")
                self._browser = None
                self._context = None
                self._page = None
                return False

        print("\n  Login timed out after 10 minutes.\n")
        self._close_browser()
        return False

    def _save_browser_cookies(self):
        if not self._context:
            return
        cookie_path = _get_cookie_path(self.config)
        store = CookieStore(cookie_path)
        cookies = store.save(self._context.cookies())
        logger.info("Saved %d cookies to %s", len(cookies), store.path)
        store.apply_to_session(self.session, cookies)

    def _close_browser(self):
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        self._browser = None
        self._context = None
        self._page = None

    def fetch(self, url: str, **kwargs) -> requests.Response:
        """Fetch a URL through the campus, EasyConnect, or connector session."""
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)

        proxy = self.config.get("network_proxy", "")
        if proxy:
            return self.session.get(url, **kwargs)

        if self._webvpn_base in url:
            proxied = url
        else:
            proxied = self.convert_url(url)

        return self.session.get(proxied, **kwargs)

    def close(self):
        self._close_browser()
        if self._session:
            self._session.close()
            self._session = None


class EZProxyAuth:
    """Manages EZproxy authentication and URL proxying."""

    def __init__(
        self,
        config: dict,
        proxy_base: str = "",
    ):
        self.config = config
        self._proxy_base = proxy_base or config.get("ezproxy_login_url", "")
        self._session: requests.Session | None = None
        self._browser = None
        self._context = None
        self._page = None

    @property
    def browser_context(self):
        return self._context

    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = requests.Session()
            self._session.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
        return self._session

    def login(self, force: bool = False) -> bool:
        if not force and self._try_load_cookies():
            logger.info("Loaded saved EZproxy cookies.")
            return True
        logger.info("No valid EZproxy session. Opening browser for login...")
        return self._browser_login()

    def _try_load_cookies(self) -> bool:
        cookie_path = _get_cookie_path(self.config)
        if not CookieStore(cookie_path).load_into(self.session):
            return False
        return self._validate_session()

    def _validate_session(self) -> bool:
        try:
            resp = self.session.get(self._proxy_base + TEST_URL, timeout=15, allow_redirects=True)
            if "login" in resp.url.lower() or "cas" in resp.url.lower():
                return False
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _browser_login(self) -> bool:
        if not _HAS_CLOAKBROWSER:
            logger.error("no browser backend available. Run: pip install patchright (or pip install cloakbrowser)")
            return False

        try:
            self._browser = launch(
                headless=False, humanize=True,
                args=self._browser_launch_args(),
            )
            self._context = self._browser.new_context()
            self._page = self._context.new_page()
        except Exception as e:
            logger.error("Failed to start CloakBrowser: %s", e)
            return False

        self._page.goto(self._proxy_base + TEST_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print(f"  Please log in at the EZproxy page.")
        print("  The tool will detect when login is complete.")
        print("=" * 60 + "\n")

        max_wait = 600
        poll_interval = 3
        elapsed = 0
        last_url = ""

        while elapsed < max_wait:
            time.sleep(poll_interval)
            elapsed += poll_interval

            try:
                if not self._context.pages:
                    logger.info("Browser closed by user.")
                    self._browser = None
                    self._context = None
                    self._page = None
                    return False

                current_url = self._page.url

                if current_url != last_url:
                    logger.info("Browser URL: %s", current_url)
                    last_url = current_url

                on_login = "login" in current_url.lower() or "cas" in current_url.lower()
                if not on_login and self._proxy_base not in current_url:
                    logger.info("EZproxy login detected! URL: %s", current_url)
                    self._save_browser_cookies()
                    print("\n  Login successful! Cookies saved. Browser kept alive for PDF download.\n")
                    return True

            except Exception:
                logger.warning("Browser connection lost.")
                self._browser = None
                self._context = None
                self._page = None
                return False

        print("\n  Login timed out after 10 minutes.\n")
        self._close_browser()
        return False

    def _save_browser_cookies(self):
        if not self._context:
            return
        cookie_path = _get_cookie_path(self.config)
        store = CookieStore(cookie_path)
        cookies = store.save(self._context.cookies())
        logger.info("Saved %d cookies to %s", len(cookies), store.path)
        store.apply_to_session(self.session, cookies)

    def _close_browser(self):
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
            self._context = None
            self._page = None

    def get_proxied_url(self, url: str) -> str:
        if self._proxy_base and self._proxy_base.rstrip("/").split("//")[-1].split("/")[0] in url:
            return url
        return self._proxy_base + url

    def fetch(self, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        proxied = self.get_proxied_url(url)
        return self.session.get(proxied, **kwargs)

    def close(self):
        self._close_browser()
        if self._session:
            self._session.close()
            self._session = None
