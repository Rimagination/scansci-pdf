"""Royal Society Publishing strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class RoyalSocietyStrategy(BasePublisherStrategy):
    name = "Royal Society"
    aliases = ("royalsociety", "royal_society", "royalsocietypublishing")
    doi_prefixes = ("10.1098/",)
    base_domains = ("royalsocietypublishing.org",)
    sample_dois = ("10.1098/rspa.2019.0567",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://royalsocietypublishing.org/doi/pdf/{doi}",
        "https://royalsocietypublishing.org/doi/epdf/{doi}",
    )
    success_url_markers = ("royalsocietypublishing.org/doi/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)
