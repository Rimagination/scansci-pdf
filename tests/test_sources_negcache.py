"""Negative cache: (source, publisher) blocking must be surgical and expiring."""

from __future__ import annotations

import scansci_pdf.sources as src
from scansci_pdf.sources import _neg_blocked, _neg_record


def setup_function():
    src._NEG_CACHE.clear()


def test_blocks_source_and_publisher_only():
    _neg_record("Sci-Hub", "10.1016/j.watres.2023.121036", {"error_type": "cloudflare_blocked", "error": "challenge page"})
    assert _neg_blocked("Sci-Hub", "10.1016/j.other.1")          # same publisher
    assert not _neg_blocked("Sci-Hub", "10.1038/nature12373")    # different publisher
    assert not _neg_blocked("Unpaywall", "10.1016/j.other.1")    # different source


def test_benign_failures_do_not_poison():
    _neg_record("OpenAlex", "10.1016/x", {"error": "404 not found"})
    _neg_record("OpenAlex", "10.1016/y", {"error_type": "paywall"})
    assert not _neg_blocked("OpenAlex", "10.1016/x")


def test_expired_entries_stop_blocking(monkeypatch):
    monkeypatch.setattr(src, "_NEG_TTL", -1.0)  # written already expired
    _neg_record("Sci-Hub", "10.1016/x", {"error_type": "cloudflare_blocked"})
    assert not _neg_blocked("Sci-Hub", "10.1016/x")
