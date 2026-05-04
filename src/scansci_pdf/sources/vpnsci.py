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


def _browser_login(config: dict[str, Any]) -> bool:
    """Open browser for CAS login."""
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
                cookies = driver.get_cookies()
                _save_cookies(cookies, config)
                driver.quit()
                print("  登录成功！Cookie 已保存。\n")
                return True

            vpn_cookies = [c for c in driver.get_cookies()
                          if "webvpn" in c.get("domain", "").lower()
                          and c.get("name", "").startswith("wengine_vpn_ticket")]
            if vpn_cookies:
                cookies = driver.get_cookies()
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


def try_vpnsci(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try downloading paper through WebVPN institutional proxy.

    Strategy:
    1. Resolve DOI to publisher URL
    2. Try constructing direct publisher PDF URL via WebVPN
    3. Fall back to DOI redirect via WebVPN, extract PDF link from HTML
    """
    if not config.get("vpnsci_enabled", False):
        return None

    if not _validate_session(config):
        log.info("   [WebVPN] No valid session. Use vpnsci_login tool first.")
        return None

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

    # Step 3: Try CARSI-authenticated publisher access
    carsi_result = _try_carsi(doi, resolved_url, output_path, config)
    if carsi_result:
        return carsi_result

    # Step 4: Fetch via WebVPN and look for PDF link in HTML
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
            with tmp_path.open("wb") as fh:
                fh.write(first)
                for chunk in iterator:
                    if chunk:
                        fh.write(chunk)
            tmp_path.replace(output_path)
            if is_pdf_file(output_path):
                return success(doi, output_path, "WebVPN")

        # HTML response - extract PDF link
        html = first + resp.raw.read(512_000, decode_content=True)
        html_str = html.decode("utf-8", errors="ignore")

        # Check for Cloudflare block
        from ..network import _is_cloudflare_block
        if any(sig in html_str.lower() for sig in ("cf-browser-verification", "challenge-platform", "just a moment")):
            log.info("   [WebVPN] Cloudflare detected, trying FlareSolverr...")
            flaresolverr_html = _try_flaresolverr_via_webvpn(doi_url, config)
            if flaresolverr_html:
                html_str = flaresolverr_html

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


def _try_carsi(doi: str, resolved_url: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try downloading via CARSI federated auth."""
    if not config.get("carsi_enabled", False):
        return None
    try:
        from .carsi import CARSIClient, detect_publisher
        publisher = detect_publisher(resolved_url)
        if not publisher:
            return None
        client = CARSIClient(config)

        # Try PDF URL first
        pdf_url = _construct_publisher_pdf_url(doi, resolved_url)
        if pdf_url:
            log.info(f"   [CARSI] Trying publisher PDF: {pdf_url[:80]}...")
            resp = client.fetch(pdf_url, stream=True)
            if resp and resp.status_code < 400:
                result = _save_pdf_response(resp, output_path, doi, "CARSI")
                if result:
                    return result

        # Try resolved URL HTML
        resp = client.fetch(resolved_url)
        if resp and resp.status_code < 400:
            html_str = resp.text
            found_pdf = _find_pdf_link(html_str, resp.url)
            if found_pdf:
                log.info(f"   [CARSI] Found PDF link: {found_pdf[:80]}...")
                pdf_resp = client.fetch(found_pdf, stream=True)
                if pdf_resp:
                    return _save_pdf_response(pdf_resp, output_path, doi, "CARSI")
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
        with tmp_path.open("wb") as fh:
            fh.write(first)
            for chunk in iterator:
                if chunk:
                    fh.write(chunk)
        tmp_path.replace(output_path)
        if is_pdf_file(output_path):
            return success(doi, output_path, source)
    except Exception:
        pass
    return None


def _try_flaresolverr_via_webvpn(url: str, config: dict[str, Any]) -> str | None:
    """Try fetching a URL through FlareSolverr, using WebVPN proxy."""
    try:
        from ..flaresolverr import get_html
        base = _get_webvpn_base(config)
        proxied_url = convert_url(url, base, config)
        return get_html(proxied_url, config)
    except Exception as e:
        log.info(f"   [FlareSolverr] {e}")
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
        with tmp_path.open("wb") as fh:
            fh.write(first_chunk)
            for chunk in iterator:
                if chunk:
                    fh.write(chunk)
        tmp_path.replace(output_path)

        if is_pdf_file(output_path):
            result = success(doi, output_path, "WebVPN")
            result["doi"] = doi
            result["identifier"] = doi
            return result
    except Exception:
        pass
    return None
