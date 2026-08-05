"""Science / AAAS publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class ScienceStrategy(BasePublisherStrategy):
    name = "Science"
    aliases = ("science", "aaas")
    doi_prefixes = ("10.1126/",)
    base_domains = ("science.org", "sciencemag.org")
    sample_dois = ("10.1126/science.abc1234",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://www.science.org/doi/pdf/{doi}",
    )
    success_url_markers = ("science.org/doi/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)
