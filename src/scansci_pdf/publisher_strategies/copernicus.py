"""Copernicus Publications publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class CopernicusStrategy(BasePublisherStrategy):
    name = "Copernicus"
    aliases = ("copernicus", "egu")
    doi_prefixes = ("10.5194/",)
    base_domains = ("copernicus.org",)
    sample_dois = ("10.5194/hess-22-3433-2018",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://www.copernicus.org/publications/{doi}",
        "https://www.copernicus.org/articles/{doi}",
    )
    success_url_markers = ("copernicus.org/",)
    auth_url_markers = ()
    sso_text_markers = ()
    sso_url_patterns = ()
    institution_input_selectors = ()
    institution_result_selectors = ()
