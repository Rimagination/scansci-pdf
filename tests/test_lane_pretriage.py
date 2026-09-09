"""Lane pretriage: S2 batch enrichment, MDPI CDN construction, default routing."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scansci_pdf import pipeline
from scansci_pdf.pipeline import (
    QueueEntry,
    _mdpi_variants,
    predict_channel,
)


class TestS2BatchEnrichment:
    def test_batch_fills_oa_urls(self, monkeypatch):
        class _Resp:
            status_code = 200

            def json(self):
                return [
                    {"isOpenAccess": True,
                     "openAccessPdf": {"url": "https://repo.example/a.pdf"}},
                    None,
                    {"isOpenAccess": False, "openAccessPdf": None},
                ]

        # _s2_batch_oa_urls imports requests inside the function; patching the
        # module attribute is enough.
        import requests
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())

        out = pipeline._s2_batch_oa_urls(
            ["10.1/a", "10.1/b", "10.1/c"], {})
        assert out == {"10.1/a": "https://repo.example/a.pdf"}

    def test_batch_failure_returns_empty(self, monkeypatch):
        import requests
        monkeypatch.setattr(
            requests, "post",
            lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        assert pipeline._s2_batch_oa_urls(["10.1/a"], {}) == {}

    def test_enrich_uses_s2_for_large_batches(self, monkeypatch, tmp_path):
        ids = [f"10.1234/x{i}" for i in range(12)]
        entries = [QueueEntry(identifier=i) for i in ids]
        monkeypatch.setattr(
            pipeline, "_s2_batch_oa_urls",
            lambda dois, cfg: {ids[0]: "https://repo.example/x0.pdf"})
        per_doi = []
        monkeypatch.setattr(
            pipeline, "_fetch_oa_pdf",
            lambda doi, cfg: per_doi.append(doi) or "")

        pipeline._enrich_oa_urls(entries, {"lane_s2_batch_min": 10})

        assert entries[0].oa_url == "https://repo.example/x0.pdf"
        assert entries[0].channel == "oa"
        assert per_doi == ids[1:], "S2 hits must not be re-queried per-DOI"

    def test_small_batches_skip_s2(self, monkeypatch):
        entries = [QueueEntry(identifier=f"10.1234/y{i}") for i in range(3)]
        s2_called = []
        monkeypatch.setattr(
            pipeline, "_s2_batch_oa_urls",
            lambda dois, cfg: s2_called.append(dois) or {})
        monkeypatch.setattr(pipeline, "_fetch_oa_pdf", lambda doi, cfg: "")

        pipeline._enrich_oa_urls(entries, {"lane_s2_batch_min": 10})
        assert s2_called == []

    def test_mdpi_and_elsevier_excluded_from_enrichment(self, monkeypatch):
        entries = [
            QueueEntry(identifier="10.3390/toxics10100577"),
            QueueEntry(identifier="10.1016/j.x.2026.01.001"),
        ]
        monkeypatch.setattr(pipeline, "_fetch_oa_pdf", lambda doi, cfg: "")
        pipeline._enrich_oa_urls(entries, {})
        # deterministic lanes handle both — no lookups at all
        for e in entries:
            assert e.oa_url == ""


class TestMdpiCdn:
    def test_variants_new_and_old_volume_style(self):
        v = _mdpi_variants("10.3390/toxics10100577")
        assert v and v[0].endswith("/toxics/toxics-10-00577/article_deploy/toxics-10-00577.pdf")
        v2 = _mdpi_variants("10.3390/min9030139")
        assert any(u.endswith("/minerals/minerals-09-00139/article_deploy/minerals-09-00139.pdf")
                   for u in v2)
        v3 = _mdpi_variants("10.3390/su15155777")
        assert any("sustainability-15-05777.pdf" in u for u in v3)

    def test_non_mdpi_returns_empty(self):
        assert _mdpi_variants("10.1016/j.envres.2024.118134") == []

    def test_prefix_routes_mdpi_to_oa(self):
        assert predict_channel("10.3390/toxics10100577") == "oa"
        assert predict_channel("10.1016/j.envres.2024.118134") == "elsevier"

    def test_fast_lane_uses_cdn_for_mdpi(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            pipeline, "_mdpi_cdn_fetch",
            lambda doi, out, headers, proxies: tmp_path / "fake.pdf")
        entries = [QueueEntry(identifier="10.3390/toxics10100577", channel="oa")]
        results = pipeline._run_fast_lane(
            entries, tmp_path, {"lane_mdpi_cdn": True})
        assert results[0]["success"] and results[0]["source"] == "mdpi_cdn"

    def test_fast_lane_cdn_kill_switch(self, tmp_path, monkeypatch):
        called = []
        monkeypatch.setattr(
            pipeline, "_mdpi_cdn_fetch",
            lambda doi, out, headers, proxies: called.append(doi) or None)
        monkeypatch.setattr(
            pipeline, "_download_url",
            lambda url, out, identifier, headers, proxies: None)
        entries = [QueueEntry(identifier="10.3390/toxics10100577", channel="oa")]
        results = pipeline._run_fast_lane(
            entries, tmp_path, {"lane_mdpi_cdn": False})
        assert called == []
        assert not results[0]["success"]


class TestBatchDefaultLanes:
    def _cfg(self, tmp_path, strategy="fastest", lanes_cfg=True):
        return {
            "batch_default_lanes": lanes_cfg,
            "download_strategy": strategy,
            "output_dir": str(tmp_path),
        }

    def test_mcp_defaults_to_lanes(self, monkeypatch, tmp_path):
        import scansci_pdf.server as server

        cfg = self._cfg(tmp_path)
        monkeypatch.setattr(server, "load_config", lambda: cfg)
        canned = [{"doi": f"10.1234/z{i}", "success": True, "file": "x"}
                  for i in range(4)]
        monkeypatch.setattr(pipeline, "run_lanes", lambda *a, **k: canned)
        written = []
        import scansci_pdf.sources as sources_mod
        monkeypatch.setattr(
            sources_mod, "_write_download_results",
            lambda results, out: written.append(results))

        ids = [f"10.1234/z{i}" for i in range(4)]
        raw = server._batch_download_impl(ids, str(tmp_path))
        data = json.loads(raw)

        assert data["mode"] == "lanes"
        assert data["succeeded"] == 4
        assert written and len(written[0]) == 4

    def test_mcp_lanes_false_uses_racing(self, monkeypatch, tmp_path):
        import scansci_pdf.server as server

        monkeypatch.setattr(server, "load_config", lambda: self._cfg(tmp_path))
        racing = {"total": 4, "succeeded": 0, "results": [], "failed": 4}
        monkeypatch.setattr(server, "batch_download",
                            lambda *a, **k: racing)

        ids = [f"10.1234/z{i}" for i in range(4)]
        data = json.loads(server._batch_download_impl(ids, str(tmp_path), lanes=False))
        assert "mode" not in data

    def test_mcp_grey_strategy_stays_on_racing(self, monkeypatch, tmp_path):
        import scansci_pdf.server as server

        monkeypatch.setattr(
            server, "load_config",
            lambda: self._cfg(tmp_path, strategy="scihub_only"))
        lanes_called = []
        monkeypatch.setattr(
            pipeline, "run_lanes",
            lambda *a, **k: lanes_called.append(1) or [])
        racing = {"total": 4, "succeeded": 4, "results": [], "failed": 0}
        monkeypatch.setattr(server, "batch_download", lambda *a, **k: racing)

        ids = [f"10.1234/z{i}" for i in range(4)]
        json.loads(server._batch_download_impl(ids, str(tmp_path)))
        assert lanes_called == [], "grey-oriented strategies must keep racing"

    def test_small_lists_stay_on_racing(self, monkeypatch, tmp_path):
        import scansci_pdf.server as server

        monkeypatch.setattr(server, "load_config", lambda: self._cfg(tmp_path))
        lanes_called = []
        monkeypatch.setattr(
            pipeline, "run_lanes",
            lambda *a, **k: lanes_called.append(1) or [])
        racing = {"total": 2, "succeeded": 2, "results": [], "failed": 0}
        monkeypatch.setattr(server, "batch_download", lambda *a, **k: racing)

        json.loads(server._batch_download_impl(["10.1234/a", "10.1234/b"],
                                               str(tmp_path)))
        assert lanes_called == []


if __name__ == "__main__":
    pytest.main([__file__])
