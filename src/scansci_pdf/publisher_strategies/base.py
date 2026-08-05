"""Base class for publisher-specific PDF download strategies.

Unifies the declarative data from PublisherProfile with the imperative
download logic from publisher_strategies, so each publisher is a single
self-contained class.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..log import get_logger

log = get_logger()


class BasePublisherStrategy:
    """Unified publisher strategy: profile data + download logic.

    Subclass this for each publisher.  Register via ``StrategyRegistry.register``.
    """

    # ------------------------------------------------------------------
    # Publisher identity
    # ------------------------------------------------------------------
    name: str = ""
    aliases: tuple[str, ...] = ()
    doi_prefixes: tuple[str, ...] = ()
    base_domains: tuple[str, ...] = ()
    sample_dois: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # URL templates
    # ------------------------------------------------------------------
    article_url_template: str | None = None
    pdf_url_templates: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # PDF detection
    # ------------------------------------------------------------------
    pdf_url_markers: tuple[str, ...] = (
        "/doi/pdf/",
        "/doi/epdf/",
        "/pdf/",
        "/pdf",
        "pdf",
    )
    pdf_link_text_markers: tuple[str, ...] = ("pdf",)
    success_url_markers: tuple[str, ...] = ()
    supplementary_url_markers: tuple[str, ...] = (
        "suppl_file",
        "suppdata",
        "supporting-information",
        "supplementary",
    )

    # ------------------------------------------------------------------
    # SSO / Auth configuration
    # ------------------------------------------------------------------
    sso_text_markers: tuple[str, ...] = ()
    sso_url_patterns: tuple[str, ...] = ()
    auth_url_markers: tuple[str, ...] = ()
    auth_title_markers: tuple[str, ...] = ()
    institution_input_selectors: tuple[str, ...] = ()
    institution_result_selectors: tuple[str, ...] = ()

    # ------------------------------------------------------------------
    # Methods
    # ------------------------------------------------------------------

    def article_url(self, doi: str) -> str:
        """Build the article landing-page URL from a DOI."""
        if self.article_url_template:
            return self.article_url_template.format(doi=doi)
        return f"https://doi.org/{doi}"

    def pdf_urls(self, doi: str) -> list[str]:
        """Build ordered candidate PDF URLs from a DOI."""
        doi_suffix = doi.split("/", 1)[-1] if "/" in doi else doi
        values = {
            "doi": doi,
            "doi_quoted": quote(doi, safe=""),
            "doi_suffix": doi_suffix,
            "doi_suffix_quoted": quote(doi_suffix, safe=""),
        }
        return [template.format(**values) for template in self.pdf_url_templates]

    async def download(
        self,
        doi: str,
        output_path: Path,
        config: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Download PDF for a DOI.  Return a success dict or ``None``.

        Subclasses must override this or set ``_uses_legacy_fn`` to ``True``
        (in which case the legacy ``try_<publisher>_browser`` function is
        called via the compatibility bridge).
        """
        raise NotImplementedError(
            f"{self.name} strategy must implement download() or set _uses_legacy_fn"
        )

    # Allow gradual migration: set to True to keep using the old
    # try_<publisher>_browser function for this publisher.
    _uses_legacy_fn: bool = True
