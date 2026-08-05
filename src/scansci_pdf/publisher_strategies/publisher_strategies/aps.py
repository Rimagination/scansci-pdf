"""APS (American Physical Society) publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class APSStrategy(BasePublisherStrategy):
    name = "APS"
    aliases = ("aps",)
    doi_prefixes = ("10.1103/",)
    base_domains = ("journals.aps.org", "aps.org")
    sample_dois = ("10.1103/PhysRevLett.120.200601",)
    article_url_template = "https://journals.aps.org/prl/abstract/{doi}"
    pdf_url_templates = (
        "https://journals.aps.org/prl/pdf/{doi}",
        "https://journals.aps.org/prb/pdf/{doi}",
        "https://journals.aps.org/pdf/{doi}",
    )
    success_url_markers = ("journals.aps.org/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)
