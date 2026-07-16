"""Sci-Hub source with domain rotation, CAPTCHA detection, and Tor support."""

from __future__ import annotations

import contextlib
import time
import threading
import shutil
import tempfile
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any

import requests

from ..config import DEFAULT_SCIHUB_DOMAINS
from ..domain_db import load_stats, record_result, update_probe, set_probe_timestamp, get_probe_timestamp
from ..log import get_logger
from ..network import (
    USER_AGENT,
    _is_cloudflare_block,
    fetch,
    proxy_dict,
    request_timeout,
    select_proxy_for_url,
)
from ..pdf_utils import (
    _response_looks_pdf,
    extract_pdf_url_from_html,
    is_pdf_file,
    publish_pdf_file_atomic,
    success,
    write_pdf_stream_atomic,
)

# Import compiled core functions if available (Cython .pyd/.so)
try:
    from .._core.scihub_core import (
        domain_score as _domain_score_compiled,
        filter_cooldown_domains as _filter_cooldown_compiled,
        rank_domains as _rank_domains_compiled,
        record_domain_result as _record_domain_result_compiled,
        select_domains_for_attempt as _select_domains_compiled,
    )
    _HAS_COMPILED_CORE = True
except ImportError:
    _HAS_COMPILED_CORE = False

log = get_logger()

# Track domains that require browser bypass (in-memory, per session)
_browser_domains: set[str] = set()

def _mark_browser_required(domain: str, config: dict[str, Any]) -> None:
    """Mark a domain as requiring browser bypass for future ranking."""
    _browser_domains.add(domain)

def _is_browser_domain(domain: str) -> bool:
    """Check if domain requires browser bypass."""
    return domain in _browser_domains

_PROBE_TTL_HOURS = 4
_SCIHUB_PROBE_WORKERS = 8
_SCIHUB_HTML_LIMIT = 512_000
_SCIHUB_BACKUP_TIMEOUT_SECONDS = 10.0


class _CombinedCancelEvent(threading.Event):
    """Event-compatible view that is set when any constituent event is set."""

    def __init__(self, *events: threading.Event | None) -> None:
        super().__init__()
        self._events = tuple(event for event in events if event is not None)

    def is_set(self) -> bool:
        return super().is_set() or any(event.is_set() for event in self._events)

    def wait(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while not self.is_set():
            if deadline is None:
                wait_for = 0.05
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self.is_set()
                wait_for = min(0.05, remaining)
            super().wait(wait_for)
        return True


def _cancelled(cancel_event: Any = None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _wait_or_cancel(cancel_event: Any, timeout: float) -> bool:
    if cancel_event is None:
        time.sleep(max(0.0, timeout))
        return False
    return bool(cancel_event.wait(max(0.0, timeout)))


def _probe_single_domain(domain: str, proxy: str | None, timeout: tuple[int, int]) -> tuple[str, bool, float]:
    proxies = proxy_dict(proxy)
    t0 = time.time()
    session: requests.Session | None = None
    resp: requests.Response | None = None
    try:
        session = requests.Session()
        session.trust_env = False
        resp = session.get(
            domain,
            timeout=timeout,
            proxies=proxies,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
        # Accept 200/302/301 as reachable (302/301 = redirect, still means domain is alive)
        # 403/503 = reachable but blocked (Cloudflare) — still mark reachable for browser bypass
        reachable = resp.status_code in (200, 301, 302, 403, 503)
        ok = resp.status_code == 200 and ("sci-hub" in resp.text[:5000].lower() or "scihub" in resp.text[:5000].lower())
        # 403/503 with Cloudflare signature = reachable via browser
        if resp.status_code in (403, 503) and _is_cloudflare_block(resp):
            _mark_browser_required(domain, None)
            reachable = True
        latency = (time.time() - t0) * 1000
        return (domain, ok if ok else reachable, latency)
    except requests.exceptions.Timeout:
        # Timeout doesn't mean unreachable - just slow
        latency = (time.time() - t0) * 1000
        return (domain, True, latency)
    except Exception as e:
        log.debug(f"Sci-Hub probe {domain}: {type(e).__name__}")
        return (domain, False, 99999.0)
    finally:
        if resp is not None:
            with contextlib.suppress(Exception):
                resp.close()
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()


def _probe_scihub_domains(config: dict[str, Any]) -> None:
    last_probe = get_probe_timestamp(config)
    now = time.time()
    if now - last_probe < _PROBE_TTL_HOURS * 3600:
        return

    proxy = select_proxy_for_url("https://sci-hub.mksa.top", config)
    domains = config.get("scihub_domains") or DEFAULT_SCIHUB_DOMAINS
    timeout = (5, 10)

    with ThreadPoolExecutor(max_workers=_SCIHUB_PROBE_WORKERS) as pool:
        futures = {pool.submit(_probe_single_domain, d, proxy, timeout): d for d in domains}
        for future in as_completed(futures, timeout=15):
            domain, ok, latency = future.result()
            update_probe(domain, ok, round(latency, 1), config)

    set_probe_timestamp(config)


def _is_browser_available(config: dict[str, Any]) -> bool:
    """Check if CloakBrowser is available. Returns False in asyncio context."""
    # Playwright Sync API cannot run inside a running asyncio event loop, so
    # don't even try — return False and let HTTP-only sources handle it.
    try:
        import asyncio
        asyncio.get_running_loop()
        return False
    except RuntimeError:
        pass
    try:
        from ..browser_engine import is_available
        return is_available(config)
    except Exception:
        return False


def _solve_altcha_and_reload(
    solve_result: dict[str, Any],
    landing_url: str,
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    cancel_event: Any = None,
) -> dict[str, Any] | None:
    """Solve ALTCHA anti-bot verification on a Sci-Hub page, then reload and extract PDF.

    ALTCHA is a proof-of-work CAPTCHA used by sci-hub.ru. The page shows a checkbox
    labelled "不是" (No/Not a robot). After clicking, a proof-of-work computation runs,
    then the page shows "您是人类！" (You are human!). Once verified, we reload the page
    to get the actual paper content.
    """
    try:
        from ..browser_engine import get_browser_page
    except ImportError:
        log.info("   [altcha] browser engine not available for ALTCHA bypass")
        return None

    page = None
    try:
        if _cancelled(cancel_event):
            return None
        page = get_browser_page(config)
        if not page:
            log.info("   [altcha] could not get browser page")
            return None

        # Navigate to the landing URL
        page.goto(landing_url, wait_until="domcontentloaded", timeout=20000)
        if _wait_or_cancel(cancel_event, 2):
            return None

        # Find and click the ALTCHA checkbox
        checkbox_selectors = [
            "input[type='checkbox']",
            "#altcha_checkbox",
            "[id^='altcha_checkbox_']",
        ]
        clicked = False
        for selector in checkbox_selectors:
            if _cancelled(cancel_event):
                return None
            try:
                el = page.query_selector(selector)
                if el:
                    el.click(timeout=5000)
                    clicked = True
                    log.info(f"   [altcha] clicked checkbox: {selector}")
                    break
            except Exception:
                continue

        if not clicked:
            # Try clicking the "不是" div
            try:
                no_btn = page.query_selector("div[onclick='check()']")
                if no_btn:
                    no_btn.click(timeout=5000)
                    clicked = True
                    log.info("   [altcha] clicked '不是' button")
            except Exception:
                pass

        if not clicked:
            log.info("   [altcha] could not find checkbox/button to click")
            return None

        # Wait for verification to complete (poll for "您是人类" or "verified")
        for i in range(15):
            if _wait_or_cancel(cancel_event, 1):
                return None
            try:
                body_text = page.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                continue
            if "您是人类" in body_text or "verified" in body_text.lower():
                log.info(f"   [altcha] verified after {i + 3}s")
                break
            if i % 5 == 4:
                log.info(f"   [altcha] still verifying... ({i + 3}s)")

        # Reload the page to get the actual paper content
        log.info("   [altcha] reloading page after verification...")
        if _cancelled(cancel_event):
            return None
        page.goto(landing_url, wait_until="domcontentloaded", timeout=20000)
        if _wait_or_cancel(cancel_event, 2):
            return None

        # Now try to extract PDF
        try:
            html = page.content()
        except Exception:
            log.info("   [altcha] failed to get page content after reload")
            return None

        from ..pdf_utils import is_pdf_file, success, extract_pdf_url_from_html
        from ..browser_engine import download_pdf_via_browser

        # Check for article not found
        lower = html.lower()
        if any(sig in lower for sig in ["article not found", "статья не найдена", "не найден"]):
            log.info("   [altcha] article not found after verification")
            return None

        # Extract PDF URL
        pdf_url = extract_pdf_url_from_html(html, landing_url)
        if pdf_url:
            log.info(f"   [altcha] found PDF: {pdf_url[:80]}")
            if download_pdf_via_browser(
                pdf_url,
                output_path,
                config,
                cancel_event=cancel_event,
            ):
                if is_pdf_file(output_path):
                    return success(doi, output_path, "Sci-Hub(altcha)")

        log.info("   [altcha] no PDF found after verification")
        return None

    except Exception as e:
        log.info(f"   [altcha] error: {e}")
        return None
    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass


def _browser_first_download(
    landing_url: str,
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    cancel_event: Any = None,
) -> dict[str, Any] | None:
    """Try browser-first download for Sci-Hub. Bypasses Cloudflare/CAPTCHA."""
    try:
        from ..browser_engine import (
            _write_pdf_bytes_atomic,
            download_pdf_via_browser,
            solve_url,
        )
        from ..pdf_utils import is_pdf_file, success, extract_pdf_url_from_html
        from urllib.parse import urlparse
        domain = urlparse(landing_url).netloc or landing_url[:40]

        if cancel_event is not None and cancel_event.is_set():
            return None
        log.info(f"   [browser-first] trying {landing_url[:80]}")
        result = solve_url(landing_url, config, max_timeout=30000)
        if (cancel_event is not None and cancel_event.is_set()) or not result:
            log.info(f"   [browser-first] no response")
            return None

        solution = result.get("solution", {})
        html = solution.get("response", "")
        if not html:
            log.info(f"   [browser-first] empty response")
            return None

        # Check for article not found
        lower = html.lower()
        if any(sig in lower for sig in ["article not found", "статья не найдена", "не найден"]):
            log.info(f"   [browser-first] article not found on {domain}")
            return None

        # Check for ALTCHA anti-bot verification (used by sci-hub.ru and other mirrors)
        if any(sig in lower for sig in ["altcha", "你是机器人吗", "not a robot"]):
            log.info(f"   [browser-first] ALTCHA detected on {domain}, attempting bypass...")
            try:
                altcha_result = _solve_altcha_and_reload(
                    result,
                    landing_url,
                    doi,
                    output_path,
                    config,
                    cancel_event=cancel_event,
                )
                if altcha_result:
                    return altcha_result
            except Exception as e:
                log.info(f"   [browser-first] ALTCHA bypass failed: {e}")

        # Check for Cloudflare challenge page
        if any(sig in lower for sig in ["checking your browser", "just a moment", "cf-browser-verification"]):
            log.info(f"   [browser-first] Cloudflare challenge on {domain}, page may need more time")
            # The solve_url should have waited, but if we still see the challenge,
            # mark this domain as needing browser bypass for future attempts
            return None

        # Check for empty embed (Sci-Hub has no PDF)
        if '<embed' in lower and 'src=""' in lower:
            log.info(f"   [browser-first] empty embed — article not in Sci-Hub database")
            return None

        # Extract PDF URL from HTML
        pdf_url = extract_pdf_url_from_html(html, solution.get("url", landing_url))
        if pdf_url:
            log.info(f"   [browser-first] found PDF: {pdf_url[:80]}")
            # Download via CloakBrowser (handles Cloudflare on PDF host too)
            if download_pdf_via_browser(
                pdf_url,
                output_path,
                config,
                cancel_event=cancel_event,
            ):
                if is_pdf_file(output_path):
                    return success(doi, output_path, f"Sci-Hub(Browser)")

        # Check if the response itself is a PDF
        import base64
        resp_data = solution.get("response", "")
        if isinstance(resp_data, str) and len(resp_data) > 5000:
            try:
                pdf_bytes = base64.b64decode(resp_data) if resp_data.startswith("JVBER") else resp_data.encode("utf-8")
                if pdf_bytes[:5] == b"%PDF-":
                    if _write_pdf_bytes_atomic(
                        output_path,
                        pdf_bytes,
                        cancel_event,
                    ) and is_pdf_file(output_path):
                        return success(doi, output_path, f"Sci-Hub(Browser)")
            except Exception:
                pass

        log.info(f"   [browser-first] no PDF found in response")
        return None
    except Exception as e:
        log.info(f"   [browser-first] error: {e}")
        return None


def _try_scihub_domain_impl(
    doi: str,
    domain: str,
    output_path: Path,
    config: dict[str, Any],
    use_tor: bool = False,
    cancel_event: Any = None,
) -> dict[str, Any] | None:
    landing_url = f"{domain.rstrip('/')}/{urllib.parse.quote(doi, safe='/')}"

    if _cancelled(cancel_event):
        return None

    # Browser-first: bypass Cloudflare/CAPTCHA before HTTP attempt
    if not use_tor and _is_browser_available(config):
        result = _browser_first_download(
            landing_url,
            doi,
            output_path,
            config,
            cancel_event=cancel_event,
        )
        if result:
            return result

    resp: requests.Response | None = None
    try:
        if _cancelled(cancel_event):
            return None
        resp = fetch(
            landing_url,
            config,
            stream=True,
            use_tor=use_tor,
            cancel_event=cancel_event,
        )
        if _cancelled(cancel_event):
            return None

        # Fallback to browser on Cloudflare/403/CAPTCHA
        if resp.status_code in (403, 503) or _is_cloudflare_block(resp):
            _mark_browser_required(domain, config)
            browser_resp = _try_browser(
                landing_url,
                config,
                resp,
                cancel_event=cancel_event,
            )
            if browser_resp is None:
                return None
            previous_resp = resp
            resp = browser_resp
            with contextlib.suppress(Exception):
                previous_resp.close()

        if _cancelled(cancel_event) or resp.status_code >= 400:
            return None

        chunks = iter(resp.iter_content(chunk_size=8192))
        first = next(chunks, b"")
        if _cancelled(cancel_event):
            return None

        # Check for CAPTCHA in first chunk
        if resp.status_code == 200 and first:
            content_sample = first[:5000].decode('utf-8', errors='ignore').lower()
            if 'captcha' in content_sample or 'recaptcha' in content_sample:
                log.info(f"   CAPTCHA detected, trying browser...")
                # Use browser to bypass CAPTCHA
                browser_resp = _try_browser(
                    landing_url,
                    config,
                    resp,
                    cancel_event=cancel_event,
                )
                if browser_resp is None:
                    log.warning(f"   browser bypass failed — is CloakBrowser installed? Run: pip install cloakbrowser")
                    return None
                # Get new content from browser response
                previous_resp = resp
                resp = browser_resp
                with contextlib.suppress(Exception):
                    previous_resp.close()
                chunks = iter(resp.iter_content(chunk_size=8192))
                first = next(chunks, b"")
                if _cancelled(cancel_event):
                    return None
                log.info(f"   browser bypassed CAPTCHA, content size: {len(first)}")

        if _response_looks_pdf(resp, first):
            if not write_pdf_stream_atomic(
                output_path,
                first,
                chunks,
                cancel_event,
            ):
                return None
            if not _cancelled(cancel_event) and is_pdf_file(output_path):
                return success(doi, output_path, f"Sci-Hub({domain})")
            with contextlib.suppress(OSError):
                output_path.unlink(missing_ok=True)
            return None

        # Read only a bounded HTML prefix, checking cancellation between chunks.
        html_parts = [first[:_SCIHUB_HTML_LIMIT]]
        html_size = len(html_parts[0])
        for chunk in chunks:
            if _cancelled(cancel_event):
                return None
            if not chunk:
                continue
            remaining = _SCIHUB_HTML_LIMIT - html_size
            if remaining <= 0:
                break
            piece = chunk[:remaining]
            html_parts.append(piece)
            html_size += len(piece)
            if html_size >= _SCIHUB_HTML_LIMIT:
                break
        html = b"".join(html_parts)
        pdf_url = extract_pdf_url_from_html(html.decode("utf-8", errors="ignore"), resp.url)
        if _cancelled(cancel_event) or not pdf_url:
            return None
        result = download_pdf_from_scihub(
            pdf_url,
            output_path,
            config,
            f"Sci-Hub({domain})",
            use_tor=use_tor,
            cookies=resp.cookies,
            cancel_event=cancel_event,
        )
        if result:
            result["doi"] = doi
            result["identifier"] = doi
        return result
    except Exception:
        return None
    finally:
        if resp is not None:
            with contextlib.suppress(Exception):
                resp.close()


def try_scihub_domain(
    doi: str,
    domain: str,
    output_path: Path,
    config: dict[str, Any],
    use_tor: bool = False,
    cancel_event: Any = None,
) -> dict[str, Any] | None:
    from .. import browser_engine
    previous_cancel_event = browser_engine._set_thread_cancel_event(cancel_event)
    try:
        if cancel_event is not None and cancel_event.is_set():
            return None
        return _try_scihub_domain_impl(
            doi,
            domain,
            output_path,
            config,
            use_tor=use_tor,
            cancel_event=cancel_event,
        )
    finally:
        browser_engine.shutdown_shared_browser()
        browser_engine._set_thread_cancel_event(previous_cancel_event)


def _try_browser(
    url: str,
    config: dict[str, Any],
    original_resp: requests.Response,
    cancel_event: Any = None,
) -> requests.Response | None:
    """Try CloakBrowser to bypass Cloudflare. Returns Response or None."""
    if not _is_browser_available(config):
        return None
    from ..browser_engine import solve_url
    if _cancelled(cancel_event):
        return None
    result = solve_url(url, config)
    if _cancelled(cancel_event) or not result:
        return None
    solution = result.get("solution", {})
    status = solution.get("status", 0)
    if status >= 400:
        return None
    # Build a Response from browser solution
    resp = requests.Response()
    resp.status_code = status
    html_content = solution.get("response", "")
    resp._content = html_content.encode("utf-8") if isinstance(html_content, str) else html_content
    resp._content_consumed = True
    resp.url = solution.get("url", url)
    cookies = solution.get("cookies", [])
    if isinstance(cookies, list):
        for c in cookies:
            if "name" in c and "value" in c:
                resp.cookies.set(c["name"], c["value"])
    # Mock raw attribute so downstream code can read content
    class _RawMock:
        def read(self, size=0, decode_content=False):
            return resp._content[size:] if size else resp._content
    resp.raw = _RawMock()
    return resp


def download_pdf_from_scihub(
    url: str,
    output_path: Path,
    config: dict[str, Any],
    source: str,
    use_tor: bool = False,
    cookies: Any = None,
    cancel_event: Any = None,
) -> dict[str, Any] | None:
    session: requests.Session | None = None
    resp: requests.Response | None = None
    try:
        if _cancelled(cancel_event):
            return None

        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": USER_AGENT})
        if cookies is not None:
            session.cookies.update(cookies)
        resp = session.get(
            url,
            timeout=request_timeout(config),
            proxies=proxy_dict(
                select_proxy_for_url(
                    url,
                    config,
                    use_tor=use_tor,
                    cancel_event=cancel_event,
                )
            ),
            allow_redirects=True,
            stream=True,
        )
        if _cancelled(cancel_event) or resp.status_code >= 400:
            return None

        chunks = iter(resp.iter_content(chunk_size=65536))
        first = next(chunks, b"")
        if _cancelled(cancel_event) or not _response_looks_pdf(resp, first):
            return None

        if not write_pdf_stream_atomic(
            output_path,
            first,
            chunks,
            cancel_event,
        ):
            return None
        if not _cancelled(cancel_event) and is_pdf_file(output_path):
            return success(output_path.stem, output_path, source)
        with contextlib.suppress(OSError):
            output_path.unlink(missing_ok=True)
        return None
    except Exception:
        return None
    finally:
        if resp is not None:
            with contextlib.suppress(Exception):
                resp.close()
        if session is not None:
            with contextlib.suppress(Exception):
                session.close()


def _race_browser_domains(
    domains: list[str],
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    max_workers: int = 3,
) -> dict[str, Any] | None:
    """Race multiple Sci-Hub domains using multiple tabs in ONE browser.

    Instead of spawning N threads each with their own browser (heavy),
    this opens N tabs in a single browser context and polls them in
    round-robin. The browser's internal network stack handles all tabs
    concurrently, so we get real parallelism without multiple processes.

    Each tab writes to its own temp output file to avoid conflicts.
    The winning file is renamed to output_path; loser tabs and temp
    files are cleaned up.
    """
    from ..browser_engine import _get_shared_browser, is_available as _browser_available

    if not _browser_available(config):
        return None

    try:
        browser, context = _get_shared_browser(config)
    except Exception as e:
        log.info(f"   Sci-Hub: cannot get browser: {e}")
        return None

    log.info(f"   Sci-Hub: racing {len(domains)} browser domains via tabs (1 browser)...")

    # Phase 1: Fire all navigations — open a tab per domain, trigger navigation
    # without blocking (use JS location assignment, returns immediately)
    tabs: list[tuple[str, Any, Path]] = []  # (domain, page, temp_output)
    for domain in domains:
        landing_url = f"{domain.rstrip('/')}/{urllib.parse.quote(doi, safe='/')}"
        safe_suffix = domain.split("//")[-1].replace(".", "_").replace("/", "_")[:25]
        temp_output = output_path.parent / f"{output_path.stem}_browser_{safe_suffix}.pdf"
        try:
            page = context.new_page()
            # Fire-and-forget via JS — navigate without blocking so all tabs load concurrently
            page.evaluate(f"window.location.href = '{landing_url}'")
            tabs.append((domain, page, temp_output))
        except Exception as e:
            log.info(f"   Sci-Hub: tab open failed for {domain}: {e}")
            try:
                page.close()
            except Exception:
                pass

    if not tabs:
        return None

    # Wait a moment for all navigations to start, then poll for DOM readiness
    time.sleep(1.5)

    # Phase 2: Poll tabs in round-robin — wait for DOM, then extract PDF URL
    deadline = time.time() + 30
    winner: tuple[dict[str, Any], Path] | None = None

    while time.time() < deadline and winner is None:
        for domain, page, temp_output in tabs:
            try:
                # Wait for this tab's DOM to be ready (short timeout per check)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass  # Not ready yet, try next tab

                html = page.content()
                if len(html) < 5000:
                    continue

                lower = html.lower()
                # Skip if still on Cloudflare challenge or article not found
                if any(sig in lower for sig in ["checking your browser", "just a moment"]):
                    continue
                if any(sig in lower for sig in ["article not found", "статья не найдена"]):
                    log.info(f"   Sci-Hub: {domain} — article not found")
                    continue

                # Try to extract PDF URL
                pdf_url = extract_pdf_url_from_html(html, page.url)
                if pdf_url:
                    log.info(f"   Sci-Hub: {domain} found PDF, downloading...")
                    from ..browser_engine import download_pdf_via_browser
                    if download_pdf_via_browser(pdf_url, temp_output, config):
                        # Verify PDF file — retry with backoff (browser may still flush)
                        for _retry in range(10):
                            if temp_output.exists() and temp_output.stat().st_size > 5000:
                                if is_pdf_file(temp_output):
                                    result = success(doi, temp_output, f"Sci-Hub(tab:{domain.split('//')[-1][:15]})")
                                    winner = (result, temp_output)
                                    break
                            time.sleep(0.3)
                        if winner is not None:
                            break
                        else:
                            log.info(f"   Sci-Hub: {domain} download finished but PDF check failed")
            except Exception:
                continue
        if winner is None:
            time.sleep(0.3)

    # Phase 3: Cleanup — close all tabs, remove loser temp files
    for domain, page, temp_output in tabs:
        try:
            page.close()
        except Exception:
            pass
        if winner is None or temp_output != winner[1]:
            if temp_output.exists():
                try:
                    temp_output.unlink(missing_ok=True)
                except OSError:
                    pass

    if winner is not None:
        result, temp_output = winner
        if temp_output != output_path and temp_output.exists():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            if output_path.exists():
                output_path.unlink()
            temp_output.rename(output_path)
            result["file"] = str(output_path)
        return result

    log.info("   Sci-Hub: no tab found a PDF")
    return None


def try_scihub(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    use_tor: bool = False,
    cancel_event: Any = None,
) -> dict[str, Any] | None:
    log.info(f"   try_scihub called for {doi}")
    if cancel_event is not None and cancel_event.is_set():
        return None
    if not config.get("scihub_enabled", False):
        log.info(f"   Sci-Hub disabled")
        return None

    # Browser-first is a clearnet pass. Close its thread-owned browser before
    # slower domain probes or Tor bootstrap so cancellation cannot strand it.
    if not use_tor and _is_browser_available(config):
        from .. import browser_engine

        try:
            all_domains_for_browser = config.get("scihub_domains") or DEFAULT_SCIHUB_DOMAINS
            for domain in all_domains_for_browser[:4]:
                if _cancelled(cancel_event):
                    return None
                landing_url = f"{domain}/{urllib.parse.quote(doi, safe='/')}"
                result = _browser_first_download(
                    landing_url,
                    doi,
                    output_path,
                    config,
                    cancel_event=cancel_event,
                )
                if result:
                    return result
        finally:
            browser_engine.shutdown_shared_browser()

    _probe_scihub_domains(config)
    if _cancelled(cancel_event):
        return None
    all_domains = config.get("scihub_domains") or DEFAULT_SCIHUB_DOMAINS
    stats = load_stats(config)

    # Track failure reason for better diagnostic messages
    _failure_reason = "all domains unreachable"
    _any_reachable = False
    _any_browser_tried = False


    if _HAS_COMPILED_CORE:
        domains = _select_domains_compiled(all_domains, stats, _is_browser_domain)
    else:
        now = time.time()
        cooldown_domains = []
        for d in all_domains:
            d_stats = stats.get(d, {})
            last_fail = d_stats.get("last_fail_time", 0)
            fail_streak = d_stats.get("fail_streak", 0)
            reachable = d_stats.get("reachable")
            # Skip domains with many consecutive failures
            if fail_streak >= 10 and (now - last_fail) < 300:
                continue
            # Skip domains that are unreachable (from recent probe)
            if reachable is False and (now - last_fail) < 600:
                continue
            cooldown_domains.append(d)

        if not cooldown_domains:
            for d in all_domains:
                stats[d] = {"success": 0, "fail": 0, "last_fail_time": 0, "fail_streak": 0}
            cooldown_domains = all_domains

        def _domain_score(d: str) -> float:
            s = stats.get(d, {})
            successes = s.get("success", 0)
            failures = s.get("fail", 0)
            reachable = s.get("reachable")
            total = successes + failures
            # Unreachable domains get lowest score
            if reachable is False:
                return -99999
            if total == 0:
                return 0.5
            success_rate = successes / total
            avg_latency = s.get("avg_latency_ms", 5000)
            score = success_rate * 1000 - avg_latency / 1000
            # Boost browser-accessible domains (bypasses Cloudflare)
            if _is_browser_available(config) and _is_browser_domain(d):
                score += 5000
            return score

        cooldown_domains.sort(key=_domain_score, reverse=True)
        domains = cooldown_domains[:3]
    log.info(f"   Sci-Hub domains to try: {domains}")

    if len(domains) == 1:
        try:
            result = try_scihub_domain(
                doi,
                domains[0],
                output_path,
                config,
                use_tor=use_tor,
                cancel_event=cancel_event,
            )
            if result:
                record_result(domains[0], True, config)
                return result
            record_result(domains[0], False, config)
        except Exception:
            record_result(domains[0], False, config)
        log.info(f"   Sci-Hub: only domain {domains[0]} failed")
        return None

    # Try best domain first with short timeout
    best_domain = domains[0]
    best_output = output_path.parent / f"{output_path.stem}_scihub_{best_domain.split('//')[1].replace('.', '_')}.pdf"
    log.info(f"   Sci-Hub: trying {best_domain} first...")
    try:
        result = try_scihub_domain(
            doi,
            best_domain,
            best_output,
            config,
            use_tor=use_tor,
            cancel_event=cancel_event,
        )
        if result and result.get("success"):
            final_path = Path(result.get("file", ""))
            if _cancelled(cancel_event):
                if final_path != output_path:
                    with contextlib.suppress(OSError):
                        final_path.unlink(missing_ok=True)
                return None
            if final_path != output_path and final_path.exists():
                if not publish_pdf_file_atomic(
                    final_path,
                    output_path,
                    cancel_event,
                ):
                    with contextlib.suppress(OSError):
                        final_path.unlink(missing_ok=True)
                    if _cancelled(cancel_event):
                        return None
                    result = None
                else:
                    result["file"] = str(output_path)
            if result is not None:
                log.info(f"   Sci-Hub: OK {best_domain}")
                record_result(best_domain, True, config)
                return result
        record_result(best_domain, False, config)
    except Exception:
        record_result(best_domain, False, config)

    if _cancelled(cancel_event):
        return None

    # Best domain failed - race remaining domains
    remaining = domains[1:]
    if not remaining:
        return None

    log.info(f"   Sci-Hub: racing {len(remaining)} backup domains...")
    backup_stop = threading.Event()
    backup_root = output_path.parent
    if backup_root.name.startswith(".race-"):
        backup_root = backup_root.parent
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_dir = Path(tempfile.mkdtemp(prefix=".scihub-race-", dir=backup_root))

    worker_cancel = _CombinedCancelEvent(backup_stop, cancel_event)
    pool = ThreadPoolExecutor(max_workers=len(remaining))
    futures = {}
    pool_stopped = False

    def cleanup_outputs() -> None:
        for future in futures:
            try:
                future.result()
            except Exception:
                pass
        shutil.rmtree(backup_dir, ignore_errors=True)

    def stop_backup_workers() -> None:
        nonlocal pool_stopped
        if pool_stopped:
            return
        backup_stop.set()
        for future in futures:
            future.cancel()
        # Running futures cannot be cancelled by Future.cancel(). Waiting here
        # guarantees each domain worker closes Playwright on its owner thread
        # before this source releases its outer browser permit or starts Tor.
        pool.shutdown(wait=True, cancel_futures=True)
        pool_stopped = True

    try:
        for domain in remaining:
            src_output = backup_dir / f"{domain.split('//')[1].replace('.', '_')}.pdf"
            futures[
                pool.submit(
                    try_scihub_domain,
                    doi,
                    domain,
                    src_output,
                    config,
                    use_tor,
                    worker_cancel,
                )
            ] = (domain, src_output)

        pending = set(futures)
        deadline = time.monotonic() + _SCIHUB_BACKUP_TIMEOUT_SECONDS
        while pending and time.monotonic() < deadline and not worker_cancel.is_set():
            done, pending = wait(
                pending,
                timeout=min(0.1, max(0.0, deadline - time.monotonic())),
                return_when=FIRST_COMPLETED,
            )
            for future in done:
                domain, src_output = futures[future]
                try:
                    result = future.result()
                except Exception:
                    result = None
                if result and result.get("success"):
                    final_path = Path(result.get("file", ""))
                    stop_backup_workers()
                    if _cancelled(cancel_event):
                        return None
                    if final_path != output_path and final_path.exists():
                        if not publish_pdf_file_atomic(
                            final_path,
                            output_path,
                            cancel_event,
                        ):
                            if _cancelled(cancel_event):
                                return None
                            record_result(domain, False, config)
                            continue
                        result["file"] = str(output_path)
                    record_result(domain, True, config)
                    log.info(f"   Sci-Hub: OK {domain}")
                    return result
                record_result(domain, False, config)
                if src_output.exists():
                    try:
                        src_output.unlink(missing_ok=True)
                    except OSError:
                        pass

        if pending and not worker_cancel.is_set():
            log.info("   Sci-Hub: backup domains timed out")
    finally:
        stop_backup_workers()
        cleanup_outputs()

    # All clearnet domains failed — auto-retry with Tor + .onion (only if config allows)
    if _cancelled(cancel_event):
        return None
    if not use_tor and config.get("use_tor_for_scihub", True):
        log.info("   Sci-Hub: all clearnet domains failed, retrying via Tor...")
        return try_scihub(
            doi,
            output_path,
            config,
            use_tor=True,
            cancel_event=cancel_event,
        )

    log.warning(f"   Sci-Hub: all domains failed for {doi}. Check: 1) network connectivity 2) Tor status (scansci-pdf tor_start)")
    return None
