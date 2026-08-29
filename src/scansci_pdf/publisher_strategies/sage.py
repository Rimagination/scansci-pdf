"""SAGE Journals publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class SAGEStrategy(BasePublisherStrategy):
    name = "SAGE"
    aliases = ("sage",)
    doi_prefixes = ("10.1177/",)
    base_domains = ("journals.sagepub.com", "sagepub.com", "sage.cnpereading.com")
    sample_dois = ("10.1177/0956797615570357",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://journals.sagepub.com/doi/pdf/{doi}?download=true",
        "https://journals.sagepub.com/doi/pdf/{doi}",
        # CN mirror (sage.cnpereading.com): CN users' institutional access is
        # on this mirror (verified /doi/pdf/<doi> pattern live); requires a
        # CARSI login on the mirror itself, cookies then make it serve PDFs.
        "https://sage.cnpereading.com/doi/pdf/{doi}",
    )
    success_url_markers = ("journals.sagepub.com/doi/", "sage.cnpereading.com/doi/")
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
    )
    sso_text_markers = ("Access through your institution",)
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = ("input[name='search']", "#searchInstitution")
    institution_result_selectors = ("input[name='search']",)
