"""CNKI / Wanfang / VIP institutional lanes (CARSI + campus egress).

Three different access models are handled here:

* **Wanfang** — the landing page is a SPA, but its gRPC-web detail endpoint
  returns the internal file id.  The actual download is a two-hop HTTP flow
  (``file/download/<id>.aspx`` → ``NewFulltext`` → PDF).  Works with a
  campus-IP session or with saved CARSI cookies; no browser needed.
* **CNKI** — the detail page exposes ``#pdfDown``/``#cajDown`` links, but
  they are session-bound.  With a saved CARSI session we fetch them over
  HTTP; if the response is a login page we open the CARSI login browser once
  and retry.  CNKI's PDF button is preferred; CAJ is kept as ``.caj`` with
  conversion guidance instead of being mislabelled as PDF.
* **VIP (维普)** — the download button is driven by JS and anti-bot tokens,
  so this lane drives a real browser: search by title, open the article,
  click 下载PDF, and capture the download/response.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from ..caj import caj_hints, guess_format
from ..chinese import (
    is_chinese_doi,
    normalize_doi_loose,
    parse_cnki_detail,
    resolve_chinese_doi,
    response_text,
)
from ..config import DATA_DIR
from ..log import get_logger
from ..network import CHROME_UA, proxy_dict, request_timeout, select_proxy_for_url
from ..pdf_utils import is_pdf_file, success

log = get_logger()

_CN_DATABASES: dict[str, dict[str, Any]] = {
    "cnki": {
        "name": "CNKI 中国知网",
        "domains": ["cnki.net", "cnki.com.cn"],
        "login_url": "https://fsso.cnki.net/",
    },
    "wanfang": {
        "name": "万方数据",
        "domains": ["wanfangdata.com.cn"],
        "login_url": "https://fsso.wanfangdata.com.cn/",
    },
    "cqvip": {
        "name": "维普中文期刊",
        "domains": ["cqvip.com"],
        "login_url": "https://fsso.cqvip.com/",
    },
}

_WANFANG_ID_CACHE: dict[str, str] = {}
def _login_required(doi: str, database: str) -> dict[str, Any]:
    info = _CN_DATABASES.get(database, {})
    name = info.get("name", database)
    return {
        "success": False,
        "identifier": doi,
        "doi": doi,
        "source": name,
        "reason": f"{name} 需要校园网出口或机构订阅（CARSI）",
        "error_type": "paywall",
        "action": "login_required",
        "hint": {
            "campus": "连接学校校园网/机构 VPN 后重试（万方/维普通常按 IP 授权）",
            "carsi": "运行 scansci-pdf config_set carsi_enabled true，并设置 carsi_idp_name 为你的学校名",
        },
    }


def _session(config: dict[str, Any]) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    return session


def _load_saved_cookies(session: requests.Session, config: dict[str, Any]) -> int:
    """Load publisher + CARSI cookies into an HTTP session."""
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    files: list[Path] = []
    publisher_file = cache_dir / "publisher_cookies.json"
    if publisher_file.exists():
        files.append(publisher_file)
    carsi_dir = cache_dir / "carsi_cookies"
    if carsi_dir.is_dir():
        files.extend(sorted(carsi_dir.glob("*.json")))
    loaded = 0
    for cookie_file in files:
        try:
            cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(cookies, list):
            continue
        for cookie in cookies:
            try:
                session.cookies.set(
                    cookie.get("name", ""),
                    cookie.get("value", ""),
                    domain=cookie.get("domain", ""),
                    path=cookie.get("path", "/"),
                )
                loaded += 1
            except (TypeError, ValueError):
                pass
    return loaded


def _ensure_cn_login(database: str, config: dict[str, Any]) -> bool:
    """Open the database's CARSI WAYF login in a visible browser once."""
    info = _CN_DATABASES[database]
    try:
        from ..browser_login import open_login_browser
    except ImportError:
        return False

    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / "carsi_cookies"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cookie_file = cache_dir / f"{database}.json"
    domains = info["domains"]

    def _detect(context: Any, page: Any) -> bool:
        try:
            host = (urlparse(page.url).hostname or "").lower()
            if not any(host == d or host.endswith("." + d) for d in domains):
                return False
            path = (page.url or "").lower()
            if any(marker in path for marker in ("fsso", "login", "cas", "sso", "saml", "wayf")):
                return False
            for cookie in context.cookies():
                cookie_domain = (cookie.get("domain") or "").lstrip(".").lower()
                if any(cookie_domain == d or cookie_domain.endswith("." + d) for d in domains):
                    return True
            return False
        except Exception:
            return False

    log.info(f"   [{info['name']}] opening CARSI login: {info['login_url']}")
    print(f"\n  请在浏览器中完成 {info['name']} 的 CARSI 机构登录（选择您自己的学校/机构）\n")
    return bool(open_login_browser(
        info["login_url"],
        config,
        cookie_file=cookie_file,
        detect_login=_detect,
        max_wait=600,
        publisher=database,
    ))


# ---------------------------------------------------------------------------
# Wanfang
# ---------------------------------------------------------------------------

def fetch_wanfang_file_id(
    wanfang_id: str,
    landing_url: str,
    config: dict[str, Any],
    *,
    session: requests.Session | None = None,
) -> str:
    """Ask Wanfang's gRPC-web detail service for the internal file id."""
    if not wanfang_id:
        return ""
    cached = _WANFANG_ID_CACHE.get(wanfang_id)
    if cached:
        return cached
    from ..chinese import fetch_wanfang_detail

    own = session is None
    client = session or _session(config)
    if own:
        _load_saved_cookies(client, config)
    detail = fetch_wanfang_detail(wanfang_id, landing_url, config, session=client)
    file_id = str(detail.get("file_id") or "")
    if file_id:
        _WANFANG_ID_CACHE[wanfang_id] = file_id
    return file_id


def _wanfang_pdf_url(file_id: str, config: dict[str, Any],
                     session: requests.Session) -> str:
    """Resolve the two-hop Wanfang download page to the PDF URL."""
    if not file_id:
        return ""
    download_id = file_id if file_id.startswith("perio_") else f"perio_{file_id}"
    page_url = f"https://oss.wanfangdata.com.cn/file/download/{download_id}.aspx"
    proxies = proxy_dict(select_proxy_for_url(page_url, config))
    try:
        resp = session.get(page_url, timeout=max(20, int(request_timeout(config)[1])),
                           allow_redirects=True, proxies=proxies)
    except requests.RequestException:
        return ""
    if resp.status_code >= 400:
        return ""
    text = response_text(resp)
    soup = BeautifulSoup(text, "html.parser")
    iframe = soup.find("iframe", id="downloadIframe")
    if iframe is None:
        iframe = soup.find("iframe", src=re.compile(r"NewFulltext", re.I))
    if iframe is None:
        return ""
    pdf_url = (iframe.get("src") or "").replace("&amp;", "&")
    if not pdf_url:
        return ""
    return urljoin("https://oss.wanfangdata.com.cn/", pdf_url)


def _save_stream(resp: requests.Response, output_path: Path) -> tuple[str, Path] | None:
    """Save a PDF/CAJ response.  Returns ``(format, path)`` or None."""
    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        first = b""
        with tmp_path.open("wb") as handle:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if not first:
                    first = chunk[:8]
                handle.write(chunk)
        fmt = guess_format(
            first,
            resp.headers.get("content-type", ""),
            resp.headers.get("content-disposition", ""),
        )
        if fmt == "pdf":
            if not is_pdf_file(tmp_path):
                return None
            tmp_path.replace(output_path)
            return "pdf", output_path
        if fmt == "caj":
            caj_path = output_path.with_suffix(".caj")
            tmp_path.replace(caj_path)
            return "caj", caj_path
        return None
    except OSError:
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def try_wanfang(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download a Wanfang article through campus-IP or CARSI authorization."""
    value = normalize_doi_loose(doi)
    resolution = resolve_chinese_doi(value, config)
    if not resolution.wanfang_id:
        return None

    session = _session(config)
    _load_saved_cookies(session, config)
    file_id = resolution.wanfang_file_id or fetch_wanfang_file_id(
        resolution.wanfang_id, resolution.landing_url, config, session=session,
    )
    if not file_id:
        log.info("   [Wanfang] detail API returned no file id")
        return None

    pdf_url = _wanfang_pdf_url(file_id, config, session)
    if not pdf_url:
        if config.get("carsi_enabled") and config.get("cn_auto_login", True) and _ensure_cn_login("wanfang", config):
            session = _session(config)
            _load_saved_cookies(session, config)
            pdf_url = _wanfang_pdf_url(file_id, config, session)
        if not pdf_url:
            return _login_required(value, "wanfang") if not config.get("carsi_enabled") else None

    proxies = proxy_dict(select_proxy_for_url(pdf_url, config))
    try:
        resp = session.get(
            pdf_url,
            timeout=max(30, int(request_timeout(config)[1])),
            allow_redirects=True,
            stream=True,
            proxies=proxies,
            headers={"Referer": resolution.landing_url, "Accept": "application/pdf,*/*"},
        )
    except requests.RequestException:
        return None
    if resp.status_code >= 400:
        return None
    content_type = (resp.headers.get("content-type") or "").lower()
    if "html" in content_type:
        text = resp.text[:6000]
        if _looks_like_login(text, resp.url) or "login" in (resp.url or "").lower():
            return _login_required(value, "wanfang") if not config.get("carsi_enabled") else None
        return None
    saved = _save_stream(resp, output_path)
    if not saved:
        return None
    fmt, path = saved
    result = success(value, path, "Wanfang")
    result["title"] = resolution.title
    if fmt == "caj":
        result["format"] = "caj"
        result.update(caj_hints(path))
    return result


# ---------------------------------------------------------------------------
# CNKI
# ---------------------------------------------------------------------------

def _looks_like_login(html_text: str, url: str = "") -> bool:
    lowered = (html_text or "")[:4000].lower()
    if "login.cnki.net" in (url or "").lower():
        return True
    markers = ("请登录", "登录后", "账号登录", "个人登录", "机构登录", "login")
    return any(marker in lowered for marker in markers)


def _cnki_download(
    session: requests.Session,
    link: str,
    landing: str,
    output_path: Path,
    config: dict[str, Any],
) -> tuple[str, Path] | str | None:
    """Fetch a CNKI download link.  Returns ``(fmt,path)``, ``'login'``, or None."""
    proxies = proxy_dict(select_proxy_for_url(link, config))
    try:
        resp = session.get(
            link,
            timeout=max(30, int(request_timeout(config)[1])),
            allow_redirects=True,
            stream=True,
            proxies=proxies,
            headers={"Referer": landing, "Accept": "application/pdf,*/*"},
        )
    except requests.RequestException:
        return None
    if resp.status_code >= 400:
        return None

    # Peek at the first chunk: a login/paywall response is HTML and must not
    # be written as a .pdf; a real download is a PDF or CAJ stream.
    iterator = resp.iter_content(chunk_size=64 * 1024)
    first = next(iterator, b"")
    fmt = guess_format(
        first[:8],
        resp.headers.get("content-type", ""),
        resp.headers.get("content-disposition", ""),
    )
    if fmt not in ("pdf", "caj"):
        try:
            rest = b"".join(iterator)
        except requests.RequestException:
            rest = b""
        text = (first + rest).decode("utf-8", "ignore")
        if _looks_like_login(text, resp.url):
            return "login"
        return None

    tmp_path = output_path.with_suffix(output_path.suffix + ".part")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tmp_path.open("wb") as handle:
            handle.write(first)
            for chunk in iterator:
                if chunk:
                    handle.write(chunk)
        if fmt == "pdf":
            if not is_pdf_file(tmp_path):
                return None
            tmp_path.replace(output_path)
            return "pdf", output_path
        caj_path = output_path.with_suffix(".caj")
        tmp_path.replace(caj_path)
        return "caj", caj_path
    except OSError:
        return None
    finally:
        tmp_path.unlink(missing_ok=True)


def try_cnki(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Download a CNKI article with saved CARSI/institutional cookies."""
    value = normalize_doi_loose(doi)
    resolution = resolve_chinese_doi(value, config)
    detail = resolution.cnki or {}
    landing = detail.get("canonical_url") or detail.get("landing_url") or resolution.landing_url
    if not landing:
        return None

    session = _session(config)
    _load_saved_cookies(session, config)
    proxies = proxy_dict(select_proxy_for_url(landing, config))

    def _fresh_links() -> tuple[str, str]:
        try:
            page = session.get(landing, timeout=max(20, int(request_timeout(config)[1])),
                               allow_redirects=True, proxies=proxies)
            parsed = parse_cnki_detail(response_text(page))
            return parsed.get("pdf_url", ""), parsed.get("caj_url", "")
        except requests.RequestException:
            return detail.get("pdf_url", ""), detail.get("caj_url", "")

    pdf_url, caj_url = _fresh_links()
    attempted_login = False
    login_needed = False
    for _ in range(2):
        # Prefer PDF; fall back to CAJ only when PDF is unavailable/blocked.
        for link in (pdf_url, caj_url):
            if not link:
                continue
            outcome = _cnki_download(session, link, landing, output_path, config)
            if outcome == "login":
                login_needed = True
                break
            if not outcome:
                continue
            fmt, path = outcome
            result = success(value, path, "CNKI")
            result["title"] = resolution.title or detail.get("title", "")
            if detail.get("filename"):
                result["cnki_filename"] = detail["filename"]
            if detail.get("dbcode"):
                result["cnki_dbcode"] = detail["dbcode"]
            if fmt == "caj":
                result["format"] = "caj"
                result.update(caj_hints(path))
            return result

        can_login = config.get("carsi_enabled") and config.get("cn_auto_login", True)
        if attempted_login or not can_login:
            break
        attempted_login = True
        if not _ensure_cn_login("cnki", config):
            break
        session = _session(config)
        _load_saved_cookies(session, config)
        pdf_url, caj_url = _fresh_links()
    if login_needed:
        return _login_required(value, "cnki")
    return None


# ---------------------------------------------------------------------------
# VIP (browser-driven)
# ---------------------------------------------------------------------------

def _capture_browser_download(page: Any, output_path: Path, trigger: Any) -> tuple[str, Path] | None:
    """Run *trigger* and capture either a Playwright download or PDF/CAJ body."""
    captured: list[bytes] = []
    downloaded: list[Path] = []
    tmp_download = output_path.with_suffix(".download.tmp")

    def _on_response(resp: Any) -> None:
        try:
            ctype = (resp.headers.get("content-type") or "").lower()
            if not any(marker in ctype for marker in ("pdf", "octet-stream", "caj")):
                return
            if resp.status >= 400:
                return
            body = resp.body()
            fmt = guess_format(body[:8], ctype)
            if fmt in ("pdf", "caj") and len(body) > 5000:
                captured.append(body)
        except Exception:
            pass

    def _on_download(download: Any) -> None:
        try:
            download.save_as(str(tmp_download))
            downloaded.append(tmp_download)
        except Exception:
            pass

    try:
        page.on("response", _on_response)
        page.on("download", _on_download)
        page.on("dialog", lambda dialog: dialog.accept())
    except Exception:
        pass

    try:
        trigger()
    except Exception:
        pass

    for _ in range(45):
        if downloaded or captured:
            break
        time.sleep(1)

    if downloaded:
        source = downloaded[0]
        head = source.read_bytes()[:8]
        fmt = guess_format(head)
        target = output_path.with_suffix(".caj") if fmt == "caj" else output_path
        try:
            if source != target:
                target.unlink(missing_ok=True)
                source.replace(target)
            if fmt == "pdf" and not is_pdf_file(target):
                return None
            return fmt, target
        except OSError:
            return None
    if captured:
        body = captured[-1]
        fmt = guess_format(body[:8])
        target = output_path.with_suffix(".caj") if fmt == "caj" else output_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        if fmt == "pdf" and not is_pdf_file(target):
            return None
        return fmt, target
    return None


def _vip_click_download(page: Any) -> bool:
    try:
        return bool(page.evaluate(
            """
            () => {
              const nodes = [...document.querySelectorAll('a, button, span, div')];
              const visible = nodes.filter(el => {
                const text = (el.innerText || el.textContent || '').trim();
                return (text === '下载PDF' || text === '下载 PDF' || text === 'PDF下载')
                    && el.offsetParent !== null;
              });
              // The result-row button is later in the DOM than the bulk
              // toolbar; prefer the last visible match.
              const hit = visible[visible.length - 1];
              if (hit) { hit.click(); return true; }
              return false;
            }
            """
        ))
    except Exception:
        return False


def _vip_http_ip_login(config: dict[str, Any]) -> bool:
    """Cheap campus-IP probe before launching a browser.

    VIP answers ``CheckUserIslogin`` with ``{"isLogined": true}`` when the
    request's egress IP is covered by an institutional subscription.  This
    lets us avoid popping a browser for users who have no access at all.
    """
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": CHROME_UA,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://qikan.cqvip.com/",
    })
    urls = ["https://qikan.cqvip.com/RegistLogin/CheckUserIslogin"]
    for url in urls:
        try:
            resp = session.post(url, timeout=12)
            if resp.status_code < 400:
                payload = resp.json()
                if payload.get("isLogined"):
                    return True
        except (requests.RequestException, ValueError):
            continue
    proxy = config.get("network_proxy", "") if config else ""
    if proxy:
        try:
            resp = session.post(
                urls[0], timeout=12,
                proxies={"http": proxy, "https": proxy},
            )
            if resp.status_code < 400 and resp.json().get("isLogined"):
                return True
        except (requests.RequestException, ValueError):
            pass
    return False


def _vip_ip_login(page: Any) -> bool:
    """Establish VIP's campus-IP session (the download API expects it)."""
    try:
        state = page.evaluate(
            """async () => {
                try {
                    const r = await fetch('/RegistLogin/CheckUserIslogin', {
                        method: 'POST',
                        headers: {'X-Requested-With': 'XMLHttpRequest'},
                    });
                    return await r.json();
                } catch (e) { return {}; }
            }"""
        )
        if state.get("isLogined"):
            return True
        result = page.evaluate(
            """async () => {
                try {
                    const r = await fetch('/RegistLogin/Login', {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded',
                            'X-Requested-With': 'XMLHttpRequest',
                        },
                        body: new URLSearchParams({LoginType: 'iplogin'}),
                    });
                    return await r.json();
                } catch (e) { return {}; }
            }"""
        )
        return bool(result.get("Status"))
    except Exception:
        return False


def _vip_article_info(page: Any, title: str) -> dict[str, str] | None:
    """Extract ``showdown(articleId, token)`` from the matching result row."""
    try:
        return page.evaluate(
            """(title) => {
                const norm = (s) => (s || '').replace(/\\s+/g, '');
                const target = norm(title);
                const links = [...document.querySelectorAll('a')]
                    .filter(a => (a.innerText || '').includes('下载PDF'));
                const hit = links.find(a => {
                    let node = a;
                    for (let i = 0; i < 7 && node; i++) {
                        if (norm(node.innerText || '').includes(target)) return true;
                        node = node.parentElement;
                    }
                    return false;
                }) || links[0];
                if (!hit) return null;
                const match = (hit.getAttribute('onclick') || '')
                    .match(/showdown\\('(\\d+)','([^']+)'\\)/);
                return match ? {id: match[1], token: match[2]} : null;
            }""",
            title,
        )
    except Exception:
        return None


def _vip_article_down_url(page: Any, article_id: str, token: str) -> str:
    """Call VIP's ArticleDown endpoint (the actual PDF URL lives in its JSON)."""
    script = """async (args) => {
        for (let attempt = 0; attempt < 2; attempt++) {
            try {
                const body = new URLSearchParams({
                    id: args.id, info: args.token, ts: String(Date.now()),
                });
                const r = await fetch('/Qikan/Article/ArticleDown', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/x-www-form-urlencoded',
                        'X-Requested-With': 'XMLHttpRequest',
                        'Accept': 'application/json, text/javascript, */*; q=0.01',
                    },
                    body,
                });
                const text = await r.text();
                try {
                    const data = JSON.parse(text);
                    if (data && data.url) return data.url;
                } catch (e) { /* anti-bot HTML */ }
            } catch (e) { /* retry */ }
        }
        return '';
    }"""
    for _ in range(2):
        try:
            url = page.evaluate(script, {"id": article_id, "token": token})
            if url:
                return str(url)
        except Exception:
            pass
    return ""


def _save_vip_pdf(context: Any, url: str, output_path: Path) -> tuple[str, Path] | None:
    body = b""
    try:
        response = context.request.get(url, timeout=60000)
        if response.ok:
            body = response.body()
    except Exception:
        body = b""
    if not body:
        try:
            response = requests.get(
                url, timeout=60,
                headers={"User-Agent": CHROME_UA, "Referer": "https://qikan.cqvip.com/"},
            )
            if response.status_code < 400:
                body = response.content
        except requests.RequestException:
            return None
    if not body:
        return None
    fmt = guess_format(body[:8])
    if fmt not in ("pdf", "caj"):
        return None
    target = output_path.with_suffix(".caj") if fmt == "caj" else output_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    if fmt == "pdf" and not is_pdf_file(target):
        return None
    return fmt, target


def try_cqvip(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """VIP is JS/anti-bot driven; search by title and use its download API."""
    value = normalize_doi_loose(doi)
    resolution = resolve_chinese_doi(value, config)
    title = resolution.title
    if not title:
        return None
    if not _vip_http_ip_login(config) and not config.get("carsi_enabled"):
        log.info("   [VIP] no campus-IP access and CARSI is not configured")
        return None
    try:
        from ..publisher_strategies import _visible_browser
    except Exception:
        return None

    for attempt in range(2):
        try:
            with _visible_browser(config, "cqvip") as (context, page):
                # The homepage has the search box; the bare Search/Index URL
                # redirects and can destroy the execution context mid-fill.
                page.goto("https://qikan.cqvip.com/",
                          wait_until="domcontentloaded", timeout=60000)
                # Campus-IP session: without this the download JS opens a
                # login layer instead of calling ArticleDown.
                _vip_ip_login(page)
                search_input = None
                for _ in range(15):
                    try:
                        search_input = page.query_selector("input[placeholder*='检索词']")
                    except Exception:
                        search_input = None
                    if search_input:
                        break
                    time.sleep(1)
                if not search_input:
                    log.info("   [VIP] search box not found")
                    return None
                try:
                    search_input.fill(title)
                    search_input.press("Enter")
                except Exception:
                    # Navigation raced the fill; retry via JS once.
                    page.evaluate(
                        """(title) => {
                            const input = document.querySelector("input[placeholder*='检索词']");
                            if (input) { input.value = title;
                                input.dispatchEvent(new Event('input', {bubbles: true}));
                                input.dispatchEvent(new Event('change', {bubbles: true}));
                                input.form && input.form.submit();
                            }
                        }""",
                        title,
                    )
                # The result list already carries a 下载PDF button; opening the
                # detail tab is unnecessary (and popup-blocked in some
                # profiles).  Wait for the result row instead.
                found = False
                for _ in range(12):
                    time.sleep(1.5)
                    try:
                        found = bool(page.evaluate(
                            """(title) => {
                                const norm = (s) => (s || '').replace(/\\s+/g, '');
                                const target = norm(title);
                                return [...document.querySelectorAll('a,div,span')]
                                    .some(el => norm(el.innerText || el.textContent || '').includes(target));
                            }""",
                            title,
                        ))
                    except Exception:
                        found = False
                    if found:
                        break
                if not found:
                    log.info("   [VIP] no result matching title")
                    return None

                info = _vip_article_info(page, title)
                if info:
                    pdf_url = _vip_article_down_url(page, info["id"], info["token"])
                    if pdf_url:
                        saved = _save_vip_pdf(context, pdf_url, output_path)
                        if saved:
                            fmt, path = saved
                            result = success(value, path, "VIP")
                            result["title"] = title
                            result["vip_article_id"] = info["id"]
                            if fmt == "caj":
                                result["format"] = "caj"
                                result.update(caj_hints(path))
                            return result
                        log.info("   [VIP] ArticleDown URL did not yield a PDF")

                # Fallback: click the button and capture a browser download.
                outcome = _capture_browser_download(
                    page, output_path, lambda: _vip_click_download(page),
                )
                if outcome:
                    fmt, path = outcome
                    result = success(value, path, "VIP")
                    result["title"] = title
                    if fmt == "caj":
                        result["format"] = "caj"
                        result.update(caj_hints(path))
                    return result
        except Exception as exc:
            log.info(f"   [VIP] browser lane error: {exc}")
        if attempt == 0 and config.get("carsi_enabled") and config.get("cn_auto_login", True):
            if not _ensure_cn_login("cqvip", config):
                break
    return None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _is_cnki_doi(doi: str) -> bool:
    lowered = doi.lower()
    return "/j.cnki" in lowered or "/b.cnki" in lowered or ".cnki." in lowered


def detect_cn_database(doi: str, resolution: Any | None = None) -> str:
    """Pick the database lane for a Chinese DOI."""
    value = normalize_doi_loose(doi)
    if _is_cnki_doi(value):
        return "cnki"
    if value.startswith("10.3969/"):
        return "wanfang"
    if resolution is not None:
        host = (resolution.host or "").lower()
        if "cnki" in host or resolution.cnki:
            return "cnki"
        if "wanfang" in host:
            return "wanfang"
        if "cqvip" in host:
            return "cqvip"
    return ""


def try_cn_database(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Try the appropriate Chinese database lane for *doi*."""
    if not config.get("cn_databases_enabled", True):
        return None
    value = normalize_doi_loose(doi)
    if not is_chinese_doi(value):
        return None
    resolution = resolve_chinese_doi(value, config)
    database = detect_cn_database(value, resolution)
    if database == "cqvip":
        return try_cqvip(value, output_path, config)

    primary = try_cnki if database == "cnki" else try_wanfang if database == "wanfang" else None
    if primary is None:
        return None
    result = primary(value, output_path, config)
    if result and result.get("success"):
        return result

    # VIP often carries the same Chinese article.  On campus it authorizes by
    # IP; off campus it can use CARSI.  Use it as the second opinion.
    if config.get("cn_vip_fallback", True) and resolution.title:
        vip_result = try_cqvip(value, output_path, config)
        if vip_result and vip_result.get("success"):
            return vip_result
    return result
