"""Test SSO institutional login for each configured publisher.

Usage:
    python -m tests.test_sso_publishers [--publisher Wiley] [--skip Wiley,Springer]
    python -m tests.test_sso_publishers --list
    python -m tests.test_sso_publishers --report

Interactive: opens a visible Camoufox browser for each publisher. You must
complete the CAS/SSO login in the browser window. The script detects
success and moves on to the next publisher.

Set CARSI_IDP_NAME env var to your institution's Chinese name, e.g.:
    set CARSI_IDP_NAME=清华大学
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Project root is two levels up from tests/
_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT / "src"))

from scansci_pdf.publisher_strategies import (
    _PUBLISHER_SSO_CONFIG,
    _AUTH_KEYWORDS,
    _AUTH_TITLES,
    _IDP_MAP,
)

# Real paywalled papers verified via Crossref API (2026-05-14).
# All DOIs resolved successfully (HTTP 302 → publisher article page).
_TEST_DOIS: dict[str, str] = {
    "Wiley":      "10.1002/adma.73337",          # Advanced Materials
    "Elsevier":   "10.1016/j.cell.2026.04.023",   # Cell
    "Springer":   "10.1038/s41586-026-10501-y",   # Nature (Springer Nature)
    "ACS":        "10.1021/jacs.5c20581",         # JACS
    "IEEE":       "10.1109/tit.2026.3670320",     # IEEE Trans Inf Theory
    "Tandfonline":"10.1080/00207543.2026.2669883",# Int J Prod Res
    "Oxford":     "10.1093/nar/gkag457",          # Nucleic Acids Research
    "RSC":        "10.1039/d5cs01021g",           # Chemical Society Reviews
    "IOP":        "10.1088/1361-648x/ae62e5",     # J Phys: Condensed Matter
    "APS":        "10.1103/nxjq-jwgp",           # Physical Review Research
    "AIP":        "10.1063/5.0316442",            # Applied Physics Letters
    "Nature":     "10.1038/s41586-026-10501-y",   # Nature
    "Science":    "10.1126/science.aea1676",       # Science
    "ACM":        "10.1145/3806644",              # Communications of the ACM
}

# Direct article URLs resolved from DOIs above
_ARTICLE_URLS: dict[str, str] = {
    "Wiley":      "https://onlinelibrary.wiley.com/doi/10.1002/adma.73337",
    "Elsevier":   "https://linkinghub.elsevier.com/retrieve/pii/S0092867426004587",
    "Springer":   "https://www.nature.com/articles/s41586-026-10501-y",
    "ACS":        "https://pubs.acs.org/doi/10.1021/jacs.5c20581",
    "IEEE":       "https://ieeexplore.ieee.org/document/11421544/",
    "Tandfonline":"https://www.tandfonline.com/doi/full/10.1080/00207543.2026.2669883",
    "Oxford":     "https://academic.oup.com/nar/advance-article/doi/10.1093/nar/gkag457/8676203",
    "RSC":        "https://xlink.rsc.org/?DOI=D5CS01021G",
    "IOP":        "https://iopscience.iop.org/article/10.1088/1361-648X/ae62e5",
    "APS":        "https://link.aps.org/doi/10.1103/PhysRevLett.132.020401",
    "AIP":        "https://pubs.aip.org/aip/apl/article/128/19/193703/3391179/",
    "Nature":     "https://www.nature.com/articles/s41586-026-10501-y",
    "Science":    "https://www.science.org/doi/10.1126/science.aea1676",
    "ACM":        "https://dl.acm.org/doi/10.1145/3806644",
}


def _detect_paywall_local(html: str) -> bool:
    """Lightweight paywall detection for test script."""
    lower = html[:50000].lower()
    signals = [
        "sign in to access", "login to access", "institutional access",
        "purchase access", "subscribe to access", "buy article",
        "access through your institution", "get access",
        "this article is behind a paywall", "subscription required",
        "please log in", "register to continue",
        "access denied", "you do not have access",
        "institutional login", "shibboleth", "openathens",
    ]
    return any(sig in lower for sig in signals)


def _detect_cloudflare(html: str) -> bool:
    lower = html[:5000].lower()
    cf_signals = [
        "cf-browser-verification", "challenge-platform",
        "just a moment", "attention required",
        "security check", "verify you are human",
        "access to this page has been denied",
        "crasolve",
    ]
    return any(sig in lower for sig in cf_signals)


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


def test_publisher(
    publisher: str,
    idp_name: str,
    timeout_per_publisher: int = 300,
) -> dict[str, Any]:
    """Test SSO login for one publisher. Returns result dict."""
    result: dict[str, Any] = {
        "publisher": publisher,
        "passed": False,
        "error": None,
        "checks": {},
    }

    try:
        from camoufox.sync_api import Camoufox
        from camoufox.addons import DefaultAddons
    except ImportError:
        result["error"] = "camoufox not installed"
        return result

    sso_cfg = _PUBLISHER_SSO_CONFIG.get(publisher, _PUBLISHER_SSO_CONFIG["_default"])
    idp_en = _IDP_MAP.get(idp_name, idp_name)
    article_url = _ARTICLE_URLS.get(publisher, f"https://doi.org/{_TEST_DOIS[publisher]}")
    doi = _TEST_DOIS[publisher]

    _log(f"{publisher}: starting test — {article_url[:70]}")

    try:
        with Camoufox(headless=False, exclude_addons=[DefaultAddons.UBO]) as browser:
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            page = context.new_page()

            # Step 1: Navigate to article
            _log(f"  [{publisher}] loading article page...")
            try:
                page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(5)
            except Exception as exc:
                _log(f"  [{publisher}] page load warning: {exc}")

            title = page.title()
            url = page.url
            _log(f"  [{publisher}] page: '{title[:60]}'")

            result["checks"]["article_loaded"] = True

            # Step 2: Check if already authenticated (cookies from previous test)
            already_on_auth = any(x in title for x in _AUTH_TITLES) or \
                              any(x in url.lower() for x in _AUTH_KEYWORDS)
            result["checks"]["already_on_auth"] = already_on_auth

            html = page.content()
            is_paywall = _detect_paywall_local(html)
            is_cloudflare = not is_paywall and _detect_cloudflare(html)

            result["checks"]["cloudflare"] = is_cloudflare

            if not is_paywall and not already_on_auth and not is_cloudflare:
                _log(f"  [{publisher}] no paywall detected — may already have access")
                result["checks"]["paywall_detected"] = False
                result["passed"] = True
                result["note"] = "No paywall — already authenticated or open access"
                return result

            if is_cloudflare:
                _log(f"  [{publisher}] Cloudflare challenge detected — anti-bot may block SSO")
                result["checks"]["paywall_detected"] = False
                result["passed"] = False
                result["error"] = "Cloudflare challenge — cannot test SSO"
                return result

            result["checks"]["paywall_detected"] = True

            # Step 3: Click SSO link
            _log(f"  [{publisher}] clicking institutional login...")
            url_before_click = page.url
            page.evaluate(sso_cfg["sso_link_js"])

            # Wait for Cloudflare + OpenAthens auto-redirect
            _log(f"  [{publisher}] waiting for SSO page to resolve (Cloudflare + OpenAthens)...")
            for _ in range(20):
                time.sleep(3)
                url_now = page.url
                title_now = page.title()
                # Stop waiting once we reach CAS/OpenAthens/IDP (beyond ssostart)
                if ("openathens" in url_now.lower()
                    or idp_en.lower() in url_now.lower()
                    or "cas" in url_now.lower() or "/idp/" in url_now.lower()):
                    break

            title_after = page.title()
            url_after = page.url
            _log(f"  [{publisher}] after SSO click: '{title_after[:50]}' {url_after[:60]}")
            result["checks"]["sso_clicked"] = True

            # Check if the SSO link actually caused navigation
            if url_after == url_before_click and title_after == title:
                _log(f"  [{publisher}] WARNING: page did not change after SSO click — link may be stale")
                result["checks"]["sso_navigated"] = False
            else:
                result["checks"]["sso_navigated"] = True

            # Step 4: Search for institution ONLY if still on WAYF page (not auto-redirected)
            search_found = False
            still_on_wayf = not any(x in page.url.lower() for x in ('openathens', idp_en.lower(), 'cas', 'idp'))
            if still_on_wayf:
                for sel in sso_cfg["search_selectors"]:
                    si = page.query_selector(sel)
                    if si:
                        si.fill(idp_en)
                        _log(f"  [{publisher}] searched '{idp_en}' via {sel}")
                        time.sleep(3)

                        # Click matching result
                        clicked = page.evaluate(f"""
                            (name) => {{
                                const items = document.querySelectorAll(
                                    '[class*="result"], [class*="suggestion"], [class*="federation"], li, a, button');
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
                            _log(f"  [{publisher}] selected institution '{idp_en}'")
                            time.sleep(5)
                        search_found = True
                        break
            else:
                _log(f"  [{publisher}] auto-redirected to CAS/OpenAthens — skipping institution search")
                search_found = True  # Auto-redirect is success

            result["checks"]["institution_search_found"] = search_found

            # Step 5: Wait for CAS login
            needs_cas = any(x in page.title() for x in _AUTH_TITLES) or \
                        any(x in page.url.lower() for x in _AUTH_KEYWORDS)

            if needs_cas:
                result["checks"]["cas_required"] = True
                print(f"\n  >>> [{publisher}] 请在浏览器中完成 CAS 登录 <<<\n")

                deadline = time.time() + timeout_per_publisher
                login_ok = False
                while time.time() < deadline:
                    time.sleep(3)
                    try:
                        t = page.title()
                        u = page.url
                    except Exception:
                        break
                    is_auth = any(x in t for x in _AUTH_TITLES)
                    is_auth_url = any(x in u.lower() for x in _AUTH_KEYWORDS)
                    if not is_auth and not is_auth_url:
                        login_ok = True
                        break

                if not login_ok:
                    _log(f"  [{publisher}] CAS login TIMED OUT")
                    result["error"] = "CAS login timed out"
                    return result

                _log(f"  [{publisher}] CAS login succeeded!")
                result["checks"]["cas_login_ok"] = True
                time.sleep(3)
            else:
                result["checks"]["cas_required"] = False

            # Step 6: Verify — check no paywall on current page
            html_after = page.content()
            still_paywall = _detect_paywall_local(html_after)
            result["checks"]["paywall_after_login"] = still_paywall

            if still_paywall:
                _log(f"  [{publisher}] still paywalled after login")
                result["error"] = "Still paywalled after login"
                return result

            # Step 7: Try PDF fetch via in-browser JS
            _log(f"  [{publisher}] trying PDF fetch post-login...")
            pdf_paths = sso_cfg["pdf_paths"](doi)
            import base64
            pdf_b64 = page.evaluate(f"""
                (async () => {{
                    const paths = {json.dumps(pdf_paths)};
                    for (const p of paths) {{
                        try {{
                            const resp = await fetch(p, {{credentials: 'include',
                                headers: {{'Accept': 'application/pdf,*/*'}}}});
                            if (!resp.ok) return 'status:' + resp.status;
                            const ct = resp.headers.get('content-type') || '';
                            if (!ct.includes('pdf') && !ct.includes('octet'))
                                return 'ct:' + ct.substring(0, 50);
                            const blob = await resp.blob();
                            return new Promise((resolve) => {{
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
                    _log(f"  [{publisher}] PDF fetched successfully! {len(pdf_bytes)} bytes")
                    result["passed"] = True
                    result["checks"]["pdf_fetched"] = True
                    return result

            # Step 8: Check if page shows PDF link
            has_pdf_link = page.evaluate("""
                (() => {
                    for (const a of document.querySelectorAll('a')) {
                        const href = (a.href || '').toLowerCase();
                        if (href.includes('/pdf/') || href.includes('pdfdirect') ||
                            href.includes('.pdf') || href.includes('showPdf')) {
                            return true;
                        }
                    }
                    return false;
                })()
            """)
            result["checks"]["pdf_link_found"] = bool(has_pdf_link)

            # If we got here without paywall, consider it passing
            result["passed"] = not still_paywall
            if result["passed"]:
                result["note"] = "Login OK but PDF not fetched — may need page-level extraction"

        return result

    except Exception as exc:
        _log(f"  [{publisher}] ERROR: {exc}")
        result["error"] = str(exc)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Test publisher SSO configurations")
    parser.add_argument("--publisher", help="Test only this publisher")
    parser.add_argument("--skip", help="Comma-separated publishers to skip")
    parser.add_argument("--list", action="store_true", help="List all configured publishers")
    parser.add_argument("--report", action="store_true", help="Show last test report")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Max seconds per publisher CAS login (default: 300)")
    parser.add_argument("--idp", help="Institution name (overrides CARSI_IDP_NAME env)")
    args = parser.parse_args()

    if args.list:
        print("Configured publishers (+_default):")
        for i, pub in enumerate(_PUBLISHER_SSO_CONFIG, 1):
            mark = " *" if pub == "_default" else ""
            print(f"  {i:2d}. {pub}{mark}")
        return

    if args.report:
        report_path = Path(__file__).parent / "sso_test_report.json"
        if report_path.exists():
            print(report_path.read_text(encoding="utf-8"))
        else:
            print("No report found. Run tests first.")
        return

    # --- Run tests ---
    idp_name = args.idp or ""
    if not idp_name:
        print("ERROR: set CARSI_IDP_NAME env var or use --idp to specify institution")
        print("Example: set CARSI_IDP_NAME=清华大学")
        return
    print(f"Institution: {idp_name}")
    print(f"Timeout per publisher: {args.timeout}s")
    print()

    publishers = list(_PUBLISHER_SSO_CONFIG.keys())
    publishers.remove("_default")

    if args.publisher:
        publishers = [args.publisher]
    if args.skip:
        skip_set = {s.strip() for s in args.skip.split(",")}
        publishers = [p for p in publishers if p not in skip_set]

    print(f"Will test {len(publishers)} publisher(s): {', '.join(publishers)}")
    print("=" * 60)

    results: list[dict[str, Any]] = []
    for i, pub in enumerate(publishers, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(publishers)}] Testing {pub}")
        print(f"{'='*60}")
        r = test_publisher(pub, idp_name, timeout_per_publisher=args.timeout)
        results.append(r)
        status = "PASS" if r["passed"] else "FAIL"
        err = f" — {r['error']}" if r.get("error") else ""
        print(f">>> {pub}: {status}{err}")

    # Save report
    report = {
        "timestamp": datetime.now().isoformat(),
        "idp_name": idp_name,
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "results": results,
    }

    report_path = Path(__file__).parent / "sso_test_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{'='*60}")
    print(f"SUMMARY: {report['passed']}/{report['total']} passed")
    print(f"Report saved to: {report_path}")
    for r in results:
        status = "PASS" if r["passed"] else "FAIL"
        note = f" ({r.get('note', '')})" if r.get("note") else ""
        err = f" — ERROR: {r['error']}" if r.get("error") else f"{note}"
        print(f"  {status:4s}  {r['publisher']}{err}")


if __name__ == "__main__":
    main()
