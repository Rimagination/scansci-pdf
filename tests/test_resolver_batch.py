"""Batch title resolution: overlap the round trips, keep the request rate.

#72 item 6. The serial loop spent one OpenAlex search per `delay`; the fix is
to overlap the waiting, not to raise the rate. These tests pin both halves:
the spacing gate stays aggregate (concurrency cannot multiply it), and the
batch still returns entries in input order with the same stats.
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
# 3. the request identifies itself, or the concurrent version collects 429s
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


def test_title_search_sends_mailto_and_api_key(monkeypatch):
    from scansci_pdf import network

    session = _FakeSession()
    monkeypatch.setattr(network, "_get_session", lambda config: session)

    doi = resolver.resolve_title_to_doi(
        "Deep learning",
        {"email": "someone@example.org", "openalex_api_key": "key-123"},
    )

    assert doi == "10.1038/nature14539"
    params = session.calls[0]["params"]
    assert params["search"] == "Deep learning"
    assert params["per_page"] == 5
    assert params["mailto"] == "someone@example.org"
    assert params["api_key"] == "key-123"


def test_title_search_omits_empty_credentials(monkeypatch):
    from scansci_pdf import network

    session = _FakeSession()
    monkeypatch.setattr(network, "_get_session", lambda config: session)

    resolver.resolve_title_to_doi("Deep learning", {})

    params = session.calls[0]["params"]
    assert "mailto" not in params
    assert "api_key" not in params
