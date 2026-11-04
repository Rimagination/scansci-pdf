"""Batch title resolution: overlap round trips within explicit API boundaries.

#72 item 6. Start reservations retain the intended aggregate spacing while a
process-wide semaphore caps actual OpenAlex requests across batches. Entries
keep their input order and stats, and credentials stay out of query strings.
"""

from __future__ import annotations

import threading
import time

import pytest

from scansci_pdf.paperlist import PaperEntry
from scansci_pdf import resolver


def _entry(title: str = "", doi: str | None = None) -> PaperEntry:
    return PaperEntry(title=title, doi=doi)


# --------------------------------------------------------------------------- #
# 1. the gate keeps one aggregate spacing, whatever the worker count
# --------------------------------------------------------------------------- #


def test_spacing_gate_keeps_the_serial_rate_when_called_in_sequence():
    state = {"time": 100.0, "slept": []}

    def clock() -> float:
        return state["time"]

    def sleep(seconds: float) -> None:
        state["slept"].append(round(seconds, 3))
        state["time"] += seconds

    gate = resolver._SpacingGate(0.3, clock=clock, sleep=sleep)

    for _ in range(4):
        gate.wait()

    # The first request goes immediately, the next three wait one spacing each:
    # four requests in 0.9 s is the rate the serial loop already spent.
    assert state["slept"] == [0.3, 0.3, 0.3]
    assert state["time"] == pytest.approx(100.9)


def test_spacing_gate_reserves_distinct_slots_across_threads():
    """Two workers that wake together must still be one spacing apart."""
    state = {"time": 1000.0, "slept": []}
    state_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def clock() -> float:
        return state["time"]

    def sleep(seconds: float) -> None:
        with state_lock:
            state["slept"].append(round(seconds, 3))
            state["time"] += seconds

    gate = resolver._SpacingGate(0.5, clock=clock, sleep=sleep)

    def worker() -> None:
        barrier.wait()
        gate.wait()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Exactly one of the two callers waits; if the slot were not reserved under
    # the lock both would see the same slot and neither (or both) would sleep.
    assert state["slept"] == [0.5]


def test_spacing_gate_is_a_no_op_when_delay_is_zero():
    gate = resolver._SpacingGate(0.0, clock=lambda: 0.0, sleep=lambda _s: 1 / 0)
    gate.wait()


# --------------------------------------------------------------------------- #
# 2. the batch keeps the entries, their order, and the stats
# --------------------------------------------------------------------------- #


def test_batch_resolve_preserves_input_order_and_stats(monkeypatch):
    seen: list[str] = []
    seen_lock = threading.Lock()

    def fake_resolve(title: str, config):
        with seen_lock:
            seen.append(title)
        return None if "unresolvable" in title else "10.1000/resolved"

    monkeypatch.setattr(resolver, "resolve_title_to_doi", fake_resolve)

    entries = [
        _entry("already has a doi", doi="10.1000/existing"),
        _entry("a resolvable title here"),
        _entry("an unresolvable title here"),
        _entry(""),  # no title at all
        _entry("another resolvable title here"),
    ]

    result = resolver.batch_resolve(entries, {}, delay=0)

    assert [entry.doi for entry in result["entries"]] == [
        "10.1000/existing",
        "10.1000/resolved",
        None,
        None,
        "10.1000/resolved",
    ]
    assert result["stats"] == {
        "total": 5,
        "already_has_doi": 1,
        "resolved_by_title": 2,
        "unresolvable": 2,
    }
    # Entries are mutated in place, so the resolver never needs a reorder step.
    assert result["entries"] is entries
    # Only the three entries that need a search are ever searched.
    assert sorted(seen) == [
        "a resolvable title here",
        "an unresolvable title here",
        "another resolvable title here",
    ]


def test_batch_resolve_overlaps_round_trips(monkeypatch):
    """Four workers, eight 200 ms round trips: the wall clock has to show it."""
    def slow_resolve(title: str, config):
        time.sleep(0.2)
        return None

    monkeypatch.setattr(resolver, "resolve_title_to_doi", slow_resolve)

    entries = [_entry(f"a slow resolvable title {i}") for i in range(8)]

    start = time.monotonic()
    resolver.batch_resolve(entries, {}, delay=0)
    elapsed = time.monotonic() - start

    # Serial would be ~1.6 s. Three rounds of 200 ms is ~0.6 s; the bound is
    # loose because this is asserting overlap, not a benchmark.
    assert elapsed < 1.0, f"took {elapsed:.2f}s, the round trips did not overlap"


def test_batch_resolve_single_entry_still_resolves(monkeypatch):
    monkeypatch.setattr(
        resolver, "resolve_title_to_doi", lambda title, config: "10.1000/one"
    )

    result = resolver.batch_resolve([_entry("just the one title here")], {}, delay=0)

    assert result["entries"][0].doi == "10.1000/one"
    assert result["stats"]["resolved_by_title"] == 1


# --------------------------------------------------------------------------- #
# 3. credentials stay in headers and the host cap covers every batch
# --------------------------------------------------------------------------- #


class _FakeResponse:
    status_code = 200

    def json(self):
        return {
            "results": [
                {"title": "Deep learning", "doi": "https://doi.org/10.1038/nature14539"}
            ]
        }


class _FakeSession:
    def __init__(self):
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _FakeResponse()


def test_title_search_sends_bearer_key_without_query_credentials(monkeypatch):
    from scansci_pdf import network

    session = _FakeSession()
    monkeypatch.setattr(network, "_get_session", lambda config: session)

    doi = resolver.resolve_title_to_doi(
        "Deep learning",
        {"email": "someone@example.org", "openalex_api_key": "key-123"},
    )

    assert doi == "10.1038/nature14539"
    call = session.calls[0]
    assert call["params"] == {"search": "Deep learning", "per_page": 5}
    assert call["headers"] == {"Authorization": "Bearer key-123"}


def test_title_search_omits_empty_credentials(monkeypatch):
    from scansci_pdf import network

    session = _FakeSession()
    monkeypatch.setattr(network, "_get_session", lambda config: session)

    resolver.resolve_title_to_doi("Deep learning", {"email": "scansci-pdf@example.invalid"})

    call = session.calls[0]
    assert "api_key" not in call["params"]
    assert "mailto" not in call["params"]
    assert call["headers"] == {}


def test_title_search_releases_host_slot_on_error(monkeypatch):
    from scansci_pdf import network

    events: list[str] = []
    session = _FakeSession()
    session.get = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    monkeypatch.setattr(network, "_get_session", lambda config: session)
    def acquire(url, config):
        events.append("acquire")
        return object()

    monkeypatch.setattr(network, "host_concurrency_acquire", acquire)
    monkeypatch.setattr(network, "host_concurrency_release", lambda url, config, lease: events.append("release"))

    assert resolver.resolve_title_to_doi("Deep learning", {}) is None
    assert events == ["acquire", "release"]


class _TrackingSession:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def get(self, url, **kwargs):
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
        try:
            time.sleep(0.05)
            return _FakeResponse()
        finally:
            with self._lock:
                self._active -= 1


def _tracking_session(monkeypatch) -> _TrackingSession:
    from scansci_pdf import network

    session = _TrackingSession()
    monkeypatch.setattr(network, "_get_session", lambda config: session)
    monkeypatch.setattr(network, "_host_semaphores", {})
    return session


def test_concurrent_batches_share_the_openalex_host_cap(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    session = _tracking_session(monkeypatch)
    batches = [[_entry("Deep learning") for _ in range(8)] for _ in range(2)]

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda entries: resolver.batch_resolve(entries, {}, delay=0), batches))

    assert session.peak == 5


def test_custom_worker_count_cannot_bypass_the_openalex_host_cap(monkeypatch):
    session = _tracking_session(monkeypatch)
    entries = [_entry("Deep learning") for _ in range(8)]

    resolver.batch_resolve(entries, {}, delay=0, max_workers=8)

    assert session.peak == 5


def test_host_gate_cold_start_creates_one_gate_and_enforces_custom_cap(monkeypatch):
    from scansci_pdf import network

    created = []
    original_gate = network._HostConcurrencyGate

    class SlowGate(original_gate):
        def __init__(self):
            created.append(self)
            time.sleep(0.02)
            super().__init__()

    monkeypatch.setattr(network, "_HostConcurrencyGate", SlowGate)
    monkeypatch.setattr(network, "_host_semaphores", {})
    config = {"host_concurrency": {"api.openalex.org": 1}}
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def worker():
        lease = network.host_concurrency_acquire(resolver._OPENALEX_WORKS_URL, config)
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.02)
        with lock:
            state["active"] -= 1
        network.host_concurrency_release(resolver._OPENALEX_WORKS_URL, config, lease)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(created) == 1
    assert state["peak"] == 1


def test_host_gate_custom_cap_applies_after_default_warmup(monkeypatch):
    from scansci_pdf import network

    monkeypatch.setattr(network, "_host_semaphores", {})
    lease = network.host_concurrency_acquire(resolver._OPENALEX_WORKS_URL, {})
    network.host_concurrency_release(resolver._OPENALEX_WORKS_URL, {}, lease)

    config = {"host_concurrency": {"api.openalex.org": 1}}
    state = {"active": 0, "peak": 0}
    lock = threading.Lock()

    def worker():
        lease = network.host_concurrency_acquire(resolver._OPENALEX_WORKS_URL, config)
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.02)
        with lock:
            state["active"] -= 1
        network.host_concurrency_release(resolver._OPENALEX_WORKS_URL, config, lease)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert state["peak"] == 1


def test_host_gate_lower_limit_waits_for_existing_default_request(monkeypatch):
    from scansci_pdf import network

    monkeypatch.setattr(network, "_host_semaphores", {})
    default_lease = network.host_concurrency_acquire(resolver._OPENALEX_WORKS_URL, {})
    acquired = threading.Event()
    config = {"host_concurrency": {"api.openalex.org": 1}}

    def worker():
        lease = network.host_concurrency_acquire(resolver._OPENALEX_WORKS_URL, config)
        acquired.set()
        network.host_concurrency_release(resolver._OPENALEX_WORKS_URL, config, lease)

    thread = threading.Thread(target=worker)
    thread.start()
    assert not acquired.wait(0.05)
    network.host_concurrency_release(resolver._OPENALEX_WORKS_URL, {}, default_lease)
    assert acquired.wait(1)
    thread.join()
