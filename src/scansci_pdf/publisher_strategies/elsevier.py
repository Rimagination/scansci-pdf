"""Elsevier / ScienceDirect / Cell Press publisher strategy."""

from .base import BasePublisherStrategy
from .registry import StrategyRegistry


@StrategyRegistry.register
class ElsevierStrategy(BasePublisherStrategy):
    name = "Elsevier"
    aliases = ("elsevier", "sciencedirect", "cellpress")
    doi_prefixes = (
        "10.1016/",
        "10.1016/j.",
        "10.1016/j.cell",
        "10.1016/j.oneear",
        "10.1016/j.cels",
        "10.1016/j.cub",
        "10.1016/j.neuron",
        "10.1016/j.molcel",
        "10.1016/j.devcel",
        "10.1016/j.immuni",
        "10.1016/j.chom",
        "10.1016/j.cmet",
        "10.1016/j.stem",
        "10.1016/j.celrep",
        "10.1016/j.isci",
        "10.1016/j.xcr",
        "10.1016/j.heliyon",
        "10.1016/j.ajhg",
    )
    base_domains = (
        "sciencedirect.com",
        "linkinghub.elsevier.com",
        "cell.com",
        "elsevier.com",
    )
    sample_dois = ("10.1016/j.cell.2022.01.001",)
    article_url_template = "https://doi.org/{doi}"
    pdf_url_templates = (
        "https://www.sciencedirect.com/science/article/pii/{doi_suffix}/pdfft",
    )
    success_url_markers = (
        "sciencedirect.com/science/article/pii/",
        "cell.com/",
    )
    auth_url_markers = (
        "id.tsinghua.edu.cn",
        "idp.tsinghua.edu.cn",
        "login.openathens.net",
        "shibboleth",
    )
    sso_text_markers = (
        "Access through your institution",
        "Institutional access",
        "Login via your institution",
    )
    sso_url_patterns = ("/shibboleth", "/institutional")
    institution_input_selectors = (
        "#institution-search",
        "input[name='query']",
        "#bdd-email",
    )
    institution_result_selectors = (
        "#institution-search",
        "input[name='query']",
    )
