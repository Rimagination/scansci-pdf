"""IOP (Institute of Physics) publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class IOPStrategy(BasePublisherStrategy):
    name = "IOP"
    aliases = ("iop",)
    doi_prefixes = ("10.1088/",)
    base_domains = ("iopscience.iop.org", "iop.org")
    sample_dois = ("10.1088/1361-6463/ab60e7",)
    article_url_template = "https://iopscience.iop.org/article/{doi}"
    pdf_url_templates = (
        "https://iopscience.iop.org/article/{doi}/pdf",
    )
    success_url_markers = ("iopscience.iop.org/article/",)
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)
