"""Per-strategy contract tests for every registered publisher strategy.

Routing silently dying when a publisher changes their site is the core
failure mode of this project; these tests pin the minimum contract of each
strategy (identity, DOI prefixes, article_url sanity) so refactors can't
break a publisher unnoticed. Live canaries (scripts/canary.py) complement
this with real-network probes.
"""

from __future__ import annotations

import pytest

from scansci_pdf.publisher_strategies import StrategyRegistry

STRATEGIES = StrategyRegistry.list_all()


def _sample_doi(strategy) -> str:
    if getattr(strategy, "sample_dois", ()):
        return strategy.sample_dois[0]
    prefix = (strategy.doi_prefixes or ("10.0000/",))[0]
    return f"{prefix}test.0001"


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.name)
def test_strategy_has_identity_and_prefixes(strategy):
    assert strategy.name, "strategy must have a name"
    if strategy.name.lower() == "generic":
        pytest.skip("Generic is the fallback strategy; prefixes are optional by design")
    assert strategy.doi_prefixes, (
        f"{strategy.name}: no doi_prefixes — the registry can never route to it"
    )


@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.name)
def test_strategy_article_url_contains_doi_on_known_domain(strategy):
    doi = _sample_doi(strategy)
    url = strategy.article_url(doi)
    assert url and url.startswith("http"), f"{strategy.name}: bad article_url {url!r}"
    assert doi in url, f"{strategy.name}: article_url lost the DOI: {url!r}"
    domains = tuple(strategy.base_domains or ()) + ("doi.org",)
    assert any(d in url for d in domains), (
        f"{strategy.name}: {url} is not on its own domains {domains}"
    )


def test_doi_prefixes_are_unique_across_strategies():
    seen: dict[str, str] = {}
    for s in STRATEGIES:
        for prefix in s.doi_prefixes:
            p = prefix.lower()
            assert p not in seen, f"prefix {p} claimed by both {seen[p]} and {s.name}"
            seen[p] = s.name


def test_registry_routes_every_declared_prefix():
    for s in STRATEGIES:
        for prefix in s.doi_prefixes:
            assert StrategyRegistry.get_for_doi(f"{prefix}test.0001") is s, (
                f"get_for_doi does not resolve {prefix} back to {s.name}"
            )


def test_elsevier_paper_routes_to_elsevier_lane():
    s = StrategyRegistry.get_for_doi("10.1016/j.watres.2023.121036")
    assert s is not None and s.name.lower() == "elsevier"
