"""Oxford Academic publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class OxfordStrategy(BasePublisherStrategy):
    name = "Oxford"
    aliases = ("oxford", "oup")
    doi_prefixes = ("10.1093/",)
    base_domains = ("academic.oup.com", "oup.com")
    sample_dois = ("10.1093/bioinformatics/btz076",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://academic.oup.com/downloadpdf/{doi}",
    )
    success_url_markers = ("academic.oup.com/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("#searchInstitution", "input[name='query']")
    institution_result_selectors = ("#searchInstitution",)
