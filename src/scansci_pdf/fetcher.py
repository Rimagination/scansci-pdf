"""Core paper fetching logic with multi-stage pipeline."""

from contextvars import ContextVar
import hashlib
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from .auth import EZProxyAuth, WebVPNAuth
from .http_utils import request_with_retry
from .extractors import html_extractor, pdf_extractor
from .models import FetchResult, NextAction, Paper
from .publisher_pdf_router import build_pdf_candidates, discover_pdf_candidates_from_html
from .publisher_profiles import infer_publisher_profile, infer_publisher_profile_from_url

try:
    from .cloakbrowser_compat import prepare_cloakbrowser_runtime
    prepare_cloakbrowser_runtime()
    import cloakbrowser  # noqa: F401
    _HAS_CLOAKBROWSER = True
except Exception:
    _HAS_CLOAKBROWSER = False

logger = logging.getLogger(__name__)

DOI_PATTERN = re.compile(r"^10\.\d{4,9}/[^\s]+$")

MIN_FULLTEXT_LEN = 1000

# Challenge keywords that indicate a Cloudflare/bot-detection page
_CHALLENGE_KEYWORDS = ("请稍候", "just a moment", "attention required", "verify", "security check")

_ATTEMPT_LOG: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "scansci_fetch_attempt_log",
    default=None,
)


def _record_attempt(stage: str, status: str, reason: str = "", detail: str = "") -> None:
    attempts = _ATTEMPT_LOG.get()
    if attempts is None:
        return
    attempt = {"stage": stage, "status": status}
    if reason:
        attempt["reason"] = reason
    if detail:
        attempt["detail"] = detail
    attempts.append(attempt)


def _record_paper_attempt(stage: str, paper: Paper | None) -> None:
    if paper is None:
        _record_attempt(stage, "miss", reason="no_result")
        return
    if len(paper.full_text or "") >= MIN_FULLTEXT_LEN:
        _record_attempt(stage, "success", reason="full_text")
        return
    _record_attempt(stage, "partial", reason=_paper_quality(paper))


def _paper_quality(paper: Paper) -> str:
    if paper.full_text:
        return "short_text"
    if paper.pdf_path:
        return "pdf_only"
    if paper.abstract:
        return "abstract_only"
    if paper.title or paper.authors or paper.journal or paper.year:
        return "metadata_only"
    return "none"


def _apply_attempt_diagnostics(result: FetchResult, identifier: str) -> None:
    for attempt in result.attempts:
        if (
            attempt.get("stage") == "doi_resolve"
            and attempt.get("status") == "miss"
            and attempt.get("reason") == "no_url"
        ):
            query = (identifier or result.paper.title or result.paper.doi).replace('"', '\\"')
            result.status = "blocked"
            result.reason = "doi_resolution_failed"
            result.next_action = NextAction(
                kind="check_identifier",
                command=f'scansci-pdf search "{query}"',
                message=(
                    "DOI did not resolve to an article URL. Check the identifier or search "
                    "for the paper, then retry with a DOI or publisher URL."
                ),
            )
            return

    if (
        result.status == "partial"
        and result.next_action
        and result.next_action.kind == "login"
        and any(
            attempt.get("stage") == "institutional_access"
            and attempt.get("status") == "partial"
            and attempt.get("reason") in {"none", "metadata_only", "abstract_only", "short_text"}
            for attempt in result.attempts
        )
    ):
        result.status = "auth_required"
        result.reason = "institution_login_required"


def _is_good_result(paper: Paper | None) -> bool:
    """Check if a paper result has enough full text to be useful."""
    return bool(paper and len(paper.full_text or "") >= MIN_FULLTEXT_LEN)


def _copy_paper_metadata(paper: Paper, url: str) -> Paper:
    """Create a new Paper copying metadata from an existing one, with a new URL."""
    return Paper(
        doi=paper.doi, title=paper.title, authors=paper.authors,
        journal=paper.journal, year=paper.year, abstract=paper.abstract,
        url=url,
    )


def _apply_pdf_bytes(paper: Paper, pdf_bytes: bytes, doi: str, source: str,
                     save_fn=None) -> None:
    """Extract text from PDF bytes and update paper fields in-place."""
    paper.full_text = pdf_extractor.extract_from_bytes(pdf_bytes)
    if save_fn:
        pdf_path = save_fn(doi, pdf_bytes)
        paper.pdf_path = str(pdf_path) if pdf_path else ""
    paper.source = source


def _close_response(response) -> None:
    """Release a consumed HTTP response without masking the fetch outcome."""
    if response is None:
        return
    try:
        response.close()
    except Exception:
        pass


def _wait_for_challenge(page, max_tries: int = 6) -> None:
    """Wait for Cloudflare/bot-detection challenges to clear."""
    for i in range(max_tries):
        title = page.title().lower()
        if any(sig in title for sig in _CHALLENGE_KEYWORDS):
            logger.info("Challenge page detected, waiting... (%d/%d)", i + 1, max_tries)
            time.sleep(5)
        else:
            break


class PaperFetcher:
    """Main class for fetching academic papers via multi-stage pipeline."""

    def __init__(self, config: dict | None = None):
        from .config import load_config
        self.config = config or load_config()
        self._auth: WebVPNAuth | EZProxyAuth | None = None
        self._last_request_time = 0.0

    @property
    def auth(self) -> WebVPNAuth | EZProxyAuth:
        if self._auth is None:
            from .schools import get_school
            school_name = self.config.get("instsci_school", "")
            entry = get_school(school_name)
            if entry.school_type == "ezproxy":
                self._auth = EZProxyAuth(self.config, proxy_base=entry.host)
            else:
                self._auth = WebVPNAuth(self.config, key=entry.key, iv=entry.iv)
        return self._auth

    def fetch(self, identifier: str, use_cache: bool = True) -> Paper:
        """Fetch a paper by DOI or URL."""
        doi = self._parse_doi(identifier)
        url = self._parse_url(identifier)

        if use_cache and doi:
            cached = self._load_cache(doi)
            if _is_good_result(cached):
                _record_paper_attempt("cache", cached)
                logger.info("Loaded from cache (good full text): %s", doi)
                return cached
            elif cached:
                _record_paper_attempt("cache", cached)
                logger.info("Cache hit but full text too short (%d chars), re-fetching: %s",
                            len(cached.full_text or ""), doi)
            else:
                _record_attempt("cache", "miss", reason="not_found")

        paper = Paper(doi=doi or "", url=url or "")

        # Helper: record attempt, cache if good, return whether result is usable
        def _check(stage: str, result: Paper | None) -> bool:
            _record_paper_attempt(stage, result)
            if _is_good_result(result):
                self._save_cache(result)
                return True
            return False

        # Step 1: Try Open Access sources first
        if doi:
            oa_paper = self._try_open_access(doi, identifier)
            if _check("open_access", oa_paper):
                return oa_paper
            if oa_paper:
                paper = oa_paper

        # Step 2: Try Elsevier API before institutional flows
        if doi and not paper.pdf_path:
            api_paper = self._try_elsevier_api(doi, paper)
            if _check("elsevier_api", api_paper):
                return api_paper

        # Step 3: Resolve DOI to URL if needed
        if doi and not url:
            url = self._resolve_doi(doi)
            _record_attempt("doi_resolve", "success" if url else "miss",
                            detail=url or "", reason="" if url else "no_url")
            paper.url = url or ""

        if not url:
            logger.error("Could not determine URL for: %s", identifier)
            return paper

        # Step 4: Try CARSI federated access
        if self.config.get("carsi_enabled") and doi:
            carsi_paper = self._try_carsi_pdf(doi, url, paper)
            if _check("carsi_pdf", carsi_paper):
                return carsi_paper
            carsi_paper = self._try_carsi_html(url, paper)
            if _check("carsi_html", carsi_paper):
                return carsi_paper

        # Step 5: Try direct publisher PDF URL construction
        if doi and not paper.pdf_path:
            pdf_paper = self._try_publisher_pdf(doi, url, paper)
            if _check("publisher_pdf", pdf_paper):
                return pdf_paper

        # Step 6: Try browser-based PDF download
        if doi and not paper.pdf_path:
            browser_paper = self._try_browser_pdf_download(doi, url, paper)
            if _check("browser_pdf", browser_paper):
                return browser_paper

        # Step 7: Fetch via institutional campus access
        self._rate_limit()
        try:
            paper = self._fetch_via_webvpn(url, paper)
        except ValueError:
            _record_attempt("institutional_access", "error", reason="config_needed")
            raise
        except requests.RequestException:
            _record_attempt("institutional_access", "error", reason="gateway_unreachable")
            raise
        _record_paper_attempt("institutional_access", paper)

        if _is_good_result(paper):
            self._save_cache(paper)

        return paper

    def fetch_with_result(self, identifier: str, use_cache: bool = True) -> FetchResult:
        """Fetch a paper and return a structured, agent-friendly outcome."""
        attempts: list[dict[str, str]] = []
        token = _ATTEMPT_LOG.set(attempts)
        try:
            paper = self.fetch(identifier, use_cache=use_cache)
        except ValueError as exc:
            doi = self._parse_doi(identifier) or ""
            url = self._parse_url(identifier) or ""
            return FetchResult(
                status="config_needed",
                quality="none",
                reason="institution_not_configured",
                paper=Paper(doi=doi, url=url),
                next_action=NextAction(
                    kind="configure_institution",
                    command="scansci-pdf config set instsci_school YOUR_SCHOOL",
                    message=f"Configure your school or institution before retrying. Detail: {exc}",
                ),
                attempts=attempts,
            )
        except requests.RequestException as exc:
            doi = self._parse_doi(identifier) or ""
            url = self._parse_url(identifier) or ""
            gateway_error = any(
                a.get("stage") == "institutional_access"
                and a.get("status") == "error"
                and a.get("reason") == "gateway_unreachable"
                for a in attempts
            )
            reason = "gateway_unreachable" if gateway_error else "network_error"
            kind = "diagnose_gateway" if gateway_error else "diagnose"
            message = (
                "Institutional gateway could not be reached. Check VPN/proxy/CARSI/WebVPN "
                f"configuration, then retry. Detail: {exc}"
                if gateway_error
                else f"Check network and institutional access configuration. Detail: {exc}"
            )
            return FetchResult(
                status="blocked",
                quality="none",
                reason=reason,
                paper=Paper(doi=doi, url=url),
                next_action=NextAction(
                    kind=kind,
                    command="scansci-pdf config show",
                    message=message,
                ),
                attempts=attempts,
            )
        finally:
            _ATTEMPT_LOG.reset(token)

        result = FetchResult.from_paper(
            paper,
            min_fulltext_len=MIN_FULLTEXT_LEN,
            institution_configured=self._institution_configured(),
            identifier=identifier,
        )
        result.attempts = attempts
        _apply_attempt_diagnostics(result, identifier)
        return result

    def _institution_configured(self) -> bool:
        return bool(
            self.config.get("instsci_school")
            or self.config.get("instsci_base_url")
            or self.config.get("ezproxy_login_url")
            or self.config.get("network_proxy")
            or (self.config.get("carsi_enabled") and self.config.get("carsi_idp_name"))
        )

    def _try_open_access(self, doi: str, identifier: str = "") -> Paper | None:
        """Try to fetch paper from Open Access sources."""
        from .sources import unpaywall, arxiv
        from .identifiers import normalize_arxiv_id

        output_dir = Path(self.config.get("output_dir", "."))
        output_dir.mkdir(parents=True, exist_ok=True)

        # Check if it's an arXiv paper first
        arxiv_id = normalize_arxiv_id(identifier or doi)
        if arxiv_id:
            logger.info("Fetching from arXiv: %s", arxiv_id)
            safe_id = arxiv_id.replace("/", "_")
            output_path = output_dir / f"arxiv_{safe_id}.pdf"
            result = arxiv.try_arxiv(arxiv_id, output_path, self.config)
            if result and result.get("path"):
                paper = Paper(
                    doi=doi,
                    url=result.get("url", f"https://arxiv.org/abs/{arxiv_id}"),
                    source="arxiv",
                    pdf_path=str(result["path"]),
                )
                self._extract_pdf_text(paper, result["path"])
                return paper

        # Try Unpaywall
        logger.info("Checking Unpaywall for OA version of %s...", doi)
        safe_doi = re.sub(r"[^\w\-.]", "_", doi)
        output_path = output_dir / f"unpaywall_{safe_doi}.pdf"
        result = unpaywall.try_unpaywall(doi, output_path, self.config)
        if result and result.get("path"):
            paper = Paper(
                doi=doi,
                url=result.get("url", ""),
                source="open_access",
                pdf_path=str(result["path"]),
            )
            self._extract_pdf_text(paper, result["path"])
            return paper

        return Paper(doi=doi)

    def _try_publisher_pdf(self, doi: str, resolved_url: str, paper: Paper) -> Paper | None:
        pdf_url = self._build_publisher_pdf_url(doi, resolved_url)
        if not pdf_url:
            return None

        logger.info("Trying constructed publisher PDF URL: %s", pdf_url)

        if not self.auth.login():
            logger.error("Institutional access authentication failed.")
            return None

        self._rate_limit()
        resp = None
        try:
            resp = self.auth.fetch(pdf_url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").lower()

            if "pdf" in ct and len(resp.content) > 10000:
                _apply_pdf_bytes(paper, resp.content, doi, "institutional", self._save_pdf)
                logger.info("Publisher PDF downloaded (%d bytes, %d chars text)",
                            len(resp.content), len(paper.full_text or ""))
                return paper
            else:
                logger.info("Publisher PDF URL returned non-PDF or too small (ct=%s, size=%d)",
                            ct, len(resp.content))
        except requests.RequestException as e:
            logger.warning("Failed to fetch publisher PDF: %s", e)
        finally:
            _close_response(resp)

        return None

    def _try_browser_pdf_download(self, doi: str, resolved_url: str, paper: Paper) -> Paper | None:
        if not _HAS_CLOAKBROWSER:
            return None

        pdf_url = self._build_publisher_pdf_url(doi, resolved_url)
        if not pdf_url:
            return None

        print("  [Browser] Downloading PDF via browser...")

        live_context = getattr(self.auth, 'browser_context', None)
        if live_context:
            result = self._browser_pdf_download(live_context, resolved_url, doi, paper)
            if result:
                return result

        logger.info("No live browser session. Triggering browser login for PDF download...")
        if self.auth.login(force=True):
            live_context = getattr(self.auth, 'browser_context', None)
            if live_context:
                result = self._browser_pdf_download(live_context, resolved_url, doi, paper)
                if result:
                    return result

        return None

    def _browser_pdf_download(self, context, article_url: str, doi: str, paper: Paper) -> Paper | None:
        page = context.new_page()
        cleanup_callbacks = []
        try:
            return self._browser_pdf_do(
                page,
                article_url,
                doi,
                paper,
                cleanup_callbacks=cleanup_callbacks,
            )
        finally:
            for cleanup in reversed(cleanup_callbacks):
                try:
                    cleanup()
                except Exception:
                    pass
            try:
                page.close()
            except Exception:
                pass

    def _browser_pdf_do(
        self,
        page,
        article_url: str,
        doi: str,
        paper: Paper,
        *,
        cleanup_callbacks: list[Any] | None = None,
    ) -> Paper | None:
        try:
            proxied_article = self.auth.convert_url(article_url)
        except Exception:
            proxied_article = article_url

        logger.info("Browser: navigating to article page %s", proxied_article[:80])
        try:
            page.goto(proxied_article, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            logger.warning("Navigation failed: %s", e)

        _wait_for_challenge(page, max_tries=6)

        current_url = page.url
        logger.info("Browser article page: url=%s, title=%s", current_url[:60], page.title()[:40])

        if "cas" in current_url.lower() or "login" in current_url.lower() or \
           "登录" in page.title() or "身份" in page.title():
            logger.info("Browser landed on CAS page")
            return None

        if "linkinghub" in current_url or "retrieve/pii" in current_url:
            logger.info("On linkinghub redirect page, waiting for redirect...")
            time.sleep(5)
            current_url = page.url
            if "linkinghub" in current_url:
                pii_match = re.search(r"pii/([A-Z0-9]+)", current_url)
                if pii_match:
                    direct_url = f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}"
                    try:
                        proxied_direct = self.auth.convert_url(direct_url)
                    except Exception:
                        proxied_direct = direct_url
                    try:
                        page.goto(proxied_direct, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(3)
                    except Exception:
                        pass
                    _wait_for_challenge(page, max_tries=6)
                    current_url = page.url

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

        pdf_link = page.evaluate("""
            (() => {
                for (const meta of document.querySelectorAll('meta[name="citation_pdf_url"]')) {
                    if (meta.content) return meta.content;
                }
                for (const a of document.querySelectorAll('a[aria-label*="PDF" i], a[title*="PDF" i]')) {
                    if (a.href) return a.href;
                }
                for (const a of document.querySelectorAll('a')) {
                    const href = (a.href || '').toLowerCase();
                    const text = (a.textContent || '').toLowerCase();
                    if (href.includes('/pdfft') || href.includes('/pdfdirect')) return a.href;
                    if (href.includes('pdf') && !href.includes('supplement') && !href.includes('cite')) return a.href;
                    if (text.includes('pdf') && (text.includes('download') || text.includes('view'))) return a.href;
                }
                return null;
            })()
        """)

        if not pdf_link or not isinstance(pdf_link, str):
            logger.info("No PDF link found on article page")
            return None

        logger.info("Found PDF link on page: %s", pdf_link[:80])

        if pdf_link.startswith("/"):
            parsed = urlparse(current_url)
            pdf_link = f"{parsed.scheme}://{parsed.netloc}{pdf_link}"

        captured_pdf = {"bytes": None}

        def _on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if "pdf" in ct or "octet" in ct:
                    body = response.body()
                    if body[:5] == b"%PDF-":
                        captured_pdf["bytes"] = body
            except Exception:
                pass

        page.on("response", _on_response)
        capture_active = True

        def _cleanup_response_capture():
            nonlocal capture_active
            if capture_active:
                try:
                    page.remove_listener("response", _on_response)
                except Exception:
                    pass
                finally:
                    capture_active = False
            captured_pdf["bytes"] = None

        if cleanup_callbacks is not None:
            cleanup_callbacks.append(_cleanup_response_capture)

        pdf_paths = self._build_browser_pdf_paths(doi, current_url)
        all_urls = [pdf_link]
        _parsed = urlparse(current_url)
        _origin = f"{_parsed.scheme}://{_parsed.netloc}"
        for p in pdf_paths:
            full = _origin + p if p.startswith("/") else p
            if full not in all_urls:
                all_urls.append(full)

        for pdf_url in all_urls:
            if captured_pdf["bytes"]:
                break
            logger.info("Navigating to PDF URL: %s", pdf_url[:80])
            try:
                page.goto(pdf_url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                logger.debug("Navigation to %s failed: %s", pdf_url[:60], e)
                continue

            _wait_for_challenge(page, max_tries=4)

            if not captured_pdf["bytes"]:
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                try:
                    pdf_from_viewer = page.evaluate("""
                        () => {
                            const obj = document.querySelector('object[type="application/pdf"]');
                            if (obj && obj.data) return obj.data;
                            const embed = document.querySelector('embed[type="application/pdf"]');
                            if (embed && embed.src) return embed.src;
                            const iframe = document.querySelector('iframe[src*="pdf"]');
                            if (iframe && iframe.src) return iframe.src;
                            return null;
                        }
                    """)
                    if pdf_from_viewer and isinstance(pdf_from_viewer, str):
                        if pdf_from_viewer.startswith("/"):
                            pdf_from_viewer = _origin + pdf_from_viewer
                        try:
                            page.goto(pdf_from_viewer, wait_until="domcontentloaded", timeout=30000)
                            time.sleep(2)
                        except Exception:
                            pass
                except Exception:
                    pass

            if not captured_pdf["bytes"]:
                final_title = page.title().lower()
                if "robot" in final_title or "captcha" in final_title:
                    logger.warning("Anti-bot detected on PDF page, trying next URL")
                    continue

        pdf_bytes = captured_pdf["bytes"]
        _cleanup_response_capture()
        if pdf_bytes and pdf_bytes[:5] == b"%PDF-" and len(pdf_bytes) > 5000:
            _apply_pdf_bytes(paper, pdf_bytes, doi, "browser", self._save_pdf)
            logger.info("Browser PDF downloaded via response capture (%d bytes)", len(pdf_bytes))
            return paper

        logger.info("Could not capture PDF from browser navigation")
        return None

    @staticmethod
    def _build_browser_pdf_paths(doi: str, current_url: str) -> list[str]:
        parsed = urlparse(current_url)
        doi_suffix = doi.split("/", 1)[-1] if "/" in doi else doi

        paths = []
        if "sciencedirect" in parsed.netloc or "elsevier" in parsed.netloc:
            pii_match = re.search(r"pii/([A-Z0-9]+)", current_url)
            if pii_match:
                pii = pii_match.group(1)
                paths.append(f"/science/article/pii/{pii}/pdfft")
            paths.append(f"/doi/pdfdirect/{doi}")
        elif "springer" in parsed.netloc:
            paths.append(f"/content/pdf/{doi}.pdf")
        elif "wiley" in parsed.netloc:
            paths.append(f"/doi/pdfdirect/{doi}")
            paths.append(f"/doi/pdf/{doi}")
        paths.append(f"/doi/pdf/{doi}")
        return paths

    def _try_carsi_pdf(self, doi: str, resolved_url: str, paper: Paper) -> Paper | None:
        from .sources.carsi import CARSIClient

        pdf_url = self._build_publisher_pdf_url(doi, resolved_url)
        if not pdf_url:
            return None

        logger.info("Trying CARSI publisher PDF: %s", pdf_url)
        self._rate_limit()
        carsi = None
        resp = None
        try:
            carsi = CARSIClient(self.config)
            resp = carsi.fetch(pdf_url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").lower()
            if "pdf" in ct and len(resp.content) > 10000:
                result = _copy_paper_metadata(paper, pdf_url)
                _apply_pdf_bytes(result, resp.content, doi, "carsi", self._save_pdf)
                logger.info("CARSI PDF downloaded (%d bytes)", len(resp.content))
                return result
        except Exception as e:
            logger.warning("CARSI PDF failed: %s", e)
        finally:
            _close_response(resp)
            if carsi is not None:
                try:
                    carsi.close()
                except Exception as e:
                    logger.warning("Failed to close CARSI PDF client: %s", e)
        return None

    def _try_carsi_html(self, url: str, paper: Paper) -> Paper | None:
        from .sources.carsi import CARSIClient

        logger.info("Trying CARSI HTML: %s", url)
        self._rate_limit()
        carsi = None
        resp = None
        try:
            carsi = CARSIClient(self.config)
            resp = carsi.fetch(url)
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "").lower()
            if "pdf" in ct:
                result = _copy_paper_metadata(paper, url)
                _apply_pdf_bytes(result, resp.content, paper.doi or "unknown", "carsi",
                                 self._save_pdf if paper.doi else None)
                return result

            result = _copy_paper_metadata(paper, url)
            self._apply_extracted(result, html_extractor.extract(resp.text, url))
            result.source = "carsi"
            if _is_good_result(result):
                return result
        except Exception as e:
            logger.warning("CARSI HTML failed: %s", e)
        finally:
            _close_response(resp)
            if carsi is not None:
                try:
                    carsi.close()
                except Exception as e:
                    logger.warning("Failed to close CARSI HTML client: %s", e)
        return None

    def _fetch_via_webvpn(self, url: str, paper: Paper) -> Paper:
        if not self.auth.login():
            logger.error("Institutional access authentication failed.")
            return paper

        paper.source = "institutional"

        resp = None
        try:
            try:
                resp = self.auth.fetch(url)
                resp.raise_for_status()
            except requests.RequestException as e:
                logger.error("Failed to fetch via institutional access: %s", e)
                return paper

            if "pdf" in resp.headers.get("content-type", "").lower():
                _apply_pdf_bytes(paper, resp.content, paper.doi or "unknown", "institutional",
                                 self._save_pdf)
                return paper

            self._apply_extracted(paper, html_extractor.extract(resp.text, resp.url))

            pdf_url = self._find_pdf_link(resp.text, resp.url)
            if pdf_url:
                logger.info("Found PDF link in HTML, downloading: %s", pdf_url)
                self._rate_limit()
                pdf_resp = None
                try:
                    pdf_resp = self.auth.fetch(pdf_url)
                    pdf_resp.raise_for_status()
                    ct = pdf_resp.headers.get("content-type", "").lower()
                    if "pdf" in ct and len(pdf_resp.content) > 10000:
                        pdf_path = self._save_pdf(paper.doi or "unknown", pdf_resp.content)
                        paper.pdf_path = str(pdf_path) if pdf_path else ""
                        if not _is_good_result(paper):
                            paper.full_text = pdf_extractor.extract_from_bytes(pdf_resp.content)
                except requests.RequestException as e:
                    logger.warning("Failed to download PDF: %s", e)
                finally:
                    _close_response(pdf_resp)

            return paper
        finally:
            _close_response(resp)

    def _try_elsevier_api(self, doi: str, paper: Paper) -> Paper | None:
        from .sources import elsevier_api

        api_key = elsevier_api.get_api_key(self.config.get("elsevier_api_key", ""))
        if not api_key:
            logger.debug("No Elsevier API key configured")
            return None

        if not doi.startswith("10.1016/"):
            return None

        logger.info("Trying Elsevier API for %s", doi)
        inst_token = self.config.get("elsevier_insttoken", "")

        data = elsevier_api.fetch_fulltext(doi, api_key=api_key, inst_token=inst_token)
        if data and data.get("full_text"):
            result = Paper(
                doi=doi,
                url=paper.url,
                source="elsevier_api",
                title=data.get("title", "") or paper.title,
                authors=data.get("authors", []) or paper.authors,
                abstract=data.get("abstract", "") or paper.abstract,
                full_text=data["full_text"],
            )
            logger.info("Elsevier API XML: %d chars of full text", len(data["full_text"]))
            return result

        pdf_bytes = elsevier_api.fetch_pdf(doi, api_key=api_key, inst_token=inst_token)
        if pdf_bytes:
            _apply_pdf_bytes(paper, pdf_bytes, doi, "elsevier_api", self._save_pdf)
            logger.info("Elsevier API PDF: %d bytes", len(pdf_bytes))
            return paper

        return None

    @staticmethod
    def _build_publisher_pdf_url(doi: str, resolved_url: str) -> str | None:
        profile = infer_publisher_profile_from_url(resolved_url) or infer_publisher_profile(doi)
        if profile is None:
            return None
        candidates = build_pdf_candidates(profile, doi, source_url=resolved_url)
        return candidates[0] if candidates else None

    @staticmethod
    def _extract_pdf_text(paper: Paper, pdf_path: str) -> None:
        """Try to extract full text from a saved PDF file into the paper."""
        try:
            paper.full_text = pdf_extractor.extract_from_bytes(Path(pdf_path).read_bytes())
        except Exception:
            pass

    def _apply_extracted(self, paper: Paper, extracted: dict):
        paper.title = paper.title or extracted.get("title", "")
        paper.authors = paper.authors or extracted.get("authors", [])
        paper.abstract = paper.abstract or extracted.get("abstract", "")
        paper.full_text = extracted.get("full_text", "")
        paper.figures = extracted.get("figures", [])
        paper.references = extracted.get("references", [])

    def _find_pdf_link(self, html: str, base_url: str) -> str | None:
        candidates = discover_pdf_candidates_from_html(html, base_url)
        if candidates:
            return candidates[0]

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        parsed = urlparse(base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        hostname = parsed.netloc.lower()

        meta_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
        if meta_pdf and meta_pdf.get("content"):
            return self._resolve_url(meta_pdf["content"], base)

        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            classes = " ".join(a.get("class", []))

            if any(kw in text for kw in ["pdf", "download pdf", "full text pdf",
                                          "view pdf", "get pdf"]):
                return self._resolve_url(href, base)
            if any(kw in classes for kw in ["pdf", "download-pdf", "pdf-download",
                                             "article-pdf", "article__pdf"]):
                return self._resolve_url(href, base)
            if href.endswith(".pdf"):
                return self._resolve_url(href, base)
            if "/doi/pdf/" in href:
                return self._resolve_url(href, base)
            if "/doi/pdfdirect/" in href or "/doi/epdf/" in href:
                return self._resolve_url(href, base)

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
                pii = pii_match.group(1)
                return f"https://www.sciencedirect.com/science/article/pii/{pii}/pdfft"

        return None

    def _resolve_url(self, href: str, base: str) -> str:
        if href.startswith("http"):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return base + href
        return base + "/" + href

    def _parse_doi(self, identifier: str) -> str | None:
        identifier = identifier.strip()

        if DOI_PATTERN.match(identifier):
            return identifier

        for prefix in ["https://doi.org/", "http://doi.org/", "https://dx.doi.org/"]:
            if identifier.lower().startswith(prefix):
                return identifier[len(prefix):]

        doi_match = re.search(r"(10\.\d{4,9}/[^\s&?#]+)", identifier)
        if doi_match:
            return doi_match.group(1)

        return None

    def _parse_url(self, identifier: str) -> str | None:
        identifier = identifier.strip()
        return identifier if identifier.startswith("http") else None

    def _resolve_doi(self, doi: str) -> str | None:
        resp = None
        try:
            resp = request_with_retry(
                "GET",
                f"https://doi.org/{doi}",
                allow_redirects=True,
                timeout=15,
                headers={"User-Agent": "scansci-pdf/1.5"},
                stream=True,
            )
            if resp.url and resp.url != f"https://doi.org/{doi}":
                logger.info("Resolved DOI %s → %s (status=%d)", doi, resp.url, resp.status_code)
                return resp.url
        except requests.RequestException as e:
            logger.warning("Failed to resolve DOI %s: %s", doi, e)
        finally:
            _close_response(resp)
        return None

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_time
        delay_min = float(self.config.get("request_delay_min", 2.0))
        delay_max = float(self.config.get("request_delay_max", 5.0))
        delay = random.uniform(delay_min, delay_max)
        if elapsed < delay:
            sleep_time = delay - elapsed
            logger.debug("Rate limiting: sleeping %.1fs", sleep_time)
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def _save_pdf(self, doi: str, pdf_bytes: bytes) -> Path | None:
        output_dir = Path(self.config.get("output_dir", "."))
        safe_name = re.sub(r"[^\w\-.]", "_", doi)
        pdf_path = output_dir / f"{safe_name}.pdf"
        try:
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(pdf_bytes)
            logger.info("Saved PDF to %s", pdf_path)
            return pdf_path
        except OSError as e:
            logger.error("Failed to save PDF: %s", e)
            return None

    def _cache_key(self, doi: str) -> Path:
        h = hashlib.md5(doi.encode()).hexdigest()
        cache_dir = Path(self.config.get("cache_dir", ".cache"))
        return cache_dir / f"{h}.json"

    def _load_cache(self, doi: str) -> Paper | None:
        path = self._cache_key(doi)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Paper.from_json(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load cache for %s: %s", doi, e)
            return None

    def _save_cache(self, paper: Paper):
        if not paper.doi:
            return
        if len(paper.full_text or "") < MIN_FULLTEXT_LEN:
            return
        path = self._cache_key(paper.doi)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(paper.to_json(), encoding="utf-8")
            logger.info("Cached result for %s (%d chars)", paper.doi, len(paper.full_text or ""))
        except OSError as e:
            logger.warning("Failed to save cache for %s: %s", paper.doi, e)

    def clear_cache(self):
        cache_dir = Path(self.config.get("cache_dir", ".cache"))
        if cache_dir.exists():
            for f in cache_dir.glob("*.json"):
                f.unlink()
            logger.info("Cache cleared.")

    def close(self):
        auth = self._auth
        self._auth = None
        if auth is not None:
            auth.close()
