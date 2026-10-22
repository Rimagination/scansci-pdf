"""Batch I/O: one connection pool per worker, one validation per DOI.

Items 1, 2, 4 and 5 of #72. The batch runs on a ThreadPoolExecutor, which is the
threading assumption behind all four: a session reused within a worker is safe,
a session shared across workers is not, and a read-modify-write on a shared file
loses updates unless it is serialized.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

DOIS = ["10.1000/a", "10.1000/b", "10.1000/c"]


# --------------------------------------------------------------------------- #
# 1. a pooled session per worker thread — reused within, never shared across
# --------------------------------------------------------------------------- #


def test_thread_session_is_reused_within_a_thread():
    from scansci_pdf.network import thread_session

    assert thread_session() is thread_session()


def test_thread_session_is_keyed_by_proxy():
    from scansci_pdf.network import thread_session

    proxied = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
    assert thread_session(proxied) is thread_session(proxied)
    assert thread_session(proxied) is not thread_session()


def test_thread_session_is_not_shared_between_threads():
    """The whole point: reuse inside a worker, no cross-thread session state."""
    from scansci_pdf.network import thread_session

    seen: dict[str, object] = {}

    def grab(name: str) -> None:
        seen[name] = thread_session()

    threads = [threading.Thread(target=grab, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert seen["a"] is not seen["b"]


def test_thread_session_pools_connections():
    """The pooling adapter is what turns one handshake per thread into one per
    PDF; without it there would be no measured win to claim."""
    from scansci_pdf.network import thread_session

    adapter = thread_session().get_adapter("https://example.org")
    assert adapter._pool_maxsize >= 10
    assert adapter._pool_connections >= 10


# --------------------------------------------------------------------------- #
# 2. a batch validates each identifier once
# --------------------------------------------------------------------------- #


def test_batch_tells_download_that_validation_already_happened(monkeypatch, tmp_path):
    from scansci_pdf import identifiers, sources

    monkeypatch.setattr(identifiers, "validate_doi", lambda doi: (True, ""))
    # batch_id is derived from the identifiers, so progress from any earlier run
    # of this test would mark them completed and skip the downloads entirely.
    monkeypatch.setattr(
        sources, "_get_progress_file", lambda batch_id: tmp_path / f"{batch_id}.jsonl"
    )

    captured: list[dict] = []

    def fake_download(identifier, output_dir=None, **kwargs):
        captured.append({"identifier": identifier, **kwargs})
        return {"success": True, "identifier": identifier, "doi": identifier}

    monkeypatch.setattr(sources, "download", fake_download)

    sources.batch_download(list(DOIS), output_dir=tmp_path)

    # Workers run concurrently, so the call order is not the input order; the
    # batch reorders its results by index, not the download calls.
    assert sorted(call["identifier"] for call in captured) == sorted(DOIS)
    assert all(call["_validated"] is True for call in captured), (
        "the batch pre-validates every DOI, so the per-download check is a second HEAD"
    )


def test_download_still_validates_on_its_own(monkeypatch, tmp_path):
    """`_validated` defaults to False, so a standalone download keeps the check.

    A failed validation returns before any source is tried, which is what makes
    this assertable without network.
    """
    from scansci_pdf import identifiers, sources

    monkeypatch.setattr(identifiers, "validate_doi", lambda doi: (False, "DOI not found (404)"))

    result = sources.download("10.1000/missing", output_dir=tmp_path)

    assert result["success"] is False
    assert "Invalid DOI" in result["error"]


# --------------------------------------------------------------------------- #
# 4 & 5. the score table is parsed once and updated under a lock
# --------------------------------------------------------------------------- #


def _isolate_scores(monkeypatch, tmp_path):
    from scansci_pdf.sources import scoring

    monkeypatch.setattr(scoring, "_SCORES_FILE", tmp_path / "source_scores.json")
    monkeypatch.setattr(scoring, "_cache", None)
    return scoring


def test_scores_file_is_parsed_once_per_process(monkeypatch, tmp_path):
    scoring = _isolate_scores(monkeypatch, tmp_path)

    loads: list[int] = []
    real_load = scoring._load_scores
    monkeypatch.setattr(
        scoring, "_load_scores", lambda: (loads.append(1) or real_load())
    )

    for _ in range(30):
        scoring.get_score("scihub")
        scoring.get_latency("scihub")

    assert len(loads) == 1, "sort_sources reads per source per DOI; the table is cached"


def test_concurrent_updates_do_not_lose_attempts(monkeypatch, tmp_path):
    """record_result is a read-modify-write. Batch workers are threads, so
    without the lock two updates can overwrite each other."""
    scoring = _isolate_scores(monkeypatch, tmp_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: scoring.record_result("scihub", True, 100.0), range(80)))

    entry = json.loads(
        (tmp_path / "source_scores.json").read_text(encoding="utf-8")
    )["scihub"]
    assert entry["attempts"] == 80


def test_get_session_is_per_thread_too():
    """`fetch` reaches _get_session from the batch worker pools, so the name the
    rest of the package calls must not be the one object shared by all of them
    (#71 item 4)."""
    from scansci_pdf.network import _get_session

    config = {"network_proxy": ""}
    assert _get_session(config) is _get_session(config), "still reused within a thread"

    seen: dict[str, object] = {}

    def grab(name: str) -> None:
        seen[name] = _get_session(config)

    threads = [threading.Thread(target=grab, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert seen["a"] is not seen["b"]
