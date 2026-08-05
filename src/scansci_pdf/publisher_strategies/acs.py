"""ACS (American Chemical Society) publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class ACSStrategy(BasePublisherStrategy):
    name = "ACS"
    aliases = ("acs",)
    doi_prefixes = ("10.1021/",)
    base_domains = ("pubs.acs.org",)
    sample_dois = ("10.1021/jacs.5b00936",)
    article_url_template = "https://pubs.acs.org/doi/{doi}"
    pdf_url_templates = (
        "https://pubs.acs.org/doi/pdf/{doi}?ref=article_openPDF",
        "https://pubs.acs.org/doi/pdf/{doi}",
        "https://pubs.acs.org/doi/epdf/{doi}",
    )
    success_url_markers = ("pubs.acs.org/doi/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
        "acs.org/sso",
    )
    sso_text_markers = (
        "Access through your institution",
        "Institutional login",
        "Find my institution",
        "Access via your institution",
    )
    sso_url_patterns = ("/action/showLogin", "/action/ssostart")
    institution_input_selectors = (
        "input#search-input",
        "input.institution-search__input",
        'input[name="search"]',
    )
    institution_result_selectors = (
        ".institution-search__item",
        ".institution-result",
    )
