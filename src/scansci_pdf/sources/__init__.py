"""Source registry and download orchestration with tiered parallel racing."""

from __future__ import annotations

import hashlib
import inspect
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..cache import cache_get, cache_set
from ..config import load_config, DATA_DIR
from ..identifiers import is_arxiv_identifier, normalize_doi, safe_filename
from ..log import get_logger
from ..pdf_utils import fail
from ..rename import rename_pdf, generate_filename as rename_pdf_generate_filename

# Import compiled core functions if available (Cython .pyd/.so)
try:
    from .._core.racing import (
        run_parallel_race as _run_parallel_race_compiled,
    )
    _HAS_COMPILED_CORE = True
except ImportError:
    _HAS_COMPILED_CORE = False

log = get_logger()

from .arxiv import try_arxiv
from .core_api import try_core
from .crossref import try_crossref_page_scrape
from .europepmc import try_europepmc, try_pmc
from .libgen import try_libgen
from .oa_discovery import try_doaj
from .openalex import try_openalex_oa, try_openalex_content_api
from .publishers import get_publisher_fast_sources
from .scibban import try_scibban
from .scihub import try_scihub
from .semantic_scholar import try_semanticscholar
from .unpaywall import try_unpaywall
from .vpnsci import try_vpnsci
from .ezproxy import try_ezproxy

# Institutional bridge — uses instsci PaperFetcher when available
def _try_institutional_bridge(doi: str, output_path: Path, config: dict) -> dict | None:
    """Lazy-loaded instsci institutional bridge."""
    try:
        from ..institutional.instsci_bridge import try_institutional
        return try_institutional(doi, output_path, config)
    except ImportError:
        return None

# Global semaphore to limit concurrent browser-based sources across all DOI
# Prevents batch_workers × browser_sources_per_DOI explosion of Chrome windows
_browser_semaphore: threading.Semaphore | None = None
_browser_semaphore_lock = threading.Lock()

# Source labels that require launching a browser (CloakBrowser)
_BROWSER_SOURCE_LABELS = frozenset({
    "ElsevierBrowser", "WileyBrowser", "IEEEBrowser", "ACSBrowser",
    "RSCBrowser", "AIPBrowser", "SpringerBrowser", "APSBrowser",
    "TandFBrowser", "IOPBrowser", "OxfordBrowser", "ACMBrowser",
    "NatureBrowser", "ScienceBrowser", "SAGEBrowser", "ASCEBrowser",
    "RoyalSocietyBrowser", "CopernicusDirect",
    "GenericBrowser", "WebVPN", "CARSI", "EZProxy",
})



def _any_institutional_path(config: dict[str, Any]) -> bool:
    """Check if any institutional access is configured."""
    return bool(
        (config.get("carsi_enabled") and config.get("carsi_idp_name", "").strip())
        or (config.get("vpnsci_enabled") and (config.get("vpnsci_school") or config.get("vpnsci_base_url")))
        or (config.get("ezproxy_enabled") and config.get("ezproxy_login_url"))
        or config.get("elsevier_api_key")
    )


def _get_browser_semaphore(config: dict[str, Any]) -> threading.Semaphore:
    """Get or create the global browser concurrency semaphore."""
    global _browser_semaphore
    max_workers = config.get("max_browser_workers", 1)
    if _browser_semaphore is None:
        with _browser_semaphore_lock:
            if _browser_semaphore is None:
                _browser_semaphore = threading.Semaphore(max_workers)
    return _browser_semaphore
from .carsi_source import try_carsi

__all__ = ["download", "batch_download"]

_cleanup_done = False


def _cleanup_stale_files(target_dir: Path) -> None:
    """Remove orphaned .part files and racing temp files from previous runs."""
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True
    if not target_dir.exists():
        return
    count = 0
    for f in target_dir.iterdir():
        if not f.is_file():
            continue
        name = f.name
        # Skip hidden files (like .doi_index.json)
        if name.startswith("."):
            continue
        # Always clean up .part files
        if name.endswith(".part"):
            try:
                f.unlink()
                count += 1
            except OSError:
                pass
            continue
        # Clean up racing temp files: DOI-based identifier + source label suffix
        # e.g. 10_1038_nature12373_Unpaywall.pdf, 10_1016_test_scihub_st.pdf
        # Must start with 10_ (DOI prefix) and have at least 4 segments (10_NNNN_suffix_label)
        if name.endswith(".pdf") and name.startswith("10_"):
            stem = name[:-4]  # remove .pdf
            # Split from the right to get the last segment as the source label
            parts = stem.rsplit("_", 1)
            if len(parts) == 2:
                base, label = parts
                # Verify: base must look like a DOI (10_NNNN_... with at least 4 segments)
                # and label must be a source name (letters, not just a number)
                base_parts = base.split("_")
                if (len(base_parts) >= 3
                        and base_parts[0] == "10"
                        and base_parts[1].isdigit()
                        and len(base_parts[1]) >= 3
                        and any(c.isalpha() for c in label)):
                    try:
                        f.unlink()
                        count += 1
                    except OSError:
                        pass
    if count > 0:
        log.info(f"Cleaned up {count} stale temp files")


def _try_source(
    source_fn: Any, doi: str, output_path: Path, config: dict[str, Any], label: str, use_tor: bool = False
) -> dict[str, Any] | None:
    from .scoring import record_result, classify_error, get_user_advice
    t0 = time.time()

    # Limit concurrency for browser-based sources
    is_browser = label in _BROWSER_SOURCE_LABELS
    sem = _get_browser_semaphore(config) if is_browser else None
    if sem:
        sem.acquire()
    try:
        sig = inspect.signature(source_fn)
        if "use_tor" in sig.parameters:
            result = source_fn(doi, output_path, config, use_tor=use_tor)
        else:
            result = source_fn(doi, output_path, config)
        latency_ms = (time.time() - t0) * 1000
        if result:
            result["doi"] = doi
            result["identifier"] = doi
            if result.get("success"):
                # Check for suspicious PDF (1-page or very small — likely not full text)
                file_path = result.get("file", "")
                if file_path:
                    from ..pdf_utils import is_suspicious_pdf, suspicious_pdf
                    fp = Path(file_path)
                    if fp.exists() and is_suspicious_pdf(fp):
                        log.info(f"   SUSPICIOUS {label}: PDF appears to be a preview/cover page, not full text")
                        record_result(label, False, latency_ms, "suspicious_pdf")
                        return suspicious_pdf(doi, fp, label)
                record_result(label, True, latency_ms)
            else:
                error_type = classify_error(result.get("status_code", 0))
                record_result(label, False, latency_ms, error_type)
        return result
    except Exception as e:
        latency_ms = (time.time() - t0) * 1000
        error_type = classify_error(exception=e)
        # Check if a valid PDF was actually written to disk despite the exception
        # (common with Sci-Hub: browser download succeeds but post-download logic raises)
        from ..pdf_utils import is_pdf_file as _is_pdf
        if output_path.exists() and _is_pdf(output_path):
            log.info(f"   OK {label} (recovered after {error_type})")
            record_result(label, True, latency_ms)
            return {"success": True, "identifier": doi, "doi": doi,
                    "file": str(output_path), "source": label}
        record_result(label, False, latency_ms, error_type)
        advice = get_user_advice(error_type, label)
        log.info(f"   FAIL {label}: {error_type} — {advice}")
        return None
    finally:
        if sem:
            sem.release()


def _run_tier(
    tier_sources: list[tuple[Any, str]],
    tier_label: str,
    timeout_sec: int,
    doi: str,
    target_dir: Path,
    output_path: Path,
    config: dict[str, Any],
    use_tor: bool = False,
) -> dict[str, Any] | None:
    if not tier_sources:
        return None

    if len(tier_sources) == 1:
        fn, label = tier_sources[0]
        log.info(f"   [{tier_label}] Racing 1 sources...")
        src_output = target_dir / f"{safe_filename(doi)}_{label}.pdf"
        try:
            result = _try_source(fn, doi, src_output, config, label, use_tor=use_tor)
            if result and result.get("success"):
                final_path = Path(result.get("file", ""))
                if final_path != output_path and final_path.exists():
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    if output_path.exists():
                        output_path.unlink()
                    final_path.rename(output_path)
                    result["file"] = str(output_path)
                log.info(f"   OK {label}")
                return result
            else:
                log.info(f"   FAIL {label}")
                if src_output.exists():
                    try:
                        src_output.unlink(missing_ok=True)
                    except OSError:
                        pass
        except Exception:
            log.info(f"   FAIL {label}")
        return None

    log.info(f"   [{tier_label}] Racing {len(tier_sources)} sources...")
    with ThreadPoolExecutor(max_workers=len(tier_sources)) as pool:
        futures = {}
        for fn, label in tier_sources:
            src_output = target_dir / f"{safe_filename(doi)}_{label}.pdf"
            futures[pool.submit(_try_source, fn, doi, src_output, config, label, use_tor)] = (label, src_output)
        try:
            for future in as_completed(futures, timeout=timeout_sec):
                label, src_output = futures[future]
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
                    log.info(f"   OK {label}")
                    return result
                else:
                    log.info(f"   FAIL {label}")
                    if src_output.exists():
                        try:
                            src_output.unlink(missing_ok=True)
                        except OSError:
                            pass
        except TimeoutError:
            log.info(f"   [{tier_label}] Timeout after {timeout_sec}s")
    return None


def _build_free_sources(doi: str, config: dict[str, Any]) -> list[tuple[Any, str]]:
    """Build Phase 1 sources: all free/OA/grey sources, sorted by adaptive score.
    
    Respects config['download_strategy']:
      - fastest:     all sources, EMA-sorted (default)
      - scihub_first: all sources, but grey sources sorted first
      - scihub_only:  only SciBban + Sci-Hub + LibGen
      - oa_first:     all sources, but OA sources sorted before grey
      - legal_only:   exclude grey sources (SciBban, Sci-Hub, LibGen)
    """
    from .scoring import sort_sources

    strategy = config.get("download_strategy", "fastest")

    publisher_fast = get_publisher_fast_sources(doi)
    _fast_names = {label for _, label in publisher_fast}

    extra_fast = []
    for fn, label in [
        (try_unpaywall, "Unpaywall"),
        (try_openalex_oa, "OpenAlexOA"),
        (try_semanticscholar, "SemanticScholar"),
    ]:
        if label not in _fast_names:
            extra_fast.append((fn, label))

    legal_sources = publisher_fast + extra_fast
    legal_sources += [(try_doaj, "DOAJ"), (try_crossref_page_scrape, "CrossrefPage")]
    legal_sources += [(try_europepmc, "EuropePMC"), (try_core, "CORE"), (try_pmc, "PMC")]

    if config.get("openalex_api_key"):
        legal_sources.append((try_openalex_content_api, "OpenAlexContent"))

    # Grey sources
    grey_sources: list[tuple[Any, str]] = []
    if config.get("scihub_enabled", False):
        grey_sources.append((try_scibban, "SciBban"))
    grey_sources.append((try_libgen, "LibGen"))
    if config.get("scihub_enabled", False):
        grey_sources.append((try_scihub, "Sci-Hub"))

    if strategy == "scihub_only":
        # Only Sci-Hub (not SciBban, not LibGen)
        return sort_sources([(try_scihub, "Sci-Hub")]) if config.get("scihub_enabled", False) else []
    elif strategy == "grey_only":
        return sort_sources(grey_sources)
    elif strategy == "legal_only":
        return sort_sources(legal_sources)
    elif strategy == "scihub_first":
        # Grey sources first, then legal
        return sort_sources(grey_sources) + sort_sources(legal_sources)
    elif strategy == "oa_first":
        # Legal sources first, then grey
        return sort_sources(legal_sources) + grey_sources
    else:
        # fastest: all sources EMA-sorted
        return sort_sources(legal_sources + grey_sources)


def _build_institutional_sources(doi: str, config: dict[str, Any], *, use_vpnsci: bool = False) -> list[tuple[Any, str]]:
    """Build Phase 2 sources: institutional access only."""
    from .scoring import sort_sources

    sources: list[tuple[Any, str]] = []

    if _any_institutional_path(config):
        sources.append((_try_institutional_bridge, "InstSci"))

    if config.get("carsi_enabled", False) and config.get("carsi_idp_name", "").strip():
        sources.append((try_carsi, "CARSI"))

    if use_vpnsci and config.get("vpnsci_enabled", False):
        sources.append((try_vpnsci, "WebVPN"))

    if use_vpnsci and config.get("ezproxy_enabled", False):
        sources.append((try_ezproxy, "EZProxy"))

    return sort_sources(sources)


def _run_tiers_parallel(
    tiers: list[tuple[list[tuple[Any, str]], str, int]],
    doi: str,
    target_dir: Path,
    output_path: Path,
    config: dict[str, Any],
    use_tor: bool,
    overall_timeout: int,
) -> dict[str, Any] | None:
    """Race all tiers in parallel. First successful tier wins.

    Uses a shared result dict so that any source thread can publish its
    success immediately, even if it's running inside a nested parallel
    call (like Sci-Hub domain racing).
    """
    # Delegate to compiled racing engine if available
    if _HAS_COMPILED_CORE:
        all_sources = []
        for tier_sources, tier_label, tier_timeout in tiers:
            for fn, label in tier_sources:
                all_sources.append((fn, label, tier_label, tier_timeout))
        return _run_parallel_race_compiled(
            all_sources, doi, target_dir, output_path, config,
            use_tor, overall_timeout, _try_source, safe_filename, log,
        )
    if not tiers:
        return None

    # Flatten all sources across tiers with their labels
    all_sources: list[tuple[Any, str, str, int]] = []  # (fn, label, tier_label, timeout)
    for tier_sources, tier_label, tier_timeout in tiers:
        for fn, label in tier_sources:
            all_sources.append((fn, label, tier_label, tier_timeout))

    if not all_sources:
        return None

    # If only one source, run directly
    if len(all_sources) == 1:
        fn, label, tier_label, timeout = all_sources[0]
        src_output = target_dir / f"{safe_filename(doi)}_{label}.pdf"
        result = _try_source(fn, doi, src_output, config, label, use_tor=use_tor)
        if result and result.get("success"):
            final_path = Path(result.get("file", ""))
            if final_path != output_path and final_path.exists():
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output_path.exists():
                    output_path.unlink()
                final_path.rename(output_path)
                result["file"] = str(output_path)
            return result
        return None

    # Shared result: any thread can publish success here, signaled via Event
    result_lock = threading.Lock()
    success_event = threading.Event()
    cancel_event = threading.Event()
    shared_result: dict[str, Any] = {"result": None}

    def _try_and_publish(fn, label, src_output):
        # Skip if another source already succeeded
        if cancel_event.is_set():
            return None
        result = _try_source(fn, doi, src_output, config, label, use_tor=use_tor)
        if result and result.get("success"):
            with result_lock:
                if shared_result["result"] is None:
                    shared_result["result"] = (result, label, src_output)
                    cancel_event.set()
                    success_event.set()
        return result

    log.info(f"   Racing {len(all_sources)} sources across {len(tiers)} tiers (parallel)...")
    pool = ThreadPoolExecutor(max_workers=len(all_sources))
    futures = {}
    try:
        for fn, label, tier_label, tier_timeout in all_sources:
            src_output = target_dir / f"{safe_filename(doi)}_{label}.pdf"
            futures[pool.submit(_try_and_publish, fn, label, src_output)] = (label, src_output)

        # Wait for first success or overall timeout - instant notification via Event
        success_event.wait(timeout=overall_timeout + 5)

        if shared_result["result"] is not None:
            result, label, src_output = shared_result["result"]
            final_path = Path(result.get("file", ""))
            if final_path != output_path and final_path.exists():
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output_path.exists():
                    output_path.unlink()
                final_path.rename(output_path)
                result["file"] = str(output_path)
            log.info(f"   OK {label}")
            return result

        # Timeout reached — give late-finishing threads a grace period.
        # Visible browser login can take 60-300s (browser launch + SSO + redirect),
        # so we wait much longer if browser-based sources are in the pool.
        has_browser = any("Browser" in lbl for _, lbl, _, _ in all_sources)
        has_carsi = any("CARSI" in lbl for _, lbl, _, _ in all_sources)
        if has_carsi:
            grace = 300
        elif has_browser:
            grace = 180
        else:
            grace = 15
        log.info(f"   Racing timed out after {overall_timeout + 5}s, waiting up to {grace}s for late results...")
        success_event.wait(timeout=grace)
        if shared_result["result"] is not None:
            result, label, src_output = shared_result["result"]
            final_path = Path(result.get("file", ""))
            if final_path != output_path and final_path.exists():
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output_path.exists():
                    output_path.unlink()
                final_path.rename(output_path)
                result["file"] = str(output_path)
            log.info(f"   OK {label} (late)")
            return result

        # Final scan: check if any source wrote a valid PDF file despite timeout
        from ..pdf_utils import is_pdf_file, is_suspicious_pdf, suspicious_pdf
        for label, src_output in futures.values():
            if src_output.exists() and is_pdf_file(src_output):
                if is_suspicious_pdf(src_output):
                    log.info(f"   SUSPICIOUS {label} (file scan): skipping suspicious PDF")
                    continue
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output_path.exists():
                    output_path.unlink()
                src_output.rename(output_path)
                log.info(f"   OK {label} (file scan)")
                return {"success": True, "identifier": doi, "doi": doi,
                        "file": str(output_path), "source": label}

        log.info(f"   All sources failed")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        # Cleanup temp files
        for _, other_path in futures.values():
            if other_path != output_path and other_path.exists():
                try:
                    other_path.unlink(missing_ok=True)
                except OSError:
                    pass

    return None


def _update_doi_index(target_dir: Path, doi: str, file_path: Path) -> None:
    """Update the DOI→file index for dedup."""
    doi_index = target_dir / ".doi_index.json"
    try:
        idx: dict[str, str] = {}
        if doi_index.exists():
            idx = json.loads(doi_index.read_text(encoding="utf-8"))
        idx[doi] = str(file_path)
        doi_index.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _auto_rename(result: dict[str, Any], identifier: str, config: dict[str, Any], doi: str = "", target_dir: Path | None = None) -> None:
    """Auto-rename downloaded PDF based on metadata."""
    if not config.get("auto_rename", True):
        return
    file_path = Path(result.get("file", ""))
    if not file_path.exists():
        return
    # Use cached metadata or fetch from Crossref
    from ..citation import fetch_metadata
    _doi = doi or result.get("doi", identifier)
    metadata = fetch_metadata(_doi, config)
    if metadata:
        new_path = rename_pdf(file_path, metadata)
        if new_path and new_path != file_path:
            result["file"] = str(new_path)
            result["renamed"] = True
            log.info(f"   Renamed: {file_path.name} -> {new_path.name}")
            # Update DOI→file index for dedup
            if target_dir and _doi:
                _update_doi_index(target_dir, _doi, new_path)
        else:
            log.info(f"   Kept original name: {file_path.name}")
    else:
        log.info(f"   No metadata for rename, keeping: {file_path.name}")


def download(
    identifier: str,
    output_dir: str | Path | None = None,
    *,
    scihub_enabled: bool | None = None,
    use_tor: bool = False,
    use_vpnsci: bool = False,
    bibtex: bool = False,
    rename: bool = True,
    _institutional: bool = True,
    strategy: str | None = None,
) -> dict[str, Any]:
    config = load_config()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if scihub_enabled is not None:
        config["scihub_enabled"] = scihub_enabled

    if strategy is not None:
        config["download_strategy"] = strategy

    target_dir = Path(output_dir) if output_dir else Path(config["output_dir"])
    target_dir.mkdir(parents=True, exist_ok=True)

    _cleanup_stale_files(target_dir)

    identifier = identifier.strip()
    output_path = target_dir / f"{safe_filename(identifier)}.pdf"

    cached = cache_get(identifier, config)
    if cached and cached.get("success"):
        cached_file = Path(cached.get("file", ""))
        if cached_file.exists():
            cached["cached"] = True
            if bibtex:
                from ..bibtex import fetch_bibtex
                cached["bibtex"] = fetch_bibtex(identifier, config)
            return cached

    # Scan output dir for existing file with same rename pattern
    doi = normalize_doi(identifier) if not is_arxiv_identifier(identifier) else identifier

    # Validate DOI before attempting download
    if not is_arxiv_identifier(identifier):
        from ..identifiers import validate_doi
        valid, msg = validate_doi(doi)
        if not valid:
            log.info(f"   DOI validation failed: {msg}")
            return {"success": False, "identifier": identifier, "doi": doi, "error": f"Invalid DOI: {msg}"}

    # Scan output dir for any PDF already downloaded for this DOI.
    # Uses a DOI→file index to catch renamed files reliably.
    doi_index = target_dir / ".doi_index.json"
    if doi_index.exists():
        try:
            idx = json.loads(doi_index.read_text(encoding="utf-8"))
            entry = idx.get(doi)
            if entry:
                candidate = Path(entry)
                if candidate.exists():
                    log.info(f"   Found existing file (index): {candidate.name}")
                    result = {
                        "success": True, "identifier": identifier,
                        "doi": doi, "file": str(candidate),
                        "source": "local_cache", "cached": True,
                    }
                    cache_set(identifier, result, config)
                    return result
                else:
                    # Stale entry — file was deleted
                    del idx[doi]
                    doi_index.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    from ..citation import fetch_metadata
    metadata = fetch_metadata(doi, config)
    if metadata:
        expected_name = rename_pdf_generate_filename(metadata)
        if expected_name:
            for suffix in ("", "_1", "_2", "_3", "_4", "_5", "_6", "_7", "_8", "_9"):
                candidate = target_dir / f"{expected_name}{suffix}.pdf"
                if candidate.exists():
                    log.info(f"   Found existing file: {candidate.name}")
                    result = {
                        "success": True, "identifier": identifier,
                        "doi": doi, "file": str(candidate),
                        "source": "local_cache", "cached": True,
                    }
                    cache_set(identifier, result, config)
                    return result

    log.info(f"ScanSci PDF - {identifier}")

    if is_arxiv_identifier(identifier):
        log.info("   [L0] arXiv direct")
        result = try_arxiv(identifier, output_path, config)
        if result:
            _update_doi_index(target_dir, identifier, Path(result.get("file", "")))
            if rename:
                _auto_rename(result, identifier, config, doi=identifier, target_dir=target_dir)
            cache_set(identifier, result, config)
            if bibtex:
                from ..bibtex import fetch_bibtex
                result["bibtex"] = fetch_bibtex(identifier, config)
            return result
        return fail(identifier, "arXiv PDF not available")

    doi = normalize_doi(identifier)

    # Phase 1: Free sources (OA + grey) — parallel race
    free_sources = _build_free_sources(doi, config)
    if free_sources:
        result = _run_tiers_parallel(
            [(free_sources, "Free", 15)], doi, target_dir, output_path, config, use_tor, 15
        )
        if result:
            _update_doi_index(target_dir, doi, Path(result.get("file", "")))
            if rename:
                _auto_rename(result, identifier, config, doi=doi, target_dir=target_dir)
            cache_set(identifier, result, config)
            if bibtex:
                from ..bibtex import fetch_bibtex
                result["bibtex"] = fetch_bibtex(doi, config)
            return result

    # Phase 2: Institutional access — only when Phase 1 failed
    # Skip institutional fallback for grey_only/scihub_only strategy
    if _institutional and config.get("download_strategy") not in ("scihub_only", "grey_only"):
        inst_sources = _build_institutional_sources(doi, config, use_vpnsci=use_vpnsci)
        if inst_sources:
            log.info("   Phase 1 failed, trying institutional access...")
            result = _run_tiers_parallel(
                [(inst_sources, "Institutional", 30)], doi, target_dir, output_path, config, use_tor, 30
            )
            if result:
                _update_doi_index(target_dir, doi, Path(result.get("file", "")))
                if rename:
                    _auto_rename(result, identifier, config, doi=doi, target_dir=target_dir)
                cache_set(identifier, result, config)
                if bibtex:
                    from ..bibtex import fetch_bibtex
                    result["bibtex"] = fetch_bibtex(doi, config)
                return result

    # Late capture: wait briefly for browser downloads that complete after race timeout,
    # then scan for any PDFs that were saved to disk by racing threads.
    import time as _time
    _time.sleep(2)  # grace period for browser threads to finish writing
    for p in target_dir.glob(f"{safe_filename(identifier)}*.pdf"):
        if p.stat().st_size > 5000:
            result = {
                "success": True, "identifier": identifier, "doi": doi,
                "file": str(p), "source": "late_capture",
            }
            cache_set(identifier, result, config)
            if rename:
                _auto_rename(result, identifier, config, doi=doi, target_dir=target_dir)
            if bibtex:
                from ..bibtex import fetch_bibtex
                result["bibtex"] = fetch_bibtex(identifier, config)
            return result

    # Build actionable guidance based on what was tried
    guidance = _build_failure_guidance(doi, config)

    # Check if browser strategy detected a specific error
    try:
        from ..publisher_strategies import get_last_error
        error_type, error_action = get_last_error()
    except Exception:
        error_type, error_action = "", ""

    hint: dict[str, Any] = {"manual_url": f"https://sci-hub.mksa.top/{doi}", "guidance": guidance}

    reason = "no PDF found"
    if error_type == "paywall":
        reason = "paywall — requires institutional access"
    elif error_type == "cloudflare_blocked":
        reason = "Cloudflare anti-bot protection blocked access"

    # Add agent hint for paywall - clear instruction on what to do next
    if error_type == "paywall" or error_action == "login_required":
        result = fail(
            identifier,
            reason,
            hint,
            error_type=error_type or "paywall",
            action=error_action or "login_required",
        )
        result["agent_hint"] = (
            f"此论文需要机构登录才能下载。请运行 scansci_pdf_login(identifier=\"{doi}\") "
            "打开浏览器让用户登录机构账号，登录后关闭浏览器，然后重试下载。"
        )
    else:
        result = fail(
            identifier,
            reason,
            hint,
            error_type=error_type or "not_found",
            action=error_action or "",
        )
    result["source"] = "none"
    return result


def _build_failure_guidance(doi: str, config: dict[str, Any]) -> list[str]:
    """Build actionable guidance when all download sources fail."""
    import os
    tips = []

    # Check if browser strategy detected a paywall
    try:
        from ..publisher_strategies import get_last_error
        error_type, _ = get_last_error()
    except Exception:
        error_type = ""

    if error_type == "paywall":
        tips.append("此论文需要机构订阅才能下载")
        tips.append(f"→ 运行 scansci_pdf_login(identifier=\"{doi}\") 打开浏览器登录")
        tips.append("→ 在浏览器中点击 'Access through your institution' 选择你的机构")
        tips.append("→ 登录后关闭浏览器，cookies 自动保存")
        tips.append("→ 重新运行下载命令即可")
    elif error_type == "cloudflare_blocked":
        tips.append("Cloudflare 防护阻止了访问")
        tips.append("→ 安装 CloakBrowser (pip install cloakbrowser) 绕过反爬")
        tips.append("→ 或配置代理: scansci_pdf config_set network_proxy \"socks5://127.0.0.1:1080\"")
    elif error_type == "browser_unavailable":
        tips.append("CloakBrowser 不可用，无法使用浏览器下载策略")
        tips.append("→ 运行: pip install cloakbrowser")

    # Check scansci-pdf proxy config
    cfg_proxy = config.get("network_proxy", "")
    env_proxy = os.environ.get("SCANSCI_PDF_PROXY", "")

    if cfg_proxy or env_proxy:
        active = env_proxy or cfg_proxy
        tips.append(f"当前代理: {active} — 如果 Sci-Hub 仍不通，尝试更换代理地址")
    else:
        # Check if system has proxy that scansci-pdf ignores
        sys_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or ""
        if sys_proxy:
            tips.append(f"检测到系统代理 {sys_proxy}，但 scansci-pdf 未使用")
            tips.append(f"→ 运行: scansci-pdf config_set network_proxy \"{sys_proxy}\"")
        else:
            tips.append("未配置代理 — 如果网络受限，运行: scansci-pdf config_set network_proxy \"socks5://127.0.0.1:1080\"")

    # Check Tor
    try:
        from ..tor import check_tor_circuit
        if not check_tor_circuit(config):
            tips.append("Tor 未运行 → 运行: scansci-pdf tor_start（匿名访问 Sci-Hub/LibGen）")
    except Exception:
        pass

    # Check WebVPN
    if not config.get("vpnsci_enabled"):
        tips.append("有高校账号？运行: scansci-pdf config_set vpnsci_enabled true")

    # Manual fallback
    tips.append(f"手动下载: https://sci-hub.mksa.top/{doi}")

    # Network diagnostic
    tips.append("运行网络诊断: scansci-pdf network_diagnose")

    return tips


def _get_progress_file(batch_id: str) -> Path:
    """Get path to batch progress file."""
    return DATA_DIR / "batch_progress" / f"{batch_id}.jsonl"


_progress_lock = threading.Lock()


def _save_progress(batch_id: str, identifier: str, result: dict[str, Any]) -> None:
    """Append a single result to the progress file (thread-safe)."""
    progress_file = _get_progress_file(batch_id)
    progress_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "identifier": identifier,
        "success": result.get("success", False),
        "source": result.get("source", "none"),
        "file": result.get("file", ""),
        "doi": result.get("doi", ""),
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with _progress_lock:
        with progress_file.open("a", encoding="utf-8") as f:
            f.write(line)


def _load_progress(batch_id: str) -> dict[str, dict[str, Any]]:
    """Load completed results from progress file. Returns {identifier: result}."""
    progress_file = _get_progress_file(batch_id)
    completed: dict[str, dict[str, Any]] = {}
    if not progress_file.exists():
        return completed
    try:
        with progress_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                ident = entry.get("identifier", "")
                if ident and entry.get("success"):
                    completed[ident] = entry
    except Exception:
        pass
    return completed


def _clear_progress(batch_id: str) -> None:
    """Remove progress file after successful completion."""
    progress_file = _get_progress_file(batch_id)
    if progress_file.exists():
        try:
            progress_file.unlink()
        except OSError:
            pass


def _publisher_result_to_standard(
    result: Any, doi: str, output_dir: Path, publisher_name: str
) -> dict[str, Any]:
    """Convert a PublisherBatchDownloader DownloadResult to the standard result format."""
    if result.ok and result.pdf_path:
        src = Path(result.pdf_path)
        dst = output_dir / f"{safe_filename(doi)}.pdf"
        try:
            if src != dst:
                output_dir.mkdir(parents=True, exist_ok=True)
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        except OSError:
            dst = src
        return {
            "success": True, "doi": doi, "identifier": doi,
            "file": str(dst), "source": f"institutional:browser:{publisher_name}",
            "full_text_length": result.text_length,
        }
    return fail(doi, result.reason or "institutional download failed")


def _batch_institutional_phase(
    failed_dois: list[str],
    output_dir: Path,
    config: dict[str, Any],
    batch_id: str,
    results_map: dict[str, dict[str, Any]],
) -> None:
    """Phase 2 for batch downloads: group DOIs by publisher, batch-download per group.

    Modifies results_map in-place with successful results.
    """
    try:
        from ..institutional.publisher_profiles import infer_publisher_profile
        from ..institutional.publisher_batch import DownloadResult, PaperRecord, PublisherBatchDownloader
    except ImportError:
        log.info("   [Batch] publisher_batch not available, skipping institutional phase")
        return

    try:
        from cloakbrowser import launch_persistent_context  # noqa: F401
    except ImportError:
        log.info("   [Batch] cloakbrowser not installed, skipping institutional phase")
        return

    # Group DOIs by publisher profile
    grouped: dict[str, list[str]] = {}  # profile_name -> [doi, ...]
    ungrouped: list[str] = []
    for doi in failed_dois:
        profile = infer_publisher_profile(doi)
        if profile:
            grouped.setdefault(profile.name, []).append(doi)
        else:
            ungrouped.append(doi)

    if not grouped and not ungrouped:
        return

    institution_query = config.get("vpnsci_school", "")
    login_timeout = config.get("browser_login_timeout", 300)

    # Process each publisher group
    for profile_name, dois in grouped.items():
        profile = infer_publisher_profile(dois[0])
        if not profile:
            ungrouped.extend(dois)
            continue

        log.info(f"   [Batch] Phase 2: {len(dois)} DOIs via {profile_name} (one login)")

        records = [PaperRecord(doi=doi) for doi in dois]
        run_dir = output_dir / ".publisher_runs" / profile_name
        run_dir.mkdir(parents=True, exist_ok=True)

        downloader = PublisherBatchDownloader(
            config=config,
            profile=profile,
            institution_query=institution_query,
            login_timeout_sec=login_timeout,
            pdf_timeout_sec=60,
            post_login_hold_sec=config.get("post_login_hold", 0),
            post_run_hold_sec=0,
        )

        try:
            summary = downloader.run_records(records, run_dir, retry_failed=True)
        except Exception as e:
            log.info(f"   [Batch] {profile_name} batch failed: {e}")
            ungrouped.extend(dois)
            continue

        # Map DOIs to results from the manifest
        manifest_path = run_dir / "complete" / "manifest.json"
        if manifest_path.exists():
            try:
                import json
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                doi_to_manifest = {entry.get("doi", ""): entry for entry in manifest}
                for doi in dois:
                    entry = doi_to_manifest.get(doi, {})
                    if entry.get("status") == "success":
                        pdf_path = entry.get("pdf_path", "")
                        if pdf_path and Path(pdf_path).exists():
                            result = _publisher_result_to_standard(
                                type("R", (), {"ok": True, "pdf_path": pdf_path, "text_length": 0})(),
                                doi, output_dir, profile_name,
                            )
                        else:
                            result = fail(doi, "PDF not found after batch download")
                    else:
                        result = fail(doi, entry.get("reason", "institutional download failed"))
                    results_map[doi] = result
                    _save_progress(batch_id, doi, result)
            except Exception as e:
                log.info(f"   [Batch] Failed to parse manifest for {profile_name}: {e}")
                ungrouped.extend(dois)
        else:
            log.info(f"   [Batch] No manifest found for {profile_name}")
            ungrouped.extend(dois)

    # Fallback: ungrouped DOIs get per-DOI institutional access
    for doi in ungrouped:
        if doi in results_map and results_map[doi].get("success"):
            continue
        # Check if a PDF already exists on disk (from prior run, doi_index, or partial download)
        safe_name = safe_filename(doi)
        existing = None
        for p in output_dir.glob(f"{safe_name}*.pdf"):
            if p.stat().st_size > 5000:
                existing = p
                break
        if existing:
            result = {
                "success": True, "doi": doi, "identifier": doi,
                "file": str(existing), "source": "local_cache",
            }
            results_map[doi] = result
            _save_progress(batch_id, doi, result)
            continue
        log.info(f"   [Batch] Phase 2 fallback: {doi}")
        result = download(doi, output_dir, scihub_enabled=config.get("scihub_enabled", True), use_vpnsci=True, _institutional=True)
        results_map[doi] = result
        _save_progress(batch_id, doi, result)


def batch_download(
    identifiers: list[str],
    output_dir: str | Path | None = None,
    *,
    scihub_enabled: bool | None = None,
    use_tor: bool = False,
    use_vpnsci: bool = False,
    progress_callback: Any = None,
    batch_id: str | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    config = load_config()
    workers = config.get("batch_workers", 5)

    # Duplicate detection via DOI normalization
    seen_dois: set[str] = set()
    unique_identifiers: list[str] = []
    skipped_duplicates = 0
    for ident in identifiers:
        normalized = normalize_doi(ident.strip()) if not is_arxiv_identifier(ident) else ident.strip()
        if normalized.lower() in seen_dois:
            skipped_duplicates += 1
            continue
        seen_dois.add(normalized.lower())
        unique_identifiers.append(ident)

    if skipped_duplicates > 0:
        log.info(f"Skipped {skipped_duplicates} duplicate identifiers")

    # Auto-generate batch_id from identifiers if not provided
    if not batch_id:
        import hashlib as _hashlib
        batch_id = _hashlib.md5("|".join(sorted(unique_identifiers)).encode()).hexdigest()[:12]

    # Load previous progress for resume
    completed_map: dict[str, dict[str, Any]] = {}
    if resume:
        completed_map = _load_progress(batch_id)
        if completed_map:
            log.info(f"Resuming batch {batch_id}: {len(completed_map)} already completed")

    # Filter out already-completed identifiers
    pending_identifiers = [i for i in unique_identifiers if i not in completed_map]
    skipped_completed = len(unique_identifiers) - len(pending_identifiers)

    if not pending_identifiers:
        log.info("All items already completed")
        all_results = [completed_map[i] for i in unique_identifiers]
        succeeded = sum(1 for r in all_results if r.get("success"))
        return {
            "total": len(identifiers),
            "unique": len(unique_identifiers),
            "skipped_duplicates": skipped_duplicates,
            "skipped_completed": skipped_completed,
            "succeeded": succeeded,
            "failed": len(unique_identifiers) - succeeded,
            "results": all_results,
            "failed_dois": [i for i in unique_identifiers if not completed_map.get(i, {}).get("success")],
            "batch_id": batch_id,
        }

    # Pre-validate DOIs concurrently
    from ..identifiers import validate_doi
    valid_identifiers: list[str] = []
    invalid_results: list[dict[str, Any]] = []
    arxiv_ids = [i for i in pending_identifiers if is_arxiv_identifier(i)]
    doi_ids = [i for i in pending_identifiers if not is_arxiv_identifier(i)]

    if doi_ids:
        log.info(f"Batch {batch_id}: validating {len(doi_ids)} DOIs...")
        with ThreadPoolExecutor(max_workers=min(10, len(doi_ids))) as pool:
            futures = {pool.submit(validate_doi, normalize_doi(i)): i for i in doi_ids}
            for future in as_completed(futures, timeout=60):
                ident = futures[future]
                try:
                    valid, msg = future.result()
                except Exception:
                    valid, msg = True, "validation error"
                if valid:
                    valid_identifiers.append(ident)
                else:
                    log.info(f"   SKIP {ident}: {msg}")
                    r = fail(ident, f"Invalid DOI: {msg}")
                    invalid_results.append(r)
                    _save_progress(batch_id, ident, r)

    valid_identifiers.extend(arxiv_ids)
    pending_identifiers = valid_identifiers

    if invalid_results:
        log.info(f"Batch {batch_id}: {len(invalid_results)} invalid DOIs skipped")

    if not pending_identifiers:
        log.info("No valid identifiers to download")
        all_results = [completed_map.get(i) or fail(i, "invalid") for i in unique_identifiers]
        succeeded = sum(1 for r in all_results if r and r.get("success"))
        return {
            "total": len(identifiers),
            "unique": len(unique_identifiers),
            "skipped_duplicates": skipped_duplicates,
            "skipped_completed": skipped_completed,
            "skipped_invalid": len(invalid_results),
            "succeeded": succeeded,
            "failed": len(unique_identifiers) - succeeded,
            "results": all_results,
            "batch_id": batch_id,
        }

    log.info(f"Batch {batch_id}: downloading {len(pending_identifiers)} items ({skipped_completed} skipped, {len(invalid_results)} invalid)")

    delay_lock = threading.Lock()
    last_download_time = [0.0]
    delay_between = float(config.get("batch_stagger_seconds", 0.3))
    total = len(pending_identifiers)
    completed_count = [0]
    num_invalid = len(invalid_results)

    def _staggered_download(ident: str) -> dict[str, Any]:
        with delay_lock:
            elapsed = time.time() - last_download_time[0]
            if elapsed < delay_between:
                time.sleep(delay_between - elapsed)
            last_download_time[0] = time.time()
        return download(ident, output_dir, scihub_enabled=scihub_enabled, use_tor=use_tor, use_vpnsci=use_vpnsci, _institutional=False)

    results: list[dict[str, Any] | None] = [None] * total
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {pool.submit(_staggered_download, ident): i for i, ident in enumerate(pending_identifiers)}
        try:
            for future in as_completed(future_to_idx, timeout=600):
                idx = future_to_idx[future]
                try:
                    result = future.result()
                except Exception:
                    result = fail(pending_identifiers[idx], "download exception")
                results[idx] = result

                # Save progress immediately
                _save_progress(batch_id, pending_identifiers[idx], result)

                completed_count[0] += 1
                if progress_callback:
                    try:
                        progress_callback(
                            completed_count[0] + skipped_completed + num_invalid,
                            len(unique_identifiers),
                            pending_identifiers[idx],
                            result,
                        )
                    except Exception:
                        pass
        except TimeoutError:
            log.info(f"Batch {batch_id}: timeout after 600s")

    for i, r in enumerate(results):
        if r is None:
            results[i] = fail(pending_identifiers[i], "timeout or incomplete")

    # Phase 2: Publisher-grouped institutional download for failed DOIs
    phase1_results = dict(zip(pending_identifiers, results))
    failed_phase1 = [doi for doi, r in phase1_results.items() if r and not r.get("success")]
    if failed_phase1:
        log.info(f"Batch {batch_id}: Phase 1 failed for {len(failed_phase1)} DOIs, starting Phase 2...")
        _batch_institutional_phase(failed_phase1, Path(config["output_dir"]) if not output_dir else Path(output_dir), config, batch_id, phase1_results)
        # Update results list with Phase 2 outcomes from progress file
        phase2_progress = _load_progress(batch_id)
        for i, ident in enumerate(pending_identifiers):
            if ident in phase2_progress:
                results[i] = phase2_progress[ident]

    # Reload progress to include newly-saved invalid results
    final_map = _load_progress(batch_id)

    # Build a lookup from pending_identifiers → download results
    pending_results = dict(zip(pending_identifiers, results))

    # Merge completed_map with new results (dict lookup, no fragile index counter)
    all_results = []
    for ident in unique_identifiers:
        if ident in final_map:
            all_results.append(final_map[ident])
        elif ident in completed_map:
            all_results.append(completed_map[ident])
        elif ident in pending_results:
            all_results.append(pending_results[ident])
        else:
            all_results.append(fail(ident, "missing result"))

    succeeded = sum(1 for r in all_results if r and r.get("success"))
    failed_dois = [r["identifier"] for r in all_results if r and not r.get("success")]

    # Clean up progress file if all succeeded
    if not failed_dois:
        _clear_progress(batch_id)

    return {
        "total": len(identifiers),
        "unique": len(unique_identifiers),
        "skipped_duplicates": skipped_duplicates,
        "skipped_completed": skipped_completed,
        "skipped_invalid": len(invalid_results),
        "succeeded": succeeded,
        "failed": len(failed_dois),
        "results": all_results,
        "failed_dois": failed_dois,
        "batch_id": batch_id,
    }