"""Tests for the .doi_index.json v2 cache: TTL, strategy-aware invalidation,
and legacy format compatibility."""

import json
import time

from scansci_pdf.sources import (
    _EXPLICIT_STRATEGIES,
    _index_entry,
    _index_entry_is_fresh,
    _index_strategy_compatible,
    _update_doi_index,
    download,
)


def test_explicit_strategies_are_defined():
    assert "scihub_only" in _EXPLICIT_STRATEGIES
    assert "legal_only" in _EXPLICIT_STRATEGIES


def test_index_entry_normalizes_legacy_str():
    entry = _index_entry("C:\\papers\\paper.pdf")
    assert entry == {"file": "C:\\papers\\paper.pdf", "source": "", "strategy": "", "ts": None}


def test_index_entry_normalizes_v2_record():
    entry = _index_entry({"file": "paper.pdf", "source": "Sci-Hub(se)", "strategy": "scihub_only", "ts": 1234.5})
    assert entry["file"] == "paper.pdf"
    assert entry["source"] == "Sci-Hub(se)"
    assert entry["strategy"] == "scihub_only"
    assert entry["ts"] == 1234.5


def test_fresh_entry_within_ttl_is_fresh():
    config = {"cache_ttl_hours": 168}
    assert _index_entry_is_fresh({"file": "x", "ts": time.time() - 3600}, config)


def test_expired_entry_is_not_fresh():
    config = {"cache_ttl_hours": 1}
    assert not _index_entry_is_fresh({"file": "x", "ts": time.time() - 7200}, config)


def test_legacy_entry_without_timestamp_is_always_fresh():
    config = {"cache_ttl_hours": 1}
    assert _index_entry_is_fresh({"file": "x", "ts": None}, config)


def test_ttl_zero_disables_expiry():
    config = {"cache_ttl_hours": 0}
    assert _index_entry_is_fresh({"file": "x", "ts": time.time() - 10**9}, config)


def test_default_strategy_accepts_any_record():
    config = {"download_strategy": "fastest"}
    assert _index_strategy_compatible({"file": "x", "strategy": "scihub_only"}, config)
    config = {"download_strategy": "oa_first"}
    assert _index_strategy_compatible({"file": "x", "strategy": ""}, config)


def test_explicit_strategy_requires_matching_record():
    config = {"download_strategy": "scihub_only"}
    assert _index_strategy_compatible({"file": "x", "strategy": "scihub_only"}, config)
    assert not _index_strategy_compatible({"file": "x", "strategy": "fastest"}, config)
    assert not _index_strategy_compatible({"file": "x", "strategy": ""}, config)


def test_update_doi_index_writes_provenance_record(tmp_path):
    _update_doi_index(tmp_path, "10.1000/x", tmp_path / "a.pdf", source="OA", strategy="oa_first")
    idx = json.loads((tmp_path / ".doi_index.json").read_text(encoding="utf-8"))
    record = idx["10.1000/x"]
    assert record["file"].endswith("a.pdf")
    assert record["source"] == "OA"
    assert record["strategy"] == "oa_first"
    assert record["ts"] > 0


def test_update_doi_index_falls_back_to_config_strategy(tmp_path):
    _update_doi_index(tmp_path, "10.1000/x", tmp_path / "a.pdf", config={"download_strategy": "legal_only"})
    record = json.loads((tmp_path / ".doi_index.json").read_text(encoding="utf-8"))["10.1000/x"]
    assert record["strategy"] == "legal_only"


def test_update_doi_index_preserves_existing_provenance_on_rename(tmp_path):
    _update_doi_index(tmp_path, "10.1000/x", tmp_path / "old.pdf", source="Sci-Hub(se)", strategy="scihub_only")
    # rename-style update: no new source/strategy, must keep the recorded ones
    _update_doi_index(tmp_path, "10.1000/x", tmp_path / "new.pdf")
    record = json.loads((tmp_path / ".doi_index.json").read_text(encoding="utf-8"))["10.1000/x"]
    assert record["file"].endswith("new.pdf")
    assert record["source"] == "Sci-Hub(se)"
    assert record["strategy"] == "scihub_only"


def _download_config(tmp_path, strategy="fastest"):
    return {
        "output_dir": str(tmp_path / "out"),
        "cache_dir": str(tmp_path / "cache"),
        "cache_ttl_hours": 168,
        "download_strategy": strategy,
        "auto_rename": False,
        "vpnsci_enabled": False,
    }


def _install_config(monkeypatch, config, config_file):
    import scansci_pdf.config as config_module

    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(config_module, "CONFIG_FILE", config_file)


def _patch_download_network(monkeypatch):
    monkeypatch.setattr("scansci_pdf.identifiers.validate_doi", lambda doi: (True, "https://doi.org/" + doi))
    monkeypatch.setattr("scansci_pdf.citation.fetch_metadata", lambda doi, config: None)
    monkeypatch.setattr("scansci_pdf.sources._build_free_sources", lambda doi, config: [])
    monkeypatch.setattr("scansci_pdf.sources._build_institutional_sources", lambda doi, config, **kw: [])


def test_download_serves_fresh_indexed_file(monkeypatch, tmp_path):
    config = _download_config(tmp_path)
    _install_config(monkeypatch, config, tmp_path / "config.json")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    pdf = out_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    (out_dir / ".doi_index.json").write_text(
        json.dumps({"10.1000/x": {"file": str(pdf), "source": "Sci-Hub(se)", "strategy": "fastest", "ts": time.time()}}),
        encoding="utf-8",
    )
    _patch_download_network(monkeypatch)

    result = download("10.1000/x", out_dir, scihub_enabled=False)

    assert result["success"] is True
    assert result["source"] == "local_cache"
    assert result["cached"] is True
    # legacy-vs-record upgrade: entry now carries strategy and fresh ts
    idx = json.loads((out_dir / ".doi_index.json").read_text(encoding="utf-8"))
    assert idx["10.1000/x"]["source"] == "Sci-Hub(se)"


def test_download_drops_expired_index_entry(monkeypatch, tmp_path):
    config = _download_config(tmp_path)
    config["cache_ttl_hours"] = 1
    _install_config(monkeypatch, config, tmp_path / "config.json")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    pdf = out_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    (out_dir / ".doi_index.json").write_text(
        json.dumps({"10.1000/x": {"file": str(pdf), "source": "OA", "strategy": "oa_first", "ts": time.time() - 7200}}),
        encoding="utf-8",
    )
    _patch_download_network(monkeypatch)

    result = download("10.1000/x", out_dir, scihub_enabled=False)

    assert result["success"] is False
    idx = json.loads((out_dir / ".doi_index.json").read_text(encoding="utf-8"))
    assert "10.1000/x" not in idx


def test_download_drops_strategy_mismatched_entry(monkeypatch, tmp_path):
    config = _download_config(tmp_path, strategy="scihub_only")
    _install_config(monkeypatch, config, tmp_path / "config.json")
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    pdf = out_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    (out_dir / ".doi_index.json").write_text(
        json.dumps({"10.1000/x": {"file": str(pdf), "source": "OA", "strategy": "oa_first", "ts": time.time()}}),
        encoding="utf-8",
    )
    _patch_download_network(monkeypatch)

    result = download("10.1000/x", out_dir, scihub_enabled=False)

    assert result["success"] is False
    idx = json.loads((out_dir / ".doi_index.json").read_text(encoding="utf-8"))
    assert "10.1000/x" not in idx
