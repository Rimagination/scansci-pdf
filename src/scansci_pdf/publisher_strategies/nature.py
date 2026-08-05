"""Nature / Nature Portfolio publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class NatureStrategy(BasePublisherStrategy):
    name = "Nature"
    aliases = ("nature",)
    doi_prefixes = ("10.1038/",)
    base_domains = ("nature.com",)
    sample_dois = ("10.1038/s41586-020-2649-2",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://www.nature.com/articles/{doi_suffix}.pdf",
    )
    success_url_markers = ("nature.com/articles/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='idpSearch']", "#searchInstitution")
    institution_result_selectors = ("input[name='idpSearch']",)
