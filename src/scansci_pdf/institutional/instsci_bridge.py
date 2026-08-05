"""Institutional access bridge — connects scansci-pdf's tier system to institutional download.

Two modes:
1. If external `instsci` package is installed: delegates to PaperFetcher for full cascade
2. Otherwise: uses the ported institutional modules directly (CARSI, WebVPN, browser PDF)

Built-in mode integrates:
- CARSI/Shibboleth federated auth
- PublisherBatchDownloader (CloakBrowser state machine for SSO+PDF capture)
- Session broker (persistent CloakBrowser sessions across downloads)
- WebVPN / EZProxy campus gateways
- Elsevier API
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..log import get_logger

log = get_logger()

_MIN_FULLTEXT_LEN = 1000


def try_institutional(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Try institutional access. Drop-in source for the tier system.

    Stage B source — skips OA/grey checks, goes straight to institutional paths:
    1. External instsci package (if installed — full cascade)
    2. CARSI federated auth (if configured)
    3. Session broker (persistent CloakBrowser session, if running)
    4. Browser batch download (CloakBrowser state machine, SSO+PDF capture)
    5. Campus gateway (WebVPN / EZProxy)
    6. Elsevier API
    """
    if not _any_institutional_configured(config):
        log.info("   [Institutional] No institutional access configured, skipping")
        return None

    from .publisher_profiles import infer_publisher_profile
    profile = infer_publisher_profile(doi)
    if profile:
        log.info(f"   [Institutional] Publisher: {profile.name}")

    # Mode 1: External instsci package — full cascade
    result = _try_external_instsci(doi, output_path, config)
    if result:
        return result

    # Mode 2: Built-in institutional access using ported modules
    return _try_builtin_institutional(doi, output_path, config, profile)


def _any_institutional_configured(config: dict[str, Any]) -> bool:
    return bool(
        (config.get("carsi_enabled") and config.get("carsi_idp_name", "").strip())
        or (config.get("vpnsci_enabled") and (config.get("vpnsci_school") or config.get("vpnsci_base_url")))
        or (config.get("ezproxy_enabled") and config.get("ezproxy_login_url"))
        or config.get("elsevier_api_key")
    )


# ---------------------------------------------------------------------------
# Mode 1: External instsci package
# ---------------------------------------------------------------------------

def _try_external_instsci(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Try using the installed instsci package's PaperFetcher."""
    try:
        from instsci.config import Config as InstSciConfig
        from instsci.fetcher import PaperFetcher
    except ImportError:
        return None

    log.info("   [Institutional] Using external instsci PaperFetcher")
    inst_config = _map_to_instsci_config(config, output_path)
    inst_config.ensure_dirs()

    fetcher = PaperFetcher(config=inst_config)
    try:
        result = fetcher.fetch_with_result(doi, use_cache=False)
    except Exception as e:
        log.info(f"   [Institutional] PaperFetcher error: {e}")
        return None
    finally:
        try:
            fetcher.close()
        except Exception:
            pass

    return _instsci_result_to_dict(result, doi, output_path)


def _map_to_instsci_config(scansci_config: dict[str, Any], output_path: Path):
    from instsci.config import Config as InstSciConfig
    return InstSciConfig(
        school=scansci_config.get("vpnsci_school", ""),
        webvpn_base_url=scansci_config.get("vpnsci_base_url", ""),
        ezproxy_base_url=scansci_config.get("ezproxy_login_url", "").replace("{url}", "").rstrip("?&="),
        email=scansci_config.get("email", ""),
        elsevier_api_key=scansci_config.get("elsevier_api_key", ""),
        elsevier_inst_token=scansci_config.get("elsevier_insttoken", ""),
        carsi_enabled=scansci_config.get("carsi_enabled", False),
        carsi_idp_name=scansci_config.get("carsi_idp_name", ""),
        output_dir=str(output_path.parent),
        cache_dir=scansci_config.get("cache_dir", ""),
    )


def _instsci_result_to_dict(result, doi: str, output_path: Path) -> dict[str, Any] | None:
    if result.status != "success":
        log.info(f"   [Institutional] instsci: {result.status}/{result.quality} — {result.reason}")
        return None

    paper = result.paper
    if paper.pdf_path:
        src = Path(paper.pdf_path)
        if src.exists():
            if src != output_path:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output_path.exists():
                    output_path.unlink()
                src.rename(output_path)
            return {
                "success": True, "identifier": doi, "doi": doi,
                "file": str(output_path),
                "source": f"institutional:{paper.source}",
                "full_text_length": len(paper.full_text or ""),
            }

    if paper.full_text and len(paper.full_text) >= _MIN_FULLTEXT_LEN:
        return {
            "success": False, "identifier": doi, "doi": doi,
            "source": f"institutional:{paper.source}",
            "error": "full_text_extracted_but_no_pdf",
        }
    return None


# ---------------------------------------------------------------------------
# Mode 2: Built-in institutional access (ported modules)
# ---------------------------------------------------------------------------

def _try_builtin_institutional(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    profile: Any | None,
) -> dict[str, Any] | None:
    """Try institutional download using ported modules directly."""
    log.info("   [Institutional] Using built-in institutional layer")

    # Step 1: Resolve DOI to publisher URL
    resolved_url = _resolve_doi(doi)
    if not resolved_url:
        log.info("   [Institutional] Could not resolve DOI")
        return None

    if not profile:
        from .publisher_profiles import infer_publisher_profile_from_url
        profile = infer_publisher_profile_from_url(resolved_url)

    # Step 2: Try CARSI if configured
    if config.get("carsi_enabled") and config.get("carsi_idp_name", "").strip():
        result = _try_carsi(doi, resolved_url, output_path, config, profile)
        if result:
            return result

    # Step 3: Try session broker (persistent CloakBrowser session)
    result = _try_session_broker(doi, output_path, config, profile)
    if result:
        return result

    # Step 4: Try browser batch download (CloakBrowser state machine)
    result = _try_browser_batch_download(doi, output_path, config, profile)
    if result:
        return result

    # Step 5: Try publisher PDF via campus gateway (WebVPN/EZproxy)
    if config.get("vpnsci_enabled") or config.get("ezproxy_enabled"):
        result = _try_campus_gateway_pdf(doi, resolved_url, output_path, config, profile)
        if result:
            return result

    # Step 6: Try Elsevier API if configured
    if config.get("elsevier_api_key") and doi.startswith("10.1016/"):
        result = _try_elsevier_api(doi, output_path, config)
        if result:
            return result

    return None


def _resolve_doi(doi: str) -> str | None:
    """Resolve DOI to publisher landing URL."""
    import requests
    try:
        resp = requests.head(
            f"https://doi.org/{doi}",
            allow_redirects=True,
            timeout=15,
            headers={"User-Agent": "scansci-pdf/1.5"},
        )
        if resp.url and resp.url != f"https://doi.org/{doi}":
            return resp.url
    except Exception:
        pass
    return None


def _try_carsi(
    doi: str,
    resolved_url: str,
    output_path: Path,
    config: dict[str, Any],
    profile: Any | None,
) -> dict[str, Any] | None:
    """Try CARSI/Shibboleth federated auth via scansci-pdf's existing CARSI module."""
    try:
        from ..sources.carsi_source import try_carsi
        result = try_carsi(doi, output_path, config)
        if result and result.get("success"):
            log.info("   [Institutional] CARSI download succeeded")
            return result
    except Exception as e:
        log.info(f"   [Institutional] CARSI failed: {e}")
    return None


def _try_session_broker(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    profile: Any | None,
) -> dict[str, Any] | None:
    """Try submitting DOI to a running session broker for the publisher."""
    if not profile:
        return None
    try:
        from .session_broker import broker_is_running, submit_broker_job
    except ImportError:
        return None

    publisher_name = getattr(profile, "name", "")
    if not publisher_name or not broker_is_running(publisher_name):
        return None

    log.info(f"   [Institutional] Session broker active for {publisher_name}, submitting job")
    try:
        result = submit_broker_job(
            publisher=publisher_name,
            records=[{"doi": doi}],
            output_dir=str(output_path.parent),
            institution=config.get("vpnsci_school", ""),
            login_timeout=300,
            pdf_timeout=60,
            post_login_hold=0,
            post_run_hold=0,
            timeout_seconds=120,
        )
        if result and result.get("status") == "success":
            pdf_path = result.get("pdf_path", "")
            if pdf_path and Path(pdf_path).exists():
                src = Path(pdf_path)
                if src != output_path:
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    if output_path.exists():
                        output_path.unlink()
                    src.rename(output_path)
                log.info(f"   [Institutional] Session broker download succeeded")
                return {
                    "success": True, "identifier": doi, "doi": doi,
                    "file": str(output_path),
                    "source": f"institutional:broker:{publisher_name}",
                }
    except Exception as e:
        log.info(f"   [Institutional] Session broker failed: {e}")
    return None


def _try_browser_batch_download(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
    profile: Any | None,
) -> dict[str, Any] | None:
    """Try single-DOI download via PublisherBatchDownloader + CloakBrowser.

    Uses the CloakBrowser state machine: navigate → SSO login → capture PDF → verify.
    This is the most capable built-in path — handles Cloudflare, SSO redirects,
    and publisher-specific PDF extraction.
    """
    if not profile:
        return None

    try:
        from .publisher_batch import DownloadResult, PaperRecord, PublisherBatchDownloader
    except ImportError:
        log.info("   [Institutional] publisher_batch not available")
        return None

    try:
        from ..browser_backend import launch_persistent_context  # noqa: F401
        from ..browser_backend import is_available as _browser_backend_available
        if not _browser_backend_available():
            log.info("   [Institutional] no browser backend available, skipping browser download")
            return None
    except ImportError:
        log.info("   [Institutional] no browser backend available, skipping browser download")
        return None

    record = PaperRecord(doi=doi)
    institution_query = config.get("vpnsci_school", "")
    login_timeout = config.get("browser_login_timeout", 300)

    downloader = PublisherBatchDownloader(
        config=config,
        profile=profile,
        institution_query=institution_query,
        login_timeout_sec=login_timeout,
        pdf_timeout_sec=60,
        post_login_hold_sec=config.get("post_login_hold", 0),
        post_run_hold_sec=0,
    )

    run_dir = output_path.parent / ".browser_runs"
    run_dir.mkdir(parents=True, exist_ok=True)

    log.info(f"   [Institutional] Browser batch download: {doi} via {profile.name}")

    try:
        context = downloader._launch_context()
        try:
            result: DownloadResult = downloader.fetch_one(context, record, run_dir)
        finally:
            try:
                context.close()
            except Exception:
                pass
    except Exception as e:
        log.info(f"   [Institutional] Browser batch download error: {e}")
        return None

    if result.ok and result.pdf_path:
        src = Path(result.pdf_path)
        if src.exists():
            if src != output_path:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output_path.exists():
                    output_path.unlink()
                src.rename(output_path)
            log.info(f"   [Institutional] Browser download succeeded: {result.size_bytes:,} bytes")
            return {
                "success": True, "identifier": doi, "doi": doi,
                "file": str(output_path),
                "source": f"institutional:browser:{profile.name}",
                "full_text_length": result.text_length,
            }

    if result.reason:
        log.info(f"   [Institutional] Browser download: {result.reason}")
    return None


def _try_campus_gateway_pdf(
    doi: str,
    resolved_url: str,
    output_path: Path,
    config: dict[str, Any],
    profile: Any | None,
) -> dict[str, Any] | None:
    """Try downloading PDF through campus gateway (WebVPN/EZproxy)."""
    if not profile:
        return None

    from .publisher_pdf_router import build_pdf_candidates
    candidates = build_pdf_candidates(profile, doi, source_url=resolved_url)
    if not candidates:
        return None

    # Try WebVPN with each PDF candidate
    if config.get("vpnsci_enabled"):
        result = _try_webvpn_candidates(doi, candidates, output_path, config)
        if result:
            return result

    # Try EZProxy with each PDF candidate
    if config.get("ezproxy_enabled"):
        result = _try_ezproxy_candidates(doi, candidates, output_path, config)
        if result:
            return result

    return None


def _try_webvpn_candidates(
    doi: str,
    pdf_candidates: list[str],
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Try downloading PDF candidates through WebVPN."""
    try:
        from ..sources.vpnsci import try_vpnsci
        # try_vpnsci handles its own URL encryption and cookie management
        result = try_vpnsci(doi, output_path, config)
        if result and result.get("success"):
            log.info("   [Institutional] WebVPN download succeeded")
            return result
    except Exception as e:
        log.info(f"   [Institutional] WebVPN failed: {e}")
    return None


def _try_ezproxy_candidates(
    doi: str,
    pdf_candidates: list[str],
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Try downloading PDF candidates through EZProxy."""
    try:
        from ..sources.ezproxy import try_ezproxy
        result = try_ezproxy(doi, output_path, config)
        if result and result.get("success"):
            log.info("   [Institutional] EZProxy download succeeded")
            return result
    except Exception as e:
        log.info(f"   [Institutional] EZProxy failed: {e}")
    return None


def _try_elsevier_api(
    doi: str,
    output_path: Path,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Try Elsevier API for ScienceDirect papers."""
    api_key = config.get("elsevier_api_key", "")
    if not api_key:
        return None

    import requests
    from .extractors import pdf_extractor

    # Try PDF download via API
    inst_token = config.get("elsevier_insttoken", "")
    url = f"https://api.elsevier.com/content/article/doi/{doi}"
    headers = {"X-ELS-APIKey": api_key, "Accept": "application/pdf"}
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200 and resp.content[:5] == b"%PDF-":
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(resp.content)
            if output_path.exists() and output_path.stat().st_size > 5000:
                log.info(f"   [Institutional] Elsevier API PDF: {output_path.stat().st_size:,} bytes")
                return {
                    "success": True, "identifier": doi, "doi": doi,
                    "file": str(output_path), "source": "institutional:elsevier_api",
                }
    except Exception as e:
        log.info(f"   [Institutional] Elsevier API failed: {e}")
    return None
