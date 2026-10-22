# cython: language_level=3
"""Tiered parallel racing download engine.

This module implements the multi-source download strategy:
- 5-tier download architecture with tuned timeouts
- Fully parallel cross-tier racing with Event-based instant notification
- Thread-safe result publication and batch orchestration
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Tier definitions with tuned timeouts (seconds)
cdef int TIER_FLASH_TIMEOUT = 4
cdef int TIER_FAST_TIMEOUT = 5
cdef int TIER_OA_TIMEOUT = 8
cdef int TIER_GREY_TIMEOUT = 25
cdef int TIER_WEBVPN_TIMEOUT = 20

# Batch download tuning
cdef double BATCH_STAGGER_DELAY = 0.3
cdef int DEFAULT_BATCH_WORKERS = 10


def run_parallel_race(
    list all_sources,
    str doi,
    object target_dir,
    object output_path,
    dict config,
    bint use_tor,
    int overall_timeout,
    try_source_fn,
    safe_filename_fn,
    logger,
) -> dict | None:
    """Race all sources in parallel across tiers. First success wins.

    Uses a shared result dict with threading.Event for instant notification
    when any source succeeds, even from nested parallel calls.

    Args:
        all_sources: List of (fn, label, tier_label, timeout) tuples
        doi: Paper DOI
        target_dir: Directory for temp files
        output_path: Final output path
        config: Configuration dict
        use_tor: Whether to use Tor
        overall_timeout: Max seconds to wait
        try_source_fn: Function to try a single source
        safe_filename_fn: Function to create safe filename
        logger: Logger instance
    """
    if not all_sources:
        return None

    cdef int n = len(all_sources)

    # Single source - run directly without pool overhead
    if n == 1:
        fn, label, tier_label, timeout = all_sources[0]
        src_output = target_dir / f"{safe_filename_fn(doi)}_{label}.pdf"
        result = try_source_fn(fn, doi, src_output, config, label, use_tor=use_tor)
        if result and result.get("success"):
            _move_to_output(result, output_path)
            return result
        return None

    # Shared state for cross-thread result publication
    result_lock = threading.Lock()
    success_event = threading.Event()
    cancel_event = threading.Event()
    all_done_event = threading.Event()
    done_counter = [0]
    done_counter_lock = threading.Lock()
    shared_result = {"result": None}

    def _try_and_publish(fn, label, src_output):
        if cancel_event.is_set():
            return None
        result = try_source_fn(fn, doi, src_output, config, label, use_tor=use_tor)
        if result and result.get("success"):
            with result_lock:
                if shared_result["result"] is None:
                    shared_result["result"] = (result, label, src_output)
                    cancel_event.set()
                    success_event.set()
        return result

    def _wait_any(timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if success_event.is_set() or all_done_event.is_set():
                return True
            time.sleep(0.05)
        return success_event.is_set() or all_done_event.is_set()

    def _on_future_done(fut):
        with done_counter_lock:
            done_counter[0] += 1
            if done_counter[0] >= n:
                all_done_event.set()

    logger.info(f"   Racing {n} sources across multiple tiers (parallel)...")

    pool = ThreadPoolExecutor(max_workers=n)
    futures = {}
    try:
        for fn, label, tier_label, tier_timeout in all_sources:
            src_output = target_dir / f"{safe_filename_fn(doi)}_{label}.pdf"
            fut = pool.submit(_try_and_publish, fn, label, src_output)
            futures[fut] = (label, src_output)
        for fut in futures:
            fut.add_done_callback(_on_future_done)

        # Wait for first success, all sources done, or the overall timeout.
        # all_done lets a full fast-failure return immediately instead of
        # burning the whole timeout + grace period.
        _wait_any(overall_timeout + 5)

        if shared_result["result"] is not None:
            result, label, src_output = shared_result["result"]
            _move_to_output(result, output_path)
            logger.info(f"   OK {label}")
            return result
        if all_done_event.is_set():
            logger.info("   All sources failed")
            return None

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
        logger.info(f"   Racing timed out after {overall_timeout + 5}s, waiting up to {grace}s for late results...")
        _wait_any(grace)
        if shared_result["result"] is not None:
            result, label, src_output = shared_result["result"]
            _move_to_output(result, output_path)
            logger.info(f"   OK {label} (late)")
            return result
        if all_done_event.is_set():
            logger.info("   All sources failed")
            return None

        # Final scan: check if any source wrote a valid PDF file despite timeout
        for label, src_output in futures.values():
            if src_output.exists():
                _move_to_output({"success": True, "identifier": doi, "doi": doi,
                                 "file": str(src_output), "source": label}, output_path)
                if output_path.exists():
                    logger.info(f"   OK {label} (file scan)")
                    return {"success": True, "identifier": doi, "doi": doi,
                            "file": str(output_path), "source": label}

        logger.info("   All sources failed")
    finally:
        # Never block termination on sources that refuse to return: cancel
        # pending futures and waive stragglers. Their own I/O deadlines
        # (connect/read timeouts, stream deadline in pdf_utils) bound how long
        # an abandoned worker thread can outlive the race.
        alive = [f for f in futures if not f.done()]
        if alive:
            logger.info(f"   Racing finished with {len(alive)} source(s) still running (waived)")
        pool.shutdown(wait=False, cancel_futures=True)
        _cleanup_temps(futures, output_path)

    return None


cdef void _move_to_output(dict result, object output_path):
    """Move downloaded file to final output path."""
    final_path = Path(result.get("file", ""))
    if final_path != output_path and final_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()
        final_path.rename(output_path)
        result["file"] = str(output_path)


cdef void _cleanup_temps(dict futures, object output_path):
    """Remove temporary files from failed downloads."""
    for _, other_path in futures.values():
        if other_path != output_path and other_path.exists():
            try:
                other_path.unlink(missing_ok=True)
            except OSError:
                pass


def build_tiers(
    list publisher_fast,
    list publisher_oa,
    list tier3_libgen,
    list tier4_scihub,
    list tier5_instsci,
    str strategy,
) -> list:
    """Build tier structure based on download strategy.

    Returns list of (sources, label, timeout) tuples.
    """
    if strategy == "legal_only":
        return [
            (publisher_fast, "Flash", TIER_FLASH_TIMEOUT),
            (publisher_oa, "OA", TIER_OA_TIMEOUT),
        ]
    elif strategy == "scihub_only":
        return [(tier4_scihub, "Sci-Hub", TIER_WEBVPN_TIMEOUT)]
    elif strategy == "oa_first":
        return [
            (publisher_fast, "Flash", TIER_FLASH_TIMEOUT),
            (publisher_oa, "OA", TIER_OA_TIMEOUT),
            (tier3_libgen + tier4_scihub, "Grey", TIER_GREY_TIMEOUT),
        ]
    else:
        # "fastest" (default): all tiers race in parallel
        tier2_fast = publisher_oa[:3] if len(publisher_oa) > 3 else publisher_oa
        tier3_oa = publisher_oa[3:] if len(publisher_oa) > 3 else []
        tiers = [
            (publisher_fast, "Flash", TIER_FLASH_TIMEOUT),
            (tier2_fast, "Fast", TIER_FAST_TIMEOUT),
            (tier3_oa, "OA", TIER_OA_TIMEOUT),
            (tier3_libgen + tier4_scihub, "Grey", TIER_GREY_TIMEOUT),
        ]
        if tier5_instsci:
            tiers.append((tier5_instsci, "WebVPN", TIER_WEBVPN_TIMEOUT))
        return tiers


def batch_download(
    list dois,
    object output_dir,
    dict config,
    download_fn,
    int workers = DEFAULT_BATCH_WORKERS,
) -> list:
    """Download multiple papers with staggered parallel execution.

    Deduplicates DOIs, then runs downloads with rate-limiting stagger.
    """
    # Deduplicate preserving order
    cdef list seen = set()
    cdef list unique_dois = []
    for doi in dois:
        if doi not in seen:
            seen.add(doi)
            unique_dois.append(doi)

    cdef list results = []
    cdef double delay = BATCH_STAGGER_DELAY
    stagger_lock = threading.Lock()

    def _download_one(doi):
        # Stagger start to avoid burst requests
        with stagger_lock:
            time.sleep(delay)
        return download_fn(doi, output_dir, config=config)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_download_one, doi): doi for doi in unique_dois}
        for future in as_completed(futures):
            doi = futures[future]
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                results.append({"doi": doi, "success": False, "error": str(e)})

    return results
