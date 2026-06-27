"""Sci-Hub source with domain rotation, CAPTCHA detection, and Tor support."""

from __future__ import annotations

import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests

from ..config import DEFAULT_SCIHUB_DOMAINS
from ..domain_db import load_stats, record_result, update_probe, set_probe_timestamp, get_probe_timestamp
from ..log import get_logger
from ..network import fetch, proxy_dict, select_proxy_for_url, _is_cloudflare_block, USER_AGENT
from ..pdf_utils import extract_pdf_url_from_html, is_pdf_file, success, _response_looks_pdf

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

# Track domains that require CloakBrowser (in-memory, per session)
_browser_domains: set[str] = set()

def _mark_browser_required(domain: str, config: dict[str, Any]) -> None:
    """Mark a domain as requiring CloakBrowser for future ranking."""
    _browser_domains.add(domain)

def _is_browser_domain(domain: str) -> bool:
    """Check if domain requires CloakBrowser."""
    return domain in _browser_domains

_PROBE_TTL_HOURS = 4
_SCIHUB_PROBE_WORKERS = 8


def _probe_single_domain(domain: str, proxy: str | None, timeout: tuple[int, int]) -> tuple[str, bool, float]:
    proxies = proxy_dict(proxy)
    t0 = time.time()
    try:
        s = requests.Session()
        s.trust_env = False
        resp = s.get(domain, timeout=timeout, proxies=proxies, allow_redirects=True,
                     headers={"User-Agent": USER_AGENT})
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


def _probe_scihub_domains(config: dict[str, Any]) -> None:
    last_probe = get_probe_timestamp(config)
    now = time.time()
    if now - last_probe < _PROBE_TTL_HOURS * 3600:
        return

    proxy = select_proxy_for_url("https://sci-hub.st", config)
    domains = config.get("scihub_domains") or DEFAULT_SCIHUB_DOMAINS
    timeout = (5, 10)

    with ThreadPoolExecutor(max_workers=_SCIHUB_PROBE_WORKERS) as pool:
        futures = {pool.submit(_probe_single_domain, d, proxy, timeout): d for d in domains}
        for future in as_completed(futures, timeout=15):
            domain, ok, latency = future.result()
            update_probe(domain, ok, round(latency, 1), config)

    set_probe_timestamp(config)


def _is_browser_available(config: dict[str, Any]) -> bool:
    """Check if CloakBrowser is reachable. Returns False in asyncio context."""
    # Playwright Sync API cannot run inside an asyncio event loop
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


def _browser_first_download(
    landing_url: str,
    doi: str,
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Try Browser-first download for Sci-Hub. Bypasses Cloudflare/CAPTCHA."""
    try:
        from ..browser_engine import solve_url, download_pdf_via_browser
        from ..pdf_utils import is_pdf_file, success, extract_pdf_url_from_html
        from urllib.parse import urlparse
        domain = urlparse(landing_url).netloc or landing_url[:40]

        log.info(f"   [browser-first] trying {landing_url[:80]}")
        result = solve_url(landing_url, config, max_timeout=30000)
        if not result:
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

        # Check for empty embed (Sci-Hub has no PDF)
        if '<embed' in lower and 'src=""' in lower:
            log.info(f"   [browser-first] empty embed — article not in Sci-Hub database")
            return None

        # Extract PDF URL from HTML
        pdf_url = extract_pdf_url_from_html(html, solution.get("url", landing_url))
        if pdf_url:
            log.info(f"   [browser-first] found PDF: {pdf_url[:80]}")
            # Download via CloakBrowser (handles Cloudflare on PDF host too)
            if download_pdf_via_browser(pdf_url, output_path, config):
                if is_pdf_file(output_path):
                    return success(doi, output_path, f"Sci-Hub(Browser)")

        # Check if the response itself is a PDF
        import base64
        resp_data = solution.get("response", "")
        if isinstance(resp_data, str) and len(resp_data) > 5000:
            try:
                pdf_bytes = base64.b64decode(resp_data) if resp_data.startswith("JVBER") else resp_data.encode("utf-8")
                if pdf_bytes[:5] == b"%PDF-":
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_bytes(pdf_bytes)
                    if is_pdf_file(output_path):
                        return success(doi, output_path, f"Sci-Hub(Browser)")
            except Exception:
                pass

        log.info(f"   [browser-first] no PDF found in response")
        return None
    except Exception as e:
        log.info(f"   [browser-first] error: {e}")
        return None


def try_scihub_domain(
    doi: str,
    domain: str,
    output_path: Path,
    config: dict[str, Any],
    use_tor: bool = False,
) -> dict[str, Any] | None:
    landing_url = f"{domain.rstrip('/')}/{urllib.parse.quote(doi, safe='/')}"

    # Browser-first: bypass Cloudflare/CAPTCHA before HTTP attempt
    if _is_browser_available(config):
        result = _browser_first_download(landing_url, doi, output_path, config)
        if result:
            return result

    try:
        resp = fetch(landing_url, config, stream=True, use_tor=use_tor)

        # Fallback to browser on Cloudflare/403/CAPTCHA
        if resp.status_code in (403, 503) or _is_cloudflare_block(resp):
            _mark_browser_required(domain, config)
            resp = _try_browser(landing_url, config, resp)
            if resp is None:
                return None

        if resp.status_code >= 400:
            return None

        first = next(resp.iter_content(chunk_size=8192), b"")
        # Check for CAPTCHA in first chunk
        if resp.status_code == 200 and first:
            content_sample = first[:5000].decode('utf-8', errors='ignore').lower()
            if 'captcha' in content_sample or 'recaptcha' in content_sample:
                log.info(f"   CAPTCHA detected, trying browser...")
                # Use browser to bypass CAPTCHA
                browser_resp = _try_browser(landing_url, config, resp)
                if browser_resp is None:
                    log.warning(f"   browser failed — is CloakBrowser running? Start: cd CloakBrowser && npm start")
                    return None
                # Get new content from browser response
                resp = browser_resp
                first = resp.content[:8192] if resp.content else b""
                log.info(f"   browser bypassed CAPTCHA, content size: {len(first)}")

        if _response_looks_pdf(resp, first):
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
                return success(doi, output_path, f"Sci-Hub({domain})")

        # Collect full HTML content (browser responses have _content, direct responses need raw.read)
        if resp._content:
            html = first + resp._content
        else:
            try:
                html = first + resp.raw.read(512_000, decode_content=True)
            except Exception:
                html = first
        pdf_url = extract_pdf_url_from_html(html.decode("utf-8", errors="ignore"), resp.url)
        if not pdf_url:
            return None
        result = download_pdf_from_scihub(pdf_url, output_path, config, f"Sci-Hub({domain})", use_tor=use_tor, cookies=resp.cookies)
        if result:
            result["doi"] = doi
            result["identifier"] = doi
        return result
    except Exception:
        return None


def _try_browser(
    url: str,
    config: dict[str, Any],
    original_resp: requests.Response,
) -> requests.Response | None:
    """Try CloakBrowser to bypass Cloudflare. Returns Response or None."""
    from ..browser_engine import solve_url
    result = solve_url(url, config)
    if not result:
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
) -> dict[str, Any] | None:
    from ..pdf_utils import download_pdf
    return download_pdf(url, output_path, config, source, require_pdf_like_url=False, use_tor=use_tor, cookies=cookies)


def try_scihub(doi: str, output_path: Path, config: dict[str, Any], use_tor: bool = False) -> dict[str, Any] | None:
    log.info(f"   try_scihub called for {doi}")
    if not config.get("scihub_enabled", False):
        log.info(f"   Sci-Hub disabled")
        return None

    # Browser-first pass: try a few reliable domains via CloakBrowser before HTTP
    if _is_browser_available(config):
        all_domains_for_browser = config.get("scihub_domains") or DEFAULT_SCIHUB_DOMAINS
        for domain in all_domains_for_browser[:4]:
            landing_url = f"{domain}/{urllib.parse.quote(doi, safe='/')}"
            result = _browser_first_download(landing_url, doi, output_path, config)
            if result:
                return result

    _probe_scihub_domains(config)
    all_domains = config.get("scihub_domains") or DEFAULT_SCIHUB_DOMAINS
    stats = load_stats(config)


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
            result = try_scihub_domain(doi, domains[0], output_path, config, use_tor=use_tor)
            if result:
                record_result(domains[0], True, config)
                return result
            record_result(domains[0], False, config)
        except Exception:
            record_result(domains[0], False, config)
        return None

    # Try best domain first with short timeout
    best_domain = domains[0]
    best_output = output_path.parent / f"{output_path.stem}_scihub_{best_domain.split('//')[1].replace('.', '_')}.pdf"
    log.info(f"   Sci-Hub: trying {best_domain} first...")
    try:
        result = try_scihub_domain(doi, best_domain, best_output, config, use_tor=use_tor)
        if result and result.get("success"):
            final_path = Path(result.get("file", ""))
            if final_path != output_path and final_path.exists():
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output_path.exists():
                    output_path.unlink()
                final_path.rename(output_path)
                result["file"] = str(output_path)
            log.info(f"   Sci-Hub: OK {best_domain}")
            record_result(best_domain, True, config)
            return result
        record_result(best_domain, False, config)
    except Exception:
        record_result(best_domain, False, config)

    # Best domain failed - race remaining domains
    remaining = domains[1:]
    if not remaining:
        return None

    log.info(f"   Sci-Hub: racing {len(remaining)} backup domains...")
    with ThreadPoolExecutor(max_workers=len(remaining)) as pool:
        futures = {}
        for domain in remaining:
            src_output = output_path.parent / f"{output_path.stem}_scihub_{domain.split('//')[1].replace('.', '_')}.pdf"
            futures[pool.submit(try_scihub_domain, doi, domain, src_output, config, use_tor)] = (domain, src_output)
        try:
            for future in as_completed(futures, timeout=10):
                domain, src_output = futures[future]
                try:
                    result = future.result(timeout=1)
                except Exception:
                    result = None
                if result and result.get("success"):
                    final_path = Path(result.get("file", ""))
                    if final_path != output_path and final_path.exists():
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        if output_path.exists():
                            output_path.unlink()
                        final_path.rename(output_path)
                        result["file"] = str(output_path)
                    for _, other_path in futures.values():
                        if other_path != output_path and other_path.exists():
                            try:
                                other_path.unlink(missing_ok=True)
                            except OSError:
                                pass
                    record_result(domain, True, config)
                    log.info(f"   Sci-Hub: OK {domain}")
                    return result
                else:
                    record_result(domain, False, config)
                    if src_output.exists():
                        try:
                            src_output.unlink(missing_ok=True)
                        except OSError:
                            pass
        except TimeoutError:
            log.info("   Sci-Hub: backup domains timed out")
        for _, src_output in futures.values():
            if src_output.exists():
                try:
                    src_output.unlink(missing_ok=True)
                except OSError:
                    pass

    # All clearnet domains failed — auto-retry with Tor + .onion
    if not use_tor:
        log.info("   Sci-Hub: all clearnet domains failed, retrying via Tor...")
        return try_scihub(doi, output_path, config, use_tor=True)

    log.warning(f"   Sci-Hub: all domains failed for {doi}. Check: 1) network connectivity 2) Tor status (scansci-pdf tor_start)")
    return None
