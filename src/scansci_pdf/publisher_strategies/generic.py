"""Generic fallback publisher strategy for unknown publishers."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class GenericStrategy(BasePublisherStrategy):
    """Fallback strategy used when no publisher-specific strategy matches."""

    name = "Generic"
    aliases = ("generic", "unknown")
    doi_prefixes = ()
    base_domains = ()
    sample_dois = ()
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = ()
    success_url_markers = ()
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
        "shibboleth",
    )
    sso_text_markers = ("Access through your institution", "Institutional login")
    sso_url_patterns = (
        "/ssostart",
        "/shibboleth",
        "/saml",
        "/institutional-login",
        "/federation",
    )
    institution_input_selectors = (
        "#searchInstitution",
        "input[name='query']",
        "input[name='search']",
        "#institution-search",
    )
    institution_result_selectors = ("#searchInstitution",)
