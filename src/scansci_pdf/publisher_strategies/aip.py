"""AIP (American Institute of Physics) publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class AIPStrategy(BasePublisherStrategy):
    name = "AIP"
    aliases = ("aip",)
    doi_prefixes = ("10.1063/",)
    base_domains = ("pubs.aip.org", "aip.org")
    sample_dois = ("10.1063/1.1625376",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://pubs.aip.org/aip/apl/article-pdf/{doi}",
    )
    success_url_markers = ("pubs.aip.org/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)
