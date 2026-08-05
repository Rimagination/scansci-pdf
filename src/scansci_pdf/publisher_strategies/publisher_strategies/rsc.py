"""RSC (Royal Society of Chemistry) publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class RSCStrategy(BasePublisherStrategy):
    name = "RSC"
    aliases = ("rsc",)
    doi_prefixes = ("10.1039/",)
    base_domains = ("pubs.rsc.org", "rsc.org")
    sample_dois = ("10.1039/C5CS00900D",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://pubs.rsc.org/en/content/articlepdf/{doi}",
    )
    success_url_markers = ("pubs.rsc.org/en/content/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)
