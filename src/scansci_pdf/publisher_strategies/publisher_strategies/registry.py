"""Auto-discovery registry for publisher strategies.

Every publisher module registers itself on import via ``StrategyRegistry.register``.
Callers resolve strategies by DOI prefix, publisher name, or domain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import BasePublisherStrategy


class StrategyRegistry:
    """Global registry of publisher strategies.

    Strategies auto-register by calling ``StrategyRegistry.register(MyStrategy)``
    at module level.  The registry is keyed by name (lowercase), DOI prefix,
    and base domain.
    """

    _by_name: dict[str, "BasePublisherStrategy"] = {}
    _by_doi_prefix: dict[str, "BasePublisherStrategy"] = {}
    _by_domain: dict[str, "BasePublisherStrategy"] = {}

    @classmethod
    def register(cls, strategy_cls: type["BasePublisherStrategy"]) -> type["BasePublisherStrategy"]:
        """Register a strategy class (called at module import time)."""
        instance = strategy_cls()
        name_key = instance.name.lower()
        cls._by_name[name_key] = instance
        for alias in instance.aliases:
            cls._by_name[alias.lower()] = instance
        for prefix in instance.doi_prefixes:
            cls._by_doi_prefix[prefix] = instance
        for domain in instance.base_domains:
            cls._by_domain[domain] = instance
        return strategy_cls  # allow use as decorator

    @classmethod
    def get_for_doi(cls, doi: str) -> "BasePublisherStrategy | None":
        """Find the strategy whose DOI prefix matches *doi*."""
        for prefix, strategy in cls._by_doi_prefix.items():
            if doi.startswith(prefix):
                return strategy
        return None

    @classmethod
    def get_by_name(cls, name: str) -> "BasePublisherStrategy | None":
        """Look up a strategy by publisher name (case-insensitive)."""
        return cls._by_name.get(name.lower())

    @classmethod
    def get_for_url(cls, url: str) -> "BasePublisherStrategy | None":
        """Find the strategy whose base domain appears in *url*."""
        for domain, strategy in cls._by_domain.items():
            if domain in url:
                return strategy
        return None

    @classmethod
    def list_all(cls) -> list["BasePublisherStrategy"]:
        """Return all registered strategies (deduplicated)."""
        seen: set[int] = set()
        result: list["BasePublisherStrategy"] = []
        for s in cls._by_name.values():
            if id(s) not in seen:
                seen.add(id(s))
                result.append(s)
        return result

    @classmethod
    def clear(cls) -> None:
        """Clear the registry (mainly for tests)."""
        cls._by_name.clear()
        cls._by_doi_prefix.clear()
        cls._by_domain.clear()
