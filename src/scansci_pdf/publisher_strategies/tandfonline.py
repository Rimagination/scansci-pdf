"""Taylor & Francis (Tandfonline) publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class TandfonlineStrategy(BasePublisherStrategy):
    name = "Tandfonline"
    aliases = ("tandfonline", "taylorandfrancis", "tandf")
    doi_prefixes = ("10.1080/",)
    base_domains = ("tandfonline.com",)
    sample_dois = ("10.1080/00268976.2019.1660819",)
    article_url_template = "https://www.tandfonline.com/doi/{doi}"
    pdf_url_templates = (
        "https://www.tandfonline.com/doi/pdf/{doi}",
    )
    success_url_markers = ("tandfonline.com/doi/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/ssostart", "/shibboleth", "/institutional")
    institution_input_selectors = (
        'input[placeholder*="institution"]',
        'input[placeholder*="Type the name"]',
        "#searchInstitution",
        "input[name='query']",
    )
    institution_result_selectors = ("#searchInstitution",)
