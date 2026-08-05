"""Springer / Springer Nature publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class SpringerStrategy(BasePublisherStrategy):
    name = "Springer"
    aliases = ("springer",)
    doi_prefixes = ("10.1007/", "10.1023/")
    base_domains = ("link.springer.com", "springer.com")
    sample_dois = ("10.1007/s00216-019-01847-0",)
    article_url_template = "https://link.springer.com/article/{doi}"
    pdf_url_templates = (
        "https://link.springer.com/content/pdf/{doi}.pdf",
    )
    success_url_markers = ("link.springer.com/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = (
        "Access through your institution",
        "Log in via your institution",
    )
    sso_url_patterns = ("/shibboleth", "/institutional-login")
    institution_input_selectors = (
        "#idp-search",
        "input[name='idpSearch']",
        "#searchInstitution",
    )
    institution_result_selectors = ("#idp-search",)
