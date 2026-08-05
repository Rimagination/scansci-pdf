"""ASCE Library publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class ASCEStrategy(BasePublisherStrategy):
    name = "ASCE"
    aliases = ("asce",)
    doi_prefixes = ("10.1061/",)
    base_domains = ("ascelibrary.org",)
    sample_dois = ("10.1061/(ASCE)0733-9364(2005)131:7(855)",)
    article_url_template = "https://ascelibrary.org/doi/{doi}"
    pdf_url_templates = (
        "https://ascelibrary.org/doi/pdf/{doi}?download=true",
        "https://ascelibrary.org/doi/pdf/{doi}",
    )
    success_url_markers = ("ascelibrary.org/doi/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)
