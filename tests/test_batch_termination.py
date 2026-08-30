"""BATCH-01 regression tests: race and batch must terminate on time.

Covers the two user-visible hangs from the field report:
- ``_run_tiers_parallel`` must return as soon as every source finished
  (all-done event), instead of burning the full timeout on fast failures;
  a hung source must never block the process.
- ``batch_download`` must respect a wall-clock budget and always write its
  report, even when worker downloads never return.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from scansci_pdf.sources import _run_tiers_parallel, batch_download


def _make_source(delay: float, ok: bool, label: str):
    def src(doi, out_path, config, label_=label, use_tor=False):
        time.sleep(delay)
        if not ok:
            return {"success": False, "error": "simulated failure", "source": label_}
        # >100KB so is_suspicious_pdf (size heuristic) does not flag it
        out_path.write_bytes(b"%PDF-1.4 fake" + b"0" * 150_000)
        return {"success": True, "file": str(out_path), "source": label_}
    return src


@pytest.fixture(autouse=True)
def _force_pure_python_race(monkeypatch: pytest.MonkeyPatch):
    """The compiled core is a dev-only artifact; PyPI wheels ship pure Python."""
    monkeypatch.setattr("scansci_pdf.sources._HAS_COMPILED_CORE", False)


def _race(tiers, overall_timeout: int, tmp_path: Path):
    t0 = time.monotonic()
    result = _run_tiers_parallel(tiers, "10.1000/test", tmp_path,
                                 tmp_path / "out.pdf", {}, False, overall_timeout)
    return result, time.monotonic() - t0


def test_race_all_fail_returns_immediately(tmp_path: Path):
    """All sources fail fast: the race must return on all-done, not after timeout."""
    tiers = [([(_make_source(0.1, False, "F1"), "F1"),
               (_make_source(0.2, False, "F2"), "F2")], "Test", 3)]
    result, elapsed = _race(tiers, overall_timeout=2, tmp_path=tmp_path)
    assert result is None
    assert elapsed < 2, f"all-fail took {elapsed:.2f}s (should return on all_done)"


def test_race_success_stops_slower_sources(tmp_path: Path):
    """First success must cancel the race and return immediately."""
    tiers = [([(_make_source(0.3, True, "Fast"), "Fast"),
               (_make_source(5.0, False, "Slow"), "Slow")], "Test", 3)]
    result, elapsed = _race(tiers, overall_timeout=5, tmp_path=tmp_path)
    assert result is not None and result.get("success")
    assert elapsed < 2, f"success took {elapsed:.2f}s (should stop at first winner)"


def test_race_hung_source_does_not_block_return(tmp_path: Path):
    """A source that never returns must be waived, not joined forever."""
    tiers = [([(_make_source(0.1, False, "F1"), "F1"),
               (_make_source(30.0, False, "Hung"), "Hung")], "Test", 3)]
    t0 = time.monotonic()
    result = _run_tiers_parallel(tiers, "10.1000/test", tmp_path,
                                 tmp_path / "out.pdf", {}, False, overall_timeout=1)
    elapsed = time.monotonic() - t0
    assert result is None
    # overall(1) + wait-any(5) is the fixed wait; grace(15) is bounded. But the
    # hung worker finishes at 30s only — so this asserts returning before that.
    assert elapsed < 25, f"hung race took {elapsed:.2f}s (should not wait for worker)"


def test_batch_download_honors_wall_clock_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Batch must exit within batch_total_timeout and still write its report."""
    import threading

    import scansci_pdf.sources as sources

    wake = threading.Event()

    def hang_download(identifier, output_dir=None, **kwargs):
        # Loop so a wake.set() at teardown lets the pytest process exit promptly.
        while not wake.wait(0.5):
            pass
        return {"success": True, "identifier": identifier}

    try:
        monkeypatch.setattr(sources, "download", hang_download)
        monkeypatch.setattr(sources, "_batch_institutional_phase", lambda *a, **k: None)
        # Keep progress bookkeeping local to the test's temp dir.
        progress: dict[str, dict] = {}

        def _fake_save(batch_id, ident, result):
            progress.setdefault(batch_id, {})[ident] = result

        def _fake_load(batch_id):
            return progress.get(batch_id, {})

        monkeypatch.setattr(sources, "_save_progress", _fake_save)
        monkeypatch.setattr(sources, "_load_progress", _fake_load)
        monkeypatch.setattr(sources, "_clear_progress", lambda batch_id: None)
        monkeypatch.setattr(sources, "load_config", lambda: {
            "batch_total_timeout": 2,
            "batch_workers": 2,
            "batch_stagger_seconds": 0.0,
            "output_dir": str(tmp_path),
        })

        t0 = time.monotonic()
        summary = batch_download(["2401.00001", "2401.00002"],
                                 output_dir=tmp_path, fallbacks={})
        elapsed = time.monotonic() - t0
    finally:
        wake.set()

    assert elapsed < 10, f"batch took {elapsed:.2f}s (budget was 2s)"
    assert summary["total"] == 2
    assert summary["failed"] == 2  # both hung workers reported as timeout
    assert (tmp_path / "download_results.json").exists()
    for r in summary["results"]:
        assert not r.get("success")