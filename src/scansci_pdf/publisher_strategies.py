"""Publisher-specific browser download strategies.

Implements tailored download paths for major academic publishers,
handling anti-bot challenges, PDF viewers, and publisher-specific DOM structures.
Inspired by ref-downloader's publisher strategy registry.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path
from typing import Any

try:
    from cloakbrowser import launch, launch_persistent_context
    _HAS_CLOAKBROWSER = True
except ImportError:
    launch = None  # type: ignore[assignment]
    launch_persistent_context = None  # type: ignore[assignment]
    _HAS_CLOAKBROWSER = False
from .log import get_logger

log = get_logger()

# Module-level error tracking for caller introspection
_last_error_type: str = ""
_last_error_action: str = ""


def get_last_error() -> tuple[str, str]:
    """Return (error_type, action) from last _browser_download call."""
    return _last_error_type, _last_error_action


def _set_error(error_type: str, action: str = "") -> None:
    global _last_error_type, _last_error_action
    _last_error_type = error_type
    _last_error_action = action


def _clear_error() -> None:
    global _last_error_type, _last_error_action
    _last_error_type = ""
    _last_error_action = ""


def _get_profile_dir(config: dict[str, Any], publisher: str = "shared") -> Path:
    from .config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    profile_dir = cache_dir / f"publisher_profile_{publisher.lower()}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _restore_cookies_to_context(context: Any, config: dict[str, Any]) -> None:
    try:
        from .browser_cookies import load_saved_cookies
        saved = load_saved_cookies(config)
        if saved:
            pw_cookies = []
            for c in saved:
                pw_c = {"name": c.get("name", ""), "value": c.get("value", ""),
                         "domain": c.get("domain", ""), "path": c.get("path", "/")}
                if pw_c["domain"]:
                    pw_cookies.append(pw_c)
            if pw_cookies:
                context.add_cookies(pw_cookies)
    except Exception:
        pass


@contextlib.contextmanager
def _visible_browser(config: dict[str, Any], publisher: str, *, viewport: dict | None = None):
    """Open visible CloakBrowser with persistent profile. Falls back to ephemeral."""
    if not _HAS_CLOAKBROWSER:
        raise RuntimeError("cloakbrowser not installed. Run: pip install cloakbrowser")
    from .browser_engine import _build_browser_args
    profile_dir = _get_profile_dir(config, publisher)
    browser = None
    args = _build_browser_args(config)

    try:
        ctx = launch_persistent_context(
            str(profile_dir),
            headless=False, humanize=True,
            args=args,
        )
        page = ctx.new_page()
        log.info(f"   [{publisher}] persistent browser profile: {profile_dir}")
    except Exception as _e:
        log.info(f"   [{publisher}] persistent context unavailable ({_e}), using ephemeral")
        _vp = viewport or {"width": 1440, "height": 900}
        browser = launch(headless=False, humanize=True, args=args)
        ctx = browser.new_context(viewport=_vp)
        _restore_cookies_to_context(ctx, config)
        page = ctx.new_page()

    try:
        yield ctx, page
    finally:
        try:
            if browser:
                browser.close()
            else:
                ctx.close()
        except Exception:
            pass


def _save_all_cookie_formats(
    cookies: list[dict[str, Any]],
    publisher: str,
    config: dict[str, Any],
) -> None:
    """Save cookies in all formats: JSON, Netscape, publisher subset, + bridge to CloakBrowser."""
    from .config import DATA_DIR
    from .browser_cookies import cookies_to_netscape, _save_cookies_json

    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))

    # 1. Save full cookies to carsi_cookies/{publisher}.json
    carsi_dir = cache_dir / "carsi_cookies"
    carsi_dir.mkdir(parents=True, exist_ok=True)
    cookie_data = [
        {"name": c.get("name", ""), "value": c.get("value", ""),
         "domain": c.get("domain", ""), "path": c.get("path", "/"),
         "secure": c.get("secure", False), "expires": c.get("expires", 0),
         "httpOnly": c.get("httpOnly", False)}
        for c in cookies
    ]
    (carsi_dir / f"{publisher.lower()}.json").write_text(
        json.dumps(cookie_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # 2. Save Netscape format (for CloakBrowser import)
    netscape_text = cookies_to_netscape(cookies)
    netscape_file = cache_dir / "publisher_cookies.txt"
    netscape_file.write_text(netscape_text, encoding="utf-8")

    # 3. Save publisher-domain subset
    _pub_domains = [
        "wiley.com", "onlinelibrary.wiley.com", "elsevier.com", "sciencedirect.com",
        "cell.com", "springer.com", "link.springer.com", "nature.com",
        "ieee.org", "ieeexplore.ieee.org", "acs.org", "pubs.acs.org",
        "rsc.org", "pubs.rsc.org", "tandfonline.com", "oup.com",
        "academic.oup.com", "iop.org", "iopscience.iop.org",
        "aps.org", "journals.aps.org", "aip.org", "pubs.aip.org",
        "dl.acm.org", "acm.org", "science.org", "sciencemag.org",
        "ascelibrary.org", "sagepub.com", "journals.sagepub.com",
    ]
    pub_cookies = [c for c in cookie_data
                   if any(c.get("domain", "").endswith(d) for d in _pub_domains)]
    if pub_cookies:
        _save_cookies_json(pub_cookies, cache_dir / "publisher_cookies.json")

    # 4. Bridge to CloakBrowser headless service
    try:
        from .browser_engine import import_cookies, is_available
        if is_available(config):
            imported = import_cookies(str(netscape_file), config)
            log.info(f"   [{publisher}] bridged {imported} cookies to CloakBrowser service")
    except Exception as _e:
        log.info(f"   [{publisher}] CloakBrowser bridge note: {_e}")

    log.info(f"   [{publisher}] saved {len(cookies)} cookies in all formats")


def _detect_paywall(html: str, status_code: int = 0) -> bool:
    """Detect if the page is a paywall/login wall rather than anti-bot challenge."""
    lower = html[:50000].lower()
    # Paywall indicators
    paywall_signals = [
        "sign in to access", "login to access", "institutional access",
        "purchase access", "subscribe to access", "buy article",
        "access through your institution", "get access",
        "this article is behind a paywall", "subscription required",
        "please log in", "register to continue",
        "access denied", "you do not have access",
        "institutional login", "shibboleth", "openathens",
    ]
    if any(sig in lower for sig in paywall_signals):
        return True
    # 403 with article content (not Cloudflare challenge) = paywall
    if status_code == 403 and not _is_challenge_page(html):
        return True
    return False


def _has_publisher_cookies(config: dict[str, Any]) -> bool:
    """Check if we have saved publisher cookies for the current session."""
    try:
        from .browser_cookies import load_saved_cookies
        if len(load_saved_cookies(config)) > 0:
            return True
    except Exception:
        pass
    # Also check CARSI cookies
    try:
        from .config import DATA_DIR
        carsi_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / "carsi_cookies"
        if carsi_dir.is_dir() and any(carsi_dir.glob("*.json")):
            return True
    except Exception:
        pass
    return False


def _inject_cookies_to_tab(tab_id: str, config: dict[str, Any], publisher: str) -> None:
    """Inject saved publisher + CARSI cookies into CloakBrowser session."""
    from .browser_engine import import_cookies
    from .config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    total = 0

    # Import publisher cookies (Netscape format if exists)
    netscape_file = cache_dir / "publisher_cookies.txt"
    if netscape_file.exists():
        try:
            total += import_cookies(str(netscape_file), config)
        except Exception:
            pass

    # Import CARSI cookies
    carsi_dir = cache_dir / "carsi_cookies"
    if carsi_dir.is_dir():
        for cf in carsi_dir.glob("*.json"):
            try:
                # Convert JSON → Netscape format
                from .browser_cookies import cookies_to_netscape
                cookies = json.loads(cf.read_text(encoding="utf-8"))
                tmp = cache_dir / f"_tmp_{cf.stem}.txt"
                tmp.write_text(cookies_to_netscape(cookies), encoding="utf-8")
                total += import_cookies(str(tmp), config)
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    if total > 0:
        log.info(f"   [{publisher}] imported {total} cookies into browser session")


def _try_institutional_login(tab_id: str, config: dict[str, Any], publisher: str) -> bool:
    """Try institutional login (OpenAthens/CARSI) when paywall detected.

    Works within the existing CloakBrowser tab:
    1. Find and click "Institutional login" / SSO link
    2. Search for configured institution
    3. Wait for user to complete CAS login
    4. Navigate back to article

    Returns True if login succeeded (cookies saved).
    """
    from .browser_engine import evaluate_js, navigate_tab

    idp_name = config.get("carsi_idp_name", "")
    if not idp_name:
        log.info(f"   [{publisher}] no carsi_idp_name configured, skipping institutional login")
        return False

    idp_en = _IDP_MAP.get(idp_name, idp_name)

    # Step 1: Click SSO/institutional login link on the article page
    sso_clicked = evaluate_js(tab_id, """
        (() => {
            const links = [...document.querySelectorAll('a')];
            for (const a of links) {
                const href = (a.href || '').toLowerCase();
                const text = (a.innerText || '').toLowerCase();
                if (href.includes('ssostart') || href.includes('saml') || href.includes('shibboleth')
                    || href.includes('institutional-login') || href.includes('federation')
                    || (text.includes('institutional') && text.includes('login'))
                    || text.includes('access through your institution')) {
                    a.click();
                    return a.href;
                }
            }
            return null;
        })()
    """, config)

    if not sso_clicked:
        log.info(f"   [{publisher}] no SSO/institutional login link found on page")
        return False

    log.info(f"   [{publisher}] clicked institutional login: {str(sso_clicked)[:60]}")
    time.sleep(8)

    # Step 2: Look for institution search box and search
    search_done = evaluate_js(tab_id, f"""
        (() => {{
            const name = {json.dumps(idp_en)};
            const selectors = {json.dumps(_INSTITUTION_SEARCH_SELECTORS)};
            for (const sel of selectors) {{
                const input = document.querySelector(sel);
                if (input) {{
                    input.focus();
                    input.value = name;
                    input.dispatchEvent(new Event('input', {{bubbles: true}}));
                    input.dispatchEvent(new Event('change', {{bubbles: true}}));
                    input.dispatchEvent(new KeyboardEvent('keyup', {{key: 'n', bubbles: true}}));
                    return sel;
                }}
            }}
            return null;
        }})()
    """, config)

    if search_done:
        log.info(f"   [{publisher}] searched for '{idp_en}' via {search_done}")
        time.sleep(3)

        # Click matching institution result
        clicked = evaluate_js(tab_id, f"""
            (() => {{
                const name = {json.dumps(idp_en)};
                const items = document.querySelectorAll('[class*="result"], [class*="suggestion"], [class*="federation"], li, a, button');
                for (const el of items) {{
                    const text = el.textContent || '';
                    if (text.includes(name) && el.offsetParent !== null) {{
                        el.click();
                        return text.trim().substring(0, 60);
                    }}
                }}
                return null;
            }})()
        """, config)

        if clicked:
            log.info(f"   [{publisher}] selected institution: {clicked}")
            time.sleep(5)
        else:
            log.info(f"   [{publisher}] no matching institution found for '{idp_en}'")
            return False
    else:
        log.info(f"   [{publisher}] no institution search box found")

    # Step 3: Check if CAS login page appeared (headless tab → user can't interact)
    # Use module-level constants
    _ak = _AUTH_KEYWORDS
    _at = _AUTH_TITLES

    current_url = evaluate_js(tab_id, "window.location.href", config) or ""
    current_title = evaluate_js(tab_id, "document.title", config) or ""

    needs_login = any(x in current_url.lower() for x in _ak) or any(x in current_title for x in _at)

    if needs_login:
        # CloakBrowser is headless — user can't see the tab
        # Open a visible browser window for the CAS login
        log.info(f"   [{publisher}] CAS login required — opening visible browser...")
        return _visible_institutional_login(current_url, config, publisher, idp_name, idp_en, tab_id)

    # If we ended up back on the article page, login might have succeeded via cookies
    log.info(f"   [{publisher}] institutional login flow completed")
    return True


def _visible_institutional_login(
    cas_url: str, config: dict[str, Any], publisher: str,
    idp_name: str, idp_en: str, headless_tab_id: str,
) -> bool:
    """Open a visible browser for CAS login, then inject cookies back."""
    from .browser_engine import evaluate_js, navigate_tab, import_cookies

    with _visible_browser(config, publisher, viewport=None) as (context, page):

        # Start from article page to get Cloudflare clearance, then do SSO flow
        article_url = evaluate_js(headless_tab_id, "window.location.href", config) or cas_url
        doi_match = re.search(r'10\.\d{4,}/[^\s?&]+', article_url)
        doi_str = doi_match.group(0) if doi_match else ""
        from urllib.parse import urlparse as _urlparse
        parsed = _urlparse(article_url)
        publisher_host = parsed.netloc or "onlinelibrary.wiley.com"
        article_page = f"https://{publisher_host}/doi/{doi_str}" if doi_str else article_url

        log.info(f"   [{publisher}] visible browser: loading article page first...")
        try:
            page.goto(article_page, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
        except Exception:
            pass

        title = page.title()
        url = page.url
        log.info(f"   [{publisher}] visible browser: '{title[:40]}' {url[:60]}")

        # Check if already on CAS page (previous cookies caused redirect)
        already_on_auth = any(x in (title or "") for x in _AUTH_TITLES) or \
                          any(x in (url or "").lower() for x in _AUTH_KEYWORDS)

        if not already_on_auth:
            # Click "Institutional login" link on article page
            page.evaluate("""
                (() => {
                    const a = [...document.querySelectorAll('a')].find(a => a.href && a.href.includes('ssostart'));
                    if (a) a.click();
                })()
            """)
            time.sleep(8)

            title = page.title()
            url = page.url
            log.info(f"   [{publisher}] after SSO click: '{title[:30]}' {url[:60]}")

            # Search for institution
            si = page.query_selector('#searchInstitution')
            if si:
                si.fill(idp_en)
                time.sleep(3)
                page.evaluate(f"""
                    (name) => {{
                        const items = document.querySelectorAll('[class*="result"], [class*="suggestion"], li, a, button');
                        for (const el of items) {{
                            if (el.textContent.includes(name) && el.offsetParent !== null) {{
                                el.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}
                """, idp_en)
                time.sleep(5)

        # Wait for user to complete CAS login
        print(f"\n  请在浏览器中完成机构登录 ({idp_name})")
        print("  登录成功后浏览器会自动跳转回文章页面，程序会自动检测\n")

        for i in range(100):
            time.sleep(3)
            try:
                title = page.title()
                url = page.url
            except Exception:
                break
            is_auth = any(x in title for x in _AUTH_TITLES)
            is_auth_url = any(x in url.lower() for x in _AUTH_KEYWORDS)
            if not is_auth and not is_auth_url:
                log.info(f"   [{publisher}] login successful in visible browser!")
                try:
                    cookies = context.cookies()
                    _save_all_cookie_formats(cookies, publisher, config)
                except Exception as e:
                    log.info(f"   [{publisher}] cookie save error: {e}")
                return True

        log.info(f"   [{publisher}] visible login timed out")
        return False


# ============================================================
# Publisher direct PDF URL patterns
# ============================================================

def _visible_browser_download(
    doi: str,
    article_url: str,
    output_path: Path,
    config: dict[str, Any],
    publisher: str,
) -> dict[str, Any] | None:
    """Open a visible browser for full SSO login + direct PDF download.

    Works for any publisher. Uses _PUBLISHER_SSO_CONFIG for publisher-specific
    SSO link patterns, institution search selectors, and PDF fetch paths.
    """
    from .pdf_utils import is_pdf_file, success
    import base64

    idp_name = config.get("carsi_idp_name", "")
    idp_en = _IDP_MAP.get(idp_name, idp_name)

    # Publisher-specific SSO config
    sso_cfg = _PUBLISHER_SSO_CONFIG.get(publisher, _PUBLISHER_SSO_CONFIG["_default"])
    sso_link_js = sso_cfg["sso_link_js"]
    search_selectors = sso_cfg["search_selectors"]
    pdf_paths = sso_cfg["pdf_paths"](doi)

    with _visible_browser(config, publisher) as (context, page):

        # For Elsevier DOIs, avoid linkinghub.elsevier.com by using direct URL
        if publisher == "Elsevier" and ("doi.org/" in article_url or "linkinghub" in article_url):
            cell_url = _build_cell_press_url(doi)
            if cell_url:
                log.info(f"   [{publisher}] bypassing linkinghub, using direct cell.com URL")
                article_url = cell_url
            else:
                # Resolve DOI → sciencedirect.com PII URL via HTTP
                sd_url = _resolve_elsevier_pii(doi, config)
                if sd_url:
                    log.info(f"   [{publisher}] bypassing linkinghub, using resolved URL")
                    article_url = sd_url

        # Navigate to article page
        log.info(f"   [{publisher}] visible browser: opening {article_url[:60]}")
        try:
            page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
        except Exception as exc:
            log.info(f"   [{publisher}] page load warning: {exc}")

        # If stuck on linkinghub redirect, extract target and navigate directly
        url = page.url
        if "linkinghub" in url or ("retrieve/pii" in url and "Loading" in (page.title() or "")):
            log.info(f"   [{publisher}] stuck on linkinghub, extracting direct URL...")
            pii_match = re.search(r"pii/([A-Z0-9]+)", url)
            if pii_match:
                pii = pii_match.group(1)
                # Try cell.com only if it's a known Cell Press journal, else sciencedirect.com
                cell_url = _build_cell_press_url(doi) if publisher == "Elsevier" else None
                direct_urls = []
                if cell_url:
                    direct_urls.append(cell_url)
                direct_urls.append(f"https://www.sciencedirect.com/science/article/pii/{pii}")
                for direct_url in direct_urls:
                    try:
                        page.goto(direct_url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(5)
                        if "linkinghub" not in page.url:
                            log.info(f"   [{publisher}] navigated to {page.url[:60]}")
                            break
                    except Exception:
                        pass

        # Wait for Cloudflare challenge to resolve (visible browser can pass it)
        from .network import is_cloudflare_challenge
        for _cf_wait in range(12):
            if is_cloudflare_challenge(page.title() or ""):
                log.info(f"   [{publisher}] Cloudflare challenge detected, waiting... ({_cf_wait+1}/12)")
                time.sleep(5)
            else:
                break
        else:
            log.info(f"   [{publisher}] Cloudflare challenge did not resolve")

        title = page.title()
        url = page.url
        log.info(f"   [{publisher}] page: '{title[:40]}' {url[:60]}")

        # Check if already on auth page (previous cookies caused redirect)
        already_on_auth = any(x in (title or "") for x in _AUTH_TITLES) or \
                          any(x in (url or "").lower() for x in _AUTH_KEYWORDS)

        # Try fetching PDF first — if it works, no login needed
        pdf_fetched = False
        if not already_on_auth:
            log.info(f"   [{publisher}] trying direct PDF fetch...")
            fetch_result = _try_browser_fetch_pdf(page, pdf_paths)
            if fetch_result:
                pdf_bytes = fetch_result
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(pdf_bytes)
                log.info(f"   [{publisher}] downloaded {len(pdf_bytes)} bytes via direct fetch")
                if is_pdf_file(output_path):
                    return success(doi, output_path, f"{publisher}(Visible)")
                pdf_fetched = True
            else:
                log.info(f"   [{publisher}] direct PDF fetch failed, need SSO login")

        # SSO login needed
        if not already_on_auth and not pdf_fetched:
            log.info(f"   [{publisher}] starting SSO login...")
            page.evaluate(sso_link_js)
            time.sleep(8)

            # SSO click may have navigated the page or opened a popup
            # Check all pages in the context for the SSO/IDP page
            sso_page = page
            for ctx_page in context.pages:
                try:
                    p_url = ctx_page.url
                    p_title = ctx_page.title()
                    if any(x in p_url.lower() for x in _AUTH_KEYWORDS) or \
                       any(x in p_title for x in _AUTH_TITLES):
                        sso_page = ctx_page
                        log.info(f"   [{publisher}] SSO detected on tab: '{p_title[:30]}' {p_url[:60]}")
                        break
                except Exception:
                    pass

            log.info(f"   [{publisher}] after SSO click: '{sso_page.title()[:30] if sso_page else '?'}' {sso_page.url[:60] if sso_page else '?'}")

            # Search for institution on the SSO page
            for sel in search_selectors:
                si = sso_page.query_selector(sel)
                if si:
                    si.fill(idp_en)
                    time.sleep(3)
                    clicked = sso_page.evaluate(f"""
                        (name) => {{
                            const items = document.querySelectorAll('[class*="result"], [class*="suggestion"], [class*="federation"], li, a, button');
                            for (const el of items) {{
                                if (el.textContent.includes(name) && el.offsetParent !== null) {{
                                    el.click();
                                    return true;
                                }}
                            }}
                            return false;
                        }}
                    """, idp_en)
                    if clicked:
                        log.info(f"   [{publisher}] selected institution '{idp_en}'")
                        time.sleep(5)
                        break

        # Wait for user to complete CAS login
        print(f"\n  请在浏览器中完成机构登录 ({idp_name})")
        print("  登录成功后会自动下载 PDF\n")

        login_ok = False
        for i in range(100):
            time.sleep(3)
            # Check ALL pages in context — SSO may happen in any tab
            any_auth = False
            for ctx_page in context.pages:
                try:
                    p_title = ctx_page.title()
                    p_url = ctx_page.url
                    is_auth = any(x in p_title for x in _AUTH_TITLES)
                    is_auth_url = any(x in p_url.lower() for x in _AUTH_KEYWORDS)
                    if is_auth or is_auth_url:
                        any_auth = True
                        break
                except Exception:
                    pass
            if not any_auth:
                login_ok = True
                break

        if not login_ok:
            log.info(f"   [{publisher}] visible login timed out")
            return None

        log.info(f"   [{publisher}] login successful, downloading PDF...")
        time.sleep(3)

        # Save cookies for future use (all formats + bridge to CloakBrowser)
        try:
            cookies = context.cookies()
            _save_all_cookie_formats(cookies, publisher, config)
        except Exception as e:
            log.info(f"   [{publisher}] cookie save note: {e}")

        # Navigate back to the article page and reload to pick up auth cookies
        log.info(f"   [{publisher}] reloading article page with new auth cookies...")
        try:
            page.goto(article_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
        except Exception:
            pass

        # Try downloading PDF via in-browser fetch (post-login)
        fetch_result = _try_browser_fetch_pdf(page, pdf_paths)
        if fetch_result:
            pdf_bytes = fetch_result
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(pdf_bytes)
            log.info(f"   [{publisher}] downloaded {len(pdf_bytes)} bytes via post-login fetch")
            if is_pdf_file(output_path):
                return success(doi, output_path, f"{publisher}(Visible)")

        # Fallback: look for PDF link on the page and fetch it
        log.info(f"   [{publisher}] fetch failed, trying page PDF links...")
        pdf_link = page.evaluate("""
            (() => {
                for (const a of document.querySelectorAll('a')) {
                    const href = (a.href || '').toLowerCase();
                    if (href.includes('pdfdirect') || href.includes('/pdf/') || href.includes('.pdf')) {
                        if (!href.includes('supplement') && !href.includes('supporting')) return a.href;
                    }
                }
                for (const el of document.querySelectorAll('iframe, embed, object')) {
                    const src = el.src || el.data || '';
                    if (src.includes('.pdf') || src.includes('pdfdirect')) return src;
                }
                return null;
            })()
        """)

        if pdf_link and isinstance(pdf_link, str):
            log.info(f"   [{publisher}] found PDF link: {pdf_link[:60]}")
            try:
                page.goto(pdf_link, wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)
            except Exception:
                pass

            # Try fetch from current page context
            current_url = page.url
            from urllib.parse import urlparse
            parsed = urlparse(current_url)
            # Try absolute fetch
            abs_paths = [parsed.path]
            fetch_result2 = _try_browser_fetch_pdf(page, abs_paths)
            if fetch_result2:
                pdf_bytes = fetch_result2
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(pdf_bytes)
                log.info(f"   [{publisher}] downloaded {len(pdf_bytes)} bytes via PDF link")
                if is_pdf_file(output_path):
                    return success(doi, output_path, f"{publisher}(Visible)")

        log.info(f"   [{publisher}] visible browser download failed")
        return None


def _try_browser_fetch_pdf(page: Any, paths: list[str]) -> bytes | None:
    """Try fetching PDF via browser JS fetch for given paths. Returns PDF bytes or None."""
    import base64
    paths_js = json.dumps(paths)
    pdf_b64 = page.evaluate(f"""
        (async () => {{
            const paths = {paths_js};
            for (const p of paths) {{
                try {{
                    const resp = await fetch(p, {{credentials: 'include', headers: {{'Accept': 'application/pdf,*/*'}}}});
                    if (!resp.ok) return 'status:' + resp.status;
                    const ct = resp.headers.get('content-type') || '';
                    if (!ct.includes('pdf') && !ct.includes('octet')) return 'ct:' + ct;
                    const blob = await resp.blob();
                    return await new Promise((resolve) => {{
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    }});
                }} catch(e) {{ return 'error:' + e.message; }}
            }}
            return null;
        }})()
    """)

    if isinstance(pdf_b64, str) and pdf_b64.startswith("data:"):
        _, data = pdf_b64.split(",", 1)
        pdf_bytes = base64.b64decode(data)
        if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
            return pdf_bytes
    return None


def _direct_pdf_urls(doi: str, publisher: str) -> list[str]:
    """Build direct PDF URL candidates for a publisher."""
    urls: list[str] = []

    if publisher == "Elsevier":
        # ScienceDirect doesn't have a simple direct PDF URL pattern
        # Need to go through the article page
        pass

    elif publisher == "Wiley":
        # PDFDirect is the fastest path
        urls.append(f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}")
        urls.append(f"https://onlinelibrary.wiley.com/doi/pdf/{doi}")

    elif publisher == "IEEE":
        # IEEE stamp URL — classic redirect-based PDF
        # Need to resolve DOI first to get the article number
        doi_suffix = doi.split("/", 1)[1] if "/" in doi else doi
        urls.append(f"https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={doi_suffix}")

    elif publisher == "ACS":
        urls.append(f"https://pubs.acs.org/doi/pdf/{doi}")
        urls.append(f"https://pubs.acs.org/doi/pdf/10.1021/{doi.split('10.1021/')[-1]}")

    elif publisher == "RSC":
        urls.append(f"https://pubs.rsc.org/en/content/articlepdf/{doi}")

    elif publisher == "AIP":
        # AIP has a loading page, direct URL may not work
        urls.append(f"https://pubs.aip.org/aip/apl/article-pdf/{doi}")

    elif publisher == "Springer":
        urls.append(f"https://link.springer.com/content/pdf/{doi}.pdf")

    elif publisher == "APS":
        # Physical Review journals
        doi_suffix = doi.split("10.1103/")[-1] if "10.1103/" in doi else doi
        urls.append(f"https://journals.aps.org/prl/pdf/{doi}")
        urls.append(f"https://journals.aps.org/prb/pdf/{doi}")
        urls.append(f"https://journals.aps.org/pdf/{doi}")

    elif publisher == "Tandfonline":
        urls.append(f"https://www.tandfonline.com/doi/pdf/{doi}")

    elif publisher == "OSA":
        urls.append(f"https://opg.optica.org/viewmedia.cfm?r=1&uri={doi}")

    elif publisher == "IOP":
        urls.append(f"https://iopscience.iop.org/article/{doi}/pdf")

    elif publisher == "Oxford":
        urls.append(f"https://academic.oup.com/downloadpdf/{doi}")

    elif publisher == "ACM":
        urls.append(f"https://dl.acm.org/doi/pdf/{doi}")

    return urls


# ============================================================
# Publisher-specific article page selectors
# ============================================================

# CSS selectors to find PDF links on article landing pages
_PDF_LINK_SELECTORS: dict[str, list[str]] = {
    "Elsevier": [
        'a[aria-label*="View PDF" i]',
        'a[aria-label*="pdf" i]',
        'a.pdf-download-btn-link',
        'a[data-aa-name="download-pdf"]',
        'a[href*="/pdf/"]',
        'a[href*="pdfft"]',
        'a[href*=".pdf"]',
        'a[href*="showPdf"]',
        'a[href*="pdfExtended"]',
    ],
    "Wiley": [
        'a[href*="pdfdirect"]',
        'a[href*="/doi/pdf/"]',
        'a[href*="/doi/epdf/"]',
        'a.c-pdf-download__link',
        'a[title*="PDF" i]',
    ],
    "IEEE": [
        'a[href*="/stamp/"]',
        'a[href*="getPDF"]',
        'a[title*="PDF" i]',
        'a.xpl-pdf-btn',
    ],
    "ACS": [
        'a[href*="/doi/pdf/"]',
        'a[title*="Download PDF" i]',
        'a.nav-item-link-download',
    ],
    "RSC": [
        'a[href*="/articlepdf/"]',
        'a[title*="PDF" i]',
    ],
    "AIP": [
        'a[href*="/article-pdf/"]',
        'a[title*="PDF" i]',
        'a[href*=".pdf"]',
    ],
    "Springer": [
        'a[href*="/content/pdf/"]',
        'a[data-track-action="download pdf"]',
        'a.c-pdf-download__link',
    ],
    "Nature": [
        'a.c-pdf-download__link',
        'a[data-track-action="download pdf"]',
        'a[href*=".pdf"]',
    ],
    "Science": [
        'a[href*="/doi/pdf/"]',
        'a[title*="PDF" i]',
        'a.article-pdf',
    ],
    "APS": [
        'a[href*="/pdf/"]',
        'a[title*="PDF" i]',
    ],
    "Tandfonline": [
        'a[href*="/doi/pdf/"]',
        'a[title*="PDF" i]',
    ],
    "IOP": [
        'a[href*="/article/"][href$="/pdf"]',
        'a[title*="PDF" i]',
    ],
    "Oxford": [
        'a[href*="/downloadpdf/"]',
        'a[title*="PDF" i]',
    ],
    "ACM": [
        'a[href*="/doi/pdf/"]',
        'a[title*="PDF" i]',
    ],
}

# ============================================================
# Anti-bot challenge detection
# ============================================================

# Auth detection keywords for institutional login flows
_AUTH_KEYWORDS = ("cas", "idp", "saml", "wayf", "sso", "passport", "accounts", "oauth", "/login", "/signin")
_AUTH_TITLES = ("登录", "身份", "二次认证", "Login", "Sign in", "Log in")


def _school_auth_patterns(config: dict[str, Any]) -> tuple[str, ...]:
    """Return school-specific URL auth patterns derived from carsi_idp_name.

    Uses _IDP_MAP to translate the configured Chinese institution name to
    English, then lowercases it so the caller can match it against IDP URLs
    (e.g. 'tsinghua' matches 'id.tsinghua.edu.cn').

    For universities not in _IDP_MAP, falls back to pinyin conversion
    so that e.g. "兰州大学" → "lanzhou" can match "lzu.edu.cn" IDP URLs.
    """
    name = (config.get("carsi_idp_name", "") or "").strip()
    if not name:
        return ()
    en = _IDP_MAP.get(name, "").lower()
    if not en:
        # Dynamic fallback: try pinyin conversion for Chinese names
        en = _chinese_to_pinyin_token(name).lower()
    if not en:
        return ()
    # Split multi-word names into individual tokens for broad matching
    tokens = tuple(t for t in en.replace("-", " ").split() if len(t) > 2)
    return tokens


def _chinese_to_pinyin_token(name: str) -> str:
    """Convert a Chinese university name to a pinyin token for URL matching.

    E.g. "兰州大学" → "lanzhou", "哈尔滨工业大学" → "haerbingongye".
    Falls back to empty string if pypinyin is not installed.
    """
    try:
        from pypinyin import lazy_pinyin
        return "".join(lazy_pinyin(name))
    except ImportError:
        pass
    # Strip common suffixes and return what we have
    for suffix in ("大学", "学院", "研究院"):
        name = name.replace(suffix, "")
    return name

# Chinese → English university name mapping for institution search (CARSI/OpenAthens WAYF)
_IDP_MAP: dict[str, str] = {
    "清华大学": "Tsinghua", "北京大学": "Peking", "浙江大学": "Zhejiang",
    "复旦大学": "Fudan", "上海交通大学": "Shanghai Jiao Tong",
    "南京大学": "Nanjing", "中国科学技术大学": "USTC",
    "武汉大学": "Wuhan", "中山大学": "Sun Yat-sen",
    "华中科技大学": "Huazhong", "哈尔滨工业大学": "Harbin Institute",
    "西安交通大学": "Xi'an Jiaotong", "同济大学": "Tongji",
    "东南大学": "Southeast", "厦门大学": "Xiamen",
    "北京师范大学": "Beijing Normal", "南开大学": "Nankai",
    "四川大学": "Sichuan", "天津大学": "Tianjin",
    "中国人民大学": "Renmin", "北京航空航天大学": "Beihang",
    "山东大学": "Shandong", "吉林大学": "Jilin",
}

# Institution search input selectors, tried in order on WAYF pages
_INSTITUTION_SEARCH_SELECTORS: list[str] = [
    "#searchInstitution", "#bdd-email", 'input[name="institution"]',
    "#institution-search", 'input[placeholder*="institution"]',
    'input[placeholder*="University"]',
]

# Shared JS snippets for SSO/institutional login interaction.
# These are Playwright-style (parametrized) — callers using string interpolation
# (e.g. evaluate_js) must wrap them in an IIFE and inject params via json.dumps.

# Find and click SSO/institutional login link on article page
_SSO_LINK_FINDER_JS: str = (
    "() => {"
    "  const links = [...document.querySelectorAll('a')];"
    "  const sso = links.find(a => a.href &&"
    "    (a.href.includes('ssostart') || a.href.includes('shibboleth')"
    "     || a.href.includes('saml') || a.href.includes('institutional-login')"
    "     || a.href.includes('federation') || a.href.includes('/action/showLogin')"
    "     || a.href.includes('/institutional-access') || a.href.includes('wayf')));"
    "  if (sso) { return sso.href; }"
    "  return false;"
    "}"
)

# Click institution result that matches the given name
_INSTITUTION_CLICK_JS: str = (
    "(name) => {"
    "  const items = document.querySelectorAll("
    "    '[class*=\"result\"], [class*=\"suggestion\"], [class*=\"federation\"], li, a, button');"
    "  for (const el of items) {"
    "    const text = el.textContent || '';"
    "    if (text.includes(name) && el.offsetParent !== null) {"
    "      el.click();"
    "      return text.trim().substring(0, 60);"
    "    }"
    "  }"
    "  return null;"
    "}"
)

# Publisher-specific SSO configuration for visible browser download
# Each entry: sso_link_js (JS to click institutional login), search_selectors, pdf_paths(doi)


def _make_sso_click_js(*href_patterns: str, text_patterns: tuple[str, ...] = ()) -> str:
    """Generate a JS IIFE that clicks the first <a> matching the given patterns.

    Args:
        href_patterns: Substrings to match against a.href (lowercased).
        text_patterns: Substrings to match against a.innerText (lowercased).
    """
    conditions = " || ".join(f"href.includes('{p}')" for p in href_patterns)
    if text_patterns:
        text_conds = " || ".join(f"text.includes('{p}')" for p in text_patterns)
        conditions = f"({conditions}) || ({text_conds})"
    return f"""
        (() => {{
            const a = [...document.querySelectorAll('a')].find(a => {{
                const href = (a.href || '').toLowerCase();
                const text = (a.innerText || '').toLowerCase();
                return {conditions};
            }});
            if (a) a.click();
        }})()
    """


def _elsevier_pdf_paths(doi: str) -> list[str]:
    """Build Elsevier/Cell Press PDF fetch paths from DOI."""
    paths: list[str] = []
    doi_suffix = doi.split("/", 1)[-1] if "/" in doi else doi
    # Cell Press showPdf uses PII — try common Cell journal DOI patterns
    import re
    cell_url = _build_cell_press_url(doi)
    if cell_url and "/abstract/" in cell_url:
        pii = cell_url.split("/abstract/")[-1]
        paths.append(f"/action/showPdf?pii={pii}")
        paths.append(f"/science/article/pii/{pii}/pdfft")
    # General ScienceDirect path with DOI
    paths.append(f"/doi/pdfdirect/{doi}")
    paths.append(f"/science/article/pii/{doi_suffix.replace('j.', 'j.')}/pdfft")
    return paths


_PUBLISHER_SSO_CONFIG: dict[str, dict[str, Any]] = {
    "Wiley": {
        "sso_link_js": _make_sso_click_js("ssostart"),
        "search_selectors": ["#searchInstitution"],
        "pdf_paths": lambda doi: [f"/doi/pdfdirect/{doi}", f"/doi/pdf/{doi}"],
    },
    "Elsevier": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional",
                                           text_patterns=("access through your institution", "institutional access")),
        "search_selectors": ["#institution-search", "input[name='query']", "#bdd-email"],
        "pdf_paths": lambda doi: _elsevier_pdf_paths(doi),
    },
    "Springer": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional-login"),
        "search_selectors": ["#idp-search", "input[name='idpSearch']", "#searchInstitution"],
        "pdf_paths": lambda doi: [f"/content/pdf/{doi}.pdf"],
    },
    "ACS": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional"),
        "search_selectors": ["input[name='search']", "#searchInstitution"],
        "pdf_paths": lambda doi: [f"/doi/pdf/{doi}"],
    },
    "IEEE": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional",
                                           text_patterns=("institutional sign in",)),
        "search_selectors": ["input[name='idpSearch']", "#searchInstitution"],
        "pdf_paths": lambda doi: [],  # IEEE uses stamp URL, needs page-level extraction
    },
    "Tandfonline": {
        "sso_link_js": _make_sso_click_js("ssostart", "shibboleth", "institutional",
                                           text_patterns=("access through your institution",)),
        "search_selectors": [
            'input[placeholder*="institution"]',
            'input[placeholder*="Type the name"]',
            "#searchInstitution",
            "input[name='query']",
        ],
        "pdf_paths": lambda doi: [f"/doi/pdf/{doi}"],
    },
    "Oxford": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional"),
        "search_selectors": ["#searchInstitution", "input[name='query']"],
        "pdf_paths": lambda doi: [f"/downloadpdf/{doi}"],
    },
    "RSC": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional"),
        "search_selectors": ["input[name='search']", "#searchInstitution"],
        "pdf_paths": lambda doi: [f"/content/articlepdf/{doi}"],
    },
    "IOP": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional"),
        "search_selectors": ["input[name='search']", "#searchInstitution"],
        "pdf_paths": lambda doi: [f"/article/{doi}/pdf"],
    },
    "APS": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional"),
        "search_selectors": ["input[name='search']", "#searchInstitution"],
        "pdf_paths": lambda doi: [f"/pdf/{doi}"],
    },
    "AIP": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional"),
        "search_selectors": ["input[name='search']", "#searchInstitution"],
        "pdf_paths": lambda doi: [],
    },
    "Nature": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional"),
        "search_selectors": ["input[name='idpSearch']", "#searchInstitution"],
        "pdf_paths": lambda doi: [f"/articles/{doi}.pdf"],
    },
    "Science": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional"),
        "search_selectors": ["input[name='search']", "#searchInstitution"],
        "pdf_paths": lambda doi: [f"/doi/pdf/{doi}"],
    },
    "ACM": {
        "sso_link_js": _make_sso_click_js("shibboleth", "institutional"),
        "search_selectors": ["input[name='search']", "#searchInstitution"],
        "pdf_paths": lambda doi: [f"/doi/pdf/{doi}"],
    },
    "_default": {
        "sso_link_js": """
            (() => {
                const links = [...document.querySelectorAll('a')];
                for (const a of links) {
                    const href = (a.href || '').toLowerCase();
                    const text = (a.innerText || '').toLowerCase();
                    if (href.includes('ssostart') || href.includes('shibboleth') ||
                        href.includes('saml') || href.includes('institutional-login') ||
                        href.includes('federation') ||
                        (text.includes('institutional') && text.includes('login')) ||
                        text.includes('access through your institution')) {
                        a.click();
                        return;
                    }
                }
            })()
        """,
        "search_selectors": ["#searchInstitution", "input[name='query']", "input[name='search']", "#institution-search"],
        "pdf_paths": lambda doi: [f"/doi/pdf/{doi}", f"/doi/pdfdirect/{doi}", f"/content/pdf/{doi}.pdf"],
    },
}

_CHALLENGE_SIGNATURES = [
    "cf-browser-verification",
    "challenge-platform",
    "just a moment",
    "attention required",
    "security check",
    "verify you are human",
    "access to this page has been denied",
    "please complete the security check",
    "px-captcha",
    "hcaptcha",
    "recaptcha",
    "radware",
    "request rejected",
    "crasolve",
]


def _is_challenge_page(html: str) -> bool:
    """Detect if the page is an anti-bot challenge."""
    lower = html[:5000].lower()
    return any(sig in lower for sig in _CHALLENGE_SIGNATURES)


# ============================================================
# Elsevier-specific detection
# ============================================================

def _is_elsevier_crasolve(url: str, html: str) -> bool:
    """Detect Elsevier's crasolve anti-bot shell."""
    if "crasolve=1" in url.lower():
        return True
    lower = html[:3000].lower()
    return "crasolve" in lower and "sciencedirect" in url.lower()


def _is_elsevier_pdf_security(html: str) -> bool:
    """Detect Elsevier's PDF security verification page."""
    lower = html[:3000].lower()
    return (
        "pdf.sciencedirectassets.com" in lower
        and ("security verification" in lower or "verify" in lower)
    )


# ============================================================
# AIP/AVS loading page detection
# ============================================================

def _is_aip_loading_page(html: str) -> bool:
    """Detect AIP's loading/waiting page."""
    lower = html[:3000].lower()
    return "loading" in lower and ("aip" in lower or "pubs.aip.org" in lower)


# ============================================================
# Core browser strategy
# ============================================================

def _try_http_download(
    pdf_url: str,
    output_path: Path,
    config: dict[str, Any],
    cookies: dict[str, str] | None = None,
) -> bool:
    """Try direct HTTP download of a PDF URL. Returns True on success."""
    import requests
    from .network import USER_AGENT
    from .pdf_utils import _response_looks_pdf, is_pdf_file
    from .sources.publishers import _write_pdf_atomic, _load_publisher_cookies

    try:
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": USER_AGENT})
        _load_publisher_cookies(s, config)
        if cookies:
            for name, value in cookies.items():
                s.cookies.set(name, value)

        resp = s.get(pdf_url, timeout=20, stream=True,
                     headers={"Accept": "application/pdf,*/*"},
                     allow_redirects=True)

        if resp.status_code >= 400:
            return False

        iterator = resp.iter_content(chunk_size=8192)
        first = next(iterator, b"")
        if not _response_looks_pdf(resp, first):
            return False

        if not _write_pdf_atomic(output_path, first, iterator):
            return False
        return is_pdf_file(output_path)
    except Exception:
        return False


def _extract_pdf_from_page(
    html: str,
    article_url: str,
    publisher: str,
) -> str | None:
    """Extract PDF URL from article page HTML using publisher-specific selectors."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None

    soup = BeautifulSoup(html, "html.parser")
    selectors = _PDF_LINK_SELECTORS.get(publisher, [])

    for selector in selectors:
        try:
            for el in soup.select(selector):
                href = el.get("href", "")
                if not href:
                    continue
                # Resolve relative URLs
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    href = urljoin(article_url, href)
                if href.startswith("http"):
                    return href
        except Exception:
            continue

    # Generic fallback: look for any link containing "pdf" (exclude supplements)
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        # Skip supplement/attachment links
        if any(skip in href for skip in ["/attachment/", "/cms/", "/mmc", "supplement", "supporting"]):
            continue
        if "/pdf/" in href or href.endswith(".pdf") or "pdfdirect" in href:
            if a["href"].startswith("http"):
                return a["href"]
            elif a["href"].startswith("/"):
                from urllib.parse import urljoin
                return urljoin(article_url, a["href"])

    return None


def _is_campus_network(config: dict[str, Any]) -> bool:
    """Check if user is on campus network (IP-authenticated access)."""
    return config.get("is_campus_network", False)


def _browser_download(
    doi: str,
    article_url: str,
    output_path: Path,
    config: dict[str, Any],
    publisher: str,
    *,
    wait_for_loading: float = 0,
) -> bool:
    """Navigate to article page via browser, find PDF link, download it."""
    from .browser_engine import (
        is_available, create_tab, close_tab, evaluate_js,
        navigate_tab, download_pdf_via_browser, _is_pdf_url,
        fetch_url, get_captured_responses,
    )
    from .pdf_utils import is_pdf_file

    _clear_error()

    # Campus network fast-path: skip HTTP, go directly to CloakBrowser
    # Campus networks use IP authentication, so CloakBrowser can access directly
    if _is_campus_network(config) and is_available(config):
        log.info(f"   [{publisher}] campus network detected, using CloakBrowser directly")
        if download_pdf_via_browser(article_url, output_path, config):
            from .pdf_utils import is_pdf_file, success
            if is_pdf_file(output_path):
                log.info(f"   [{publisher}] campus network download succeeded")
                return success(doi, output_path, f"{publisher}(Campus)")

    # Fast-path: if we have publisher cookies and a PDF-like URL, try HTTP first
    # This avoids 15-30s browser startup overhead
    if _has_publisher_cookies(config) and _is_pdf_url(article_url):
        log.info(f"   [{publisher}] trying HTTP with cookies first (fast-path)")
        if _try_http_download(article_url, output_path, config):
            from .pdf_utils import is_pdf_file, success
            if is_pdf_file(output_path):
                log.info(f"   [{publisher}] HTTP download succeeded with cached cookies")
                return success(doi, output_path, f"{publisher}(HTTP)")

    if not is_available(config):
        log.info(f"   [{publisher}] browser not available, skipping browser strategy")
        _set_error("browser_unavailable", "start_browser")
        return False

    log.info(f"   [{publisher}] browser download: {article_url[:80]}")

    # Create tab to a lightweight page first, inject cookies, then navigate to target
    tab_id = create_tab("https://www.google.com/", config, timeout=15.0)
    if not tab_id:
        return False

    try:
        # Inject saved publisher/CARSI cookies into the browser session
        _inject_cookies_to_tab(tab_id, config, publisher)

        # Now navigate to the actual article page
        log.info(f"   [{publisher}] navigating to {article_url[:80]}")
        nav_ok = navigate_tab(tab_id, article_url, config, timeout=60.0)
        if not nav_ok:
            log.info(f"   [{publisher}] navigation failed, tab may be destroyed")
            _set_error("navigate_failed", "cloudflare_timeout")
            return False

        # Wait for loading pages (AIP/AVS)
        if wait_for_loading > 0:
            time.sleep(wait_for_loading)

        # Wait for page to settle
        time.sleep(3)

        # Detect meta-refresh redirects (e.g., linkinghub.elsevier.com)
        # Wait longer if we're on a redirect page
        current_url = evaluate_js(tab_id, "window.location.href", config) or ""
        if "linkinghub" in current_url or "retrieve/pii" in current_url:
            log.info(f"   [{publisher}] on Elsevier redirect hub, waiting for meta-refresh...")
            # Wait for the browser to follow the meta-refresh chain
            for _ in range(5):
                time.sleep(3)
                new_url = evaluate_js(tab_id, "window.location.href", config) or ""
                if new_url != current_url and "linkinghub" not in new_url and "retrieve/pii" not in new_url:
                    log.info(f"   [{publisher}] redirected to {new_url[:80]}")
                    break
            else:
                # Manually extract and follow meta-refresh
                redirect_url = evaluate_js(tab_id, """
                    (() => {
                        const meta = document.querySelector('meta[http-equiv="REFRESH"], meta[http-equiv="refresh"]');
                        if (meta) {
                            const content = meta.content || '';
                            const urlMatch = content.match(/url\\s*=\\s*['"]?([^'"]+)/i);
                            if (urlMatch) {
                                let target = urlMatch[1];
                                if (target.startsWith('/')) target = window.location.origin + target;
                                return target;
                            }
                        }
                        return null;
                    })()
                """, config)
                if redirect_url and isinstance(redirect_url, str) and redirect_url.startswith("http"):
                    log.info(f"   [{publisher}] manually following redirect to {redirect_url[:80]}")
                    navigate_tab(tab_id, redirect_url, config, timeout=30.0)
                    time.sleep(5)

            # Wait for the final page to fully render
            time.sleep(3)

        # Get page HTML
        html = evaluate_js(tab_id, "document.documentElement.outerHTML", config) or ""

        # Check for anti-bot challenges
        if _is_challenge_page(html):
            log.info(f"   [{publisher}] challenge detected, waiting for auto-resolve...")
            time.sleep(8)
            html = evaluate_js(tab_id, "document.documentElement.outerHTML", config) or ""
            if _is_challenge_page(html):
                log.info(f"   [{publisher}] challenge did not resolve")
                _set_error("cloudflare_blocked", "use_proxy_or_browser")
                return False

        # Check for paywall AFTER challenge resolution
        if _detect_paywall(html):
            log.info(f"   [{publisher}] paywall detected — trying institutional login...")
            if _try_institutional_login(tab_id, config, publisher):
                # Login succeeded, re-fetch page content
                time.sleep(3)
                html = evaluate_js(tab_id, "document.documentElement.outerHTML", config) or html
                current_url = evaluate_js(tab_id, "window.location.href", config) or article_url
                if _detect_paywall(html):
                    log.info(f"   [{publisher}] still behind paywall after login")
                    _set_error("paywall", "login_required")
                    return False
            else:
                _set_error("paywall", "login_required")
                return False

        # Also detect paywall by absence of PDF links (Cell Press pattern)
        if publisher == "Elsevier" and "cell.com" in str(evaluate_js(tab_id, "window.location.href", config) or ""):
            has_showpdf = evaluate_js(tab_id, """
                (() => {
                    return document.querySelectorAll('a[href*="showPdf"], a[href*="pdfExtended"]').length;
                })()
            """, config)
            # Use JS to check for institutional access links (HTML can be 500K+)
            has_institutional = evaluate_js(tab_id, """
                (() => {
                    const links = document.querySelectorAll('a[href*="institution"], a[href*="subscribe"]');
                    for (const a of links) {
                        const text = (a.innerText || '').toLowerCase();
                        const href = (a.href || '').toLowerCase();
                        if (text.includes('institutional access') || text.includes('access through your institution')
                            || text.includes('subscribe') || text.includes('purchase')) return true;
                    }
                    return false;
                })()
            """, config)
            if has_institutional and not has_showpdf:
                log.info(f"   [{publisher}] Cell Press paywall detected (institutional links present, no PDF links)")
                _set_error("paywall", "login_required")
                return False

        # Elsevier-specific: detect crasolve
        if publisher == "Elsevier":
            current_url = evaluate_js(tab_id, "window.location.href", config) or article_url
            if _is_elsevier_crasolve(str(current_url), html):
                log.info(f"   [{publisher}] crasolve shell detected, waiting...")
                time.sleep(5)
                html = evaluate_js(tab_id, "document.documentElement.outerHTML", config) or ""

        # AIP-specific: check for loading page
        if publisher == "AIP" and _is_aip_loading_page(html):
            log.info(f"   [{publisher}] loading page detected, waiting...")
            time.sleep(10)
            html = evaluate_js(tab_id, "document.documentElement.outerHTML", config) or ""

        # Get current URL after potential redirects
        current_url = evaluate_js(tab_id, "window.location.href", config) or article_url

        # Try to extract PDF link from page
        pdf_url = _extract_pdf_from_page(html, str(current_url), publisher)

        if pdf_url:
            log.info(f"   [{publisher}] found PDF link: {pdf_url[:80]}")

            # Strategy 1: Network response capture (navigate and intercept PDF at network layer)
            captured = fetch_url(tab_id, pdf_url, config, timeout=30.0)
            if captured and captured.get("data"):
                pdf_data = captured["data"]
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(pdf_data)
                log.info(f"   [{publisher}] downloaded {len(pdf_data)} bytes via network capture")
                close_tab(tab_id, config)
                from .pdf_utils import is_pdf_file, success
                if is_pdf_file(output_path):
                    return success(doi, output_path, f"{publisher}(Network)")
                return True

            # Strategy 2: Check any already-captured responses
            captured_resps = get_captured_responses(tab_id, config, consume=True)
            import base64 as _b64_net
            for resp in captured_resps:
                data = resp.get("dataBase64", "")
                if data:
                    pdf_bytes = _b64_net.b64decode(data)
                    if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(pdf_bytes)
                        log.info(f"   [{publisher}] downloaded {len(pdf_bytes)} bytes from captured response")
                        close_tab(tab_id, config)
                        from .pdf_utils import is_pdf_file, success
                        if is_pdf_file(output_path):
                            return success(doi, output_path, f"{publisher}(Network)")
                        return True

            # Strategy 3: Try in-browser fetch for the PDF URL
            # This bypasses Cloudflare because the request comes from the browser context
            import base64 as _b64
            from urllib.parse import urlparse as _urlparse

            parsed = _urlparse(pdf_url)
            fetch_paths = [parsed.path]
            # Also try pdfdirect variant
            if "/doi/pdf/" in parsed.path:
                fetch_paths.append(parsed.path.replace("/doi/pdf/", "/doi/pdfdirect/"))
            elif "/doi/epdf/" in parsed.path:
                fetch_paths.append(parsed.path.replace("/doi/epdf/", "/doi/pdfdirect/"))

            for fetch_path in fetch_paths:
                log.info(f"   [{publisher}] trying in-browser fetch {fetch_path[:60]}")
                pdf_b64 = evaluate_js(tab_id, f"""
                    (async () => {{
                        try {{
                            const resp = await fetch('{fetch_path}', {{
                                credentials: 'include',
                                headers: {{'Accept': 'application/pdf,*/*'}}
                            }});
                            if (!resp.ok) return 'status:' + resp.status;
                            const ct = resp.headers.get('content-type') || '';
                            if (!ct.includes('pdf') && !ct.includes('octet')) return 'ct:' + ct;
                            const blob = await resp.blob();
                            return new Promise((resolve) => {{
                                const reader = new FileReader();
                                reader.onload = () => resolve(reader.result);
                                reader.readAsDataURL(blob);
                            }});
                        }} catch(e) {{
                            return 'error:' + e.message;
                        }}
                    }})()
                """, config, timeout=45.0)

                if isinstance(pdf_b64, str) and pdf_b64.startswith("data:"):
                    header, data = pdf_b64.split(",", 1)
                    pdf_bytes = _b64.b64decode(data)
                    if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(pdf_bytes)
                        log.info(f"   [{publisher}] downloaded {len(pdf_bytes)} bytes via in-browser fetch")
                        close_tab(tab_id, config)
                        from .pdf_utils import is_pdf_file, success
                        if is_pdf_file(output_path):
                            return success(doi, output_path, f"{publisher}(Browser)")
                        return True
                elif isinstance(pdf_b64, str) and "status:403" in pdf_b64:
                    log.info(f"   [{publisher}] PDF fetch returned 403 — trying institutional login...")
                    if _try_institutional_login(tab_id, config, publisher):
                        # Retry fetch after login
                        pdf_b64_retry = evaluate_js(tab_id, f"""
                            (async () => {{
                                try {{
                                    const resp = await fetch('{fetch_path}', {{
                                        credentials: 'include',
                                        headers: {{'Accept': 'application/pdf,*/*'}}
                                    }});
                                    if (!resp.ok) return 'status:' + resp.status;
                                    const ct = resp.headers.get('content-type') || '';
                                    if (!ct.includes('pdf') && !ct.includes('octet')) return 'ct:' + ct;
                                    const blob = await resp.blob();
                                    return new Promise((resolve) => {{
                                        const reader = new FileReader();
                                        reader.onload = () => resolve(reader.result);
                                        reader.readAsDataURL(blob);
                                    }});
                                }} catch(e) {{
                                    return 'error:' + e.message;
                                }}
                            }})()
                        """, config, timeout=45.0)
                        if isinstance(pdf_b64_retry, str) and pdf_b64_retry.startswith("data:"):
                            _, data = pdf_b64_retry.split(",", 1)
                            pdf_bytes = _b64.b64decode(data)
                            if pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
                                output_path.parent.mkdir(parents=True, exist_ok=True)
                                output_path.write_bytes(pdf_bytes)
                                log.info(f"   [{publisher}] downloaded {len(pdf_bytes)} bytes after institutional login")
                                close_tab(tab_id, config)
                                from .pdf_utils import is_pdf_file, success
                                if is_pdf_file(output_path):
                                    return success(doi, output_path, f"{publisher}(Institutional)")
                                return True
                    _set_error("paywall", "login_required")
                elif isinstance(pdf_b64, str) and "status:401" in pdf_b64:
                    log.info(f"   [{publisher}] PDF fetch returned 401 — trying institutional login...")
                    if not _try_institutional_login(tab_id, config, publisher):
                        _set_error("paywall", "login_required")
                elif isinstance(pdf_b64, str) and pdf_b64.startswith("ct:text/html"):
                    log.info(f"   [{publisher}] PDF fetch returned HTML — trying institutional login...")
                    if not _try_institutional_login(tab_id, config, publisher):
                        _set_error("paywall", "login_required")

            close_tab(tab_id, config)

            # Fall back to HTTP download
            if _try_http_download(pdf_url, output_path, config):
                return True

            # Fall back to full browser download
            return download_pdf_via_browser(pdf_url, output_path, config)

        # For Elsevier/Cell Press: try specific PDF link patterns
        if publisher == "Elsevier":
            pdf_url = evaluate_js(tab_id, """
                (() => {
                    // Cell Press: showPdf link
                    for (const a of document.querySelectorAll('a[href*="showPdf"]')) {
                        if (a.href) return a.href;
                    }
                    // ScienceDirect: click View PDF button
                    const btn = document.querySelector('a[aria-label*="View PDF" i], a.pdf-download-btn-link');
                    if (btn && btn.href) return btn.href;
                    // Check for pdfft links (PDF viewer)
                    for (const a of document.querySelectorAll('a[href*="pdfft"]')) {
                        if (a.href) return a.href;
                    }
                    return null;
                })()
            """, config)

            if pdf_url and isinstance(pdf_url, str):
                log.info(f"   [{publisher}] Elseviewer PDF button: {pdf_url[:80]}")
                close_tab(tab_id, config)
                if _try_http_download(pdf_url, output_path, config):
                    return True
                return download_pdf_via_browser(pdf_url, output_path, config)

        # For Wiley: specifically look for pdfdirect
        if publisher == "Wiley":
            pdf_url = evaluate_js(tab_id, """
                (() => {
                    for (const a of document.querySelectorAll('a[href*="pdfdirect"]')) {
                        if (a.href) return a.href;
                    }
                    // Filter out supplement links
                    for (const a of document.querySelectorAll('a[href*="/doi/pdf/"]')) {
                        const href = a.href.toLowerCase();
                        if (!href.includes('supplement') && !href.includes('supporting')) {
                            return a.href;
                        }
                    }
                    return null;
                })()
            """, config)

            if pdf_url and isinstance(pdf_url, str):
                log.info(f"   [{publisher}] Wiley PDFDirect: {pdf_url[:80]}")
                close_tab(tab_id, config)
                if _try_http_download(pdf_url, output_path, config):
                    return True
                return download_pdf_via_browser(pdf_url, output_path, config)

        # For IEEE: look for stamp URL
        if publisher == "IEEE":
            pdf_url = evaluate_js(tab_id, """
                (() => {
                    for (const a of document.querySelectorAll('a[href*="/stamp/"], a[href*="getPDF"]')) {
                        if (a.href) return a.href;
                    }
                    const btn = document.querySelector('a.xpl-pdf-btn');
                    if (btn && btn.href) return btn.href;
                    return null;
                })()
            """, config)

            if pdf_url and isinstance(pdf_url, str):
                log.info(f"   [{publisher}] IEEE stamp: {pdf_url[:80]}")
                close_tab(tab_id, config)
                return download_pdf_via_browser(pdf_url, output_path, config)

        log.info(f"   [{publisher}] no PDF link found on page")
        if not _last_error_type:
            _set_error("no_pdf_found", "try_other_source")
        return False
    finally:
        close_tab(tab_id, config)


def _browser_download_with_fallback(
    doi: str,
    article_url: str,
    output_path: Path,
    config: dict[str, Any],
    publisher: str,
) -> bool:
    """Try headless browser download, fallback to visible browser if paywall detected."""
    result = _browser_download(doi, article_url, output_path, config, publisher)
    if result:
        return True

    # If paywall or navigate failure, try visible browser with SSO login
    err_type, err_action = get_last_error()
    if err_type in ("paywall", "navigate_failed", "cloudflare_blocked") and config.get("carsi_idp_name"):
        log.info(f"   [{publisher}] headless failed ({err_type}), trying visible browser fallback...")
        try:
            vbd_result = _visible_browser_download(doi, article_url, output_path, config, publisher)
            if vbd_result:
                return True
        except Exception as e:
            log.info(f"   [{publisher}] visible browser fallback error: {e}")

    return False


# ============================================================
# Elsevier API (no browser needed)
# ============================================================

_CLOUDFLARE_COOKIE_NAMES = {"__cf_bm", "_cfuvid", "cf_clearance", "cf_chl_rc_ni"}


def _persist_api_cookies(session: Any, config: dict[str, Any]) -> int:
    """Extract cookies from a requests.Session and merge into publisher_cookies.json.

    Skips cookies that are only Cloudflare bot-management cookies (__cf_bm, _cfuvid, etc.)
    since those don't carry authentication and don't help the browser strategy.

    Returns the number of cookies persisted.
    """
    from .browser_cookies import merge_cookies

    raw = []
    meaningful = 0
    for c in session.cookies:
        cookie = {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "secure": c.secure,
            "expires": c.expires or 0,
        }
        raw.append(cookie)
        if c.name not in _CLOUDFLARE_COOKIE_NAMES:
            meaningful += 1

    if not raw:
        return 0

    if meaningful == 0:
        log.info(f"   [ElsevierAPI] {len(raw)} cookies are Cloudflare-only, skipping persist")
        return 0

    merged = merge_cookies(raw, config)
    log.info(f"   [ElsevierAPI] persisted {meaningful} meaningful cookies "
             f"({len(raw)} total, merged: {len(merged)})")
    return meaningful


def _elsevier_header(headers: Any, name: str) -> str:
    """Return a response header value using case-insensitive matching."""
    if not headers:
        return ""
    value = headers.get(name)
    if value is not None:
        return str(value)
    needle = name.lower()
    for key, value in headers.items():
        if str(key).lower() == needle:
            return str(value)
    return ""


def _elsevier_response_is_pdf(resp: Any) -> bool:
    content_type = _elsevier_header(getattr(resp, "headers", {}), "content-type").lower()
    content = getattr(resp, "content", b"") or b""
    return "pdf" in content_type or content[:5] == b"%PDF-"


def _elsevier_response_is_xml(resp: Any) -> bool:
    content_type = _elsevier_header(getattr(resp, "headers", {}), "content-type").lower()
    content = (getattr(resp, "content", b"") or b"").lstrip()
    return (
        "xml" in content_type
        or content.startswith(b"<?xml")
        or content.startswith(b"<full-text-retrieval-response")
    )


def _elsevier_response_text(resp: Any) -> str:
    text = getattr(resp, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(resp, "content", b"") or b""
    return content.decode("utf-8", errors="replace")


def _elsevier_xml_local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1].split(":", 1)[-1].lower()


def _elsevier_element_text(el: Any) -> str:
    return " ".join(" ".join(el.itertext()).split())


def _elsevier_looks_like_pdf_eid(value: str) -> bool:
    lowered = value.strip().lower()
    return bool(lowered) and (lowered.endswith(".pdf") or ".pdf" in lowered)


def _elsevier_article_eid_to_main_pdf(value: str) -> str:
    candidate = value.strip()
    if candidate.lower().startswith("eid:"):
        candidate = candidate.split(":", 1)[1].strip()
    if not candidate.startswith("1-s2.0-"):
        return ""
    if candidate.lower().endswith(".pdf"):
        return candidate
    return f"{candidate}-main.pdf"


def _elsevier_attachment_container(el: Any, parent_map: dict[Any, Any]) -> Any:
    node = parent_map.get(el, el)
    while node is not None:
        local = _elsevier_xml_local_name(str(node.tag))
        if "attachment" in local or "object" in local:
            return node
        node = parent_map.get(node)
    return parent_map.get(el, el)


def _elsevier_attachment_metadata(el: Any) -> str:
    parts: list[str] = []
    for node in el.iter():
        local = _elsevier_xml_local_name(str(node.tag))
        text = _elsevier_element_text(node)
        if text:
            parts.append(f"{local}:{text}")
        for attr_name, attr_value in node.attrib.items():
            attr_local = _elsevier_xml_local_name(str(attr_name))
            if attr_value:
                parts.append(f"{attr_local}:{attr_value}")
    return " ".join(parts).lower()


def _elsevier_attachment_score(eid: str, metadata: str) -> int:
    haystack = f"{eid} {metadata}".lower()
    score = 0
    if eid.lower().endswith(".pdf"):
        score += 20
    if "pdf" in haystack:
        score += 10
    if "main" in haystack or "full-text" in haystack or "fulltext" in haystack:
        score += 100
    if "page-count" in haystack or "pages" in haystack:
        score += 5
    if "attachment-size" in haystack or "filesize" in haystack or "file-size" in haystack:
        score += 5
    if any(marker in haystack for marker in (
        "supplement", "supplementary", "mmc", "appendix", "graphical", "thumbnail",
    )):
        score -= 100
    return score


def _extract_elsevier_pdf_attachment_eids(xml_text: str) -> list[str]:
    """Extract candidate publisher PDF object EIDs from Elsevier full-text XML."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.info(f"   [ElsevierAPI] XML parse failed: {exc}")
        return []

    parent_map = {child: parent for parent in root.iter() for child in parent}
    candidates: list[tuple[int, int, str]] = []
    seen: set[str] = set()

    for el in root.iter():
        found: list[str] = []
        local = _elsevier_xml_local_name(str(el.tag))
        if local in {"attachment-eid", "object-eid"}:
            text = _elsevier_element_text(el)
            if _elsevier_looks_like_pdf_eid(text):
                found.append(text)
        elif local in {"eid", "identifier"}:
            text = _elsevier_element_text(el)
            main_pdf = _elsevier_article_eid_to_main_pdf(text)
            if main_pdf:
                found.append(main_pdf)

        for attr_name, attr_value in el.attrib.items():
            attr_local = _elsevier_xml_local_name(str(attr_name))
            if attr_local in {"attachment-eid", "object-eid", "eid"}:
                value = str(attr_value).strip()
                if _elsevier_looks_like_pdf_eid(value):
                    found.append(value)
                else:
                    main_pdf = _elsevier_article_eid_to_main_pdf(value)
                    if main_pdf:
                        found.append(main_pdf)

        for eid in found:
            if eid in seen:
                continue
            seen.add(eid)
            container = _elsevier_attachment_container(el, parent_map)
            metadata = _elsevier_attachment_metadata(container)
            candidates.append((
                _elsevier_attachment_score(eid, metadata),
                len(candidates),
                eid,
            ))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [eid for _, _, eid in candidates]


def _elsevier_pdf_page_count(content: bytes) -> int | None:
    """Best-effort page count for detecting Elsevier one-page preview PDFs."""
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return None

    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            return int(doc.page_count)
    except Exception:
        return None


def _save_elsevier_pdf_content(
    doi: str,
    output_path: Path,
    content: bytes,
    config: dict[str, Any],
    source: str,
    *,
    reject_single_page: bool = False,
) -> dict[str, Any] | None:
    from .pdf_utils import is_pdf_file, success

    if len(content) < config.get("min_pdf_size_bytes", 10000):
        log.info(f"   [ElsevierAPI] response too small ({len(content)} bytes)")
        return None
    if reject_single_page:
        page_count = _elsevier_pdf_page_count(content)
        if page_count == 1:
            output_path.unlink(missing_ok=True)
            log.info(f"   [ElsevierAPI] direct PDF is a 1-page preview for {doi}")
            return None

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(content)
    if is_pdf_file(output_path):
        log.info(f"   [ElsevierAPI] downloaded {len(content)} bytes for {doi}")
        return success(doi, output_path, source)

    output_path.unlink(missing_ok=True)
    return None


def _try_elsevier_object_pdf_from_xml(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    session: Any,
    article_url: str,
    headers: dict[str, str],
    first_resp: Any,
    *,
    full_xml_already_tried: bool = False,
) -> dict[str, Any] | None:
    import urllib.parse

    xml_resp = None
    if (
        first_resp is not None
        and getattr(first_resp, "status_code", 0) == 200
        and _elsevier_response_is_xml(first_resp)
    ):
        xml_resp = first_resp

    if xml_resp is None:
        xml_headers = dict(headers)
        xml_headers["Accept"] = "application/xml"
        param_options = [None] if full_xml_already_tried else [{"view": "FULL"}, None]
        for params in param_options:
            try:
                request_kwargs: dict[str, Any] = {
                    "headers": xml_headers,
                    "timeout": 30,
                    "allow_redirects": True,
                }
                if params:
                    request_kwargs["params"] = params
                candidate = session.get(article_url, **request_kwargs)
            except Exception as exc:
                log.info(f"   [ElsevierAPI] XML request failed: {exc}")
                continue

            if getattr(candidate, "status_code", 0) != 200:
                view_name = "FULL XML" if params else "XML"
                log.info(
                    f"   [ElsevierAPI] {view_name} HTTP "
                    f"{getattr(candidate, 'status_code', 0)} for {doi}"
                )
                continue
            if not _elsevier_response_is_xml(candidate):
                content_type = _elsevier_header(
                    getattr(candidate, "headers", {}),
                    "content-type",
                )
                view_name = "FULL XML" if params else "XML"
                log.info(
                    f"   [ElsevierAPI] {view_name} request returned non-XML "
                    f"({content_type[:50]})"
                )
                continue

            xml_resp = candidate
            break

        if xml_resp is None:
            return None

    eids = _extract_elsevier_pdf_attachment_eids(_elsevier_response_text(xml_resp))
    if not eids:
        log.info(f"   [ElsevierAPI] XML has no PDF attachment EID for {doi}")
        return None

    object_headers = dict(headers)
    object_headers["Accept"] = "application/pdf"
    for eid in eids:
        object_url = (
            "https://api.elsevier.com/content/object/eid/"
            f"{urllib.parse.quote(eid, safe='')}"
        )
        try:
            object_resp = session.get(
                object_url,
                headers=object_headers,
                timeout=30,
                allow_redirects=True,
            )
        except Exception as exc:
            log.info(f"   [ElsevierAPI] object {eid} request failed: {exc}")
            continue

        if getattr(object_resp, "status_code", 0) != 200:
            log.info(
                f"   [ElsevierAPI] object {eid} HTTP "
                f"{getattr(object_resp, 'status_code', 0)}"
            )
            continue
        if not _elsevier_response_is_pdf(object_resp):
            content_type = _elsevier_header(
                getattr(object_resp, "headers", {}),
                "content-type",
            )
            log.info(f"   [ElsevierAPI] object {eid} returned non-PDF ({content_type[:50]})")
            continue

        result = _save_elsevier_pdf_content(
            doi,
            output_path,
            getattr(object_resp, "content", b"") or b"",
            config,
            "ElsevierAPI",
            reject_single_page=True,
        )
        if result:
            log.info(f"   [ElsevierAPI] downloaded object PDF via attachment EID {eid}")
            return result

    return None


def try_elsevier_api(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Download Elsevier/ScienceDirect PDF via Article Retrieval API.

    Uses the Elsevier Institutional API (api.elsevier.com) with an API key
    and optional institutional token. This is far faster and more reliable
    than browser-based login flows.

    When the API does not return a PDF directly (no institutional access),
    cookies from the redirect chain (api.elsevier.com → linkinghub.elsevier.com
    → sciencedirect.com) are persisted for the browser strategy to reuse.

    Config keys:
        elsevier_api_key   — personal or institutional API key (required)
        elsevier_insttoken — institutional token for campus-level access
    """
    api_key = config.get("elsevier_api_key", "")
    if not api_key:
        log.info(f"   [ElsevierAPI] no API key configured, skipping. "
                 f"Run scansci_pdf_elsevier_setup to configure (free).")
        return None

    from .network import USER_AGENT, proxy_dict, select_proxy_for_url

    url = f"https://api.elsevier.com/content/article/doi/{doi}"

    headers: dict[str, str] = {
        "User-Agent": USER_AGENT,
        "X-ELS-APIKey": api_key,
    }
    insttoken = config.get("elsevier_insttoken", "")
    if insttoken:
        headers["X-ELS-InstToken"] = insttoken

    proxy = select_proxy_for_url(url, config)
    configured_proxies = proxy_dict(proxy)
    route_options: list[tuple[str, dict[str, str]]] = [("direct", {})]
    if configured_proxies:
        route_options.append(("configured proxy", configured_proxies))

    import requests

    for route_name, proxies in route_options:
        try:
            session = requests.Session()
            session.trust_env = False
            if proxies:
                session.proxies = proxies

            log.info(f"   [ElsevierAPI] trying {route_name} route")
            xml_headers = dict(headers)
            xml_headers["Accept"] = "application/xml"
            resp = session.get(
                url,
                headers=xml_headers,
                params={"view": "FULL"},
                timeout=30,
                allow_redirects=True,
            )
        except Exception as e:
            log.info(f"   [ElsevierAPI] {route_name} FULL XML request failed: {e}")
            continue

        if resp is not None and resp.status_code != 200:
            if resp.status_code in (403, 429):
                log.info(
                    f"   [ElsevierAPI] {route_name} HTTP {resp.status_code}; "
                    "API access may be unavailable or rate limited"
                )
            else:
                log.info(f"   [ElsevierAPI] {route_name} HTTP {resp.status_code} for {doi}")

        if resp is not None and resp.status_code == 200 and _elsevier_response_is_pdf(resp):
            result = _save_elsevier_pdf_content(
                doi,
                output_path,
                resp.content,
                config,
                "ElsevierAPI",
                reject_single_page=True,
            )
            if result:
                try:
                    _persist_api_cookies(session, config)
                except Exception:
                    pass
                return result

        result = _try_elsevier_object_pdf_from_xml(
            doi,
            output_path,
            config,
            session,
            url,
            headers,
            resp,
            full_xml_already_tried=True,
        )
        if result:
            try:
                _persist_api_cookies(session, config)
            except Exception:
                pass
            return result

        content_type = _elsevier_header(
            getattr(resp, "headers", {}) if resp is not None else {},
            "content-type",
        )
        log.info(
            f"   [ElsevierAPI] {route_name} non-PDF response "
            f"({content_type[:50]}), trying next route if available"
        )
        try:
            _persist_api_cookies(session, config)
        except Exception as e:
            log.info(f"   [ElsevierAPI] cookie persist failed: {e}")

    return None


# ============================================================
# Publisher entry points
# ============================================================

def try_elsevier_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Elsevier/ScienceDirect/Cell Press browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success
    from .browser_engine import is_available as browser_available, download_pdf_via_browser

    # Campus network fast-path: skip HTTP, go directly to CloakBrowser
    if _is_campus_network(config) and browser_available(config):
        cell_url = _build_cell_press_url(doi)
        if cell_url:
            log.info(f"   [Elsevier] campus network detected, trying CloakBrowser directly: {cell_url[:80]}")
            if download_pdf_via_browser(cell_url, output_path, config):
                if is_pdf_file(output_path):
                    log.info(f"   [Elsevier] campus network download succeeded")
                    return success(doi, output_path, "CellPress(Campus)")

    # Fast-path: if we have publisher cookies, try HTTP download first
    # This avoids 15-30s browser startup overhead
    if _has_publisher_cookies(config):
        # Try Cell Press showPdf with PII first (most reliable for Cell Press)
        cell_url = _build_cell_press_url(doi)
        if cell_url and "/abstract/" in cell_url:
            pii = cell_url.split("/abstract/")[-1]
            show_pdf_url = f"https://www.cell.com/action/showPdf?pii={pii}"
            log.info(f"   [Elsevier] trying Cell Press showPdf HTTP with cookies: {show_pdf_url[:80]}")
            if _try_http_download(show_pdf_url, output_path, config):
                if is_pdf_file(output_path):
                    log.info(f"   [Elsevier] Cell Press HTTP download succeeded with cached cookies")
                    return success(doi, output_path, "CellPress(HTTP)")

    # For Cell Press DOIs, try direct showPdf download first (bypasses linkinghub)
    cell_url = _build_cell_press_url(doi)
    if cell_url and "/abstract/" in cell_url:
        pii = cell_url.split("/abstract/")[-1]
        show_pdf_url = f"https://www.cell.com/action/showPdf?pii={pii}"
        log.info(f"   [CellPress] trying showPdf via network capture: {show_pdf_url[:80]}")
        result = _cell_press_showpdf_download(show_pdf_url, output_path, config)
        if result and is_pdf_file(output_path):
            return result

        # If showPdf failed, navigate to the abstract page directly
        log.info(f"   [CellPress] trying abstract page: {cell_url[:80]}")
        if _browser_download_with_fallback(doi, cell_url, output_path, config, "Elsevier"):
            if is_pdf_file(output_path):
                return success(doi, output_path, "CellPress(Browser)")

    # Fallback to general browser download for ScienceDirect articles
    # Only try if we haven't already detected a paywall
    if not _last_error_type:
        # Resolve DOI to direct sciencedirect.com URL (avoid linkinghub)
        article_url = _resolve_elsevier_pii(doi, config) or f"https://doi.org/{doi}"
        if _browser_download_with_fallback(doi, article_url, output_path, config, "Elsevier"):
            if is_pdf_file(output_path):
                return success(doi, output_path, "Elsevier(Browser)")
    return None


def _cell_press_showpdf_download(
    show_pdf_url: str,
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Download a Cell Press showPdf URL via browser network capture."""
    from .browser_engine import create_tab, close_tab, fetch_url
    from .pdf_utils import success

    tab_id = create_tab("https://example.com", config, timeout=15.0)
    if not tab_id:
        return None

    try:
        _inject_cookies_to_tab(tab_id, config, "Elsevier")
        result = fetch_url(tab_id, show_pdf_url, config, timeout=30.0)
        if result and result.get("data"):
            pdf_bytes = result["data"]
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(pdf_bytes)
            log.info(f"   [CellPress] downloaded {len(pdf_bytes)} bytes via showPdf")
            return success(show_pdf_url, output_path, "CellPress(Network)")
    except Exception as e:
        log.info(f"   [CellPress] showPdf failed: {e}")
    finally:
        close_tab(tab_id, config)
    return None


# Cell Press journal mapping from DOI prefix to cell.com path
_CELL_JOURNAL_MAP = {
    "j.oneear": "one-earth",
    "j.cell": "cell",
    "j.cels": "cancer-cell",
    "j.cub": "current-biology",
    "j.neuron": "neuron",
    "j.molcel": "molecular-cell",
    "j.devcel": "developmental-cell",
    "j.immuni": "immunity",
    "j.chom": "cell-host-microbe",
    "j.cmet": "cell-metabolism",
    "j.stem": "cell-stem-cell",
    "j.celrep": "cell-reports",
    "j.isci": "iscience",
    "j.xcr": "cell-reports-physical-science",
    "j.heliyon": "heliyon",
    "j.applanim": "ajhg",
    "j.ajhg": "ajhg",
}


def _build_cell_press_url(doi: str) -> str | None:
    """Build direct cell.com abstract URL from DOI if it's a Cell Press journal."""
    import re
    doi_suffix = doi.split("/", 1)[-1] if "/" in doi else ""
    if not doi_suffix.startswith("j."):
        return None

    parts = doi_suffix.split(".")
    if len(parts) < 3:
        return None

    # Try to match journal: j.oneear, j.cell, etc.
    journal_key = f"{parts[0]}.{parts[1]}"
    journal_path = _CELL_JOURNAL_MAP.get(journal_key)
    if not journal_path:
        return None

    # Try to get PII from Crossref alternative-id
    pii = _get_elsevier_pii(doi)
    if pii:
        return f"https://www.cell.com/{journal_path}/abstract/{pii}"

    # Fallback: use /doi/ path (may not have showPdf links)
    return f"https://www.cell.com/{journal_path}/doi/{doi}"


def _get_elsevier_pii(doi: str) -> str | None:
    """Get PII (Publisher Item Identifier) from Crossref API."""
    try:
        import requests
        from .network import USER_AGENT
        resp = requests.get(
            f"https://api.crossref.org/works/{doi}",
            timeout=10, verify=False,
            headers={"User-Agent": USER_AGENT},
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("message", {})
        alt_ids = data.get("alternative-id", [])
        for aid in alt_ids:
            if aid.startswith("S") and len(aid) >= 16:
                return aid
    except Exception:
        pass
    return None


def _resolve_elsevier_pii(doi: str, config: dict[str, Any]) -> str | None:
    """Resolve Elsevier DOI to direct article URL, bypassing linkinghub.elsevier.com.

    Uses HTTP to get the PII from linkinghub, then uses Crossref to determine the journal.
    Returns a direct cell.com/sciencedirect.com URL, or None.
    """
    import re
    try:
        import requests
        from .network import USER_AGENT
        s = requests.Session()
        s.trust_env = False
        s.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html"})
        resp = s.get(f"https://doi.org/{doi}", timeout=10, allow_redirects=True)
        final_url = str(resp.url)
        html = resp.text

        # Extract PII from URL
        pii_match = re.search(r'pii/(S\d+)', final_url)
        if not pii_match:
            return None
        pii = pii_match.group(1)

        # Check if meta-refresh points to sciencedirect.com or cell.com
        if "sciencedirect.com" in html:
            return f"https://www.sciencedirect.com/science/article/pii/{pii}"

        # Check for cell.com in meta-refresh
        if "cell.com" in html:
            cell_match = re.search(r'cell\.com/([^/]+)/', html)
            if cell_match:
                journal = cell_match.group(1)
                return f"https://www.cell.com/{journal}/abstract/{pii}"

        return None
    except Exception:
        return None


def try_wiley_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Wiley browser strategy with PDFDirect."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success
    from .browser_engine import is_available as browser_available, download_pdf_via_browser

    # Campus network fast-path: skip HTTP, go directly to CloakBrowser
    if _is_campus_network(config) and browser_available(config):
        article_url = f"https://doi.org/{doi}"
        log.info(f"   [Wiley] campus network detected, trying CloakBrowser directly")
        if download_pdf_via_browser(article_url, output_path, config):
            if is_pdf_file(output_path):
                log.info(f"   [Wiley] campus network download succeeded")
                return success(doi, output_path, "Wiley(Campus)")

    # Try direct PDF URLs first
    for url in _direct_pdf_urls(doi, "Wiley"):
        if _try_http_download(url, output_path, config):
            if is_pdf_file(output_path):
                return success(doi, output_path, "Wiley(PDFDirect)")

    # Fall back to browser — use direct Wiley URL to avoid slow DOI redirect
    article_url = f"https://onlinelibrary.wiley.com/doi/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "Wiley"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "Wiley(Browser)")
    return None


def try_ieee_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """IEEE browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "IEEE"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "IEEE(Browser)")
    return None


def try_acs_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """ACS browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    # Try direct PDF URL first
    for url in _direct_pdf_urls(doi, "ACS"):
        if _try_http_download(url, output_path, config):
            if is_pdf_file(output_path):
                return success(doi, output_path, "ACS(Direct)")

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "ACS"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "ACS(Browser)")
    return None


def try_rsc_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """RSC browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    for url in _direct_pdf_urls(doi, "RSC"):
        if _try_http_download(url, output_path, config):
            if is_pdf_file(output_path):
                return success(doi, output_path, "RSC(Direct)")

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "RSC"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "RSC(Browser)")
    return None


def try_aip_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """AIP browser strategy with loading page wait."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "AIP", wait_for_loading=10):
        if is_pdf_file(output_path):
            return success(doi, output_path, "AIP(Browser)")
    return None


def try_springer_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Springer browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    # Try direct PDF URL first
    for url in _direct_pdf_urls(doi, "Springer"):
        if _try_http_download(url, output_path, config):
            if is_pdf_file(output_path):
                return success(doi, output_path, "Springer(Direct)")

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "Springer"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "Springer(Browser)")
    return None


def try_aps_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """APS (Physical Review) browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    for url in _direct_pdf_urls(doi, "APS"):
        if _try_http_download(url, output_path, config):
            if is_pdf_file(output_path):
                return success(doi, output_path, "APS(Direct)")

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "APS"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "APS(Browser)")
    return None


def try_tandfonline_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Taylor & Francis browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    for url in _direct_pdf_urls(doi, "Tandfonline"):
        if _try_http_download(url, output_path, config):
            if is_pdf_file(output_path):
                return success(doi, output_path, "T&F(Direct)")

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "Tandfonline"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "T&F(Browser)")
    return None


def try_iop_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """IOP browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    for url in _direct_pdf_urls(doi, "IOP"):
        if _try_http_download(url, output_path, config):
            if is_pdf_file(output_path):
                return success(doi, output_path, "IOP(Direct)")

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "IOP"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "IOP(Browser)")
    return None


def try_oxford_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Oxford Academic browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    for url in _direct_pdf_urls(doi, "Oxford"):
        if _try_http_download(url, output_path, config):
            if is_pdf_file(output_path):
                return success(doi, output_path, "Oxford(Direct)")

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "Oxford"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "Oxford(Browser)")
    return None


def try_acm_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """ACM browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    for url in _direct_pdf_urls(doi, "ACM"):
        if _try_http_download(url, output_path, config):
            if is_pdf_file(output_path):
                return success(doi, output_path, "ACM(Direct)")

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "ACM"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "ACM(Browser)")
    return None


def try_nature_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Nature browser strategy (fallback when direct fails)."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "Nature"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "Nature(Browser)")
    return None


def try_science_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Science/AAAS browser strategy."""
    from .pdf_utils import is_pdf_file
    from .pdf_utils import success

    article_url = f"https://doi.org/{doi}"
    if _browser_download_with_fallback(doi, article_url, output_path, config, "Science"):
        if is_pdf_file(output_path):
            return success(doi, output_path, "Science(Browser)")
    return None


# ============================================================
# Generic publisher browser fallback
# ============================================================

def try_generic_browser(
    doi: str, output_path: Path, config: dict[str, Any],
) -> dict[str, Any] | None:
    """Generic browser fallback for unknown publishers."""
    from .pdf_utils import is_pdf_file
    from .sources.publishers import get_publisher
    from .pdf_utils import success

    publisher = get_publisher(doi) or "Unknown"
    article_url = f"https://doi.org/{doi}"

    if _browser_download_with_fallback(doi, article_url, output_path, config, publisher):
        if is_pdf_file(output_path):
            return success(doi, output_path, f"{publisher}(Browser)")
    return None
