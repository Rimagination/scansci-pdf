"""IEEE publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class IEEEStrategy(BasePublisherStrategy):
    name = "IEEE"
    aliases = ("ieee",)
    doi_prefixes = ("10.1109/",)
    base_domains = ("ieeexplore.ieee.org", "ieee.org")
    sample_dois = ("10.1109/5.771073",)
    article_url_template = "https://ieeexplore.ieee.org/document/{doi}"
    pdf_url_templates = ()
    success_url_markers = ("ieeexplore.ieee.org/document/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Institutional Sign In", "Access through your institution")
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = (
        "input[name='idpSearch']",
        "#searchInstitution",
    )
    institution_result_selectors = ("input[name='idpSearch']",)
