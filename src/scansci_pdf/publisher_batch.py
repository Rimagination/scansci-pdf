"""Profile-driven CloakBrowser batch downloader with retryable machine states."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import inspect
import json
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from .config import load_config
from .extractors import pdf_extractor
from .publisher_pdf_router import (
    build_pdf_candidates,
    extract_elsevier_pii,
    is_pdf_candidate_url,
    is_supplementary_url,
)
from .publisher_profiles import ACS_PROFILE, PublisherProfile

EST_ISSN = "1520-5851"
MIN_PDF_BYTES = 5_000
MAX_BROWSER_CONCURRENCY = 4
PDF_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)

NON_ARTICLE_PDF_MARKERS = (
    "plain language summary",
    "p l a i n l a n g u a g e s u m m a r y",
    "electronic supporting information",
    "we are delighted to inform you that your manuscript",
    "department of health and human services food and drug administration",
    "new drug application",
)

RETRYABLE_REASONS = {
    "pdf_not_captured",
    "navigation_error",
    "challenge_or_viewer_timeout",
}

# Consecutive ip_blocked results from one run that halt the remaining records.
# An ACS / publisher IP block is not transient for the session (the IP stays
# blocked), so continuing only deepens it. 3 tolerates a stray single-record 403
# (e.g. one entitlement miss) while still tripping fast on a real block.
# NOTE: "ip_blocked" is intentionally NOT in RETRYABLE_REASONS.
IP_BLOCK_STOP_THRESHOLD = 3

# HTTP status codes that, when seen on a publisher PDF/article endpoint, are
# treated as an IP block signal. 403 is ACS's block-page status; 429 is the
# generic rate-limit/block code.
_IP_BLOCK_STATUS_CODES = {403, 429}

# Reusable JS helpers injected into page.evaluate() calls
_JS_VISIBLE = """(el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
}"""

_JS_TEXTOF = """(el) => [
    el.innerText || '',
    el.textContent || '',
    el.value || '',
    el.getAttribute('aria-label') || '',
    el.getAttribute('title') || ''
].join(' ')"""


@dataclass
class PaperRecord:
    doi: str
    title: str = ""
    published: str = ""
    url: str = ""


@dataclass
class DownloadResult:
    doi: str
    status: str
    reason: str = ""
    state: str = ""
    article_url: str = ""
    final_url: str = ""
    title: str = ""
    pdf_url: str = ""
    pdf_path: str = ""
    text_length: int = 0
    size_bytes: int = 0
    verified_match: bool = False
    diagnostic_path: str = ""
    events: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"


def safe_name(doi: str) -> str:
    return re.sub(r"[^\w\-.]", "_", doi)


def fetch_est_records(
    *,
    year: int,
    limit: int,
    email: str = "",
    session: requests.Session | None = None,
) -> list[PaperRecord]:
    """Fetch EST records from Crossref in newest-first publication order."""
    params = {
        "filter": (
            f"issn:{EST_ISSN},type:journal-article,"
            f"from-pub-date:{year}-01-01,until-pub-date:{year}-12-31"
        ),
        "sort": "published",
        "order": "desc",
        "rows": str(limit),
    }
    if email:
        params["mailto"] = email
    url = "https://api.crossref.org/works?" + urlencode(params)
    sess = session or requests.Session()
    resp = sess.get(
        url,
        timeout=30,
        headers={"User-Agent": f"instsci/0.1 ({email or 'local'})"},
    )
    resp.raise_for_status()
    records: list[PaperRecord] = []
    for item in resp.json().get("message", {}).get("items", []):
        doi = str(item.get("DOI", "")).strip()
        if not doi:
            continue
        title = " ".join(item.get("title") or [])
        published = item.get("published-print") or item.get("published-online") or item.get("published") or {}
        date_parts = published.get("date-parts") or [[]]
        published_text = ""
        if date_parts and date_parts[0]:
            published_text = "-".join(
                f"{part:02d}" if idx else str(part)
                for idx, part in enumerate(date_parts[0])
            )
        records.append(
            PaperRecord(
                doi=doi,
                title=re.sub(r"\s+", " ", title).strip(),
                published=published_text,
                url=str(item.get("URL", "")),
            )
        )
    return records[:limit]


class _PagePool:
    """Thread-safe page pool sharing a single browser context.

    All pages share the same context, which means they share:
    - Cookies (session tokens, auth cookies)
    - localStorage (publisher auth tokens)
    - sessionStorage (temporary session data)
    - HTTP connection pool (keep-alive authenticated connections)
    - TLS session tickets (established encrypted sessions)

    This is essential: login state is the ENTIRE browser context, not just cookies.
    Creating a new context is like switching devices — publishers may reject it.
    """

    def __init__(self, context: Any, max_size: int = 2):
        self._context = context
        self._max_size = max_size
        self._available: list[Any] = []
        self._in_use: set[int] = set()
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)

    def acquire(self) -> Any:
        """Acquire a page from the pool. Creates new if pool is empty and under limit."""
        with self._not_empty:
            # Try to reuse an available page
            while self._available:
                page = self._available.pop()
                try:
                    _ = page.url  # Test if page is still alive
                    self._in_use.add(id(page))
                    return page
                except Exception:
                    try:
                        page.close()
                    except Exception:
                        pass

            # Create new page if under limit
            if len(self._in_use) < self._max_size:
                page = self._context.new_page()
                self._in_use.add(id(page))
                return page

            # Wait for a page to be returned
            while not self._available:
                self._not_empty.wait(timeout=30)
                if self._available:
                    page = self._available.pop()
                    try:
                        _ = page.url
                        self._in_use.add(id(page))
                        return page
                    except Exception:
                        try:
                            page.close()
                        except Exception:
                            pass
            # Should not reach here, but just in case
            page = self._context.new_page()
            self._in_use.add(id(page))
            return page

    def release(self, page: Any) -> None:
        """Return a page to the pool for reuse."""
        with self._not_empty:
            self._in_use.discard(id(page))
            try:
                self._available.append(page)
            except Exception:
                pass
            self._not_empty.notify()

    def close_all(self) -> None:
        """Close all pages."""
        with self._lock:
            for page in self._available:
                try:
                    page.close()
                except Exception:
                    pass
            self._available.clear()
            self._in_use.clear()


class PublisherBatchDownloader:
    """Deterministic publisher workflow with diagnostic packets for surprises."""

    def __init__(
        self,
        config: dict | None = None,
        *,
        profile: PublisherProfile = ACS_PROFILE,
        institution_query: str = "",
        login_timeout_sec: int = 900,
        pdf_timeout_sec: int = 60,
        post_login_hold_sec: int = 0,
        post_run_hold_sec: int = 0,
    ) -> None:
        self.config = config or load_config()
        self.profile = profile
        self.institution_query = institution_query.strip()
        self.login_timeout_sec = login_timeout_sec
        self.pdf_timeout_ms = max(1, pdf_timeout_sec) * 1_000
        self.post_login_hold_sec = max(0, int(post_login_hold_sec or 0))
        self.post_run_hold_sec = max(0, int(post_run_hold_sec or 0))
        # Set True by _run_once/_run_once_parallel when IP_BLOCK_STOP_THRESHOLD
        # consecutive ip_blocked results trip an auto-stop. Read by run_records.
        self._ip_block_stopped: bool = False

    def run_records(
        self,
        records: list[PaperRecord],
        run_dir: str | Path,
        *,
        retry_failed: bool = True,
        target_verified: int | None = None,
        attempt_cache: str | Path | None = None,
        skip_attempted: bool = False,
        concurrency: int = 1,
    ) -> dict[str, Any]:
        """Download all records and write summary/manifest artifacts."""
        run_path = Path(run_dir)
        run_path.mkdir(parents=True, exist_ok=True)
        target = target_verified if target_verified and target_verified > 0 else None
        worker_count = min(max(1, int(concurrency or 1)), MAX_BROWSER_CONCURRENCY)
        if target:
            worker_count = 1
        attempt_cache_path = Path(attempt_cache) if attempt_cache else run_path / "attempts.jsonl"
        (run_path / "records.json").write_text(
            json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (run_path / "dois.txt").write_text(
            "\n".join(r.doi for r in records) + "\n",
            encoding="utf-8",
        )

        missing_reasons: dict[str, str] = {}
        records_to_run = records
        cached_skipped = 0
        if skip_attempted:
            attempted_dois = self._read_attempted_dois(attempt_cache_path)
            records_to_run = []
            for record in records:
                if record.doi.lower() in attempted_dois:
                    cached_skipped += 1
                    missing_reasons[record.doi.lower()] = "skipped_cached_attempt"
                else:
                    records_to_run.append(record)

        self._ip_block_stopped = False  # reset; set by _run_once(_parallel) on trip

        results = self._run_once(
            records_to_run,
            run_path / "primary",
            target_verified=target,
            attempt_cache_path=attempt_cache_path,
            phase="primary",
            concurrency=worker_count,
        )
        primary_ip_blocked = self._ip_block_stopped
        primary_counts = self._count_results(results)
        target_reached = bool(target and self._count_verified(results) >= target)
        if target_reached and len(results) < len(records_to_run):
            for record in records_to_run[len(results):]:
                missing_reasons[record.doi.lower()] = "target_verified_reached"

        failed_records = [
            record
            for record, result in zip(records, results)
            if result.status == "failed" and result.reason in RETRYABLE_REASONS
        ]

        retry_results: list[DownloadResult] = []
        # Skip the retry pass entirely when the primary pass was auto-stopped
        # for an IP block — retrying against a still-blocked IP only deepens it.
        retry_skipped_ip_block = primary_ip_blocked
        if retry_failed and failed_records and not target_reached and not retry_skipped_ip_block:
            remaining_target = target - self._count_verified(results) if target else None
            retry_results = self._run_once(
                failed_records,
                run_path / "retry",
                target_verified=remaining_target,
                attempt_cache_path=attempt_cache_path,
                phase="retry",
                concurrency=worker_count,
            )
            retry_by_doi = {result.doi.lower(): result for result in retry_results if result.ok}
            results = [
                retry_by_doi.get(result.doi.lower(), result)
                for result in results
            ]
            target_reached = bool(target and self._count_verified(results) >= target)

        summary = self._write_complete_artifacts(records, results, run_path, missing_reasons=missing_reasons)
        summary["publisher"] = self.profile.name
        summary["primary"] = primary_counts
        summary["final"] = self._count_results(results)
        summary["retry_attempted"] = len(failed_records)
        summary["retry_success"] = sum(1 for result in retry_results if result.ok)
        summary["target_verified"] = target
        summary["target_reached"] = target_reached
        summary["skipped"] = max(0, len(records) - len(results) - cached_skipped)
        summary["cached_skipped"] = cached_skipped
        summary["attempt_cache"] = str(attempt_cache_path)
        summary["concurrency"] = worker_count
        summary["browser_profile_dir"] = str(Path(self.config.get("chrome_profile_dir", "")))
        # Surface auto-stop so callers (CLI/MCP) can tell a halt from a clean
        # finish. ip_block_count is how many records came back ip_blocked in the
        # primary pass (the trip trigger).
        ip_blocked_total = sum(1 for r in results if r.reason == "ip_blocked")
        summary["auto_stopped"] = bool(retry_skipped_ip_block)
        summary["stop_reason"] = "ip_blocked" if retry_skipped_ip_block else ""
        summary["ip_blocked_count"] = ip_blocked_total
        (run_path / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return summary

    def _run_once(
        self,
        records: list[PaperRecord],
        run_dir: Path,
        *,
        target_verified: int | None = None,
        attempt_cache_path: Path | None = None,
        phase: str = "primary",
        concurrency: int = 1,
    ) -> list[DownloadResult]:
        run_dir.mkdir(parents=True, exist_ok=True)
        worker_count = min(max(1, int(concurrency or 1)), len(records) or 1)
        if worker_count > 1 and not target_verified:
            return self._run_once_parallel(
                records,
                run_dir,
                worker_count=worker_count,
                attempt_cache_path=attempt_cache_path,
                phase=phase,
            )

        results: list[DownloadResult] = []
        verified_count = 0
        consecutive_ip_blocks = 0

        context = self._launch_context()
        try:
            for record in records:
                result = self.fetch_one(context, record, run_dir)
                results.append(result)
                if result.ok and result.verified_match:
                    verified_count += 1
                self._append_attempt(attempt_cache_path, result, phase)
                self._write_results(run_dir / "summary_partial.json", results)
                # Auto-stop on consecutive IP blocks (serial mirror of the
                # parallel path's stop_event logic).
                if result.reason == "ip_blocked":
                    consecutive_ip_blocks += 1
                    if consecutive_ip_blocks >= IP_BLOCK_STOP_THRESHOLD:
                        self._ip_block_stopped = True
                        break
                else:
                    consecutive_ip_blocks = 0
                if target_verified and verified_count >= target_verified:
                    break
        finally:
            try:
                context.close()
            except Exception:
                pass

        self._write_results(run_dir / "summary.json", results)
        return results

    def _run_once_parallel(
        self,
        records: list[PaperRecord],
        run_dir: Path,
        *,
        worker_count: int,
        attempt_cache_path: Path | None = None,
        phase: str = "primary",
    ) -> list[DownloadResult]:
        """Run downloads in parallel using a shared browser context + page pool.

        Architecture:
        - ONE persistent context (preserves cookies, localStorage, sessionStorage,
          HTTP connections, TLS sessions — the complete login state)
        - N worker threads, each borrows a page from the pool, downloads, returns it
        - All pages share the same context = same identity, no re-login needed

        This is the correct model: login state is NOT just cookies. It includes
        localStorage tokens, sessionStorage, connection pools, and TLS sessions.
        Exporting cookies to a new context is like "switching devices" — some
        publishers reject that. Sharing the context keeps the same device identity.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        profile_root = run_dir / "worker-profiles"
        source_profile = Path(self.config.get("chrome_profile_dir", ""))
        profile_dir = self._prepare_worker_profile(source_profile, profile_root / f"{phase}-shared")

        results_by_index: dict[int, DownloadResult] = {}
        results_lock = threading.Lock()
        attempt_lock = threading.Lock()

        # Auto-stop state: once IP_BLOCK_STOP_THRESHOLD consecutive records come
        # back ip_blocked, the remaining records are skipped. A blocked IP does
        # not unblock mid-run, so churning the rest only deepens the block.
        stop_event = threading.Event()
        ip_block_count = {"n": 0}
        count_lock = threading.Lock()

        def record_result(index: int, result: DownloadResult) -> None:
            with results_lock:
                results_by_index[index] = result
                partial = [results_by_index[item_index] for item_index in sorted(results_by_index)]
                self._write_results(run_dir / "summary_partial.json", partial)
            with attempt_lock:
                self._append_attempt(attempt_cache_path, result, phase)
            # Escalate to a hard stop after N consecutive IP blocks.
            if result.reason == "ip_blocked":
                with count_lock:
                    ip_block_count["n"] += 1
                    tripped = ip_block_count["n"] >= IP_BLOCK_STOP_THRESHOLD
                if tripped and not stop_event.is_set():
                    stop_event.set()
                    self._ip_block_stopped = True
            elif result.ok or result.reason:
                # A success or any non-block failure resets the streak — only
                # *consecutive* blocks should trip the stop.
                with count_lock:
                    ip_block_count["n"] = 0

        # Single shared context — login state fully preserved
        context = self._launch_context(profile_dir=profile_dir)
        page_pool = _PagePool(context, max_size=worker_count)

        def run_worker(items: list[tuple[int, PaperRecord]]) -> None:
            for item_index, record in items:
                if stop_event.is_set():
                    break  # IP block tripped — skip the rest of this chunk
                page = page_pool.acquire()
                try:
                    record_result(item_index, self.fetch_one(page, record, run_dir))
                finally:
                    page_pool.release(page)

        indexed_records = list(enumerate(records))
        chunks = [indexed_records[i::worker_count] for i in range(worker_count)]

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [executor.submit(run_worker, chunk) for chunk in chunks if chunk]
            for future in as_completed(futures):
                future.result()

        page_pool.close_all()
        try:
            context.close()
        except Exception:
            pass

        results = [results_by_index[i] for i in range(len(records)) if i in results_by_index]
        self._write_results(run_dir / "summary.json", results)
        return results

    def _prepare_worker_profile(self, source: Path, target: Path) -> Path:
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            target.mkdir(parents=True, exist_ok=True)
            return target
        ignored_names = {
            "SingletonCookie",
            "SingletonLock",
            "SingletonSocket",
            "BrowserMetrics",
            "Cache",
            "Code Cache",
            "Crashpad",
            "DawnCache",
            "GPUCache",
            "GrShaderCache",
            "ShaderCache",
        }

        def ignore(_directory: str, names: list[str]) -> set[str]:
            return {name for name in names if name in ignored_names}

        try:
            shutil.copytree(source, target, ignore=ignore)
        except Exception:
            target.mkdir(parents=True, exist_ok=True)
        return target

    def _login_and_export_cookies(self, run_dir: Path, profile_dir: Path) -> list[dict]:
        """Login once and export cookies for all workers to share.

        This ensures the user only needs to complete SSO/login once,
        while each worker still gets its own browser fingerprint.

        Returns list of cookies that can be injected via context.add_cookies().
        """
        cookie_cache = run_dir / "shared_cookies.json"

        # Check for cached cookies from previous run
        if cookie_cache.exists():
            try:
                cookies = json.loads(cookie_cache.read_text(encoding="utf-8"))
                if cookies and isinstance(cookies, list):
                    self._event(None, "shared_cookies_loaded", f"{len(cookies)} cookies from cache")
                    return cookies
            except Exception:
                pass

        # Launch a dedicated login context
        self._event(None, "login_phase_start", "Launching browser for authentication...")
        context = self._launch_context(profile_dir=profile_dir)
        try:
            # Navigate to the publisher's institutional login page
            login_url = self._get_login_url()
            if not login_url:
                return []

            page = context.new_page()
            try:
                page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
                # Wait for user to complete login
                login_timeout = self.login_timeout_sec
                self._event(None, "login_waiting", f"Complete login in browser (timeout={login_timeout}s)")
                print(f"\n  🔐 Complete login in the browser window")
                print(f"     Timeout: {login_timeout}s")
                print(f"     Cookies will be shared with all download workers\n")

                deadline = time.time() + login_timeout
                while time.time() < deadline:
                    time.sleep(3)
                    try:
                        current_url = page.url
                    except Exception:
                        break
                    # Check if login is complete
                    if self._looks_like_logged_in(page, current_url):
                        break

                # Export cookies
                cookies = context.cookies()
                if cookies:
                    cookie_cache.write_text(
                        json.dumps(cookies, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    self._event(None, "shared_cookies_exported", f"{len(cookies)} cookies saved")
                    print(f"  ✅ Login successful! Exported {len(cookies)} cookies for {len(cookies)} workers")
                return cookies
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        finally:
            try:
                context.close()
            except Exception:
                pass

    def _get_login_url(self) -> str:
        """Get the institutional login URL for this publisher."""
        profile = self.profile
        login_url = profile.login_url
        if not login_url:
            # Construct from publisher name
            publisher = profile.name.lower()
            if "elsevier" in publisher or "sciencedirect" in publisher:
                login_url = "https://www.sciencedirect.com/"
            elif "springer" in publisher or "nature" in publisher:
                login_url = "https://link.springer.com/institutional-login"
            elif "wiley" in publisher:
                login_url = "https://onlinelibrary.wiley.com/"
            elif "ieee" in publisher:
                login_url = "https://ieeexplore.ieee.org/"
            elif "acs" in publisher:
                login_url = "https://pubs.acs.org/"
            else:
                login_url = f"https://www.{publisher}.com/"
        return login_url

    def _looks_like_logged_in(self, page: Any, current_url: str) -> bool:
        """Check if the page looks like a successful login."""
        url_lower = current_url.lower()
        # If we're no longer on a login/CAS page, consider it done
        if not any(kw in url_lower for kw in ("login", "cas", "sso", "signin", "sign-in")):
            return True
        # Check for SSO-specific indicators
        try:
            title = (page.title() or "").lower()
            if any(kw in title for kw in ("登录成功", "welcome", "dashboard")):
                return True
        except Exception:
            pass
        return False

    def _launch_context(self, profile_dir: str | Path | None = None):
        from .browser_engine import get_persistent_context

        profile_path = Path(profile_dir) if profile_dir else Path(self.config.get("chrome_profile_dir", ""))
        profile_path.mkdir(parents=True, exist_ok=True)
        return get_persistent_context(profile_path, self.config)

    def fetch_one(self, context_or_page: Any, record: PaperRecord, run_dir: Path) -> DownloadResult:
        # Support both context (creates new page) and pre-created page (from page pool)
        if hasattr(context_or_page, "new_page") and hasattr(context_or_page, "cookies"):
            # It's a context — create a new page
            page = context_or_page.new_page()
            _owns_page = True
        else:
            # It's already a page (from page pool)
            page = context_or_page
            _owns_page = False
        result = DownloadResult(
            doi=record.doi,
            status="failed",
            state="started",
            article_url=self.profile.article_url(record.doi),
        )
        try:
            self._event(result, "article_open", result.article_url)
            if not self._ensure_login(page, result):
                if self._article_access_available(page):
                    self._event(result, "login_completed_after_timeout", getattr(page, "url", ""))
                else:
                    result.reason = self._login_block_reason(page) or "sso_required"
                    result.state = result.reason
                    self._write_diagnostic(page, result, run_dir)
                    return result

            time.sleep(2)
            if self._looks_logged_out(page):
                self._event(result, "auth_wall_after_article_load", getattr(page, "url", ""))
                if not self._complete_login_from_current_page(page, result):
                    if self._article_access_available(page):
                        self._event(result, "login_completed_after_timeout", getattr(page, "url", ""))
                    else:
                        result.reason = self._login_block_reason(page) or "sso_required"
                        result.state = result.reason
                        self._write_diagnostic(page, result, run_dir)
                        return result
                time.sleep(2)
                if self._looks_logged_out(page):
                    result.reason = self._login_block_reason(page) or "sso_required"
                    result.state = result.reason
                    self._write_diagnostic(page, result, run_dir)
                    return result

            result.final_url = page.url
            result.title = self._title(page)
            result.state = "article_loaded"
            self._hold_after_login(page, result)
            ip_block: str | None = None
            pdf_bytes, pdf_url, ip_block = self._capture_pdf(page, record.doi, result)
            if not pdf_bytes:
                if self._looks_logged_out(page):
                    self._event(result, "auth_wall_after_pdf_attempt", getattr(page, "url", ""))
                    if self._complete_login_from_current_page(page, result):
                        result.final_url = page.url
                        result.title = self._title(page)
                        result.state = "article_loaded_after_sso"
                        pdf_bytes, pdf_url, cap_block = self._capture_pdf(page, record.doi, result)
                        ip_block = ip_block or cap_block
                    elif self._article_access_available(page):
                        self._event(result, "login_completed_after_timeout", getattr(page, "url", ""))
                        result.final_url = page.url
                        result.title = self._title(page)
                        result.state = "article_loaded_after_sso"
                        pdf_bytes, pdf_url, cap_block = self._capture_pdf(page, record.doi, result)
                        ip_block = ip_block or cap_block
                    # An IP block is terminal for this session — surface it ahead
                    # of any auth-wall reason so the batch can auto-stop instead
                    # of pointlessly retrying login against a blocked IP.
                    if ip_block:
                        result.reason = "ip_blocked"
                        result.state = "ip_blocked"
                        result.final_url = page.url
                        result.title = self._title(page)
                        self._write_diagnostic(page, result, run_dir)
                        return result
                    block_reason = self._login_block_reason(page)
                    if not pdf_bytes and (block_reason or self._looks_logged_out(page)):
                        result.reason = block_reason or "sso_required"
                        result.state = result.reason
                        result.final_url = page.url
                        result.title = self._title(page)
                        self._write_diagnostic(page, result, run_dir)
                        return result
                    if not pdf_bytes:
                        result.reason = "pdf_not_captured"
                        result.state = "pdf_not_captured"
                        result.final_url = page.url
                        result.title = self._title(page)
                        self._write_diagnostic(page, result, run_dir)
                        return result
                else:
                    if ip_block:
                        result.reason = "ip_blocked"
                        result.state = "ip_blocked"
                        result.final_url = page.url
                        result.title = self._title(page)
                        self._write_diagnostic(page, result, run_dir)
                        return result
                    block_reason = self._login_block_reason(page)
                    result.reason = block_reason or "pdf_not_captured"
                    result.state = result.reason
                    result.final_url = page.url
                    result.title = self._title(page)
                    self._write_diagnostic(page, result, run_dir)
                    return result

            if not pdf_bytes:
                result.reason = "pdf_not_captured"
                result.state = "pdf_not_captured"
                result.final_url = page.url
                result.title = self._title(page)
                self._write_diagnostic(page, result, run_dir)
                return result

            if not pdf_bytes:
                result.reason = "pdf_not_captured"
                result.state = "pdf_not_captured"
                result.final_url = page.url
                result.title = self._title(page)
                self._write_diagnostic(page, result, run_dir)
                return result

            pdf_dir = run_dir / "pdfs"
            pdf_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = pdf_dir / f"{safe_name(record.doi)}.pdf"
            pdf_path.write_bytes(pdf_bytes)
            text = pdf_extractor.extract_from_bytes(pdf_bytes)
            result.status = "success"
            result.reason = ""
            result.state = "pdf_response_captured"
            result.pdf_url = pdf_url
            result.pdf_path = str(pdf_path)
            result.size_bytes = len(pdf_bytes)
            result.text_length = len(text or "")
            result.verified_match = self._text_matches_record(text, record, fallback_title=result.title)
            (run_dir / f"{safe_name(record.doi)}.json").write_text(
                json.dumps({**asdict(result), "full_text": text}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return result
        except Exception as exc:
            result.reason = f"{type(exc).__name__}: {exc}"
            result.state = "unexpected_error"
            self._write_diagnostic(page, result, run_dir)
            return result
        finally:
            self._hold_after_run(page, result)
            if _owns_page:
                try:
                    page.close()
                except Exception:
                    pass

    def _hold_after_login(self, page: Any, result: DownloadResult) -> None:
        if not self.post_login_hold_sec:
            return
        self._event(result, "post_login_hold", f"{self.post_login_hold_sec}s")
        deadline = time.time() + self.post_login_hold_sec
        while time.time() < deadline:
            if self._is_human_login_page(page):
                self._event(result, "post_login_hold_login_page", getattr(page, "url", ""))
            elif self._looks_logged_out(page):
                self._event(result, "post_login_hold_auth_wall", getattr(page, "url", ""))
            else:
                self._event(result, "post_login_hold_article_access", getattr(page, "url", ""))
            time.sleep(min(5, max(1, int(deadline - time.time()))))

    def _hold_after_run(self, page: Any, result: DownloadResult) -> None:
        if not self.post_run_hold_sec:
            return
        self._event(result, "post_run_hold", f"{self.post_run_hold_sec}s")
        deadline = time.time() + self.post_run_hold_sec
        while time.time() < deadline:
            time.sleep(min(5, max(1, int(deadline - time.time()))))

    def _ensure_login(self, page: Any, result: DownloadResult) -> bool:
        page.goto(result.article_url, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(3)
        if not self._wait_for_challenge(page, result):
            return False
        self._dismiss_cookie_banners(page, result)
        if not self._looks_logged_out(page):
            return True

        return self._complete_login_from_current_page(page, result)

    def _complete_login_from_current_page(self, page: Any, result: DownloadResult) -> bool:
        result.state = "sso_required"
        self._event(result, "sso_start", page.url)
        self._dismiss_cookie_banners(page, result)
        self._click_sso_entry(page, result)
        time.sleep(5)
        self._dismiss_cookie_banners(page, result)
        self._click_openathens_entry(page, result)
        time.sleep(2)
        self._select_institution(page, result)

        deadline = time.time() + self.login_timeout_sec
        last_state = ""
        last_auto_action = ""
        last_auto_action_at = 0.0
        while time.time() < deadline:
            time.sleep(3)
            marker = f"{self._title(page)} | {getattr(page, 'url', '')[:160]}"
            if marker != last_state:
                self._event(result, "login_state", marker)
                last_state = marker
            if self._is_human_login_page(page):
                continue
            self._dismiss_cookie_banners(page, result)
            self._click_optional_continue(page, result)
            if self._is_challenge_page(page):
                if self._wait_for_challenge(page, result, deadline=deadline):
                    continue
                self._event(result, "challenge_or_viewer_timeout", self._body_text(page, 500))
                return False
            if self._return_to_record_article_if_needed(page, result, result.doi):
                if not self._looks_logged_out(page):
                    return True
                continue
            if self._article_access_available(page):
                return True
            if self._has_publisher_institution_session(page) and not self._is_success_article_url(getattr(page, "url", "")):
                try:
                    page.goto(result.article_url, wait_until="domcontentloaded", timeout=60_000)
                    self._event(result, "institution_session_return_article", result.article_url)
                    time.sleep(3)
                    self._dismiss_cookie_banners(page, result)
                    if not self._looks_logged_out(page):
                        return True
                    continue
                except Exception as exc:
                    self._event(result, "institution_session_return_error", f"{type(exc).__name__}: {exc}")
            block_reason = self._login_block_reason(page)
            if block_reason:
                self._event(result, block_reason, self._body_text(page, 500))
                return False
            if marker != last_auto_action or time.time() - last_auto_action_at > 10:
                if self._select_institution(page, result):
                    time.sleep(2)
                    last_auto_action = marker
                    last_auto_action_at = time.time()
                    continue
                if self._looks_logged_out(page) and self._click_sso_entry(page, result):
                    time.sleep(2)
                    self._select_institution(page, result)
                    last_auto_action = marker
                    last_auto_action_at = time.time()
                    continue
                if self._click_openathens_entry(page, result):
                    time.sleep(2)
                    self._select_institution(page, result)
                    last_auto_action = marker
                    last_auto_action_at = time.time()
                    continue
                self._select_institution(page, result)
                last_auto_action = marker
                last_auto_action_at = time.time()
            if self._article_access_available(page):
                return True
        if self._has_publisher_institution_session(page):
            try:
                page.goto(result.article_url, wait_until="domcontentloaded", timeout=60_000)
                self._event(result, "institution_session_return_article", result.article_url)
                time.sleep(3)
                self._dismiss_cookie_banners(page, result)
                return not self._looks_logged_out(page)
            except Exception as exc:
                self._event(result, "institution_session_return_error", f"{type(exc).__name__}: {exc}")
        return False

    def _login_block_reason(self, page: Any) -> str:
        current_url = str(getattr(page, "url", "") or "").lower()
        haystack = f"{self._title(page)} {self._body_text(page, 2_000)}".lower()
        if self._is_challenge_page(page):
            return "challenge_or_viewer_timeout"
        if (
            "are you a robot" in haystack
            or "verify you are human" in haystack
            or "checking your browser" in haystack
            or "正在进行安全验证" in haystack
            or ("ray id:" in haystack and "cloudflare" in haystack)
        ):
            return "challenge_or_viewer_timeout"
        if self.profile.name.lower() == "world scientific" and "worldscientific.com/action/ssostart" in current_url:
            if "find your institution" in haystack or "type the name of your institution" in haystack:
                return ""
            return "sso_redirect_stalled"
        if (
            "ieee xplore is temporarily unavailable" in haystack
            or ("temporarily unavailable" in haystack and "onlinesupport@ieee.org" in haystack)
            ):
            return "publisher_temporarily_unavailable"
        if self._elsevier_has_tsinghua_access_entry(haystack):
            return ""
        if self._elsevier_lacks_pdf_entitlement(haystack):
            return "institution_pdf_entitlement_missing"
        if "unsupported request" in haystack and "not registered for use with this service" in haystack:
            return "institution_not_registered"
        if "application you have accessed is not registered" in haystack:
            return "institution_not_registered"
        if "your institution may not be enabled for this type of authentication" in haystack:
            return "institution_not_registered"
        if "can't find your institution" in haystack and "institution sign in" in haystack:
            return "institution_not_registered"
        return ""

    def _click_sso_entry(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self._is_human_login_page(page):
            return False
        if self._open_aps_article_institution_login(page, result):
            return True
        if self.profile.name.lower() == "aps":
            if result is not None:
                self._event(result, "aps_institution_entry_missing", getattr(page, "url", ""))
            return False
        if self._click_iop_access_wall(page, result):
            return True
        if self._click_elsevier_institution_entry(page, result):
            return True
        sso_selectors = (
            "button:has-text('Access Through Your Institution')",
            "button:has-text('Access through your institution')",
            "a:has-text('Access Through Your Institution')",
            "a:has-text('Access through your institution')",
            "[role='button']:has-text('Access Through Your Institution')",
            "[role='button']:has-text('Access through your institution')",
            "button:has-text('Institutional Access')",
            "a:has-text('Institutional Access')",
            "button:has-text('Institutional Sign In')",
        )
        for selector in sso_selectors:
            try:
                control = page.locator(selector).first
                if not control.is_visible(timeout=1_500):
                    continue
                text = ""
                href = ""
                try:
                    text = control.inner_text(timeout=1_000)
                except Exception:
                    pass
                try:
                    href = control.get_attribute("href", timeout=1_000) or ""
                except Exception:
                    pass
                detail = {"selector": selector, "text": text[:200], "href": href[:300]}
                try:
                    control.click(timeout=10_000, no_wait_after=True)
                except Exception:
                    control.click(timeout=10_000, no_wait_after=True, force=True)
                if result is not None:
                    self._event(result, "sso_entry_clicked", json.dumps(detail, ensure_ascii=False))
                return True
            except Exception:
                continue
        if self._click_ieee_institution_entry(page, result):
            return True
        markers = [marker.lower() for marker in self.profile.sso_text_markers]
        try:
            clicked = page.evaluate(
                """
                (markers) => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const candidates = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"],div,span')]
                    .filter(visible);
                  const exactMarkers = [
                    'access through your institution',
                    'access through institution',
                    'access through your organization',
                    'institutional login',
                    'log in through your institution',
                    'log in via your institution',
                    'provided by your institution',
                    'username/password provided by your institution',
                    'log in with username/password provided by your institution'
                  ];
                  const clickableSelector = 'a,button,[role="button"],input[type="button"],input[type="submit"]';
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const detailOf = (el) => ({
                    text: textOf(el).slice(0, 180),
                    href: ((el.href || el.getAttribute('href') || '') + '').toLowerCase().slice(0, 240)
                  });
                  const matches = (el) => {
                    const text = textOf(el);
                    const href = ((el.href || el.getAttribute('href') || el.getAttribute('formaction') || '') + '').toLowerCase();
                    return exactMarkers.some(marker => text.includes(marker) || href.includes(marker))
                        || markers.some(marker => text.includes(marker) || href.includes(marker))
                        || href.includes('ssostart');
                  };
                  const clickMatched = (el) => {
                    const target = el.matches(clickableSelector)
                      ? el
                      : (el.closest(clickableSelector) || el.querySelector(clickableSelector));
                    if (!target) return null;
                    const detail = detailOf(el);
                    target.click();
                    return detail;
                  };
                  for (const el of candidates.filter((candidate) => candidate.matches(clickableSelector))) {
                    const matched = matches(el);
                    if (matched) {
                      const detail = clickMatched(el);
                      if (detail) return detail;
                    }
                  }
                  for (const el of candidates) {
                    const matched = matches(el);
                    if (matched) {
                      const detail = clickMatched(el);
                      if (detail) return detail;
                    }
                  }
                  return null;
                }
                """,
                markers,
            )
            if clicked:
                if result is not None:
                    self._event(result, "sso_entry_clicked", json.dumps(clicked, ensure_ascii=False))
                return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
            return False
        return False

    def _click_wiley_institution_login_entry(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "wiley":
            return False
        try:
            detail = page.evaluate(
                """
                () => {
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const hrefOf = (el) => {
                    const raw = el.href || el.getAttribute('href') || el.getAttribute('formaction') || '';
                    if (!raw) return '';
                    try { return new URL(raw, location.href).href; } catch { return raw; }
                  };
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                      const href = hrefOf(el);
                      const haystack = `${text} ${href} ${el.className || ''} ${el.id || ''}`.toLowerCase();
                      const exact = /^institutional login$/i.test(text);
                      const institutionLogin = exact || haystack.includes('institutional login');
                      if (!institutionLogin) return null;
                      if (haystack.includes('personal') || haystack.includes('account') || haystack.includes('register') || haystack.includes('login / register')) return null;
                      const rect = el.getBoundingClientRect();
                      return {el, text, href, rect, score: (exact ? 100 : 20) - Math.max(0, rect.top / 1000)};
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target) return null;
                  target.el.scrollIntoView({block: 'center', inline: 'center'});
                  target.el.click();
                  return {
                    selector: 'wiley-institutional-login',
                    text: target.text.slice(0, 200),
                    href: target.href.slice(0, 500),
                    score: Math.round(target.score)
                  };
                }
                """
            )
            if isinstance(detail, dict) and detail.get("text"):
                if result is not None:
                    self._event(result, "sso_entry_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_elsevier_institution_entry(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "elsevier":
            return False
        if self._is_human_login_page(page):
            return False
        try:
            detail = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim();
                  const hrefOf = (el) => ((el.href || el.getAttribute('href') || el.getAttribute('formaction') || '') + '');
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const text = textOf(el);
                      const href = hrefOf(el);
                      const haystack = `${text} ${href}`.toLowerCase();
                      let score = 0;
                      let matched = false;
                      if (href.toLowerCase().includes('auth.elsevier.com/shibauth/institutionlogin')) { score += 100; matched = true; }
                      if (haystack.includes('access through tsinghua university')) { score += 80; matched = true; }
                      if (haystack.includes('access through your organization')) { score += 40; matched = true; }
                      if (haystack.includes('access through another organization')) score -= 60;
                      if (haystack.includes('purchase pdf')) score -= 80;
                      if (haystack.includes('go to elsevier homepage') || href.toLowerCase().replace(/\\/$/, '') === 'http://www.elsevier.com') return null;
                      if (!matched) return null;
                      if (el.tagName === 'A') score += 10;
                      return {el, text, href, score};
                    })
                    .filter(Boolean)
                    .filter((item) => item.score > 0)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target) return null;
                  target.el.scrollIntoView({block: 'center', inline: 'center'});
                  target.el.click();
                  return {
                    selector: 'elsevier-institution-access',
                    text: target.text.slice(0, 220),
                    href: target.href.slice(0, 500),
                    score: target.score
                  };
                }
                """
            )
            if isinstance(detail, dict) and detail:
                if result is not None:
                    self._event(result, "sso_entry_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            if result is not None:
                self._event(result, "elsevier_institution_entry_error", f"{type(exc).__name__}: {exc}")
        return False

    def _is_human_login_page(self, page: Any) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        host = (urlparse(current_url).netloc or "").lower()
        return host.endswith("id.tsinghua.edu.cn") or host.endswith("idp.tsinghua.edu.cn")

    def _open_aps_article_institution_login(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "aps":
            return False
        current_url = str(getattr(page, "url", "") or "").lower()
        if "/login_inst_user" in current_url:
            return False
        try:
            detail = page.evaluate(
                """
                () => {
                  const root = document.querySelector('#inline-unauthorized-page') || document.body;
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const link = [...root.querySelectorAll('a')].find((el) => {
                    const href = (el.href || el.getAttribute('href') || '').toLowerCase();
                    const text = norm(el.innerText || el.textContent || '').toLowerCase();
                    return href.includes('/login_inst_user')
                      || text.includes('username/password provided by your institution');
                  });
                  if (!link) return null;
                  return {
                    text: norm(link.innerText || link.textContent || '').slice(0, 200),
                    href: link.href || link.getAttribute('href') || ''
                  };
                }
                """
            )
            if not isinstance(detail, dict) or not detail.get("href"):
                return False
            page.goto(str(detail["href"]), wait_until="domcontentloaded", timeout=30_000)
            if result is not None:
                self._event(result, "sso_entry_clicked", json.dumps(detail, ensure_ascii=False))
            return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
            return False

    def _click_ieee_institution_entry(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "ieee":
            return False
        current_url = str(getattr(page, "url", "") or "")
        if "ieeexplore.ieee.org/servlet/wayf" in current_url.lower():
            return False
        if "ieeexplore.ieee.org" not in current_url.lower():
            return False
        text = self._body_text(page, 5_000).lower()
        ieee_entry_markers = (
            "you do not have access to this pdf",
            "sign in to continue reading",
            "institutional sign in",
            "access through your institution",
            "search for your institution",
        )
        if not any(marker in text for marker in ieee_entry_markers):
            return False
        try:
            clicked = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim();
                  const controls = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible);
                  const wanted = [
                    'access through your institution',
                    'institutional sign in',
                    'institutional login'
                  ];
                  for (const marker of wanted) {
                    const target = controls.find((el) => textOf(el).toLowerCase().includes(marker));
                    if (!target) continue;
                    const detail = {
                      text: textOf(target).slice(0, 200),
                      href: (target.href || target.getAttribute('href') || '').slice(0, 300)
                    };
                    target.click();
                    return detail;
                  }
                  return null;
                }
                """
            )
            if not clicked:
                return False
            if result is not None:
                self._event(result, "sso_entry_clicked", json.dumps(clicked, ensure_ascii=False))
            return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
        return False

    def _dismiss_cookie_banners(self, page: Any, result: DownloadResult | None = None) -> bool:
        try:
            clicked = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim().toLowerCase();
                  const controls = [...document.querySelectorAll('button,a,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible);
                  const patterns = [
                    'accept all',
                    'accept cookies',
                    'accept',
                    'agree',
                    'i agree',
                    'got it',
                    'close',
                    '×'
                  ];
                  const exactOnly = ['accept', 'close'];
                  const target = controls.find((el) => {
                    const text = textOf(el);
                    if (!text) return false;
                    return patterns.some((pattern) => text === pattern || (!exactOnly.includes(pattern) && text.includes(pattern)));
                  });
                  if (!target) return null;
                  const detail = textOf(target).slice(0, 120);
                  target.click();
                  return detail;
                }
                """
            )
            if clicked:
                if result is not None:
                    self._event(result, "cookie_banner_dismissed", str(clicked))
                return True
        except Exception:
            return False
        return False

    def _click_iop_access_wall(self, page: Any, result: DownloadResult | None = None) -> bool:
        if self.profile.name.lower() != "iop":
            return False
        current_url = str(getattr(page, "url", "") or "")
        if "iopscience.iop.org/article/" not in current_url.lower():
            return False
        text = self._body_text(page, 5_000).lower()
        if "access this article" not in text and "access through your institution" not in text:
            return False
        signin_url = "https://myiopscience.iop.org/signin?" + urlencode(
            {"origin": "deeplink", "target": current_url}
        )
        try:
            page.goto(signin_url, wait_until="domcontentloaded", timeout=30_000)
            if result is not None:
                self._event(result, "sso_entry_clicked", signin_url)
            return True
        except Exception as exc:
            if result is not None:
                self._event(result, "sso_entry_error", f"{type(exc).__name__}: {exc}")
            return False

    def _click_openathens_entry(self, page: Any, result: DownloadResult) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        lower_url = current_url.lower()
        parsed = urlparse(current_url)
        host = (parsed.netloc or "").lower()
        if (
            host.endswith("openathens.net")
            or host.endswith("tsinghua.edu.cn")
            or "id.tsinghua.edu.cn" in lower_url
            or "idp.tsinghua.edu.cn" in lower_url
        ):
            return False
        if parsed.netloc.endswith("annualreviews.org") and parsed.path.lower().startswith("/session/ext/shib"):
            target_url = parse_qs(parsed.query).get("url", ["/"])[0] or "/"
            openathens_url = f"{parsed.scheme or 'https'}://{parsed.netloc}/session/ext/athens?{urlencode({'url': target_url, 'athensWayfSearch': self.institution_query})}"
            try:
                page.goto(openathens_url, wait_until="domcontentloaded", timeout=30_000)
                self._event(result, "openathens_entry", openathens_url)
                return True
            except Exception as exc:
                self._event(result, "openathens_entry_error", f"{type(exc).__name__}: {exc}")
                return False

        if parsed.netloc.endswith("annualreviews.org") and parsed.path.lower().startswith("/session/ext/athens"):
            return False

        try:
            clicked = page.evaluate(
                """
                () => {
                  const candidates = [...document.querySelectorAll('a,button,input[type="button"],input[type="submit"]')];
                  for (const el of candidates) {
                    const text = [
                      el.innerText || '',
                      el.textContent || '',
                      el.value || '',
                      el.getAttribute('aria-label') || '',
                      el.getAttribute('title') || ''
                    ].join(' ').toLowerCase();
                    const href = ((el.href || el.getAttribute('formaction') || '') + '').toLowerCase();
                    if ((text.includes('openathens') || href.includes('openathens'))
                        && !text.includes('shibboleth')) {
                      el.click();
                      return {text: text.slice(0, 160), href: href.slice(0, 240)};
                    }
                  }
                  return null;
                }
                """
            )
            if clicked:
                self._event(result, "openathens_entry", json.dumps(clicked, ensure_ascii=False))
                return True
        except Exception:
            return False
        return False

    def _select_institution(self, page: Any, result: DownloadResult) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        host = (urlparse(current_url).netloc or "").lower()
        if host.endswith("tsinghua.edu.cn"):
            return False
        if not self.institution_query:
            self._event(result, "institution_required", "No subscription institution was configured for publisher login.")
            return False
        if self._select_openathens_wayfinder(page, result):
            return True
        if self._select_annual_reviews_openathens(page, result):
            return True
        if self._select_ieee_institution(page, result):
            return True
        for selector in self.profile.institution_input_selectors:
            try:
                inp = page.locator(selector).first
                if inp.is_visible(timeout=5_000):
                    inp.fill(self.institution_query)
                    self._event(result, "institution_search", selector)
                    time.sleep(2)
                    if self._click_institution_search_result(page, result):
                        return True
                    try:
                        inp.press("Enter", timeout=3_000)
                        self._event(result, "institution_search_submitted", selector)
                        time.sleep(3)
                        if self._click_institution_search_result(page, result):
                            return True
                        self._click_optional_continue(page, result)
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def _select_ieee_institution(self, page: Any, result: DownloadResult) -> bool:
        if self.profile.name.lower() != "ieee":
            return False
        input_selectors = (
            "input[aria-label='Search for your Institution']",
            "input[aria-label*='Institution']",
            "input.inst-typeahead-input",
            "xpath=(//*[normalize-space()='Search for your Institution']/following::input[1])",
        )
        input_box = None
        input_selector = ""
        for selector in input_selectors:
            try:
                candidate = page.locator(selector).first
                if candidate.is_visible(timeout=1_500):
                    input_box = candidate
                    input_selector = selector
                    break
            except Exception:
                continue
        if input_box is None:
            return False
        try:
            input_box.fill("")
            try:
                input_box.type(self.institution_query, timeout=5_000)
            except Exception:
                input_box.fill(self.institution_query)
            self._event(result, "institution_search", input_selector)
            time.sleep(2)
        except Exception as exc:
            self._event(result, "institution_select_error", f"{type(exc).__name__}: {exc}")
            return False

        result_selectors = list(self._institution_result_selectors())
        if self._institution_query_is_tsinghua():
            result_selectors.extend(
                (
                    "text=Tsinghua University(OpenAthens)",
                    "text=Tsinghua University (OpenAthens)",
                    "text=Tsinghua University",
                    "[role='option']:has-text('Tsinghua University')",
                    "li:has-text('Tsinghua University')",
                    "div:has-text('Tsinghua University(OpenAthens)')",
                )
            )
        for selector in result_selectors:
            try:
                option = page.locator(selector).first
                if option.is_visible(timeout=2_000):
                    option.click(timeout=5_000, no_wait_after=True)
                    self._event(result, "institution_selected", selector)
                    return True
            except Exception:
                continue
        try:
            input_box.press("ArrowDown", timeout=2_000)
            input_box.press("Enter", timeout=2_000)
            self._event(result, "institution_selected", "IEEE typeahead ArrowDown+Enter")
            return True
        except Exception as exc:
            self._event(result, "institution_select_error", f"{type(exc).__name__}: {exc}")
            return False

    def _click_institution_search_result(self, page: Any, result: DownloadResult) -> bool:
        for result_selector in self._institution_result_selectors():
            try:
                page.locator(result_selector).first.click(timeout=5_000)
                self._event(result, "institution_selected", result_selector)
                self._click_optional_continue(page, result)
                return True
            except Exception:
                continue
        try:
            clicked = page.evaluate(
                """
                (query) => {
                  const needle = (query || '').toLowerCase();
                  if (!needle) return null;
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim();
                  const candidates = [...document.querySelectorAll('a,button,[role="button"],[role="option"],li,div,span')]
                    .filter(visible)
                    .filter((el) => {
                      const text = textOf(el).toLowerCase();
                      return text.includes(needle) && text.length < 300;
                    });
                  const target = candidates.find((el) => textOf(el).toLowerCase().includes(needle));
                  if (!target) return null;
                  const clickable = target.closest('a,button,[role="button"],[role="option"],li') || target;
                  const detail = textOf(clickable).slice(0, 160);
                  clickable.click();
                  return detail;
                }
                """,
                self.institution_query,
            )
            if clicked:
                self._event(result, "institution_selected", str(clicked))
                self._click_optional_continue(page, result)
                return True
        except Exception:
            return False
        return False

    def _institution_query_is_tsinghua(self) -> bool:
        query = self.institution_query.lower()
        return "tsinghua" in query or "qinghua" in query or "清华" in self.institution_query

    def _institution_result_selectors(self) -> tuple[str, ...]:
        query = self.institution_query.strip()
        if not query:
            return ()
        literal = query.replace("\\", "\\\\").replace("'", "\\'")
        selectors = [
            f"text={query}",
            f"button:has-text('{literal}')",
            f"a:has-text('{literal}')",
            f"[role='button']:has-text('{literal}')",
            f"[role='option']:has-text('{literal}')",
            f"li:has-text('{literal}')",
            f"div:has-text('{literal}')",
        ]
        if self._institution_query_is_tsinghua():
            selectors.extend(self.profile.institution_result_selectors)
        return tuple(dict.fromkeys(selectors))

    def _select_openathens_wayfinder(self, page: Any, result: DownloadResult) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        parsed = urlparse(current_url)
        host = (parsed.netloc or "").lower()
        if not host.endswith("wayfinder.openathens.net"):
            return False
        if not self._institution_query_is_tsinghua():
            return False
        return_url = parse_qs(parsed.query).get("return", [""])[0]
        if not return_url.startswith("https://connect.openathens.net/"):
            return False
        sep = "&" if "?" in return_url else "?"
        direct_url = return_url + sep + urlencode(
            {
                "entityID": "https://idp.tsinghua.edu.cn/idp/shibboleth",
                "target": current_url,
            }
        )
        try:
            page.goto(direct_url, wait_until="domcontentloaded", timeout=30_000)
            self._event(result, "institution_selected", "OpenAthens entityID: Tsinghua")
            return True
        except Exception as exc:
            self._event(result, "institution_select_error", f"{type(exc).__name__}: {exc}")
            return False

    def _select_annual_reviews_openathens(self, page: Any, result: DownloadResult) -> bool:
        current_url = str(getattr(page, "url", "") or "").lower()
        if "annualreviews.org/session/ext/athens" not in current_url:
            return False
        if self._institution_query_is_tsinghua():
            try:
                tsinghua = page.locator("text=Tsinghua University (OpenAthens)").last
                if tsinghua.is_visible(timeout=1_500):
                    tsinghua.click(timeout=5_000, no_wait_after=True)
                    page.locator("text=Go To Sign-in").last.click(timeout=5_000, no_wait_after=True)
                    self._event(result, "institution_selected", "Tsinghua University (OpenAthens)")
                    return True
            except Exception:
                pass
        try:
            input_box = page.locator(
                "xpath=(//*[contains(normalize-space(.), 'Option 2: Sign-in with OpenAthens')]/following::input[contains(@placeholder, 'organization')])[1]"
            ).first
            if input_box.is_visible(timeout=1_500):
                input_box.fill(self.institution_query)
                page.locator("text=Find Your Organization").last.click(timeout=5_000, no_wait_after=True)
                self._event(result, "institution_search", f"OpenAthens: {self.institution_query}")
                time.sleep(2)
                if self._institution_query_is_tsinghua():
                    try:
                        tsinghua = page.locator("text=Tsinghua University (OpenAthens)").last
                        if tsinghua.is_visible(timeout=3_000):
                            tsinghua.click(timeout=5_000, no_wait_after=True)
                            page.locator("text=Go To Sign-in").last.click(timeout=5_000, no_wait_after=True)
                            self._event(result, "institution_selected", "Tsinghua University (OpenAthens)")
                    except Exception:
                        pass
                return True
        except Exception:
            pass
        try:
            action = page.evaluate(
                """
                (query) => {
                  const needle = (query || '').toLowerCase();
                  if (!needle) return null;
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || ''
                  ].join(' ').trim();
                  const controls = [...document.querySelectorAll('a,button,input[type="button"],input[type="submit"]')].filter(visible);
                  const goButtons = controls.filter((el) => textOf(el).toLowerCase().includes('go to sign-in'));
                  const resultEls = [...document.querySelectorAll('a,button,li,div,span,option')]
                    .filter(visible)
                    .filter((el) => textOf(el).toLowerCase().includes(needle));
                  if (resultEls.length) {
                    resultEls[resultEls.length - 1].click();
                    if (goButtons.length) {
                      goButtons[goButtons.length - 1].click();
                      return {action: 'openathens_go', result: textOf(resultEls[resultEls.length - 1]).slice(0, 160)};
                    }
                    return {action: 'openathens_result', result: textOf(resultEls[resultEls.length - 1]).slice(0, 160)};
                  }
                  const inputs = [...document.querySelectorAll('input')]
                    .filter(visible)
                    .filter((el) => {
                      const haystack = [el.placeholder || '', el.name || '', el.id || '', el.getAttribute('aria-label') || ''].join(' ').toLowerCase();
                      return haystack.includes('organization') || haystack.includes('institution');
                    });
                  if (inputs.length) {
                    const input = inputs[inputs.length - 1];
                    input.focus();
                    input.value = query;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    const findButtons = controls.filter((el) => textOf(el).toLowerCase().includes('find your organization'));
                    if (findButtons.length) {
                      findButtons[findButtons.length - 1].click();
                    }
                    return {action: 'openathens_search', query};
                  }
                  if (goButtons.length) {
                    goButtons[goButtons.length - 1].click();
                    return {action: 'openathens_go'};
                  }
                  return null;
                }
                """,
                self.institution_query,
            )
            if action:
                self._event(result, "institution_selected", json.dumps(action, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "institution_select_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_optional_continue(self, page: Any, result: DownloadResult) -> None:
        selectors = (
            "button:has-text('提交并继续')",
            "button:has-text('继续')",
            "button:has-text('Submit and continue')",
            "button:has-text('Continue')",
            "button:has-text('Yes')",
            "[role='button']:has-text('Yes')",
            "a:has-text('Yes')",
            "input[value='Yes']",
            "input[type='submit']",
        )
        for selector in selectors:
            try:
                button = page.locator(selector).first
                if button.is_visible(timeout=1_500):
                    try:
                        text = (button.inner_text(timeout=1_000) or "").strip().lower()
                    except Exception:
                        text = ""
                    if "continue reading" in text:
                        continue
                    button.click(timeout=5_000, no_wait_after=True)
                    self._event(result, "institution_continue", selector)
                    return
            except Exception:
                continue

    def _capture_pdf(self, page: Any, doi: str, result: DownloadResult) -> tuple[bytes | None, str, str | None]:
        """Capture the PDF for ``doi`` from ``page``.

        Returns ``(body, url, block_reason)`` where ``block_reason`` is
        ``"ip_blocked"`` if a publisher IP-block signal (403/429 or block page)
        was observed during capture, else ``None``.
        """
        captured: dict[str, Any] = {"bytes": None, "url": "", "deferred_url": "", "block_reason": None}

        def on_response(response: Any) -> None:
            if captured["bytes"]:
                return
            try:
                url = response.url
                status = getattr(response, "status", None)
                # Detect a publisher IP-block response before anything else:
                # a 403/429 on a PDF/article endpoint means our IP is blocked,
                # and continuing to churn只会加深封锁。
                if status in _IP_BLOCK_STATUS_CODES and not captured["block_reason"]:
                    try:
                        snippet = response.text()[:8192] if hasattr(response, "text") else response.body()
                    except Exception:
                        snippet = ""
                    if self._is_ip_block_response(status, snippet):
                        captured["block_reason"] = "ip_blocked"
                        self._event(result, "ip_blocked_response", f"{status} {url}")
                content_type = (response.headers.get("content-type") or "").lower()
                if self._is_supplementary_url(url):
                    return
                if "pdf" not in content_type and not self._is_pdf_candidate_url(url):
                    return
                if self._should_defer_response_body(url):
                    captured["deferred_url"] = url
                    return
                body = response.body()
                # Body-level block-page detection (some block pages are 200
                # with an HTML "IP Address Blocked" body).
                if not body[:5] == b"%PDF-" and not captured["block_reason"]:
                    if self._is_ip_block_response(None, body):
                        captured["block_reason"] = "ip_blocked"
                        self._event(result, "ip_blocked_body", url)
                if body[:5] == b"%PDF-" and len(body) > MIN_PDF_BYTES:
                    captured["bytes"] = body
                    captured["url"] = url
            except Exception:
                return

        page.on("response", on_response)
        try:
            self._return_to_record_article_if_needed(page, result, doi)
            if self._click_pdf_entry(page, result, doi=doi):
                time.sleep(5)
                if not captured["bytes"]:
                    body, final_url, block_reason = self._fetch_page_state_pdf(page)
                    if body:
                        captured["bytes"] = body
                        captured["url"] = final_url
                    if block_reason and not captured["block_reason"]:
                        captured["block_reason"] = block_reason
            for pdf_url in self._pdf_candidates(page, doi):
                if captured["bytes"]:
                    break
                self._event(result, "pdf_candidate", pdf_url)
                if self._should_use_async_pdf_navigation(pdf_url):
                    body, final_url = self._capture_pdf_via_async_navigation(page, pdf_url, result)
                    if body:
                        captured["bytes"] = body
                        captured["url"] = final_url
                        break
                    continue
                try:
                    response = page.goto(pdf_url, wait_until="commit", timeout=self.pdf_timeout_ms)
                    if response is not None and not captured["bytes"]:
                        response_url = str(getattr(response, "url", "") or "")
                        if self._should_defer_response_body(response_url):
                            captured["deferred_url"] = response_url
                        else:
                            body = response.body()
                            if body[:5] == b"%PDF-" and len(body) > MIN_PDF_BYTES:
                                captured["bytes"] = body
                                captured["url"] = response.url
                    if not captured["bytes"]:
                        body, final_url, block_reason = self._fetch_page_state_pdf(page, response, [str(captured["deferred_url"])])
                        if body:
                            captured["bytes"] = body
                            captured["url"] = final_url
                        if block_reason and not captured["block_reason"]:
                            captured["block_reason"] = block_reason
                except Exception as exc:
                    self._event(result, "pdf_navigation_error", f"{type(exc).__name__}: {exc}")
                    if self._is_download_navigation_abort(exc):
                        body, final_url = self._capture_browser_download(page, pdf_url, result)
                        if body:
                            captured["bytes"] = body
                            captured["url"] = final_url
                self._wait_for_challenge(page, result)
                time.sleep(3)
                if not captured["bytes"]:
                    body, final_url, block_reason = self._fetch_page_state_pdf(page, extra_urls=[str(captured["deferred_url"])])
                    if body:
                        captured["bytes"] = body
                        captured["url"] = final_url
                        break
                    if block_reason and not captured["block_reason"]:
                        captured["block_reason"] = block_reason
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
        return captured["bytes"], str(captured["url"]), captured["block_reason"]

    def _click_pdf_entry(self, page: Any, result: DownloadResult, *, doi: str = "") -> bool:
        if self.profile.name.lower() == "elsevier":
            if self._click_elsevier_view_pdf_entry(page, result):
                return True
        if doi and self.profile.name.lower() == "aps":
            if self._click_current_doi_pdf_entry(page, result, doi):
                return True

        selectors = (
            "a.xpl-btn-pdf",
            ".xpl-btn-pdf",
            "a.stats-document-lh-action-downloadPdf_3",
            "[title*='PDF']",
            "[aria-label*='PDF']",
            "button:has-text('PDF')",
            "a:has-text('PDF')",
        )
        for selector in selectors:
            try:
                control = page.locator(selector).first
                if not control.is_visible(timeout=1_500):
                    continue
                text = ""
                href = ""
                try:
                    text = control.inner_text(timeout=1_000)
                except Exception:
                    pass
                try:
                    href = control.get_attribute("href", timeout=1_000) or ""
                except Exception:
                    pass
                detail = {"selector": selector, "text": text[:200], "href": href[:300]}
                control.click(timeout=10_000, no_wait_after=True)
                self._event(result, "pdf_button_clicked", json.dumps(detail, ensure_ascii=False))
                return True
            except Exception:
                continue

        try:
            clicked = page.evaluate(
                """
                () => {
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const textOf = (el) => [
                    el.innerText || '',
                    el.textContent || '',
                    el.value || '',
                    el.getAttribute('aria-label') || '',
                    el.getAttribute('title') || '',
                    el.className || '',
                    el.id || ''
                  ].join(' ').replace(/\\s+/g, ' ').trim();
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible);
                  const isPdfControl = (el) => {
                    const haystack = textOf(el).toLowerCase();
                    const href = ((el.href || el.getAttribute('href') || el.getAttribute('formaction') || '') + '').toLowerCase();
                    if (haystack.includes('reference') || haystack.includes('citation')) return false;
                    if (haystack.includes('download references')) return false;
                    return /(^|\\s)pdf(\\s|$)/i.test(textOf(el))
                      || haystack.includes('download pdf')
                      || haystack.includes('xpl-btn-pdf')
                      || href.includes('/pdf')
                      || href.includes('stamppdf')
                      || href.includes('/stamp/');
                  };
                  const target = controls.find(isPdfControl);
                  if (!target) return null;
                  const detail = {
                    text: textOf(target).slice(0, 200),
                    href: ((target.href || target.getAttribute('href') || target.getAttribute('formaction') || '') + '').slice(0, 300)
                  };
                  target.click();
                  return detail;
                }
                """
            )
            if isinstance(clicked, (dict, str)) and clicked:
                self._event(result, "pdf_button_clicked", json.dumps(clicked, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "pdf_button_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_elsevier_view_pdf_entry(self, page: Any, result: DownloadResult) -> bool:
        try:
            detail = page.evaluate(
                """
                () => {
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const hrefOf = (el) => {
                    const raw = el.href || el.getAttribute('href') || el.getAttribute('formaction') || '';
                    if (!raw) return '';
                    try { return new URL(raw, location.href).href; } catch { return raw; }
                  };
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const rect = el.getBoundingClientRect();
                      const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                      const href = hrefOf(el);
                      const haystack = `${text} ${href} ${el.className || ''} ${el.id || ''}`.toLowerCase();
                      const isPdf = /\\bview pdf\\b/.test(haystack)
                        || /\\bdownload pdf\\b/.test(haystack)
                        || haystack.includes('/pdfft')
                        || haystack.includes('pdfreader');
                      if (!isPdf) return null;
                      const isRecommendedColumn = rect.left > Math.max(900, window.innerWidth * 0.70);
                      const exactScore = /^view pdf$/i.test(text) ? 120 : 0;
                      const topBarScore = rect.top < 160 ? 60 : 0;
                      const hrefScore = href.toLowerCase().includes('/pdfft') ? 30 : 0;
                      const recommendedPenalty = isRecommendedColumn ? 150 : 0;
                      return {el, text, href, rect, score: exactScore + topBarScore + hrefScore - recommendedPenalty - Math.max(0, rect.top / 1000)};
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target || target.score < 0) return null;
                  const detail = {
                    selector: 'elsevier-view-pdf',
                    text: target.text.slice(0, 200),
                    href: target.href.slice(0, 500),
                    score: Math.round(target.score)
                  };
                  target.el.click();
                  return detail;
                }
                """
            )
            if isinstance(detail, dict) and detail.get("text"):
                self._event(result, "pdf_button_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "pdf_button_error", f"{type(exc).__name__}: {exc}")
        return False

    def _click_current_doi_pdf_entry(self, page: Any, result: DownloadResult, doi: str) -> bool:
        try:
            detail = page.evaluate(
                """
                (doi) => {
                  const norm = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                  };
                  const hrefOf = (el) => {
                    const raw = el.href || el.getAttribute('href') || el.getAttribute('formaction') || '';
                    if (!raw) return '';
                    try { return new URL(raw, location.href).href; } catch { return raw; }
                  };
                  const doiLower = doi.toLowerCase();
                  const doiEscaped = encodeURIComponent(doi).toLowerCase();
                  const controls = [...document.querySelectorAll('a,button,[role="button"],input[type="button"],input[type="submit"]')]
                    .filter(visible)
                    .map((el) => {
                      const rect = el.getBoundingClientRect();
                      const text = norm(el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '');
                      const href = hrefOf(el);
                      const haystack = `${text} ${href} ${el.className || ''} ${el.id || ''}`.toLowerCase();
                      const isCurrentPdf = href.toLowerCase().includes('/pdf/')
                        && (href.toLowerCase().includes(doiLower) || href.toLowerCase().includes(doiEscaped));
                      if (!isCurrentPdf) return null;
                      const textScore = /^pdf$/i.test(text) ? 100 : 0;
                      const referencePenalty = /references|citation/i.test(text) ? 50 : 0;
                      const primaryScore = /primary|button|pdf/i.test(`${el.className || ''} ${el.id || ''}`) ? 20 : 0;
                      return {el, text, href, rect, score: textScore + primaryScore - referencePenalty - Math.max(0, rect.y / 1000)};
                    })
                    .filter(Boolean)
                    .sort((a, b) => b.score - a.score);
                  const target = controls[0];
                  if (!target) return null;
                  return {
                    selector: 'aps-current-doi-pdf',
                    text: target.text.slice(0, 200),
                    href: target.href.slice(0, 300)
                  };
                }
                """,
                doi,
            )
            if isinstance(detail, dict) and detail.get("href"):
                try:
                    page.goto(str(detail["href"]), wait_until="commit", timeout=self.pdf_timeout_ms)
                except Exception as exc:
                    if not self._is_download_navigation_abort(exc):
                        raise
                self._event(result, "pdf_button_clicked", json.dumps(detail, ensure_ascii=False))
                return True
        except Exception as exc:
            self._event(result, "pdf_button_error", f"{type(exc).__name__}: {exc}")
        return False

    def _fetch_page_state_pdf(
        self,
        page: Any,
        response: Any | None = None,
        extra_urls: list[str] | None = None,
    ) -> tuple[bytes | None, str, str | None]:
        """Try to fetch a PDF from URLs visible in the page state.

        Returns ``(body, url, block_reason)`` — ``block_reason`` is propagated
        up from :meth:`_fetch_pdf_url` so an IP block observed on the plain-HTTP
        fallback path is not lost.
        """
        for fallback_url in self._page_state_pdf_urls(page, response, extra_urls):
            if not self._is_pdf_candidate_url(fallback_url) or self._is_supplementary_url(fallback_url):
                continue
            body, final_url, block_reason = self._fetch_pdf_url_with_browser_state(fallback_url, page)
            if body:
                return body, final_url, None
            if block_reason:
                # Keep trying only the URL that produced a real PDF; but remember
                # the block signal so the caller can escalate even if no PDF is
                # ever captured.
                return None, final_url, block_reason
        return None, "", None

    def _page_state_pdf_urls(
        self,
        page: Any,
        response: Any | None = None,
        extra_urls: list[str] | None = None,
    ) -> list[str]:
        raw_values: list[str] = list(extra_urls or [])
        if response is not None:
            raw_values.append(str(getattr(response, "url", "") or ""))
        raw_values.append(str(getattr(page, "url", "") or ""))
        raw_values.append(self._title(page))

        urls: list[str] = []
        for value in raw_values:
            if value.startswith("http"):
                urls.append(value)
            urls.extend(match.group(0).rstrip(").,;") for match in PDF_URL_RE.finditer(value))
        return list(dict.fromkeys(url for url in urls if url.startswith("http")))

    def _should_defer_response_body(self, url: str) -> bool:
        return self.profile.name.lower() == "elsevier" and self._is_pdf_candidate_url(url)

    def _capture_browser_download(self, page: Any, pdf_url: str, result: DownloadResult) -> tuple[bytes | None, str]:
        expect_download = getattr(page, "expect_download", None)
        if expect_download is None:
            return None, pdf_url
        try:
            with expect_download(timeout=self.pdf_timeout_ms) as download_info:
                try:
                    page.goto(pdf_url, wait_until="commit", timeout=self.pdf_timeout_ms)
                except Exception as exc:
                    if not self._is_download_navigation_abort(exc):
                        raise
            download = download_info.value
            path = download.path()
            body = Path(path).read_bytes()
            if body[:5] == b"%PDF-" and len(body) > MIN_PDF_BYTES:
                return body, str(getattr(download, "url", "") or pdf_url)
        except Exception as exc:
            self._event(result, "download_capture_error", f"{type(exc).__name__}: {exc}")
        return None, pdf_url

    def _capture_pdf_via_async_navigation(
        self,
        page: Any,
        pdf_url: str,
        result: DownloadResult,
    ) -> tuple[bytes | None, str]:
        try:
            page.evaluate("(url) => { window.location.href = url; }", pdf_url)
            self._event(result, "pdf_async_navigation", pdf_url)
        except Exception as exc:
            self._event(result, "pdf_async_navigation_error", f"{type(exc).__name__}: {exc}")
            return None, ""

        timeout_sec = max(10, int(self.pdf_timeout_ms / 1000))
        deadline = time.time() + timeout_sec
        attempted: set[str] = set()
        while time.time() < deadline:
            if self._is_challenge_page(page):
                if not self._wait_for_challenge_with_deadline(page, result, deadline):
                    return None, str(getattr(page, "url", "") or "")
                attempted.clear()
                continue
            for candidate_url in self._page_state_pdf_urls(page):
                if candidate_url in attempted:
                    continue
                attempted.add(candidate_url)
                if not self._is_pdf_candidate_url(candidate_url) or self._is_supplementary_url(candidate_url):
                    continue
                self._event(result, "pdf_state_candidate", candidate_url)
                body, final_url = self._fetch_pdf_url_with_browser_state(candidate_url, page)
                if body:
                    return body, final_url
            time.sleep(2)
        return None, ""

    def _should_use_async_pdf_navigation(self, url: str) -> bool:
        return self.profile.name.lower() == "elsevier" and self._is_pdf_candidate_url(url)

    @staticmethod
    def _is_download_navigation_abort(exc: Exception) -> bool:
        message = str(exc)
        return "Download is starting" in message or "net::ERR_ABORTED" in message

    def _fetch_pdf_url_with_browser_state(self, url: str, page: Any) -> tuple[bytes | None, str, str | None]:
        try:
            signature = inspect.signature(self._fetch_pdf_url)
        except (TypeError, ValueError):
            return self._fetch_pdf_url(url)
        accepts_page = "page" in signature.parameters or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
        )
        if accepts_page:
            return self._fetch_pdf_url(url, page=page)
        return self._fetch_pdf_url(url)

    def _wait_for_challenge_with_deadline(
        self,
        page: Any,
        result: DownloadResult,
        deadline: float,
    ) -> bool:
        try:
            signature = inspect.signature(self._wait_for_challenge)
        except (TypeError, ValueError):
            return self._wait_for_challenge(page, result)
        accepts_deadline = "deadline" in signature.parameters or any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values()
        )
        if accepts_deadline:
            return self._wait_for_challenge(page, result, deadline=deadline)
        return self._wait_for_challenge(page, result)

    def _is_ip_block_response(self, status_code: int | None, body_or_text: str | bytes | None) -> bool:
        """Return True if this response looks like a publisher IP block.

        Detects two independent signals so a block is caught whichever path it
        surfaces on:
        - HTTP status 403 (ACS serves its block page with 403) or 429
        - Body text containing ACS's block-page markers, e.g.
          "IP Address Blocked", "your IP address has been blocked", or the
          contact mailbox "ipblock@acs.org"

        ``body_or_text`` may be bytes or str; only a prefix is inspected.
        """
        if status_code is not None and status_code in _IP_BLOCK_STATUS_CODES:
            return True
        if not body_or_text:
            return False
        if isinstance(body_or_text, bytes):
            try:
                body_or_text = body_or_text[:8192].decode("utf-8", errors="ignore")
            except Exception:
                return False
        text = body_or_text[:8192].lower()
        if "ipblock@acs.org" in text:
            return True
        # "ip address" near "blocked" covers "IP Address Blocked" and
        # "your IP address has been blocked automatically".
        if "ip address" in text and "block" in text:
            return True
        return False

    def _fetch_pdf_url(self, url: str, *, page: Any | None = None) -> tuple[bytes | None, str, str | None]:
        """Fetch a PDF URL with the browser's cookies/headers.

        Returns ``(body, final_url, block_reason)``. ``block_reason`` is
        ``"ip_blocked"`` when the publisher returned a 403/429 or an IP-block
        page (so the caller can stop the batch instead of churning); ``None``
        otherwise.
        """
        headers = self._pdf_request_headers(url, page)
        try:
            resp = requests.get(
                url,
                headers=headers,
                timeout=(10, 60),
                allow_redirects=True,
            )
            if self._is_ip_block_response(resp.status_code, resp.text):
                return None, resp.url, "ip_blocked"
            body = resp.content
            if body[:5] == b"%PDF-" and len(body) > MIN_PDF_BYTES:
                return body, resp.url, None
        except Exception:
            return None, url, None
        return None, resp.url, None

    def _pdf_request_headers(self, url: str, page: Any | None = None) -> dict[str, str]:
        headers: dict[str, str] = {"User-Agent": "scansci-pdf/1.5"}
        if page is None:
            return headers

        try:
            user_agent = page.evaluate("() => navigator.userAgent")
            if isinstance(user_agent, str) and user_agent.strip():
                headers["User-Agent"] = user_agent.strip()
        except Exception:
            pass

        referer = str(getattr(page, "url", "") or "")
        if referer.startswith("http"):
            headers["Referer"] = referer

        try:
            context = getattr(page, "context", None)
            cookies = context.cookies(url) if context is not None else []
            cookie_header = "; ".join(
                f"{cookie.get('name')}={cookie.get('value')}"
                for cookie in cookies
                if cookie.get("name") and cookie.get("value") is not None
            )
            if cookie_header:
                headers["Cookie"] = cookie_header
        except Exception:
            pass
        return headers

    def _pdf_candidates(self, page: Any, doi: str) -> list[str]:
        candidates: list[str] = []
        try:
            found = page.evaluate(
                """
                (rules) => {
                  const out = [];
                  const urlMarkers = rules.urlMarkers.map(marker => marker.toLowerCase());
                  const textMarkers = rules.textMarkers.map(marker => marker.toLowerCase());
                  for (const meta of document.querySelectorAll('meta[name="citation_pdf_url"]')) {
                    if (meta.content) out.push(meta.content);
                  }
                  for (const a of document.querySelectorAll('a')) {
                    const href = a.href || '';
                    const lowerHref = href.toLowerCase();
                    const text = (a.textContent || '').toLowerCase();
                    const aria = (a.getAttribute('aria-label') || '').toLowerCase();
                    const title = (a.getAttribute('title') || '').toLowerCase();
                    if (href && (
                      urlMarkers.some(marker => lowerHref.includes(marker)) ||
                      textMarkers.some(marker => (
                        text.includes(marker) || aria.includes(marker) || title.includes(marker)
                      ))
                    )) out.push(href);
                  }
                  return Array.from(new Set(out));
                }
                """,
                {
                    "urlMarkers": list(self.profile.pdf_url_markers),
                    "textMarkers": list(self.profile.pdf_link_text_markers),
                },
            )
            if isinstance(found, list):
                candidates.extend(
                    str(url)
                    for url in found
                    if isinstance(url, str)
                    and url.startswith("http")
                    and self._is_pdf_candidate_url(url)
                    and not self._is_supplementary_url(url)
                )
        except Exception:
            pass

        return build_pdf_candidates(
            self.profile,
            doi,
            source_url=str(getattr(page, "url", "") or ""),
            discovered_urls=candidates,
        )

    def _filter_pdf_candidates_for_current_article(self, urls: list[str], page: Any, doi: str) -> list[str]:
        return build_pdf_candidates(
            self.profile,
            doi,
            source_url=str(getattr(page, "url", "") or ""),
            discovered_urls=urls,
        )

    @staticmethod
    def _extract_elsevier_pii(url: str) -> str:
        return extract_elsevier_pii(url)

    def _is_pdf_candidate_url(self, url: str) -> bool:
        return is_pdf_candidate_url(self.profile, url)

    def _is_supplementary_url(self, url: str) -> bool:
        return is_supplementary_url(self.profile, url)

    def _return_to_record_article_if_needed(self, page: Any, result: DownloadResult, doi: str) -> bool:
        if self.profile.name.lower() != "aps":
            return False
        current_url = str(getattr(page, "url", "") or "")
        if not current_url or self._url_matches_record(current_url, doi):
            return False
        host = (urlparse(current_url).netloc or "").lower()
        if not any(host == domain or host.endswith(f".{domain}") for domain in self.profile.base_domains):
            return False
        try:
            page.goto(result.article_url, wait_until="domcontentloaded", timeout=60_000)
            self._event(result, "record_article_return", result.article_url)
            time.sleep(3)
            self._wait_for_challenge(page, result)
            self._dismiss_cookie_banners(page, result)
            return True
        except Exception as exc:
            self._event(result, "record_article_return_error", f"{type(exc).__name__}: {exc}")
            return False

    @staticmethod
    def _url_matches_record(url: str, doi: str) -> bool:
        lower_url = str(url or "").lower()
        doi_lower = doi.lower()
        return doi_lower in lower_url or doi_lower.replace("/", "%2f") in lower_url

    def _wait_for_challenge(self, page: Any, result: DownloadResult, *, deadline: float | None = None) -> bool:
        wait_interval_sec = 5
        if deadline is None:
            max_checks = max(8, int(max(self.login_timeout_sec, wait_interval_sec) / wait_interval_sec))
        else:
            remaining = max(0.0, deadline - time.time())
            max_checks = max(1, int(max(remaining, wait_interval_sec) / wait_interval_sec))
        waited = False
        for index in range(max_checks):
            if self._is_challenge_page(page):
                if not waited:
                    self._event(result, "challenge_manual_wait", "complete verification in visible browser")
                waited = True
                result.state = "challenge_or_viewer_timeout"
                self._event(result, "challenge_wait", str(index + 1))
                time.sleep(wait_interval_sec)
                continue
            if waited:
                self._event(result, "challenge_resolved", getattr(page, "url", ""))
            return True
        return not self._is_challenge_page(page)

    def _is_challenge_page(self, page: Any) -> bool:
        haystack = f"{self._title(page)} {self._body_text(page, 1_200)}".lower()
        direct_markers = (
            "just a moment",
            "attention required",
            "verify you are human",
            "checking your browser",
            "are you a robot",
            "please confirm you are a human",
        )
        if any(marker in haystack for marker in direct_markers):
            return True
        return "cloudflare" in haystack and (
            "ray id:" in haystack
            or "security verification" in haystack
            or "security service" in haystack
            or "not a robot" in haystack
            or "瀹夊叏楠岃瘉" in haystack
        )

    def _looks_logged_out(self, page: Any) -> bool:
        url = getattr(page, "url", "").lower()
        title = self._title(page).lower()
        text = self._body_text(page, 5_000).lower()
        if any(marker in url for marker in self.profile.auth_url_markers):
            return True
        if any(marker in title for marker in self.profile.auth_url_markers):
            return True
        if any(marker.lower() in title for marker in self.profile.auth_title_markers):
            return True
        if self._elsevier_has_full_text_access(text):
            return False
        if self._elsevier_has_tsinghua_access_entry(text):
            return True
        if self._elsevier_lacks_pdf_entitlement(text):
            return True
        if self._has_publisher_institution_session(page):
            return False
        if self._is_success_article_url(getattr(page, "url", "")):
            hard_wall_markers = (
                "access this article",
                "not registered by an institution",
                "authorization required",
                "provide your credentials",
                "get access",
                "log in via your institution",
                "access through your organization",
                "access through your institution",
                "institutional access",
                "no access",
                "purchase pdf",
                "purchase this article",
                "sign in to continue reading",
                "subscribe to unlock",
                "you do not have access to this pdf",
            )
            return any(marker in text for marker in hard_wall_markers)
        return any(marker in text for marker in self.profile.sso_text_markers)

    def _article_access_available(self, page: Any) -> bool:
        return self._is_success_article_url(getattr(page, "url", "")) and not self._looks_logged_out(page)

    def _elsevier_has_full_text_access(self, text: str) -> bool:
        if self.profile.name.lower() != "elsevier":
            return False
        return "full text access" in text or "view pdf" in text

    def _elsevier_has_tsinghua_access_entry(self, text: str) -> bool:
        if self.profile.name.lower() != "elsevier":
            return False
        return "access through tsinghua university" in text.lower()

    def _elsevier_lacks_pdf_entitlement(self, text: str) -> bool:
        if self.profile.name.lower() != "elsevier":
            return False
        lower = text.lower()
        if "purchase pdf" not in lower or "article preview" not in lower:
            return False
        return (
            "brought to you by" in lower
            or "tsinghua university" in lower
        )

    def _has_publisher_institution_session(self, page: Any) -> bool:
        current_url = str(getattr(page, "url", "") or "")
        host = (urlparse(current_url).netloc or "").lower()
        if self.profile.base_domains and not any(host == domain or host.endswith(f".{domain}") for domain in self.profile.base_domains):
            return False
        text = self._body_text(page, 5_000).lower()
        return (
            "brought to you by" in text
            or "tsinghua university china" in text
            or ("tsinghua university" in text and "institutional access" in text)
        )

    def _is_success_article_url(self, url: str) -> bool:
        lower = url.lower()
        if self.profile.name.lower() == "aps":
            parsed = urlparse(lower)
            host = parsed.netloc
            path = parsed.path
            if host.endswith("link.aps.org") and path.startswith("/doi/"):
                return True
            return host.endswith("journals.aps.org") and ("/abstract/" in path or "/pdf/" in path)
        return any(marker in lower for marker in self.profile.success_url_markers)

    @staticmethod
    def _title(page: Any) -> str:
        try:
            return str(page.title() or "")
        except Exception:
            return ""

    @staticmethod
    def _body_text(page: Any, limit: int = 2_000) -> str:
        try:
            text = page.locator("body").inner_text(timeout=3_000)
        except Exception:
            return ""
        return re.sub(r"\s+", " ", text).strip()[:limit]

    @staticmethod
    def _event(result: DownloadResult | None, state: str, detail: str = "") -> None:
        if result is not None:
            result.events.append({"state": state, "detail": detail[:500]})

    def _write_diagnostic(self, page: Any, result: DownloadResult, run_dir: Path) -> None:
        diag_dir = run_dir / "diagnostics" / safe_name(result.doi)
        diag_dir.mkdir(parents=True, exist_ok=True)
        result.final_url = getattr(page, "url", result.final_url)
        result.title = self._title(page)
        packet = {
            **asdict(result),
            "publisher": self.profile.name,
            "browser_profile_dir": str(Path(self.config.get("chrome_profile_dir", ""))),
            "body_excerpt": self._body_text(page, 2_000),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        packet_path = diag_dir / "diagnostic.json"
        packet_path.write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            page.screenshot(path=str(diag_dir / "screenshot.png"), full_page=True)
        except Exception:
            pass
        result.diagnostic_path = str(packet_path)

    @staticmethod
    def _write_results(path: Path, results: list[DownloadResult]) -> None:
        summary = {
            "count": len(results),
            "success": sum(1 for result in results if result.status == "success"),
            "partial": sum(1 for result in results if result.status == "partial"),
            "failed": sum(1 for result in results if result.status == "failed"),
            "results": [asdict(result) for result in results],
        }
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def _count_results(results: list[DownloadResult]) -> dict[str, int]:
        return {
            "count": len(results),
            "success": sum(1 for result in results if result.status == "success"),
            "failed": sum(1 for result in results if result.status == "failed"),
        }

    @staticmethod
    def _count_verified(results: list[DownloadResult]) -> int:
        return sum(1 for result in results if result.ok and result.verified_match)

    @staticmethod
    def _read_attempted_dois(path: Path) -> set[str]:
        if not path.exists():
            return set()
        attempted: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            doi = str(item.get("doi", "")).strip().lower()
            if doi:
                attempted.add(doi)
        return attempted

    @staticmethod
    def _append_attempt(path: Path | None, result: DownloadResult, phase: str) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        item = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "phase": phase,
            "doi": result.doi,
            "status": result.status,
            "reason": result.reason,
            "state": result.state,
            "verified_match": result.verified_match,
            "size_bytes": result.size_bytes,
            "text_length": result.text_length,
            "pdf_path": result.pdf_path,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _write_complete_artifacts(
        self,
        records: list[PaperRecord],
        results: list[DownloadResult],
        run_dir: Path,
        *,
        missing_reasons: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        complete_dir = run_dir / "complete"
        pdf_dir = complete_dir / "pdfs"
        pdf_dir.mkdir(parents=True, exist_ok=True)
        missing_reasons = missing_reasons or {}
        result_by_doi = {result.doi.lower(): result for result in results if result.ok}
        manifest: list[dict[str, Any]] = []

        for record in records:
            result = result_by_doi.get(record.doi.lower())
            item = {
                "doi": record.doi,
                "published": record.published,
                "title": record.title,
                "status": "missing",
                "reason": missing_reasons.get(record.doi.lower(), ""),
                "pdf_path": "",
                "size_bytes": 0,
                "text_length": 0,
                "verified_match": False,
            }
            if result and result.pdf_path:
                src = Path(result.pdf_path)
                dst = pdf_dir / src.name
                if src.exists():
                    dst.write_bytes(src.read_bytes())
                    text = pdf_extractor.extract_text(dst)
                    verified_match = self._text_matches_record(text, record, fallback_title=result.title)
                    item.update(
                        {
                            "status": "success" if verified_match else "unverified",
                            "reason": result.reason,
                            "pdf_path": str(dst),
                            "size_bytes": dst.stat().st_size,
                            "text_length": len(text or ""),
                            "verified_match": verified_match,
                        }
                    )
            manifest.append(item)

        complete_dir.mkdir(parents=True, exist_ok=True)
        (complete_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (complete_dir / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(manifest[0].keys()) if manifest else [])
            writer.writeheader()
            writer.writerows(manifest)

        return {
            "count": len(manifest),
            "success": sum(1 for item in manifest if item["status"] == "success"),
            "missing": sum(1 for item in manifest if item["status"] == "missing"),
            "unverified": sum(1 for item in manifest if item["status"] == "unverified"),
            "verified_match": sum(1 for item in manifest if item["verified_match"]),
            "pdf_dir": str(pdf_dir),
            "manifest": str(complete_dir / "manifest.csv"),
        }

    @staticmethod
    def _text_matches_record(text: str, record: PaperRecord, fallback_title: str = "") -> bool:
        lower = (text or "").lower()
        head = re.sub(r"\s+", " ", lower[:5_000]).strip()
        if any(marker in head for marker in NON_ARTICLE_PDF_MARKERS):
            return False
        doi = record.doi.lower()
        if doi and doi in lower:
            return True
        title_source = record.title.strip()
        if not title_source and fallback_title:
            title_source = re.split(r"\s+\|\s+|\s+-\s+", fallback_title.strip(), maxsplit=1)[0]
            if title_source.lower().startswith("loading "):
                title_source = ""
        stop_words = {
            "article",
            "journal",
            "journals",
            "research",
            "science",
            "sciencedirect",
            "annual",
            "reviews",
            "loading",
            "https",
            "content",
        }
        title_words = [
            word.lower()
            for word in re.findall(r"[A-Za-z0-9]{5,}", title_source)[:10]
            if word.lower() not in stop_words
        ]
        required_title_hits = min(3, len(title_words))
        if required_title_hits == 0:
            return False
        return sum(1 for word in title_words if word in lower) >= required_title_hits


class ACSCloakBatchDownloader(PublisherBatchDownloader):
    """Compatibility wrapper for the original ACS/EST downloader API."""

    def __init__(
        self,
        config: dict | None = None,
        *,
        institution_query: str = "",
        login_timeout_sec: int = 900,
        pdf_timeout_sec: int = 60,
    ) -> None:
        super().__init__(
            config,
            profile=ACS_PROFILE,
            institution_query=institution_query,
            login_timeout_sec=login_timeout_sec,
            pdf_timeout_sec=pdf_timeout_sec,
        )
