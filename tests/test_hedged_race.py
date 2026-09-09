"""Hedged cascade: lanes are launched score-ordered, widening only on need.

The default race_mode=hedge submits the best lane first and only launches
the next after hedge_delay_seconds without an answer (or when every launched
lane already failed fast). race_mode=full restores the flat all-at-once
race. Both must preserve first-success-wins and failure recording.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import scansci_pdf.sources as sources
from scansci_pdf.sources import _run_tiers_parallel


@pytest.fixture(autouse=True)
def _pure_python_race(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sources, "_HAS_COMPILED_CORE", False)


def _pdf_bytes(pages: int = 3, min_size: int = 120_000) -> bytes:
    """A real multi-page PDF padded past the size heuristics."""
    import fitz

    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    import os
    blob = os.urandom(min_size)
    xref = doc.get_new_xref()
    doc.update_object(xref, f"<< /Type /EmbeddedFile /Length {len(blob)} >>")
    doc.update_stream(xref, blob, compress=False)
    return doc.tobytes()


_PDF = _pdf_bytes()


def _tracked_source(delay: float, ok: bool, label: str, starts: dict[str, float],
                    lock: threading.Lock):
    def src(doi, out_path, config, label_=label):
        with lock:
            starts[label_] = time.monotonic()
        time.sleep(delay)
        if not ok:
            return {"success": False, "error": "simulated", "source": label_}
        out_path.write_bytes(_PDF)
        return {"success": True, "file": str(out_path), "source": label_}
    return src


def _race(tiers, tmp_path: Path, config: dict | None = None):
    failures: list = []
    t0 = time.monotonic()
    result = _run_tiers_parallel(
        tiers, "10.1000/hedge", tmp_path, tmp_path / "out.pdf",
        config or {}, False, 10, failures=failures)
    return result, failures, time.monotonic() - t0


class TestHedgedCascade:
    def test_fast_winner_skips_slower_lanes(self, tmp_path: Path):
        """The best lane answers within the hedge delay: later lanes must
        never START (a flat race would have fired every one)."""
        starts: dict[str, float] = {}
        lock = threading.Lock()
        fast = _tracked_source(0.05, True, "Fast", starts, lock)
        slow1 = _tracked_source(5.0, False, "Slow1", starts, lock)
        slow2 = _tracked_source(5.0, False, "Slow2", starts, lock)
        tiers = [([(fast, "Fast"), (slow1, "Slow1"), (slow2, "Slow2")], "T", 3)]

        result, failures, elapsed = _race(
            tiers, tmp_path, {"race_mode": "hedge", "hedge_delay_seconds": 1.0})

        assert result is not None and result.get("success")
        assert elapsed < 2.5, f"winner path took {elapsed:.2f}s"
        assert "Fast" in starts
        assert "Slow1" not in starts and "Slow2" not in starts, (
            f"hedged cascade must not start losing lanes: {sorted(starts)}"
        )

    def test_slow_first_widens_after_delay(self, tmp_path: Path):
        """Best lane is slow: the second lane launches ~hedge_delay later and
        wins; the third lane is never started."""
        starts: dict[str, float] = {}
        lock = threading.Lock()
        hung = _tracked_source(5.0, False, "Hung", starts, lock)
        second = _tracked_source(0.05, True, "Second", starts, lock)
        third = _tracked_source(0.05, True, "Third", starts, lock)
        tiers = [([(hung, "Hung"), (second, "Second"), (third, "Third")], "T", 3)]

        result, _, elapsed = _race(
            tiers, tmp_path, {"race_mode": "hedge", "hedge_delay_seconds": 0.4})

        assert result is not None and result.get("success")
        assert "Second" in starts and "Third" not in starts
        # second launched after ~0.4s hedge delay, won immediately after
        assert 0.3 <= elapsed < 3.0, f"widening took {elapsed:.2f}s"

    def test_fast_failure_widens_immediately(self, tmp_path: Path):
        """A lane failing in milliseconds must not serialize behind the full
        hedge delay — widening happens as soon as everything launched has
        answered."""
        starts: dict[str, float] = {}
        lock = threading.Lock()
        fails = [_tracked_source(0.02, False, f"F{i}", starts, lock) for i in range(3)]
        winner = _tracked_source(0.02, True, "Winner", starts, lock)
        tiers = [([(fails[0], "F0"), (fails[1], "F1"), (fails[2], "F2"),
                   (winner, "Winner")], "T", 3)]

        result, _, elapsed = _race(
            tiers, tmp_path, {"race_mode": "hedge", "hedge_delay_seconds": 2.0})

        assert result is not None and result.get("success")
        # 3 instant failures + instant winner: must not pay 3 x 2s delays
        assert elapsed < 2.5, f"fast-failure widening took {elapsed:.2f}s"

    def test_all_fail_records_every_lane(self, tmp_path: Path):
        starts: dict[str, float] = {}
        lock = threading.Lock()
        lanes = [_tracked_source(0.02, False, f"L{i}", starts, lock) for i in range(3)]
        tiers = [([(fn, f"L{i}") for i, fn in enumerate(lanes)], "T", 3)]

        result, failures, _ = _race(
            tiers, tmp_path, {"race_mode": "hedge", "hedge_delay_seconds": 0.05})

        assert result is None
        assert sorted(f["source"] for f in failures) == ["L0", "L1", "L2"]

    def test_full_mode_launches_everything(self, tmp_path: Path):
        starts: dict[str, float] = {}
        lock = threading.Lock()
        slow_ok = _tracked_source(0.5, True, "Slow", starts, lock)
        others = [_tracked_source(0.5, True, f"O{i}", starts, lock) for i in range(2)]
        tiers = [([(slow_ok, "Slow"), (others[0], "O0"), (others[1], "O1")], "T", 3)]

        result, _, _ = _race(
            tiers, tmp_path, {"race_mode": "full", "hedge_delay_seconds": 5.0})

        assert result is not None and result.get("success")
        assert set(starts) == {"Slow", "O0", "O1"}, (
            "race_mode=full must launch all lanes immediately"
        )

    def test_single_source_path_unchanged(self, tmp_path: Path):
        starts: dict[str, float] = {}
        lock = threading.Lock()
        src = _tracked_source(0.02, True, "Only", starts, lock)
        tiers = [([(src, "Only")], "T", 3)]

        result, failures, _ = _race(tiers, tmp_path)
        assert result is not None and result.get("success")
        assert failures == []


if __name__ == "__main__":
    pytest.main([__file__])
