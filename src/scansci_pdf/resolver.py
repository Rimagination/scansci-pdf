"""Title-to-DOI resolver via OpenAlex search with similarity matching."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Any

from .paperlist import PaperEntry

log = logging.getLogger(__name__)

TITLE_SIMILARITY_THRESHOLD = 0.75

# Keep the resolver's 0.3 s spacing independent from network.fetch's 0.5 s
# domain spacing. The host semaphore is shared process-wide, so concurrent
# batches and custom worker counts cannot exceed OpenAlex's configured cap.
BATCH_RESOLVE_WORKERS = 4
_OPENALEX_WORKS_URL = "https://api.openalex.org/works"


def _title_search_params(title: str) -> dict[str, Any]:
    return {"search": title, "per_page": 5}


def _title_search_headers(config: dict[str, Any]) -> dict[str, str]:
    api_key = str(config.get("openalex_api_key") or "").strip()
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def resolve_title_to_doi(title: str, config: dict[str, Any]) -> str | None:
    """Search OpenAlex by title and return best-matching DOI."""
    from .network import (
        _get_session,
        host_concurrency_acquire,
        host_concurrency_release,
        request_timeout,
    )
    from .search import _reconstruct_abstract

    if not title or len(title) < 10:
        return None

    host_concurrency_acquire(_OPENALEX_WORKS_URL, config)
    try:
        session = _get_session(config)
        resp = session.get(
            _OPENALEX_WORKS_URL,
            params=_title_search_params(title),
            headers=_title_search_headers(config),
            timeout=request_timeout(config),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as exc:
        log.debug("Title search failed for '%s': %s", title[:50], exc)
        return None
    finally:
        host_concurrency_release(_OPENALEX_WORKS_URL, config)

    best_doi = None
    best_score = 0.0
    title_lower = title.lower().strip()

    for work in data.get("results", []):
        result_title = (work.get("title") or "").lower().strip()
        if not result_title:
            continue
        score = SequenceMatcher(None, title_lower, result_title).ratio()
        if score > best_score:
            best_score = score
            doi_raw = work.get("doi", "") or ""
            best_doi = doi_raw.replace("https://doi.org/", "") if doi_raw else None

    if best_score >= TITLE_SIMILARITY_THRESHOLD and best_doi:
        log.info("Resolved title '%s...' → %s (score=%.2f)", title[:40], best_doi, best_score)
        return best_doi

    log.debug("No match for '%s...' (best score=%.2f)", title[:40], best_score)
    return None


class _SpacingGate:
    """Aggregate spacing for a pool of workers, with the sleep outside the lock.

    The serial loop slept `delay` after each round trip, so its achieved rate
    was `1 / (delay + latency)`. Reserving one start slot per `delay` restores
    the intended budget while overlapping the round trips; it does not preserve
    the lower achieved rate. Per-worker sleeps would instead multiply the
    budget by the worker count. Each caller reserves under the lock and sleeps
    after releasing it.

    `clock` and `sleep` are injectable so the reservation order can be tested
    without real time passing.
    """

    def __init__(self, spacing: float, *, clock=time.monotonic, sleep=time.sleep):
        self._spacing = max(0.0, float(spacing))
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_slot = 0.0

    @property
    def spacing(self) -> float:
        return self._spacing

    def wait(self) -> None:
        if self._spacing <= 0:
            return
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self._spacing
        pause = slot - self._clock()
        if pause > 0:
            self._sleep(pause)


def batch_resolve(
    entries: list[PaperEntry],
    config: dict[str, Any],
    delay: float = 0.3,
    *,
    max_workers: int = BATCH_RESOLVE_WORKERS,
) -> dict[str, Any]:
    """Batch resolve: fix DOI format + search by title for missing DOIs.

    The title searches run on `max_workers` threads under one `_SpacingGate`.
    Start reservations retain the intended `1 / delay` budget while round trips
    overlap; `resolve_title_to_doi` separately enforces the process-wide host
    concurrency cap (#72 item 6). Entries are mutated in place, which keeps the
    output order identical to the input order without a reorder step.

    Returns dict with 'entries' (resolved list) and 'stats'.
    """
    total = len(entries)
    already_has_doi = sum(1 for entry in entries if entry.doi)
    no_title = sum(1 for entry in entries if not entry.doi and not entry.title)
    pending = [(index, entry) for index, entry in enumerate(entries) if not entry.doi and entry.title]

    gate = _SpacingGate(delay)
    resolved = [False] * len(pending)

    def _resolve_one(slot: int, entry: PaperEntry) -> None:
        gate.wait()
        doi = resolve_title_to_doi(entry.title, config)
        if doi:
            entry.doi = doi
            resolved[slot] = True

    workers = min(max(1, int(max_workers)), len(pending))
    if workers <= 1:
        for slot, (_, entry) in enumerate(pending):
            _resolve_one(slot, entry)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_resolve_one, slot, entry) for slot, (_, entry) in enumerate(pending)]
            for future in futures:
                future.result()

    resolved_by_title = sum(resolved)
    return {
        "entries": entries,
        "stats": {
            "total": total,
            "already_has_doi": already_has_doi,
            "resolved_by_title": resolved_by_title,
            "unresolvable": no_title + (len(pending) - resolved_by_title),
        },
    }
