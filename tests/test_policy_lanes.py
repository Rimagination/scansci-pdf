"""POLICY-01 / SI-01 regression tests.

- ``grey_allowed`` is a pure veto on grey sources: explicit user restrictions
  (scihub_enabled=false, download_strategy=legal_only) survive lane scheduling.
- ``run_lanes`` must never widen authorization: overflow must not get a grey
  retry when the user disabled grey sources.
- ``StrategyRegistry.get_for_doi`` must fall back to the Generic strategy for
  unknown DOI prefixes instead of returning None.
"""

from __future__ import annotations

import pytest

from scansci_pdf.pipeline import grey_allowed, run_lanes
from scansci_pdf.publisher_strategies.registry import StrategyRegistry
from scansci_pdf.pipeline import QueueEntry


class TestGreyAllowed:
    def test_default_permits_grey(self):
        assert grey_allowed(None) is True
        assert grey_allowed({}) is True
        assert grey_allowed({"download_strategy": "fastest"}) is True
        assert grey_allowed({"download_strategy": "scihub_only"}) is True
        assert grey_allowed({"scihub_enabled": True}) is True

    def test_explicit_vetoes(self):
        assert grey_allowed({"scihub_enabled": False}) is False
        assert grey_allowed({"download_strategy": "legal_only"}) is False
        assert grey_allowed({"scihub_enabled": False, "download_strategy": "fastest"}) is False
        assert grey_allowed({"scihub_enabled": True, "download_strategy": "legal_only"}) is False


class TestRunLanesNoAuthorizationWidening:
    def _entries(self, identifiers: list[str]) -> list[QueueEntry]:
        return [QueueEntry(identifier=i, channel="grey") for i in identifiers]

    def test_grey_disabled_skips_grey_batch(self, tmp_path, monkeypatch):
        called = {}
        monkeypatch.setattr(
            "scansci_pdf.sources.batch_download",
            lambda *a, **k: called.setdefault("batch", k) or {"results": []},
        )
        results = run_lanes(
            self._entries(["10.1000/grey1", "10.1000/grey2"]),
            tmp_path, config={"scihub_enabled": False}, allow_grey=True,
        )
        assert "batch" not in called, "grey batch must not run when grey disabled"

    def test_grey_enabled_runs_grey_batch(self, tmp_path, monkeypatch):
        called = {}
        monkeypatch.setattr(
            "scansci_pdf.sources.batch_download",
            lambda *a, **k: called.setdefault("batch", k) or {"results": []},
        )
        run_lanes(
            self._entries(["10.1000/grey1"]),
            tmp_path, config={"scihub_enabled": True}, allow_grey=True,
        )
        assert "batch" in called


class TestGetForDoiGenericFallback:
    def test_unknown_prefix_falls_back_to_generic(self):
        strategy = StrategyRegistry.get_for_doi("10.99999/unknown.1")
        assert strategy is not None
        assert strategy.name.lower() == "generic"

    def test_known_prefix_prefers_specific_strategy(self):
        from scansci_pdf.publisher_strategies import StrategyRegistry as Reg
        generic = Reg._by_name.get("generic")
        strategy = Reg.get_for_doi("10.1016/j.foo.2026.01.001")
        assert strategy is not None
        assert strategy is not generic, "Elsevier DOI must not fall back to Generic"
        assert any(p.startswith("10.1016") for p in strategy.doi_prefixes)