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

# `api.openalex.org` is capped at 5 concurrent requests by
# `network._DEFAULT_HOST_CONCURRENCY`. That cap is only enforced inside
# `network.fetch`, and routing the resolver through `fetch` would also inherit
# its 0.5 s domain spacing -- slower than the 0.3 s budget the serial loop
# already spends, which is exactly the trade the maintainer rejected on #72.
# So the worker count is a promise made here instead: 4 stays under the
# published cap by construction.
BATCH_RESOLVE_WORKERS = 4


def _title_search_params(title: str, config: dict[str, Any]) -> dict[str, Any]:
    """Query parameters for the OpenAlex title search.

    Unauthenticated OpenAlex traffic defaults to roughly 1 request/s since the
    mailto polite pool was retired (2026-02), so a concurrent resolver that
    sends neither `mailto` nor `api_key` collects 429s the serial one did not
    (#72 item 6). `email` is the same key `pipeline._enrich_oa` already uses.
    """
    params: dict[str, Any] = {"search": title, "per_page": 5}
    email = str(config.get("email") or "").strip()
    if email:
        params["mailto"] = email
    api_key = str(config.get("openalex_api_key") or "").strip()
    if api_key:
        params["api_key"] = api_key
    return params


def resolve_title_to_doi(title: str, config: dict[str, Any]) -> str | None:
    """Search OpenAlex by title and return best-matching DOI."""
    from .network import _get_session, request_timeout
    from .search import _reconstruct_abstract

    if not title or len(title) < 10:
        return None

    try:
        session = _get_session(config)
        resp = session.get(
            "https://api.openalex.org/works",
            params=_title_search_params(title, config),
            timeout=request_timeout(config),
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception as exc:
        log.debug("Title search failed for '%s': %s", title[:50], exc)
        return None

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

    The serial loop this replaces slept `delay` seconds between requests, so
    the batch spent one request per `delay`. Sleeping per worker instead would
    multiply that rate by the worker count, which is the one thing a
    performance fix must not do to a rate-limited API. Each caller therefore
    reserves a slot under the lock and sleeps until its own slot -- the batch
    keeps the serial rate, only the waiting overlaps.

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

    The title searches run on `max_workers` threads under one `_SpacingGate`,
    so the aggregate request rate is the one the serial loop spent while the
    round trips overlap (#72 item 6). Entries are mutated in place, which keeps
    the output order identical to the input order without a reorder step.

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
