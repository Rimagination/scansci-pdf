"""Wiley publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class WileyStrategy(BasePublisherStrategy):
    name = "Wiley"
    aliases = ("wiley",)
    doi_prefixes = ("10.1002/", "10.1111/")
    base_domains = ("onlinelibrary.wiley.com", "wiley.com")
    sample_dois = ("10.1002/adma.201706259",)
    article_url_template = "https://onlinelibrary.wiley.com/doi/{doi}"
    pdf_url_templates = (
        "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}",
        "https://onlinelibrary.wiley.com/doi/pdf/{doi}",
    )
    success_url_markers = ("onlinelibrary.wiley.com/doi/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = (
        "Access through your institution",
        "Institutional Login",
        "Institution Login",
    )
    sso_url_patterns = ("/ssostart",)
    institution_input_selectors = ("#searchInstitution",)
    institution_result_selectors = ("#searchInstitution",)
