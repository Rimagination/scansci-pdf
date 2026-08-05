"""ACM (Association for Computing Machinery) publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class ACMStrategy(BasePublisherStrategy):
    name = "ACM"
    aliases = ("acm",)
    doi_prefixes = ("10.1145/",)
    base_domains = ("dl.acm.org", "acm.org")
    sample_dois = ("10.1145/3292500.3330893",)
    article_url_template = "https://dl.acm.org/doi/{doi}"
    pdf_url_templates = (
        "https://dl.acm.org/doi/pdf/{doi}",
    )
    success_url_markers = ("dl.acm.org/doi/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)
