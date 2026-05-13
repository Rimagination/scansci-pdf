"""WebVPN institutional proxy source (multi-university support).

Uses AES-CFB encrypted URL conversion to access papers through
Chinese university WebVPN systems. Supports 100+ schools with
per-school encryption keys.

Password safety: Login happens in your browser via CAS.
The code only stores session cookies, never your password.
"""

from __future__ import annotations

import binascii
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from ..log import get_logger
from ..pdf_utils import (
    _response_looks_pdf,
    extract_pdf_url_from_html,
    is_pdf_file,
    is_plausible_pdf_url,
    success,
)

# Import compiled core functions if available (Cython .pyd/.so)
try:
    from .._core.vpnsci_core import (
        convert_url as _convert_url_compiled,
        construct_publisher_pdf_url as _construct_publisher_pdf_url_compiled,
        find_pdf_link_in_html as _find_pdf_link_compiled,
    )
    _HAS_COMPILED_CORE = True
except ImportError:
    _HAS_COMPILED_CORE = False

log = get_logger()

# Rate limiting between WebVPN requests
_last_vpnsci_time = 0.0
_VPNSCI_DELAY_MIN = 2.0
_VPNSCI_DELAY_MAX = 5.0



def _vpnsci_rate_limit() -> None:
    global _last_vpnsci_time
    now = time.time()
    elapsed = now - _last_vpnsci_time
    delay = __import__("random").uniform(_VPNSCI_DELAY_MIN, _VPNSCI_DELAY_MAX)
    if elapsed < delay:
        time.sleep(delay - elapsed)
    _last_vpnsci_time = time.time()


def vpnsci_cookie_path(config: dict[str, Any]) -> Path:
    configured = config.get("vpnsci_cookie_file")
    if configured:
        return Path(configured).expanduser()
    from ..config import DEFAULT_CONFIG
    return Path(config.get("cache_dir", DEFAULT_CONFIG["cache_dir"])).expanduser() / "vpnsci-cookies.json"


def vpnsci_is_configured(config: dict[str, Any]) -> bool:
    return bool(config.get("vpnsci_enabled") and _get_webvpn_base(config))


def _get_webvpn_base(config: dict[str, Any]) -> str:
    """Get WebVPN base URL, resolving from school if needed."""
    base = config.get("vpnsci_base_url", "").strip()
    if base:
        return base.rstrip("/")
    school = config.get("vpnsci_school", "")
    if school:
        try:
            from ..schools import get_school
            entry = get_school(school)
            return entry.host.rstrip("/")
        except ValueError:
            pass
    return ""


def _get_aes():
    """Lazy import AES (pycryptodome may not be installed)."""
    try:
        from Crypto.Cipher import AES
        return AES
    except ImportError:
        try:
            from Cryptodome.Cipher import AES
            return AES
        except ImportError:
            raise ImportError(
                "pycryptodome required for WebVPN. Install: pip install pycryptodome"
            )


def _get_school_keys(config: dict[str, Any]) -> tuple[bytes, bytes]:
    """Get AES key and IV for the configured school."""
    default_key = b"wrdvpnisthebest!"
    school = config.get("vpnsci_school", "")
    if school:
        try:
            from ..schools import get_school
            entry = get_school(school)
            return entry.key, entry.iv
        except ValueError:
            pass
    return default_key, default_key


def convert_url(url: str, webvpn_base: str, config: dict[str, Any] | None = None) -> str:
    """Convert a regular URL to a WebVPN URL using AES-CFB encryption.

    Encrypts only the hostname; path and query are kept as-is.
    Uses per-school encryption keys when config is provided.
    """
    key, iv = _get_school_keys(config) if config else (b"wrdvpnisthebest!", b"wrdvpnisthebest!")

    if _HAS_COMPILED_CORE:
        return _convert_url_compiled(url, webvpn_base, key, iv)

    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname
    port = parsed.port
    path = parsed.path
    query = parsed.query

    if not hostname:
        return url

    AES = _get_aes()
    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    encrypted = cipher.encrypt(hostname.encode("utf-8"))

    encrypted_hex = binascii.hexlify(iv).decode() + binascii.hexlify(encrypted).decode()

    scheme_part = scheme
    if port:
        scheme_part = f"{scheme}-{port}"

    result = f"{webvpn_base.rstrip('/')}/{scheme_part}/{encrypted_hex}{path}"
    if query:
        result += f"?{query}"
    return result


def _load_cookies(config: dict[str, Any]) -> requests.cookies.RequestsCookieJar:
    path = vpnsci_cookie_path(config)
    jar = requests.cookies.RequestsCookieJar()
    if not path.exists():
        return jar
    try:
        cookies = json.loads(path.read_text(encoding="utf-8"))
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            if name and value is not None:
                kwargs: dict[str, Any] = {}
                if c.get("domain"):
                    kwargs["domain"] = c["domain"]
                if c.get("path"):
                    kwargs["path"] = c["path"]
                jar.set(name, value, **kwargs)
    except Exception:
        pass
    return jar


def _save_cookies(cookies: list[dict], config: dict[str, Any]) -> None:
    path = vpnsci_cookie_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info(f"   [WebVPN] Saved {len(cookies)} cookies")


def _validate_session(config: dict[str, Any]) -> bool:
    """Check if saved cookies still work."""
    from ..network import USER_AGENT
    jar = _load_cookies(config)
    if not jar:
        return False
    base = _get_webvpn_base(config)
    if not base:
        return False
    test_url = convert_url("https://www.nature.com", base, config)
    try:
        s = requests.Session()
        s.trust_env = False
        s.cookies.update(jar)
        resp = s.get(test_url, timeout=15, allow_redirects=True,
                     headers={"User-Agent": USER_AGENT})
        if "cas" in resp.url.lower() or "login" in resp.url.lower():
            return False
        return resp.status_code == 200
    except Exception:
        return False


def vpnsci_login(config: dict[str, Any]) -> bool:
    """Open browser for CAS login. Called from MCP tool, not interactively."""
    return _browser_login(config)


def _get_all_cookies(driver: Any) -> list[dict]:
    """Get ALL cookies from all domains via CDP (not just current domain)."""
    try:
        result = driver.execute_cdp_cmd("Network.getAllCookies", {})
        cookies = result.get("cookies", [])
        # Normalize CDP cookie format to match Selenium format
        normalized = []
        for c in cookies:
            normalized.append({
                "name": c.get("name", ""),
                "value": c.get("value", ""),
                "domain": c.get("domain", ""),
                "path": c.get("path", "/"),
                "secure": c.get("secure", False),
                "httpOnly": c.get("httpOnly", False),
            })
        return normalized
    except Exception:
        # Fallback to Selenium's get_cookies (current domain only)
        return driver.get_cookies()


def _browser_login(config: dict[str, Any]) -> bool:
    """Open browser for CAS login. Tries camoufox first, falls back to Selenium."""
    # Try camoufox (stealth browser) first
    try:
        from ..camofox_login import webvpn_login
        if webvpn_login(config):
            return True
    except Exception as exc:
        log.info(f"   [WebVPN] camoufox login failed: {exc}")

    # Fallback to Selenium
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except ImportError:
        log.info("   [WebVPN] selenium not installed. Run: pip install selenium")
        return False

    base = _get_webvpn_base(config)
    if not base:
        log.info("   [WebVPN] No base URL configured. Set vpnsci_school or vpnsci_base_url.")
        return False

    options = Options()
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--remote-allow-origins=*")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        log.warning(f"   [WebVPN] Chrome launch failed: {e}")
        return False

    driver.get(base)
    print(f"\n  请在浏览器中登录 WebVPN ({base})")
    print("  程序会自动检测登录完成...\n")

    max_wait = 600
    poll_interval = 3
    elapsed = 0

    while elapsed < max_wait:
        time.sleep(poll_interval)
        elapsed += poll_interval

        try:
            current_url = driver.current_url

            if base in current_url and "cas" not in current_url.lower() and "login" not in current_url.lower():
                log.info(f"   [WebVPN] Login detected: {current_url}")
                cookies = _get_all_cookies(driver)
                _save_cookies(cookies, config)
                driver.quit()
                print("  登录成功！Cookie 已保存。\n")
                return True

            vpn_cookies = [c for c in _get_all_cookies(driver)
                          if "webvpn" in c.get("domain", "").lower()
                          and c.get("name", "").startswith("wengine_vpn_ticket")]
            if vpn_cookies:
                cookies = _get_all_cookies(driver)
                _save_cookies(cookies, config)
                driver.quit()
                print("  登录成功！Cookie 已保存。\n")
                return True
        except Exception:
            pass

    print("  登录超时（10 分钟）。\n")
    try:
        driver.quit()
    except Exception:
        pass
    return False


def _fetch_via_webvpn(url: str, config: dict[str, Any], *, stream: bool = False) -> requests.Response:
    from ..network import USER_AGENT, request_timeout
    base = _get_webvpn_base(config)
    proxied = convert_url(url, base, config)

    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": USER_AGENT})
    s.cookies.update(_load_cookies(config))

    return s.get(proxied, timeout=request_timeout(config), allow_redirects=True, stream=stream)


def _resolve_doi_url(doi: str) -> str | None:
    """Resolve DOI to get the publisher URL."""
    try:
        resp = requests.get(
            f"https://doi.org/{doi}",
            allow_redirects=True,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            stream=True,
            verify=False,
        )
        resp.close()
        if resp.url and resp.url != f"https://doi.org/{doi}":
            return resp.url
    except Exception:
        pass
    return None


def _construct_publisher_pdf_url(doi: str, resolved_url: str) -> str | None:
    """Try to construct a direct publisher PDF URL from the resolved URL."""
    if _HAS_COMPILED_CORE:
        return _construct_publisher_pdf_url_compiled(doi, resolved_url)

    parsed = urllib.parse.urlparse(resolved_url)
    hostname = parsed.netloc.lower()
    doi_suffix = doi.split("/", 1)[-1] if "/" in doi else doi

    if "pubs.acs.org" in hostname:
        return f"https://pubs.acs.org/doi/pdf/{doi}"
    elif "onlinelibrary.wiley.com" in hostname:
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    elif "tandfonline.com" in hostname:
        return f"https://www.tandfonline.com/doi/pdf/{doi}?needAccess=true"
    elif "nature.com" in hostname:
        return f"https://www.nature.com/articles/{doi_suffix}.pdf"
    elif "link.springer.com" in hostname:
        return f"https://link.springer.com/content/pdf/{doi}.pdf"
    elif "pubs.rsc.org" in hostname:
        pdf_url = resolved_url.replace("/articlelanding/", "/articlepdf/")
        return pdf_url if pdf_url != resolved_url else None
    elif "elsevier.com" in hostname or "sciencedirect.com" in hostname:
        pii_match = re.search(r"pii/([A-Z0-9]+)", resolved_url)
        if pii_match:
            return f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}/pdfft"

    return None


def _find_pdf_link(html: str, base_url: str) -> str | None:
    """Find a PDF download link in an HTML page.

    Tries: citation_pdf_url meta, <a> tags with PDF text/class,
    and publisher-specific URL patterns.
    """
    if _HAS_COMPILED_CORE:
        return _find_pdf_link_compiled(html, base_url)

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    parsed = urllib.parse.urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    hostname = parsed.netloc.lower()

    # Strategy 1: <meta name="citation_pdf_url">
    meta_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta_pdf and meta_pdf.get("content"):
        pdf_url = meta_pdf["content"]
        if pdf_url.startswith("http"):
            return pdf_url
        return base + pdf_url

    # Strategy 2: <a> tags with PDF-related text/class/href
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        classes = " ".join(a.get("class", []))

        if any(kw in text for kw in ["pdf", "download pdf", "full text pdf", "view pdf", "get pdf"]):
            return _resolve_href(href, base)
        if any(kw in classes for kw in ["pdf", "download-pdf", "pdf-download", "article-pdf"]):
            return _resolve_href(href, base)
        if href.endswith(".pdf"):
            return _resolve_href(href, base)
        if "/doi/pdf/" in href or "/doi/pdfdirect/" in href:
            return _resolve_href(href, base)

    # Strategy 3: Publisher-specific URL patterns
    path = parsed.path
    if "pubs.acs.org" in hostname and "/doi/" in path and "/pdf/" not in path:
        doi_part = path.split("/doi/")[-1]
        if doi_part:
            return f"{base}/doi/pdf/{doi_part}"

    if "onlinelibrary.wiley.com" in hostname and "/doi/" in path and "/pdfdirect/" not in path:
        doi_part = path.split("/doi/")[-1]
        if doi_part:
            return f"{base}/doi/pdfdirect/{doi_part}"

    if "pubs.rsc.org" in hostname and "/articlelanding/" in path:
        return base_url.replace("/articlelanding/", "/articlepdf/")

    if "tandfonline.com" in hostname and "/doi/" in path and "/pdf/" not in path:
        doi_part = re.sub(r"/doi/(?:full|abs)/", "/doi/pdf/", path)
        if doi_part != path:
            return f"{base}{doi_part}"

    if ("elsevier.com" in hostname or "sciencedirect.com" in hostname):
        pii_match = re.search(r"pii/([A-Z0-9]+)", path)
        if pii_match:
            return f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}/pdfft"

    return None


def _resolve_href(href: str, base: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base + href
    return base + "/" + href


def _is_login_page(url: str, config: dict[str, Any] | None = None) -> bool:
    """Check if URL indicates a login/CAS page (not yet authenticated)."""
    lower = url.lower()
    # If on the WebVPN itself (not login subpage), we're authenticated
    if "webvpn." in lower and "/login" not in lower:
        return False
    # If on auth check page (2FA), user is actively authenticating — treat as login
    keywords = ["cas", "sso", "/do/off/ui/auth"]
    if config:
        from ..publisher_strategies import _school_auth_patterns
        keywords.extend(_school_auth_patterns(config))
    return any(x in lower for x in keywords)


def _is_inline_pdf_page(page: Any) -> bool:
    """Check if the page is displaying an inline PDF."""
    try:
        url = page.url.lower()
        if url.endswith(".pdf"):
            return True
        # Check for PDF embed/object
        has_embed = page.evaluate("""
            (() => {
                const e = document.querySelector('embed[type="application/pdf"], object[type="application/pdf"], iframe[src*=".pdf"]');
                return !!e;
            })()
        """)
        if has_embed:
            return True
        # Check if page content starts with %PDF
        content = page.evaluate("document.contentType || ''")
        if "pdf" in content.lower():
            return True
    except Exception:
        pass
    return False


def _extract_inline_pdf(page: Any) -> bytes | None:
    """Extract PDF bytes from an inline PDF page via JS fetch."""
    try:
        result = page.evaluate("""
            (() => {
                // Try embed/object src first
                const embed = document.querySelector('embed[type="application/pdf"], object[type="application/pdf"]');
                if (embed) {
                    const src = embed.src || embed.data;
                    if (src) return src;
                }
                // Try iframe
                const iframe = document.querySelector('iframe[src*=".pdf"]');
                if (iframe) return iframe.src;
                // Use current URL
                return window.location.href;
            })()
        """)
        if not result:
            return None

        # Fetch the PDF bytes using JS fetch in the page context
        pdf_bytes = page.evaluate(f"""
            (async () => {{
                try {{
                    const resp = await fetch({json.dumps(result)});
                    if (!resp.ok) return null;
                    const buf = await resp.arrayBuffer();
                    const bytes = new Uint8Array(buf);
                    // Check PDF magic
                    if (bytes[0] !== 0x25 || bytes[1] !== 0x50 || bytes[2] !== 0x44 || bytes[3] !== 0x46) return null;
                    if (bytes.length < 5000) return null;
                    // Convert to base64
                    let binary = '';
                    for (let i = 0; i < bytes.length; i++) {{
                        binary += String.fromCharCode(bytes[i]);
                    }}
                    return btoa(binary);
                }} catch(e) {{
                    return null;
                }}
            }})()
        """)
        if pdf_bytes:
            import base64
            return base64.b64decode(pdf_bytes)
    except Exception:
        pass
    return None


def _try_vpnsci_camofox(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download via visible Camoufox browser. Login + download in same session."""
    try:
        from camoufox.sync_api import Camoufox
        from camoufox.addons import DefaultAddons
    except ImportError:
        log.info("   [WebVPN-Camofox] camoufox not installed")
        return None

    base = _get_webvpn_base(config)
    if not base:
        return None

    resolved_url = _resolve_doi_url(doi)
    if not resolved_url:
        resolved_url = f"https://doi.org/{doi}"

    webvpn_url = convert_url(resolved_url, base, config)
    log.info(f"   [WebVPN-Camofox] Target: {webvpn_url[:80]}")
    print(f"\n  [WebVPN] 正在打开浏览器，请在浏览器中登录 WebVPN...")
    print(f"  登录完成后等待 5 秒，程序会自动继续下载。\n")

    try:
        with Camoufox(headless=False, exclude_addons=[DefaultAddons.UBO]) as browser:
            context = browser.new_context()
            page = context.new_page()

            # Restore saved cookies before navigating
            cookie_path = vpnsci_cookie_path(config)
            if cookie_path.exists():
                try:
                    saved_cookies = json.loads(cookie_path.read_text(encoding="utf-8"))
                    if saved_cookies:
                        # Convert to Playwright cookie format
                        pw_cookies = []
                        for c in saved_cookies:
                            pw_c = {
                                "name": c["name"],
                                "value": c["value"],
                                "domain": c.get("domain", ""),
                                "path": c.get("path", "/"),
                            }
                            if c.get("secure"):
                                pw_c["secure"] = True
                            if c.get("httpOnly"):
                                pw_c["httpOnly"] = True
                            if pw_c["domain"]:
                                pw_cookies.append(pw_c)
                        if pw_cookies:
                            context.add_cookies(pw_cookies)
                            log.info(f"   [WebVPN-Camofox] Restored {len(pw_cookies)} cookies")
                except Exception:
                    pass

            # Capture PDF from network responses
            captured_pdf = []

            def on_response(response):
                try:
                    ct = response.headers.get("content-type", "")
                    url = response.url
                    # Capture PDF responses (any content type that's actually a PDF)
                    is_pdf_ct = "pdf" in ct.lower() or "octet-stream" in ct.lower()
                    is_pdf_url = url.lower().endswith(".pdf") or "/pdfdirect/" in url or "/doi/pdf/" in url
                    if not (is_pdf_ct or is_pdf_url):
                        return
                    if response.status >= 400:
                        return
                    body = response.body()
                    if len(body) > 5000 and body[:4] == b"%PDF-":
                        captured_pdf.append(body)
                        log.info(f"   [WebVPN-Camofox] PDF captured: {len(body)} bytes from {url[:60]}")
                except Exception:
                    pass

            page.on("response", on_response)

            # Navigate to paper URL directly via WebVPN
            # If not logged in, will redirect to login page
            try:
                page.goto(webvpn_url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            time.sleep(3)

            # If on login page, wait for user to login then retry
            title = page.title()
            url_now = page.url
            from ..publisher_strategies import _school_auth_patterns
            _stoks = _school_auth_patterns(config)
            _auth_url_signals = list(_stoks) + ["/do/off/ui/auth"]

            log.info(f"   [WebVPN-Camofox] Page title: '{title}' URL: {url_now[:80]}")
            if "登录" in title or "身份" in title or "二次认证" in title or "CAS" in title or any(t in url_now for t in _auth_url_signals):
                print(f"  检测到登录页面，请完成登录...")
                # Wait up to 5 minutes, checking title every 3 seconds
                for i in range(100):
                    time.sleep(3)
                    try:
                        title = page.title()
                        url_now = page.url
                    except Exception:
                        return None
                    if i % 10 == 0:
                        log.info(f"   [WebVPN-Camofox] Waiting... title='{title}' url={url_now[:60]}")
                    # Detect login success: no longer on auth pages
                    is_auth = "登录" in title or "身份" in title or "二次认证" in title or "CAS" in title
                    is_auth_url = any(t in url_now for t in _auth_url_signals)
                    if not is_auth and not is_auth_url:
                        print(f"  登录成功！正在保存 cookies...")
                        # Save cookies immediately after login
                        try:
                            cookies = context.cookies()
                            from ..config import DATA_DIR
                            cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
                            cache_dir.mkdir(parents=True, exist_ok=True)
                            cookie_data = [
                                {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
                                for c in cookies
                            ]
                            (cache_dir / "vpnsci-cookies.json").write_text(
                                json.dumps(cookie_data, indent=2, ensure_ascii=False), encoding="utf-8")
                            lines = ["# Netscape HTTP Cookie File\n"]
                            for c in cookies:
                                d = c.get("domain", "")
                                flag = "TRUE" if d.startswith(".") else "FALSE"
                                p = c.get("path", "/")
                                sec = "TRUE" if c.get("secure") else "FALSE"
                                exp = str(int(c.get("expires", 0)))
                                lines.append(f"{d}\t{flag}\t{p}\t{sec}\t{exp}\t{c['name']}\t{c['value']}\n")
                            (cache_dir / "vpnsci-cookies.txt").write_text("".join(lines), encoding="utf-8")
                            log.info(f"   [WebVPN-Camofox] Saved {len(cookies)} cookies")
                        except Exception as e:
                            log.info(f"   [WebVPN-Camofox] Cookie save warning: {e}")
                        break
                else:
                    print("  登录超时。")
                    return None

                # Now navigate to paper URL
                time.sleep(2)
                try:
                    page.goto(webvpn_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                time.sleep(8)
            else:
                time.sleep(5)

            # Helper: try to save captured PDF
            def _save_captured():
                if captured_pdf:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(captured_pdf[-1])
                    if is_pdf_file(output_path):
                        return success(doi, output_path, "WebVPN(Camofox)")
                return None

            # Check if page itself is a PDF (inline viewer)
            page_url = page.url
            page_title = page.title()
            log.info(f"   [WebVPN-Camofox] On page: title='{page_title[:40]}' url={page_url[:60]}")

            # Check network-captured PDF
            result = _save_captured()
            if result:
                return result

            # If page looks like inline PDF viewer, try to get the PDF bytes
            if _is_inline_pdf_page(page):
                pdf_bytes = _extract_inline_pdf(page)
                if pdf_bytes:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(pdf_bytes)
                    if is_pdf_file(output_path):
                        return success(doi, output_path, "WebVPN(Camofox)")

            # Strategy 1: Try direct publisher PDF URL with Playwright download
            resolved_for_pdf = _resolve_doi_url(doi) or f"https://doi.org/{doi}"
            pdf_url = _construct_publisher_pdf_url(doi, resolved_for_pdf)
            if pdf_url:
                pdf_webvpn = convert_url(pdf_url, base, config)
                log.info(f"   [WebVPN-Camofox] Trying direct PDF: {pdf_webvpn[:80]}")
                captured_pdf.clear()
                # Use expect_download to catch file downloads
                try:
                    with page.expect_download(timeout=30000) as download_info:
                        page.goto(pdf_webvpn, wait_until="commit", timeout=30000)
                    download = download_info.value
                    tmp = download.path()
                    pdf_bytes = tmp.read_bytes() if tmp else None
                    if pdf_bytes and pdf_bytes[:4] == b"%PDF-" and len(pdf_bytes) > 5000:
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(pdf_bytes)
                        if is_pdf_file(output_path):
                            return success(doi, output_path, "WebVPN(Camofox)")
                except Exception as dl_exc:
                    log.info(f"   [WebVPN-Camofox] Download event not triggered: {dl_exc}")
                    # Fall through to response capture
                    time.sleep(5)
                    result = _save_captured()
                    if result:
                        return result
                    # Check if page is now showing PDF
                    if _is_inline_pdf_page(page):
                        pdf_bytes = _extract_inline_pdf(page)
                        if pdf_bytes:
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            output_path.write_bytes(pdf_bytes)
                            if is_pdf_file(output_path):
                                return success(doi, output_path, "WebVPN(Camofox)")

            # Strategy 2: Find PDF link in HTML and navigate
            html = page.content()
            pdf_url = extract_pdf_url_from_html(html, page.url)
            if pdf_url:
                log.info(f"   [WebVPN-Camofox] Found PDF link: {pdf_url[:80]}")
                captured_pdf.clear()
                try:
                    with page.expect_download(timeout=30000) as download_info:
                        page.goto(pdf_url, wait_until="commit", timeout=30000)
                    download = download_info.value
                    tmp = download.path()
                    pdf_bytes = tmp.read_bytes() if tmp else None
                    if pdf_bytes and pdf_bytes[:4] == b"%PDF-" and len(pdf_bytes) > 5000:
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(pdf_bytes)
                        if is_pdf_file(output_path):
                            return success(doi, output_path, "WebVPN(Camofox)")
                except Exception:
                    time.sleep(5)
                    result = _save_captured()
                    if result:
                        return result
                    if _is_inline_pdf_page(page):
                        pdf_bytes = _extract_inline_pdf(page)
                        if pdf_bytes:
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            output_path.write_bytes(pdf_bytes)
                            if is_pdf_file(output_path):
                                return success(doi, output_path, "WebVPN(Camofox)")

            log.info(f"   [WebVPN-Camofox] No PDF found. Title: {page.title()[:40]} URL: {page.url[:60]}")
            return None

    except Exception as e:
        log.info(f"   [WebVPN-Camofox] Error: {e}")
        return None


def try_vpnsci(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try downloading paper through institutional access.

    Strategy:
    1. Try CARSI federated auth (direct publisher login, no proxy)
    2. Try Camofox browser download (handles CAS auth + Cloudflare)
    3. Try WebVPN HTTP approach (if session cookies valid)
    4. Try WebVPN Selenium browser download (legacy fallback)
    """
    if not config.get("vpnsci_enabled", False):
        return None

    # Step 1: Try CARSI (direct publisher auth, works without WebVPN proxy)
    resolved_url = _resolve_doi_url(doi)
    if not resolved_url:
        resolved_url = f"https://doi.org/{doi}"
    carsi_result = _try_carsi(doi, resolved_url, output_path, config)
    if carsi_result:
        return carsi_result

    # Step 2: Try Camofox browser download (preferred, handles CAS + Cloudflare)
    result = _try_vpnsci_camofox(doi, output_path, config)
    if result:
        return result

    # Step 3: Try WebVPN HTTP approach (if session cookies valid)
    if _validate_session(config):
        result = _try_vpnsci_http(doi, output_path, config)
        if result:
            return result

    # Step 4: Try Selenium browser download (legacy fallback)
    log.info("   [WebVPN] Trying Selenium browser download...")
    result = _try_vpnsci_selenium(doi, output_path, config)
    if result:
        return result

    log.info("   [WebVPN] No valid session. Use vpnsci_login or carsi_login tool first.")
    return None


def _try_vpnsci_http(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try downloading via HTTP with saved cookies."""

    _vpnsci_rate_limit()

    log.info(f"   [WebVPN] Trying {doi}")

    # Step 1: Resolve DOI to get publisher URL
    resolved_url = _resolve_doi_url(doi)
    if not resolved_url:
        resolved_url = f"https://doi.org/{doi}"

    # Step 2: Try direct publisher PDF URL
    pdf_url = _construct_publisher_pdf_url(doi, resolved_url)
    if pdf_url:
        log.info(f"   [WebVPN] Trying publisher PDF: {pdf_url[:80]}...")
        result = _download_pdf_vpnsci(pdf_url, output_path, config, doi)
        if result:
            return result

    # Step 3: Fetch via WebVPN and look for PDF link in HTML
    try:
        doi_url = f"https://doi.org/{doi}"
        resp = _fetch_via_webvpn(doi_url, config, stream=True)
        if resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")

        # Direct PDF response
        if _response_looks_pdf(resp, first):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = output_path.with_suffix(output_path.suffix + ".part")
            try:
                with tmp_path.open("wb") as fh:
                    fh.write(first)
                    for chunk in iterator:
                        if chunk:
                            fh.write(chunk)
                tmp_path.replace(output_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            if is_pdf_file(output_path):
                return success(doi, output_path, "WebVPN")

        # HTML response - extract PDF link
        html = first + resp.raw.read(512_000, decode_content=True)
        html_str = html.decode("utf-8", errors="ignore")

        # Check for Cloudflare block
        from ..network import _is_cloudflare_block
        if any(sig in html_str.lower() for sig in ("cf-browser-verification", "challenge-platform", "just a moment")):
            log.info("   [WebVPN] Cloudflare detected, trying camofox...")
            camofox_html = _try_camofox_via_webvpn(doi_url, config)
            if camofox_html:
                html_str = camofox_html

        # Try _find_pdf_link (more thorough)
        found_pdf = _find_pdf_link(html_str, resp.url)
        if found_pdf:
            log.info(f"   [WebVPN] Found PDF link in HTML: {found_pdf[:80]}...")
            result = _download_pdf_vpnsci(found_pdf, output_path, config, doi)
            if result:
                return result

        # Fallback to extract_pdf_url_from_html
        pdf_url = extract_pdf_url_from_html(html_str, resp.url)
        if pdf_url:
            return _download_pdf_vpnsci(pdf_url, output_path, config, doi)

    except Exception as e:
        log.info(f"   [WebVPN] {e}")

    return None


def _try_vpnsci_selenium(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download paper via Selenium with anti-detection (bypasses publisher bot detection).

    Strategy:
    1. Use selenium-stealth to avoid CAPTCHA/bot detection
    2. Load saved cookies via CDP
    3. Navigate to paper URL and download PDF
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium_stealth import stealth
    except ImportError:
        log.info("   [WebVPN-Selenium] selenium/selenium-stealth not installed")
        return None

    base = _get_webvpn_base(config)
    if not base:
        return None
    base_host = urllib.parse.urlparse(base).hostname or ""

    # Resolve DOI to get publisher URL
    resolved_url = _resolve_doi_url(doi)
    if not resolved_url:
        resolved_url = f"https://doi.org/{doi}"

    # Construct the WebVPN URL
    webvpn_url = convert_url(resolved_url, base, config)
    log.info(f"   [WebVPN-Selenium] Accessing {webvpn_url[:80]}...")

    download_dir = str(output_path.parent)

    def _load_cookies_into(driver: Any) -> None:
        cookie_data = _load_cookies_raw(config)
        driver.get(base)
        # Use CDP to set cookies without domain visit requirement
        for c in cookie_data:
            try:
                cdp_params: dict[str, Any] = {
                    "name": c["name"],
                    "value": c["value"],
                    "path": c.get("path", "/"),
                }
                if c.get("domain"):
                    cdp_params["domain"] = c["domain"]
                if c.get("secure"):
                    cdp_params["secure"] = True
                driver.execute_cdp_cmd("Network.setCookie", cdp_params)
            except Exception:
                pass
        driver.execute_cdp_cmd("Network.enable", {})
        # Also try Selenium's add_cookie for the base domain
        for c in cookie_data:
            try:
                if c.get("domain", "") and base_host in c["domain"]:
                    driver.add_cookie({
                        "name": c["name"],
                        "value": c["value"],
                        "path": c.get("path", "/"),
                        "domain": c["domain"],
                    })
            except Exception:
                pass

    def _is_login_page(url: str) -> bool:
        lower = url.lower()
        return "cas" in lower or "login" in lower

    def _try_download(driver: Any) -> dict[str, Any] | None:
        """Try to download PDF from current page state."""
        current_url = driver.current_url
        if _is_login_page(current_url):
            log.info("   [WebVPN-Selenium] Still on login page, cannot download.")
            return None
        log.info(f"   [WebVPN-Selenium] Page loaded: {current_url[:80]}")

        if _is_pdf_page(driver):
            return _save_selenium_pdf(driver, output_path, doi)

        pdf_link = _find_pdf_link_selenium(driver, current_url)
        if pdf_link:
            log.info(f"   [WebVPN-Selenium] Found PDF link: {pdf_link[:80]}")
            driver.get(pdf_link)
            time.sleep(3)
            if _is_pdf_page(driver):
                return _save_selenium_pdf(driver, output_path, doi)
            downloaded = _find_downloaded_pdf(download_dir, doi)
            if downloaded:
                return success(doi, downloaded, "WebVPN-Selenium")

        # ScienceDirect pdfft pattern
        if "sciencedirect.com" in current_url or "elsevier.com" in current_url:
            pii_match = re.search(r"pii/([A-Z0-9]+)", current_url)
            if pii_match:
                pdfft_url = current_url.split("?")[0].rstrip("/")
                if not pdfft_url.endswith("/pdfft"):
                    pdfft_url += "/pdfft"
                driver.get(pdfft_url)
                time.sleep(3)
                if _is_pdf_page(driver):
                    return _save_selenium_pdf(driver, output_path, doi)
        return None

    # Use Selenium with stealth patches to bypass publisher bot detection
    try:
        options = Options()
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-allow-origins=*")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option("useAutomationExtension", False)
        prefs = {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
        }
        options.add_experimental_option("prefs", prefs)
        driver = webdriver.Chrome(options=options)
        stealth(driver,
                languages=["en-US", "en"],
                vendor="Google Inc.",
                platform="Win32",
                webgl_vendor="Intel Inc.",
                renderer="Intel Iris OpenGL Engine",
                fix_hairline=True,
        )
    except Exception as e:
        log.info(f"   [WebVPN-Selenium] Chrome launch failed: {e}")
        return None

    try:
        _load_cookies_into(driver)
        driver.get(webvpn_url)
        time.sleep(5)

        # Check if CAS redirect appeared
        current_url = driver.current_url
        if "cas" in current_url.lower() or "login" in current_url.lower():
            log.info("   [WebVPN-Selenium] Please complete CAS login in the browser window...")

            # Wait for user to complete login (up to 120 seconds)
            max_wait = 120
            elapsed = 0
            login_done = False
            while elapsed < max_wait:
                time.sleep(3)
                elapsed += 3
                try:
                    url = driver.current_url
                except Exception:
                    log.info("   [WebVPN-Selenium] Browser closed by user.")
                    return None
                if "cas" not in url.lower() and "login" not in url.lower():
                    new_cookies = _get_all_cookies(driver)
                    _save_cookies(new_cookies, config)
                    log.info("   [WebVPN-Selenium] Login successful, cookies saved.")
                    login_done = True
                    break

            if not login_done:
                log.info("   [WebVPN-Selenium] Login timed out. Please try again.")
                return None

            # Re-navigate to paper URL after login
            time.sleep(2)
            driver.get(webvpn_url)
            time.sleep(8)

        # Try download
        result = _try_download(driver)
        if result:
            return result

    except Exception as e:
        log.info(f"   [WebVPN-Selenium] Error: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return None


def _load_cookies_raw(config: dict[str, Any]) -> list[dict]:
    """Load raw cookie data from file."""
    path = vpnsci_cookie_path(config)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _is_pdf_page(driver: Any) -> bool:
    """Check if the current page is displaying a PDF."""
    try:
        url = driver.current_url.lower()
        if url.endswith(".pdf"):
            return True
        # Chrome PDF viewer indicators
        if "pdf" in url and ("blob:" in url or "chrome-extension" in url):
            return True
        # Check if page has PDF embed/object
        embeds = driver.find_elements("css selector", 'embed[type="application/pdf"], object[type="application/pdf"]')
        if embeds:
            return True
        # Check content type via CDP
        try:
            result = driver.execute_cdp_cmd("Network.getCookies", {"urls": [driver.current_url]})
        except Exception:
            pass
        # Check page source for PDF header (may appear in some render modes)
        source = driver.page_source[:200]
        if "%PDF" in source:
            return True
    except Exception:
        pass
    return False


def _save_selenium_pdf(driver: Any, output_path: Path, doi: str) -> dict[str, Any] | None:
    """Save PDF content from the current Selenium page."""
    try:
        url = driver.current_url

        # Check for PDF embed/object — extract actual PDF URL
        embeds = driver.find_elements("css selector", 'embed[type="application/pdf"], object[type="application/pdf"]')
        if embeds:
            src = embeds[0].get_attribute("src") or embeds[0].get_attribute("data")
            if src:
                url = src

        # For blob URLs, use CDP to print to PDF
        if url.startswith("blob:"):
            pdf_bytes = driver.execute_cdp_cmd("Page.printToPDF", {})["data"]
            import base64
            content = base64.b64decode(pdf_bytes)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = output_path.with_suffix(output_path.suffix + ".part")
            try:
                with tmp_path.open("wb") as fh:
                    fh.write(content)
                tmp_path.replace(output_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
            if is_pdf_file(output_path):
                return success(doi, output_path, "WebVPN-Selenium")
            return None

        # For regular URLs, fetch with browser cookies
        cookies = _get_all_cookies(driver)
        s = requests.Session()
        s.trust_env = False
        for c in cookies:
            s.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))

        resp = s.get(url, timeout=30, stream=True)
        if resp.status_code < 400:
            first = next(resp.iter_content(chunk_size=8192), b"")
            if first.startswith(b"%PDF-"):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = output_path.with_suffix(output_path.suffix + ".part")
                try:
                    with tmp_path.open("wb") as fh:
                        fh.write(first)
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                fh.write(chunk)
                    tmp_path.replace(output_path)
                except Exception:
                    tmp_path.unlink(missing_ok=True)
                    raise
                if is_pdf_file(output_path):
                    return success(doi, output_path, "WebVPN-Selenium")
    except Exception as e:
        log.info(f"   [WebVPN-Selenium] Save failed: {e}")
    return None


def _find_pdf_link_selenium(driver: Any, base_url: str) -> str | None:
    """Find PDF download link on the current page using Selenium."""
    try:
        # Try citation_pdf_url meta tag
        meta = driver.find_elements("css selector", 'meta[name="citation_pdf_url"]')
        if meta:
            content = meta[0].get_attribute("content")
            if content:
                return content if content.startswith("http") else base_url.split("/")[0] + "//" + base_url.split("/")[2] + content

        # Try links with PDF text
        links = driver.find_elements("css selector", 'a[href*="pdf"], a[href*="PDF"]')
        for link in links:
            href = link.get_attribute("href")
            text = link.text.lower()
            if href and ("pdf" in text or "download" in text or href.endswith(".pdf")):
                return href

        # Try buttons with PDF text
        buttons = driver.find_elements("css selector", 'button, a[role="button"]')
        for btn in buttons:
            text = btn.text.lower()
            if "pdf" in text or "download" in text:
                btn.click()
                time.sleep(3)
                return driver.current_url
    except Exception:
        pass
    return None


def _find_downloaded_pdf(download_dir: str, doi: str) -> Path | None:
    """Check download directory for recently downloaded PDF files."""
    dir_path = Path(download_dir)
    if not dir_path.exists():
        return None
    # Look for PDF files modified in the last 30 seconds
    now = time.time()
    for f in dir_path.iterdir():
        if f.suffix.lower() == ".pdf" and (now - f.stat().st_mtime) < 30:
            if is_pdf_file(f):
                return f
    return None


def _try_carsi(doi: str, resolved_url: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try downloading via CARSI federated auth (browser-based)."""
    if not config.get("carsi_enabled", False):
        return None
    try:
        from .carsi import CARSIClient, detect_publisher
        publisher = detect_publisher(resolved_url)
        if not publisher:
            return None
        client = CARSIClient(config)

        # Try Camoufox first (stealth browser, handles Cloudflare)
        log.info(f"   [CARSI] Trying camofox download for {doi}...")
        result = client.download_via_camofox(doi, resolved_url, output_path)
        if result:
            return result

        # Fallback to Selenium browser
        log.info(f"   [CARSI] Trying selenium download for {doi}...")
        result = client.download_via_browser(doi, resolved_url, output_path)
        if result:
            return result
    except Exception as e:
        log.info(f"   [CARSI] {e}")
    return None


def _save_pdf_response(resp: requests.Response, output_path: Path, doi: str, source: str) -> dict[str, Any] | None:
    """Save a PDF response to disk and validate it."""
    try:
        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if not _response_looks_pdf(resp, first):
            return None
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".part")
        try:
            with tmp_path.open("wb") as fh:
                fh.write(first)
                for chunk in iterator:
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(output_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        if is_pdf_file(output_path):
            return success(doi, output_path, source)
    except Exception:
        pass
    return None


def _try_camofox_via_webvpn(url: str, config: dict[str, Any]) -> str | None:
    """Try fetching a URL through camofox-browser, using WebVPN proxy."""
    base = _get_webvpn_base(config)
    proxied_url = convert_url(url, base, config)
    try:
        from ..camofox import is_available as camofox_avail, get_html as camofox_html
        if camofox_avail(config):
            result = camofox_html(proxied_url, config)
            if result:
                return result
    except Exception as e:
        log.info(f"   [camofox] {e}")
    return None


def _download_pdf_vpnsci(
    url: str,
    output_path: Path,
    config: dict[str, Any],
    doi: str,
) -> dict[str, Any] | None:
    if not is_plausible_pdf_url(url):
        return None
    try:
        _vpnsci_rate_limit()
        resp = _fetch_via_webvpn(url, config, stream=True)
        if resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first_chunk = next(iterator, b"")
        if not _response_looks_pdf(resp, first_chunk):
            return None

        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".part")
        try:
            with tmp_path.open("wb") as fh:
                fh.write(first_chunk)
                for chunk in iterator:
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(output_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        if is_pdf_file(output_path):
            result = success(doi, output_path, "WebVPN")
            result["doi"] = doi
            result["identifier"] = doi
            return result
    except Exception:
        pass
    return None
