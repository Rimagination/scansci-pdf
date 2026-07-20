from __future__ import annotations

from typer.testing import CliRunner

from scansci_pdf import main
from scansci_pdf import sources


def test_ezproxy_only_download_calls_only_ezproxy(monkeypatch, tmp_path):
    config = {
        "output_dir": str(tmp_path),
        "cache_dir": str(tmp_path / "cache"),
        "download_strategy": "fastest",
        "ezproxy_enabled": True,
        "ezproxy_login_url": "https://proxy.example.edu/login?url={url}",
        "auto_rename": False,
    }
    monkeypatch.setattr(sources, "load_config", lambda: config.copy())
    monkeypatch.setattr(sources, "cache_get", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sources, "cache_set", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("scansci_pdf.identifiers.validate_doi", lambda _doi: (True, ""))
    monkeypatch.setattr(
        sources,
        "_build_free_sources",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("free sources must not run")
        ),
    )
    monkeypatch.setattr(
        sources,
        "_build_institutional_sources",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("institutional race must not run")
        ),
    )

    def fake_ezproxy(doi, output_path, _config):
        output_path.write_bytes(b"%PDF-1.7\n" + (b"x" * 6000) + b"%%EOF")
        return {
            "success": True,
            "identifier": doi,
            "doi": doi,
            "file": str(output_path),
            "source": "EZProxy",
        }

    monkeypatch.setattr(sources, "try_ezproxy", fake_ezproxy)
    monkeypatch.setattr(
        "scansci_pdf.citation.fetch_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("metadata lookup must not precede EZProxy")
        ),
    )

    result = sources.download(
        "10.1234/example",
        tmp_path,
        strategy="ezproxy_only",
        rename=False,
        ezproxy_interactive=True,
    )

    assert result["success"] is True
    assert result["source"] == "EZProxy"


def test_get_ezproxy_only_flag_maps_to_strategy(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        sources,
        "download",
        lambda identifier, output, **kwargs: calls.append((identifier, output, kwargs))
        or {"success": True, "file": str(tmp_path / "paper.pdf"), "source": "EZProxy"},
    )

    result = CliRunner().invoke(
        main.app,
        ["get", "10.1234/example", "--output", str(tmp_path), "--ezproxy-only"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["strategy"] == "ezproxy_only"
    assert calls[0][2]["ezproxy_interactive"] is True


def test_get_normal_strategy_also_allows_interactive_ezproxy_wait(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        sources,
        "download",
        lambda identifier, output, **kwargs: calls.append((identifier, output, kwargs))
        or {"success": True, "file": str(tmp_path / "paper.pdf"), "source": "OpenAlex"},
    )

    result = CliRunner().invoke(
        main.app,
        ["get", "10.1234/example", "--output", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][2]["ezproxy_interactive"] is True


def test_get_rejects_ezproxy_only_with_other_strategy(monkeypatch):
    monkeypatch.setattr(
        sources,
        "download",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("download must not run")
        ),
    )

    result = CliRunner().invoke(
        main.app,
        ["get", "10.1234/example", "--ezproxy-only", "--strategy", "legal_only"],
    )

    assert result.exit_code != 0
    assert "cannot be combined" in result.output.lower()


def test_get_prints_ezproxy_failure_reason(monkeypatch):
    monkeypatch.setattr(
        sources,
        "download",
        lambda *_args, **_kwargs: {
            "success": False,
            "reason": "EZProxy download failed or timed out",
        },
    )

    result = CliRunner().invoke(
        main.app,
        ["get", "10.1234/example", "--ezproxy-only"],
    )

    assert "FAILED: EZProxy download failed or timed out" in result.output
