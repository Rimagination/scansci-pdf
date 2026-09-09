"""Unpaywall must ask for the user's email instead of silently skipping.

Unpaywall ALWAYS requires a real mailbox and 422s placeholders. The source
must return a structured config_needed failure (so the agent asks the user)
rather than returning None and invisibly losing the channel.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scansci_pdf.sources.unpaywall import _is_placeholder_email, try_unpaywall


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code

    def json(self):
        return {}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch):
    def _boom(*a, **k):
        raise AssertionError("network must not be reached in these tests")

    monkeypatch.setattr("scansci_pdf.sources.unpaywall.fetch", _boom)


class TestPlaceholderEmail:
    def test_default_placeholder_asks_for_email(self, tmp_path: Path):
        # No email configured -> DEFAULT_CONFIG placeholder -> ask, don't skip.
        result = try_unpaywall("10.1/x", tmp_path / "a.pdf", {})
        assert result is not None and not result["success"]
        assert result["error_type"] == "config_needed"
        assert result["action"] == "ask_user_email"

    def test_example_com_is_placeholder(self, tmp_path: Path):
        result = try_unpaywall(
            "10.1/x", tmp_path / "a.pdf", {"email": "user@example.com"})
        assert result["error_type"] == "config_needed"

    def test_is_placeholder_email(self):
        assert _is_placeholder_email("")
        assert _is_placeholder_email("scansci-pdf@example.invalid")
        assert _is_placeholder_email("a@example.com")
        assert not _is_placeholder_email("liang@tsinghua.edu.cn")


class TestStatusHandling:
    """With a real email configured, HTTP statuses are classified, not folded."""

    def _run(self, tmp_path: Path, status: int):
        import scansci_pdf.sources.unpaywall as upw

        upw.fetch = lambda *a, **k: _Resp(status)  # type: ignore[assignment]
        return try_unpaywall(
            "10.1/x", tmp_path / "a.pdf", {"email": "user@real.cn"})

    def test_422_rejected_email_asks_user(self, tmp_path: Path):
        result = self._run(tmp_path, 422)
        assert result["error_type"] == "config_needed"
        assert result["action"] == "ask_user_email"

    def test_429_is_temporary_retry_later(self, tmp_path: Path):
        result = self._run(tmp_path, 429)
        assert result["error_type"] == "rate_limited"
        assert result["action"] == "retry_later"
        assert result["status_code"] == 429

    def test_404_genuinely_absent_stays_silent(self, tmp_path: Path):
        assert self._run(tmp_path, 404) is None

    def test_network_error_lets_race_continue(self, tmp_path: Path):
        import scansci_pdf.sources.unpaywall as upw

        def _raise(*a, **k):
            raise OSError("down")

        upw.fetch = _raise  # type: ignore[assignment]
        assert try_unpaywall(
            "10.1/x", tmp_path / "a.pdf", {"email": "user@real.cn"}) is None


class TestRaceFailureCollector:
    def test_single_source_failure_is_recorded(self, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch):
        import scansci_pdf.sources as sources

        monkeypatch.setattr(sources, "_HAS_COMPILED_CORE", False)
        sources._race_failures().clear()

        def failing_src(doi, out_path, config):
            return {"success": False, "reason": "needs user email",
                    "error_type": "config_needed", "action": "ask_user_email"}

        tiers = [([(failing_src, "FakeUPW")], "Test", 3)]
        result = sources._run_tiers_parallel(
            tiers, "10.1000/x", tmp_path, tmp_path / "out.pdf", {}, False, 2)
        assert result is None

        failures = sources._race_failures()
        assert len(failures) == 1
        assert failures[0]["source"] == "FakeUPW"
        assert failures[0]["error_type"] == "config_needed"
        assert failures[0]["action"] == "ask_user_email"

        # successes are never recorded as failures
        def ok_src(doi, out_path, config):
            return None

        sources._race_failures().clear()
        tiers = [([(ok_src, "Silent")], "Test", 3)]
        sources._run_tiers_parallel(
            tiers, "10.1000/y", tmp_path, tmp_path / "out2.pdf", {}, False, 2)
        assert sources._race_failures() == []


if __name__ == "__main__":
    pytest.main([__file__])
