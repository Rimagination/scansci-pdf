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
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from ..log import get_logger
from ..pdf_utils import (
    _bind_session_to_response,
    _response_looks_pdf,
    extract_pdf_url_from_html,
    is_pdf_file,
    is_plausible_pdf_url,
    success,
    write_pdf_bytes_atomic,
    write_pdf_stream_atomic,
)

# Import compiled core functions if available (Cython .pyd/.so)
try:
    from .._core.instsci_core import (
        convert_url as _convert_url_compiled,
        construct_publisher_pdf_url as _construct_publisher_pdf_url_compiled,
        find_pdf_link_in_html as _find_pdf_link_compiled,
    )
    _HAS_COMPILED_CORE = True
except ImportError:
    _HAS_COMPILED_CORE = False

log = get_logger()

# Config key migration: v1.7.0 renamed instsci_* → vpnsci_*.  Prefer the new
# vpnsci_* keys, fall back to legacy instsci_* so existing user configs keep
# working.  Both naming conventions resolve to the same value.
def _cfg(config: dict[str, Any], suffix: str, default: Any = None) -> Any:
    """Read a config value trying vpnsci_<suffix> then instsci_<suffix>."""
    if "vpnsci_" + suffix in config:
        return config["vpnsci_" + suffix]
    return config.get("instsci_" + suffix, default)


# Rate limiting between WebVPN requests
_last_instsci_time = 0.0
_INSTSCI_DELAY_MIN = 2.0
_INSTSCI_DELAY_MAX = 5.0


def _cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _wait_or_cancel(
    cancel_event: threading.Event | None,
    timeout: float,
) -> bool:
    """Wait for ``timeout`` seconds; return True when cancellation wins."""
    if cancel_event is None:
        time.sleep(max(0.0, timeout))
        return False
    return cancel_event.wait(max(0.0, timeout))


def _close_browser_resource(resource: Any) -> None:
    """Best-effort close on the resource's owner thread, with one retry."""
    if resource is None:
        return
    for _attempt in range(2):
        try:
            resource.close()
            return
        except Exception:
            pass



def _instsci_rate_limit(cancel_event: threading.Event | None = None) -> bool:
    global _last_instsci_time
    if _cancelled(cancel_event):
        return False
    now = time.time()
    elapsed = now - _last_instsci_time
    delay = __import__("random").uniform(_INSTSCI_DELAY_MIN, _INSTSCI_DELAY_MAX)
    if elapsed < delay and _wait_or_cancel(cancel_event, delay - elapsed):
        return False
    _last_instsci_time = time.time()
    return True


def instsci_cookie_path(config: dict[str, Any]) -> Path:
    configured = _cfg(config, "cookie_file")
    if configured:
        return Path(configured).expanduser()
    from ..config import DEFAULT_CONFIG
    return Path(config.get("cache_dir", DEFAULT_CONFIG["cache_dir"])).expanduser() / "instsci-cookies.json"


def instsci_is_configured(config: dict[str, Any]) -> bool:
    return bool(_cfg(config, "enabled", False) and _get_webvpn_base(config))


def _get_webvpn_base(config: dict[str, Any]) -> str:
    """Get WebVPN base URL, resolving from school if needed."""
    base = _cfg(config, "base_url", "").strip()
    if base:
        return base.rstrip("/")
    school = _cfg(config, "school", "")
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
    school = _cfg(config, "school", "")
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
    path = instsci_cookie_path(config)
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
    path = instsci_cookie_path(config)
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
    s = None
    resp = None
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
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def instsci_login(config: dict[str, Any]) -> bool:
    """Open browser for CAS login. Called from MCP tool, not interactively."""
    return _browser_login(config)


def _browser_login(config: dict[str, Any]) -> bool:
    """Open browser for CAS login via CloakBrowser."""
    try:
        from ..browser_login import webvpn_login
        if webvpn_login(config):
            return True
    except Exception as exc:
        log.info(f"   [WebVPN] stealth browser login failed: {exc}")
    return False


def _get_socks5_proxy(config: dict[str, Any]) -> str:
    """Get SOCKS5 proxy URL from config (for EasyConnect/aTrust campus connectors)."""
    proxy = config.get("network_proxy", "").strip()
    if proxy and proxy.lower().startswith("socks5://"):
        return proxy
    return ""


def _is_campus_connector_mode(config: dict[str, Any]) -> bool:
    """Check if we should use direct access via SOCKS5 campus connector instead of WebVPN."""
    proxy = _get_socks5_proxy(config)
    if not proxy:
        return False
    # If school type is easyconnect or atrust, use direct access via SOCKS5
    school = _cfg(config, "school", "")
    if school:
        try:
            from ..schools import get_school
            entry = get_school(school)
            if entry.school_type in ("easyconnect", "atrust"):
                return True
        except (ValueError, AttributeError):
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

    # Use SOCKS5 proxy if configured (for campus connectors like EasyConnect/aTrust)
    socks5 = _get_socks5_proxy(config)
    if socks5:
        s.proxies = {"http": socks5, "https": socks5}
        s.verify = False  # Campus connectors often use self-signed certs

    try:
        resp = s.get(
            proxied,
            timeout=request_timeout(config),
            allow_redirects=True,
            stream=stream,
        )
    except Exception:
        s.close()
        raise
    return _bind_session_to_response(resp, s)


def _fetch_direct_via_socks5(url: str, config: dict[str, Any], *, stream: bool = False) -> requests.Response:
    """Fetch a URL directly through SOCKS5 campus connector (no WebVPN URL conversion)."""
    from ..network import USER_AGENT, request_timeout
    socks5 = _get_socks5_proxy(config)

    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": USER_AGENT})
    s.proxies = {"http": socks5, "https": socks5}
    s.verify = False

    try:
        resp = s.get(
            url,
            timeout=request_timeout(config),
            allow_redirects=True,
            stream=stream,
        )
    except Exception:
        s.close()
        raise
    return _bind_session_to_response(resp, s)


def _resolve_doi_url(doi: str) -> str | None:
    """Resolve DOI to get the publisher URL."""
    resp = None
    try:
        resp = requests.get(
            f"https://doi.org/{doi}",
            allow_redirects=True,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            stream=True,
            verify=False,
        )
        final_url = resp.url
        if final_url and final_url != f"https://doi.org/{doi}":
            return final_url
    except Exception:
        pass
    finally:
        if resp is not None:
            try:
                resp.close()
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
    elif "pnas.org" in hostname:
        return f"https://www.pnas.org/doi/pdf/{doi}"
    elif "science.org" in hostname or "sciencemag.org" in hostname:
        return f"https://www.science.org/doi/pdf/{doi}"
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

    if "pnas.org" in hostname and "/doi/" in path and "/pdf/" not in path:
        doi_part = path.split("/doi/")[-1]
        if doi_part:
            return f"{base}/doi/pdf/{doi_part}"

    if "science.org" in hostname and "/doi/" in path and "/pdf/" not in path:
        doi_part = path.split("/doi/")[-1]
        if doi_part:
            return f"{base}/doi/pdf/{doi_part}"

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


def _download_pdf_with_browser_cookies(
    pdf_url: str,
    output_path: Path,
    config: dict[str, Any],
    doi: str,
    context: Any,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Download PDF via WebVPN using cookies from a live browser context.

    Bypasses stale cookie files by pulling cookies directly from the
    Playwright browser context that was just used for login/page-navigation.
    """
    if _cancelled(cancel_event) or not is_plausible_pdf_url(pdf_url):
        return None
    session = None
    resp = None
    try:
        from ..network import USER_AGENT, request_timeout

        base = _get_webvpn_base(config)
        if not base:
            return None
        proxied = convert_url(pdf_url, base, config)

        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": USER_AGENT})
        for c in context.cookies():
            if _cancelled(cancel_event):
                return None
            name = c.get("name")
            value = c.get("value")
            if name and value is not None:
                session.cookies.set(name, value, domain=c.get("domain", ""), path=c.get("path", "/"))

        if _cancelled(cancel_event):
            return None
        resp = session.get(
            proxied,
            timeout=request_timeout(config),
            allow_redirects=True,
            stream=True,
        )
        if _cancelled(cancel_event) or resp.status_code >= 400:
            log.info(f"   [WebVPN-Browser] Browser-cookie HTTP status={resp.status_code} for PDF URL")
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if _cancelled(cancel_event):
            return None
        if not _response_looks_pdf(resp, first):
            return None

        if not write_pdf_stream_atomic(
            output_path,
            first,
            iterator,
            cancel_event,
        ):
            return None

        if not _cancelled(cancel_event) and is_pdf_file(output_path):
            log.info(f"   [WebVPN-Browser] PDF downloaded via browser-cookie HTTP")
            return success(doi, output_path, "WebVPN(Browser)")
    except Exception as e:
        if not _cancelled(cancel_event):
            log.info(f"   [WebVPN-Browser] Browser-cookie HTTP error: {e}")
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
    return None


def _try_instsci_socks5(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Try downloading via SOCKS5 campus connector (EasyConnect/aTrust).

    The SOCKS5 connector handles authentication at the network level,
    so no WebVPN URL conversion or CAS login is needed — just fetch
    the publisher URL directly through the proxy.
    """
    if not _instsci_rate_limit(cancel_event):
        return None
    socks5 = _get_socks5_proxy(config)
    log.info(f"   [CampusConnector] Trying {doi} via {socks5}")

    # Step 1: Resolve DOI to publisher URL
    resolved_url = _resolve_doi_url(doi)
    if _cancelled(cancel_event):
        return None
    if not resolved_url:
        resolved_url = f"https://doi.org/{doi}"

    # Step 2: Try direct publisher PDF URL
    pdf_url = _construct_publisher_pdf_url(doi, resolved_url)
    if pdf_url:
        log.info(f"   [CampusConnector] Trying publisher PDF: {pdf_url[:80]}")
        result = _download_pdf_socks5(
            pdf_url,
            output_path,
            config,
            doi,
            cancel_event=cancel_event,
        )
        if result:
            return result

    # Step 3: Fetch landing page via SOCKS5 and find PDF link
    resp = None
    try:
        if _cancelled(cancel_event):
            return None
        resp = _fetch_direct_via_socks5(resolved_url, config, stream=True)
        if _cancelled(cancel_event):
            return None
        if resp.status_code >= 400:
            log.info(f"   [CampusConnector] HTTP {resp.status_code} for {resolved_url[:60]}")
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if _cancelled(cancel_event):
            return None

        # Direct PDF response
        if _response_looks_pdf(resp, first):
            if not write_pdf_stream_atomic(
                output_path,
                first,
                iterator,
                cancel_event,
            ):
                return None
            if not _cancelled(cancel_event) and is_pdf_file(output_path):
                log.info("   [CampusConnector] PDF downloaded directly")
                return success(doi, output_path, "CampusConnector")

        # HTML response - look for PDF link
        html = first + resp.raw.read(512_000, decode_content=True)
        if _cancelled(cancel_event):
            return None
        html_str = html.decode("utf-8", errors="ignore")

        found_pdf = _find_pdf_link(html_str, resp.url)
        if found_pdf:
            log.info(f"   [CampusConnector] Found PDF link: {found_pdf[:80]}")
            result = _download_pdf_socks5(
                found_pdf,
                output_path,
                config,
                doi,
                cancel_event=cancel_event,
            )
            if result:
                return result

        pdf_url = extract_pdf_url_from_html(html_str, resp.url)
        if pdf_url:
            result = _download_pdf_socks5(
                pdf_url,
                output_path,
                config,
                doi,
                cancel_event=cancel_event,
            )
            if result:
                return result

    except Exception as e:
        if not _cancelled(cancel_event):
            log.info(f"   [CampusConnector] {e}")
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    return None


def _download_pdf_socks5(
    url: str,
    output_path: Path,
    config: dict[str, Any],
    doi: str,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Download a PDF URL directly through SOCKS5 campus connector."""
    if _cancelled(cancel_event) or not is_plausible_pdf_url(url):
        return None
    resp = None
    try:
        if not _instsci_rate_limit(cancel_event):
            return None
        resp = _fetch_direct_via_socks5(url, config, stream=True)
        if _cancelled(cancel_event) or resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first_chunk = next(iterator, b"")
        if _cancelled(cancel_event):
            return None
        if not _response_looks_pdf(resp, first_chunk):
            return None

        if not write_pdf_stream_atomic(
            output_path,
            first_chunk,
            iterator,
            cancel_event,
        ):
            return None

        if not _cancelled(cancel_event) and is_pdf_file(output_path):
            log.info(f"   [CampusConnector] PDF downloaded: {doi}")
            return success(doi, output_path, "CampusConnector")
    except Exception as e:
        if not _cancelled(cancel_event):
            log.info(f"   [CampusConnector] Download error: {e}")
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
    return None


def _try_instsci_browser(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Download via visible stealth browser browser. Login + download in same session."""
    if _cancelled(cancel_event):
        return None
    try:
        from ..cloakbrowser_compat import (
            launch_with_driver_cleanup,
            prepare_cloakbrowser_runtime,
        )

        prepare_cloakbrowser_runtime()
        from cloakbrowser import launch
    except Exception:
        log.info("   [WebVPN-Browser] cloakbrowser not installed")
        return None

    base = _get_webvpn_base(config)
    if not base:
        return None

    resolved_url = _resolve_doi_url(doi)
    if _cancelled(cancel_event):
        return None
    if not resolved_url:
        resolved_url = f"https://doi.org/{doi}"

    webvpn_url = convert_url(resolved_url, base, config)
    log.info(f"   [WebVPN-Browser] Target: {webvpn_url[:80]}")
    print(f"\n  [WebVPN] 正在打开浏览器，请在浏览器中登录 WebVPN...")
    print(f"  登录完成后等待 5 秒，程序会自动继续下载。\n")

    browser = None
    context = None
    page = None
    slot_lease = None
    captured_pdf: list[bytes] = []
    capture_lock = threading.Lock()
    capture_in_progress = False
    on_response = None
    try:
        if _cancelled(cancel_event):
            return None
        from .. import browser_engine
        slot_lease = browser_engine._retain_browser_slot(config, cancel_event)
        raw_browser = launch_with_driver_cleanup(
            launch,
            headless=False,
            humanize=True,
            args=["--disable-features=CrossOriginOpenerPolicy"],
        )
        browser = browser_engine._LeasedBrowser(raw_browser, slot_lease)
        slot_lease = None
        if _cancelled(cancel_event):
            return None
        context = browser.new_context()
        if _cancelled(cancel_event):
            return None
        page = context.new_page()
        if _cancelled(cancel_event):
            return None

        # Restore saved cookies before navigating
        cookie_path = instsci_cookie_path(config)
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
                        log.info(f"   [WebVPN-Browser] Restored {len(pw_cookies)} cookies")
            except Exception:
                pass

        # Capture PDF from network responses
        def on_response(response):
            nonlocal capture_in_progress
            if _cancelled(cancel_event):
                return
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
                with capture_lock:
                    if _cancelled(cancel_event) or capture_in_progress or captured_pdf:
                        return
                    capture_in_progress = True
                try:
                    body = response.body()
                    if _cancelled(cancel_event) or captured_pdf:
                        return
                    if len(body) > 5000 and body.startswith(b"%PDF-"):
                        captured_pdf.append(body)
                        log.info(f"   [WebVPN-Browser] PDF captured: {len(body)} bytes from {url[:60]}")
                finally:
                    with capture_lock:
                        capture_in_progress = False
            except Exception:
                pass

        page.on("response", on_response)

        # Navigate to paper URL directly via WebVPN
        # If not logged in, will redirect to login page
        if _cancelled(cancel_event):
            return None
        try:
            page.goto(webvpn_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        if _wait_or_cancel(cancel_event, 3):
            return None

        # If on login page, wait for user to login then retry
        title = page.title()
        url_now = page.url
        from ..publisher_strategies import _school_auth_patterns
        _stoks = _school_auth_patterns(config)
        _auth_url_signals = list(_stoks) + ["/do/off/ui/auth"]

        log.info(f"   [WebVPN-Browser] Page title: '{title}' URL: {url_now[:80]}")
        if "登录" in title or "身份" in title or "二次认证" in title or "CAS" in title or any(t in url_now for t in _auth_url_signals):
            print(f"  检测到登录页面，请完成登录...")
            # Wait up to 5 minutes, checking title every 3 seconds
            for i in range(100):
                if _wait_or_cancel(cancel_event, 3):
                    return None
                try:
                    title = page.title()
                    url_now = page.url
                except Exception:
                    return None
                if i % 10 == 0:
                    log.info(f"   [WebVPN-Browser] Waiting... title='{title}' url={url_now[:60]}")
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
                        (cache_dir / "instsci-cookies.json").write_text(
                            json.dumps(cookie_data, indent=2, ensure_ascii=False), encoding="utf-8")
                        lines = ["# Netscape HTTP Cookie File\n"]
                        for c in cookies:
                            d = c.get("domain", "")
                            flag = "TRUE" if d.startswith(".") else "FALSE"
                            p = c.get("path", "/")
                            sec = "TRUE" if c.get("secure") else "FALSE"
                            exp = str(int(c.get("expires", 0)))
                            lines.append(f"{d}\t{flag}\t{p}\t{sec}\t{exp}\t{c['name']}\t{c['value']}\n")
                        (cache_dir / "instsci-cookies.txt").write_text("".join(lines), encoding="utf-8")
                        log.info(f"   [WebVPN-Browser] Saved {len(cookies)} cookies")
                    except Exception as e:
                        log.info(f"   [WebVPN-Browser] Cookie save warning: {e}")
                    break
            else:
                print("  登录超时。")
                return None

            # Try HTTP download with fresh browser cookies (bypasses JS-heavy pages)
            pdf_url = _construct_publisher_pdf_url(doi, resolved_url)
            if pdf_url:
                if _wait_or_cancel(cancel_event, 2):
                    return None
                result = _download_pdf_with_browser_cookies(
                    pdf_url,
                    output_path,
                    config,
                    doi,
                    context,
                    cancel_event=cancel_event,
                )
                if result:
                    return result

            # Fall back to browser-based PDF extraction
            if _wait_or_cancel(cancel_event, 2):
                return None
            if _cancelled(cancel_event):
                return None
            try:
                page.goto(webvpn_url, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass
            if _cancelled(cancel_event):
                return None
            for _w in range(20):
                if _wait_or_cancel(cancel_event, 2):
                    return None
                try:
                    _t = (page.title() or "").lower()
                    _b = (page.evaluate("document.body?.innerText?.length || 0") or 0)
                    if _b > 200 and not any(x in _t for x in ("请稍候", "loading", "please wait", "just a moment")):
                        break
                except Exception:
                    break
        else:
            # Already authenticated — try HTTP download with browser cookies
            pdf_url = _construct_publisher_pdf_url(doi, resolved_url)
            if pdf_url:
                if _wait_or_cancel(cancel_event, 2):
                    return None
                result = _download_pdf_with_browser_cookies(
                    pdf_url,
                    output_path,
                    config,
                    doi,
                    context,
                    cancel_event=cancel_event,
                )
                if result:
                    return result
            if _wait_or_cancel(cancel_event, 5):
                return None

        # Helper: try to save captured PDF
        def _save_captured():
            if _cancelled(cancel_event):
                return None
            if captured_pdf:
                if write_pdf_bytes_atomic(
                    output_path,
                    captured_pdf[-1],
                    cancel_event,
                ) and is_pdf_file(output_path):
                    return success(doi, output_path, "WebVPN(Browser)")
            return None

        # Check if page itself is a PDF (inline viewer)
        if _cancelled(cancel_event):
            return None
        page_url = page.url
        page_title = page.title()
        log.info(f"   [WebVPN-Browser] On page: title='{page_title[:40]}' url={page_url[:60]}")

        # Check network-captured PDF
        result = _save_captured()
        if result:
            return result

        # If page looks like inline PDF viewer, try to get the PDF bytes
        if _is_inline_pdf_page(page):
            pdf_bytes = _extract_inline_pdf(page)
            if _cancelled(cancel_event):
                return None
            if pdf_bytes:
                if write_pdf_bytes_atomic(
                    output_path,
                    pdf_bytes,
                    cancel_event,
                ) and is_pdf_file(output_path):
                    return success(doi, output_path, "WebVPN(Browser)")

        # Strategy 1: Try direct publisher PDF URL via browser-cookie HTTP first
        resolved_for_pdf = _resolve_doi_url(doi) or f"https://doi.org/{doi}"
        if _cancelled(cancel_event):
            return None
        pdf_url = _construct_publisher_pdf_url(doi, resolved_for_pdf)
        if pdf_url:
            result = _download_pdf_with_browser_cookies(
                pdf_url,
                output_path,
                config,
                doi,
                context,
                cancel_event=cancel_event,
            )
            if result:
                return result

            # Fallback: expect_download in browser
            pdf_webvpn = convert_url(pdf_url, base, config)
            log.info(f"   [WebVPN-Browser] Trying direct PDF via browser: {pdf_webvpn[:80]}")
            captured_pdf.clear()
            if _cancelled(cancel_event):
                return None
            try:
                with page.expect_download(timeout=30000) as download_info:
                    page.goto(pdf_webvpn, wait_until="commit", timeout=30000)
                if _cancelled(cancel_event):
                    return None
                download = download_info.value
                tmp = download.path()
                pdf_bytes = tmp.read_bytes() if tmp else None
                if pdf_bytes and pdf_bytes.startswith(b"%PDF-") and len(pdf_bytes) > 5000:
                    if write_pdf_bytes_atomic(
                        output_path,
                        pdf_bytes,
                        cancel_event,
                    ) and is_pdf_file(output_path):
                        return success(doi, output_path, "WebVPN(Browser)")
            except Exception as dl_exc:
                log.info(f"   [WebVPN-Browser] Download event not triggered: {dl_exc}")
                if _wait_or_cancel(cancel_event, 5):
                    return None
                result = _save_captured()
                if result:
                    return result
                if _is_inline_pdf_page(page):
                    pdf_bytes = _extract_inline_pdf(page)
                    if _cancelled(cancel_event):
                        return None
                    if pdf_bytes:
                        if write_pdf_bytes_atomic(
                            output_path,
                            pdf_bytes,
                            cancel_event,
                        ) and is_pdf_file(output_path):
                            return success(doi, output_path, "WebVPN(Browser)")

        # Strategy 2: Find PDF link in HTML, try browser-cookie HTTP first
        if _cancelled(cancel_event):
            return None
        html = page.content()
        if _cancelled(cancel_event):
            return None
        found_pdf_url = extract_pdf_url_from_html(html, page.url)
        if found_pdf_url:
            log.info(f"   [WebVPN-Browser] Found PDF link: {found_pdf_url[:80]}")
            result = _download_pdf_with_browser_cookies(
                found_pdf_url,
                output_path,
                config,
                doi,
                context,
                cancel_event=cancel_event,
            )
            if result:
                return result

            # Fallback: expect_download in browser
            captured_pdf.clear()
            if _cancelled(cancel_event):
                return None
            try:
                with page.expect_download(timeout=30000) as download_info:
                    page.goto(found_pdf_url, wait_until="commit", timeout=30000)
                if _cancelled(cancel_event):
                    return None
                download = download_info.value
                tmp = download.path()
                pdf_bytes = tmp.read_bytes() if tmp else None
                if pdf_bytes and pdf_bytes.startswith(b"%PDF-") and len(pdf_bytes) > 5000:
                    if write_pdf_bytes_atomic(
                        output_path,
                        pdf_bytes,
                        cancel_event,
                    ) and is_pdf_file(output_path):
                        return success(doi, output_path, "WebVPN(Browser)")
            except Exception:
                if _wait_or_cancel(cancel_event, 5):
                    return None
                result = _save_captured()
                if result:
                    return result
                if _is_inline_pdf_page(page):
                    pdf_bytes = _extract_inline_pdf(page)
                    if _cancelled(cancel_event):
                        return None
                    if pdf_bytes:
                        if write_pdf_bytes_atomic(
                            output_path,
                            pdf_bytes,
                            cancel_event,
                        ) and is_pdf_file(output_path):
                            return success(doi, output_path, "WebVPN(Browser)")

        if _cancelled(cancel_event):
            return None
        log.info(f"   [WebVPN-Browser] No PDF found. Title: {page.title()[:40]} URL: {page.url[:60]}")
        return None

    except Exception as e:
        log.info(f"   [WebVPN-Browser] Error: {e}")
        return None
    finally:
        if page is not None and on_response is not None:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
        with capture_lock:
            captured_pdf.clear()
        for resource in (page, context, browser):
            _close_browser_resource(resource)
        if browser is None and slot_lease is not None:
            slot_lease.close()


def try_instsci(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Try downloading paper through institutional access.

    Strategy:
    1. If SOCKS5 campus connector (EasyConnect/aTrust) is configured, try direct access
    2. Try CloakBrowser download (handles CAS auth + Cloudflare)
    3. Try WebVPN HTTP approach (if session cookies valid)

    Note: CARSI is now a standalone source tier (carsi_source.try_carsi),
    called independently from the download orchestrator.
    """
    if _cancelled(cancel_event) or not _cfg(config, "enabled", False):
        return None

    # Step 0: SOCKS5 campus connector mode (EasyConnect/aTrust) — direct access
    if _is_campus_connector_mode(config):
        result = _try_instsci_socks5(
            doi,
            output_path,
            config,
            cancel_event=cancel_event,
        )
        if result:
            return result

    # Step 1: Try stealth browser download (handles CAS auth + Cloudflare)
    if _cancelled(cancel_event):
        return None
    result = _try_instsci_browser(
        doi,
        output_path,
        config,
        cancel_event=cancel_event,
    )
    if result:
        return result

    # Step 2: Try WebVPN HTTP approach (use any saved cookies, even if
    # _validate_session fails — the stealth browser may have just logged in
    # and saved fresh cookies that work for the target paper but fail
    # validation's unrelated test URL)
    if not _cancelled(cancel_event) and instsci_cookie_path(config).exists():
        result = _try_instsci_http(
            doi,
            output_path,
            config,
            cancel_event=cancel_event,
        )
        if result:
            return result

    log.info("   [WebVPN] No valid session. Use instsci_login or carsi_login tool first.")
    return None


def _try_instsci_http(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Try downloading via HTTP with saved cookies."""

    if not _instsci_rate_limit(cancel_event):
        return None

    log.info(f"   [WebVPN] Trying {doi}")

    # Step 1: Resolve DOI to get publisher URL
    resolved_url = _resolve_doi_url(doi)
    if _cancelled(cancel_event):
        return None
    if not resolved_url:
        resolved_url = f"https://doi.org/{doi}"

    # Step 2: Try direct publisher PDF URL
    pdf_url = _construct_publisher_pdf_url(doi, resolved_url)
    if pdf_url:
        log.info(f"   [WebVPN] Trying publisher PDF: {pdf_url[:80]}...")
        result = _download_pdf_instsci(
            pdf_url,
            output_path,
            config,
            doi,
            cancel_event=cancel_event,
        )
        if result:
            return result

    # Step 3: Fetch via WebVPN and look for PDF link in HTML
    resp = None
    try:
        doi_url = f"https://doi.org/{doi}"
        if _cancelled(cancel_event):
            return None
        resp = _fetch_via_webvpn(doi_url, config, stream=True)
        if _cancelled(cancel_event) or resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if _cancelled(cancel_event):
            return None

        # Direct PDF response
        if _response_looks_pdf(resp, first):
            if not write_pdf_stream_atomic(
                output_path,
                first,
                iterator,
                cancel_event,
            ):
                return None
            if not _cancelled(cancel_event) and is_pdf_file(output_path):
                return success(doi, output_path, "WebVPN")

        # HTML response - extract PDF link
        html = first + resp.raw.read(512_000, decode_content=True)
        if _cancelled(cancel_event):
            return None
        html_str = html.decode("utf-8", errors="ignore")

        # Check for Cloudflare block
        from ..network import _is_cloudflare_block
        if any(sig in html_str.lower() for sig in ("cf-browser-verification", "challenge-platform", "just a moment", "请稍候", "正在验证", "checking your browser")):
            log.info("   [WebVPN] Cloudflare detected, trying browser...")
            browser_html = _try_browser_via_webvpn(
                doi_url,
                config,
                cancel_event=cancel_event,
            )
            if browser_html:
                html_str = browser_html

        # Try _find_pdf_link (more thorough)
        found_pdf = _find_pdf_link(html_str, resp.url)
        if found_pdf:
            log.info(f"   [WebVPN] Found PDF link in HTML: {found_pdf[:80]}...")
            result = _download_pdf_instsci(
                found_pdf,
                output_path,
                config,
                doi,
                cancel_event=cancel_event,
            )
            if result:
                return result

        # Fallback to extract_pdf_url_from_html
        pdf_url = extract_pdf_url_from_html(html_str, resp.url)
        if pdf_url:
            return _download_pdf_instsci(
                pdf_url,
                output_path,
                config,
                doi,
                cancel_event=cancel_event,
            )

    except Exception as e:
        if not _cancelled(cancel_event):
            log.info(f"   [WebVPN] {e}")
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    return None


def _try_carsi(
    doi: str,
    resolved_url: str,
    output_path: Path,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Try downloading via CARSI federated auth (browser-based)."""
    if _cancelled(cancel_event) or not config.get("carsi_enabled", False):
        return None
    client = None
    try:
        from .carsi import CARSIClient, detect_publisher
        publisher = detect_publisher(resolved_url)
        if not publisher:
            return None
        client = CARSIClient(config)

        # Try stealth browser first (stealth browser, handles Cloudflare)
        log.info(f"   [CARSI] Trying browser download for {doi}...")
        result = client.download_via_browser(
            doi,
            resolved_url,
            output_path,
            cancel_event=cancel_event,
        )
        if result:
            return result
    except Exception as e:
        if not _cancelled(cancel_event):
            log.info(f"   [CARSI] {e}")
    finally:
        if client is not None:
            client.close()
    return None


def _save_pdf_response(
    resp: requests.Response,
    output_path: Path,
    doi: str,
    source: str,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Save a PDF response to disk and validate it."""
    try:
        if _cancelled(cancel_event):
            return None
        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if _cancelled(cancel_event):
            return None
        if not _response_looks_pdf(resp, first):
            return None
        if not write_pdf_stream_atomic(
            output_path,
            first,
            iterator,
            cancel_event,
        ):
            return None
        if not _cancelled(cancel_event) and is_pdf_file(output_path):
            return success(doi, output_path, source)
    except Exception:
        pass
    return None


def _try_browser_via_webvpn(
    url: str,
    config: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> str | None:
    """Try fetching a URL through CloakBrowser, using WebVPN proxy."""
    if _cancelled(cancel_event):
        return None
    base = _get_webvpn_base(config)
    proxied_url = convert_url(url, base, config)
    try:
        from ..browser_engine import is_available as browser_avail, get_html as browser_html
        if browser_avail(config):
            if _cancelled(cancel_event):
                return None
            result = browser_html(proxied_url, config)
            if result and not _cancelled(cancel_event):
                return result
    except Exception as e:
        log.info(f"   [browser] {e}")
    return None


def _download_pdf_instsci(
    url: str,
    output_path: Path,
    config: dict[str, Any],
    doi: str,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    if _cancelled(cancel_event) or not is_plausible_pdf_url(url):
        return None
    resp = None
    try:
        if not _instsci_rate_limit(cancel_event):
            return None
        resp = _fetch_via_webvpn(url, config, stream=True)
        if _cancelled(cancel_event) or resp.status_code >= 400:
            return None

        iterator = resp.iter_content(chunk_size=8192)
        first_chunk = next(iterator, b"")
        if _cancelled(cancel_event):
            return None
        if not _response_looks_pdf(resp, first_chunk):
            return None

        if not write_pdf_stream_atomic(
            output_path,
            first_chunk,
            iterator,
            cancel_event,
        ):
            return None

        if not _cancelled(cancel_event) and is_pdf_file(output_path):
            result = success(doi, output_path, "WebVPN")
            result["doi"] = doi
            result["identifier"] = doi
            return result
    except Exception:
        pass
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
    return None
