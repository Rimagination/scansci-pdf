"""Publisher profiles for deterministic browser workflows.

INSTITUTION RECIPE SYSTEM
=========================
Each publisher profile has GENERIC markers that work for ANY institution (auth_url_markers,
auth_title_markers, sso_text_markers, institution_input_selectors). These are used for
detecting SSO pages, searching for institutions, and clicking generic UI elements.

Additionally, profiles may have institution_result_selectors — these are Tsinghua-specific
overrides that supplement the generic text-based selectors. They are ONLY activated when
the user's configured institution IS Tsinghua University (gated by
_institution_query_is_tsinghua() in publisher_batch.py).

To add support for another institution's specialized selectors, follow the same pattern:
1. Add the institution-specific selectors to the profile's institution_result_selectors
2. Add a gating condition in publisher_batch.py (e.g., _institution_query_is_peking())
3. The generic text-based selectors will still work for unrecognized institutions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote, urlparse


@dataclass(frozen=True)
class PdfSourcePathTemplate:
    """Route a publisher landing/source URL path to a PDF path."""

    domain: str
    path_prefix: str
    path_template: str


@dataclass(frozen=True)
class PublisherProfile:
    """Declarative rules for a publisher-specific download workflow."""

    name: str
    article_url_template: str
    pdf_url_templates: tuple[str, ...]
    success_url_markers: tuple[str, ...]
    auth_url_markers: tuple[str, ...]
    auth_title_markers: tuple[str, ...]
    sso_text_markers: tuple[str, ...]
    aliases: tuple[str, ...] = field(default_factory=tuple)
    doi_prefixes: tuple[str, ...] = field(default_factory=tuple)
    sample_dois: tuple[str, ...] = field(default_factory=tuple)
    base_domains: tuple[str, ...] = field(default_factory=tuple)
    pdf_source_path_templates: tuple[PdfSourcePathTemplate, ...] = field(default_factory=tuple)
    pdf_url_markers: tuple[str, ...] = (
        "/doi/pdf/",
        "/doi/epdf/",
        "/pdf/",
        "/pdf",
        "pdf",
    )
    pdf_link_text_markers: tuple[str, ...] = ("pdf",)
    supplementary_url_markers: tuple[str, ...] = (
        "suppl_file",
        "suppdata",
        "supporting-information",
        "supplementary",
    )
    institution_input_selectors: tuple[str, ...] = field(default_factory=tuple)
    institution_result_selectors: tuple[str, ...] = field(default_factory=tuple)

    def article_url(self, doi: str) -> str:
        if self.name.lower() == "aps":
            return _aps_article_url(doi) or self.article_url_template.format(doi=doi)
        return self.article_url_template.format(doi=doi)

    def pdf_urls(self, doi: str) -> list[str]:
        doi_suffix = doi.split("/", 1)[-1] if "/" in doi else doi
        values = {
            "doi": doi,
            "doi_quoted": quote(doi, safe=""),
            "doi_suffix": doi_suffix,
            "doi_suffix_quoted": quote(doi_suffix, safe=""),
        }
        return [template.format(**values) for template in self.pdf_url_templates]


def _aps_article_url(doi: str) -> str:
    suffix = doi.split("/", 1)[-1] if "/" in doi else doi
    journal_codes = (
        ("PhysRevLett.", "prl"),
        ("PhysRevA.", "pra"),
        ("PhysRevB.", "prb"),
        ("PhysRevC.", "prc"),
        ("PhysRevD.", "prd"),
        ("PhysRevE.", "pre"),
        ("PhysRevX.", "prx"),
        ("PhysRevApplied.", "prapplied"),
        ("PhysRevResearch.", "prresearch"),
        ("RevModPhys.", "rmp"),
        ("PRXQuantum.", "prxquantum"),
    )
    for prefix, journal_code in journal_codes:
        if suffix.startswith(prefix):
            return f"https://journals.aps.org/{journal_code}/abstract/{doi}"
    return ""


ACS_PROFILE = PublisherProfile(
    name="ACS",
    article_url_template="https://pubs.acs.org/doi/{doi}",
    pdf_url_templates=(
        "https://pubs.acs.org/doi/pdf/{doi}?ref=article_openPDF",
        "https://pubs.acs.org/doi/pdf/{doi}",
        "https://pubs.acs.org/doi/epdf/{doi}",
    ),
    success_url_markers=("pubs.acs.org/doi/",),
    auth_url_markers=(
        "login.openathens.net",
        "/action/ssostart",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=(
        "OpenAthens",
        "Identity",
        "Login",
        "Sign In",
    ),
    sso_text_markers=(
        "access through",
        "access through institution",
        "access through your institution",
        "institutional access",
        "log in through your institution",
    ),
    aliases=("acs", "american-chemical-society"),
    doi_prefixes=("10.1021",),
    sample_dois=("10.1021/acs.est.6c00693",),
    base_domains=("pubs.acs.org",),
    institution_input_selectors=(
        "input[placeholder='Search By University or Organization']",
        "input[placeholder*='University']",
        "input[placeholder*='Organization']",
        "#searchInstitution",
    ),
    # Tsinghua-specific: only used when institution IS Tsinghua (see module docstring)
    institution_result_selectors=(
        "button:has-text('Tsinghua University')",
        "button:has-text('清华大学')",
        "[role='button']:has-text('Tsinghua')",
        "li:has-text('Tsinghua')",
        "li:has-text('清华')",
    ),
)

ACM_PROFILE = PublisherProfile(
    name="ACM",
    article_url_template="https://dl.acm.org/doi/{doi}",
    pdf_url_templates=(
        "https://dl.acm.org/doi/pdf/{doi}",
    ),
    success_url_markers=("dl.acm.org/doi/",),
    auth_url_markers=(
        "login.openathens.net",
        "dl.acm.org/action/showLogin",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "log in via your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("acm", "association-for-computing-machinery"),
    doi_prefixes=("10.1145", "10.5555"),
    sample_dois=("10.1145/3448016.3452834",),
    base_domains=("dl.acm.org", "acm.org"),
    pdf_url_markers=("/doi/pdf/", "pdf"),
)

APS_PROFILE = PublisherProfile(
    name="APS",
    article_url_template="https://link.aps.org/doi/{doi}",
    pdf_url_templates=(
        "https://link.aps.org/pdf/{doi}",
    ),
    success_url_markers=("link.aps.org/doi/", "journals.aps.org/prl/abstract/", "journals.aps.org/prl/pdf/"),
    auth_url_markers=(
        "connect.openathens.net",
        "login.openathens.net",
        "journals.aps.org/login",
        "journals.aps.org/account",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "log in via your institution",
        "access through your institution",
        "provided by your institution",
        "openathens",
    ),
    aliases=("aps", "american-physical-society"),
    doi_prefixes=("10.1103",),
    sample_dois=("10.1103/PhysRevLett.128.161102",),
    base_domains=("journals.aps.org", "link.aps.org"),
    pdf_url_markers=("/pdf/", "/pdf", "pdf"),
)

ANNUAL_REVIEWS_PROFILE = PublisherProfile(
    name="Annual Reviews",
    article_url_template="https://annualreviews.org/doi/{doi}",
    pdf_url_templates=(
        "https://annualreviews.org/doi/pdf/{doi}",
    ),
    success_url_markers=("annualreviews.org/doi/", "annualreviews.org/content/journals/"),
    auth_url_markers=(
        "login.openathens.net",
        "/action/showLogin",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "institutional sign in",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("annual-reviews", "annualreviews"),
    doi_prefixes=("10.1146",),
    sample_dois=("10.1146/annurev-phyto-011325-012824",),
    base_domains=("annualreviews.org",),
    pdf_url_markers=("/doi/pdf/", "pdf"),
)

FRONTIERS_PROFILE = PublisherProfile(
    name="Frontiers",
    article_url_template="https://doi.org/{doi}",
    pdf_url_templates=(
        "https://www.frontiersin.org/articles/{doi}/pdf",
    ),
    success_url_markers=("frontiersin.org/articles/",),
    auth_url_markers=(
        "login.openathens.net",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "institutional access",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("frontiers",),
    doi_prefixes=("10.3389",),
    sample_dois=("10.3389/fmicb.2026.1831710",),
    base_domains=("www.frontiersin.org", "frontiersin.org"),
    pdf_url_markers=("/pdf", "pdf"),
)

WILEY_PROFILE = PublisherProfile(
    name="Wiley",
    article_url_template="https://onlinelibrary.wiley.com/doi/{doi}",
    pdf_url_templates=(
        "https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}",
        "https://onlinelibrary.wiley.com/doi/pdf/{doi}",
        "https://onlinelibrary.wiley.com/doi/epdf/{doi}",
    ),
    success_url_markers=("onlinelibrary.wiley.com/doi/",),
    auth_url_markers=(
        "login.openathens.net",
        "/action/showlogin",
        "/login",
        "/saml/",
        "/sso/",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "shibboleth",
        "openathens",
    ),
    aliases=("wiley", "wiley-online-library", "onlinelibrary"),
    doi_prefixes=("10.1002", "10.1111"),
    sample_dois=("10.1002/adfm.202525261", "10.1002/adfm.76235"),
    base_domains=("onlinelibrary.wiley.com",),
    pdf_url_markers=("/doi/pdf/", "/doi/epdf/", "/doi/pdfdirect/", "pdf"),
    supplementary_url_markers=(
        "suppl_file",
        "suppdata",
        "supporting-information",
        "supplementary",
        "/pb-assets/",
        "wechat-wiley-chem",
    ),
)

ELSEVIER_PROFILE = PublisherProfile(
    name="Elsevier",
    article_url_template="https://doi.org/{doi}",
    pdf_url_templates=(),
    success_url_markers=(
        "linkinghub.elsevier.com/retrieve/pii",
        "sciencedirect.com/science/article/",
        "elsevier.com/",
    ),
    auth_url_markers=(
        "login.elsevier.com",
        "id.elsevier.com",
        "login.openathens.net",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Sign in", "Login", "Identity"),
    sso_text_markers=(
        "institutional sign in",
        "sign in via your institution",
        "access through your organization",
        "access through your institution",
        "openathens",
    ),
    aliases=("elsevier", "sciencedirect", "science-direct"),
    doi_prefixes=("10.1016",),
    sample_dois=("10.1016/j.watres.2024.121507",),
    base_domains=("www.sciencedirect.com", "sciencedirect.com", "linkinghub.elsevier.com"),
    pdf_url_markers=("/pdfft", "/pdf", "download", "pdf"),
    supplementary_url_markers=(
        "suppl_file",
        "suppdata",
        "supporting-information",
        "supplementary",
        "/content/image/",
        "-mmc",
        "_mmc",
    ),
    institution_input_selectors=(
        "input[type='search']",
        "input[type='text']",
        "input",
    ),
    institution_result_selectors=(
        # Tsinghua-specific (gated by _institution_query_is_tsinghua)
        "button:has-text('清华大学')",
        "button:has-text('Tsinghua University')",
        "[role='button']:has-text('Tsinghua')",
        "li:has-text('Tsinghua')",
    ),
)

IEEE_PROFILE = PublisherProfile(
    name="IEEE",
    article_url_template="https://doi.org/{doi}",
    pdf_url_templates=(),
    success_url_markers=("ieeexplore.ieee.org/document/",),
    auth_url_markers=(
        "login.openathens.net",
        "ieeexplore.ieee.org/servlet/wayf",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "institutional access",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("ieee", "ieee-xplore"),
    doi_prefixes=("10.1109",),
    sample_dois=("10.1109/jstqe.2026.3687110",),
    base_domains=("ieeexplore.ieee.org", "ieee.org"),
    pdf_url_markers=("stampPDF", "getPDF", "pdf"),
    institution_input_selectors=(
        "input[aria-label='Search for your Institution']",
        "input[aria-label*='Institution']",
        "input.inst-typeahead-input",
        "xpath=(//*[normalize-space()='Search for your Institution']/following::input[1])",
    ),
    # Tsinghua-specific (gated by _institution_query_is_tsinghua)
    institution_result_selectors=(
        "button:has-text('Tsinghua University')",
        "[role='button']:has-text('Tsinghua University')",
        "[role='option']:has-text('Tsinghua University')",
        "li:has-text('Tsinghua University')",
    ),
)

IOP_PROFILE = PublisherProfile(
    name="IOP",
    article_url_template="https://iopscience.iop.org/article/{doi}",
    pdf_url_templates=(
        "https://iopscience.iop.org/article/{doi}/pdf",
    ),
    success_url_markers=("iopscience.iop.org/article/",),
    auth_url_markers=(
        "myiopscience.iop.org/signin",
        "connect.openathens.net",
        "login.openathens.net",
        "sesame.cld.iop.org",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "access this article",
        "openathens",
    ),
    aliases=("iop", "iopscience"),
    doi_prefixes=("10.1088",),
    sample_dois=("10.1088/1361-648x/ae72dd",),
    base_domains=("iopscience.iop.org",),
    pdf_url_markers=("/pdf", "pdf"),
    institution_input_selectors=(
        "input[type='search']",
        "input[type='text']",
        "input",
    ),
    # Tsinghua-specific (gated by _institution_query_is_tsinghua)
    institution_result_selectors=(
        "button:has-text('Tsinghua University')",
        "button:has-text('清华大学')",
        "[role='button']:has-text('Tsinghua')",
        "[role='option']:has-text('Tsinghua')",
        "li:has-text('Tsinghua')",
    ),
)

RSC_PROFILE = PublisherProfile(
    name="RSC",
    article_url_template="https://doi.org/{doi}",
    pdf_url_templates=(
        "https://pubs.rsc.org/en/content/articlepdf/{doi}",
    ),
    success_url_markers=("pubs.rsc.org/",),
    auth_url_markers=(
        "login.openathens.net",
        "pubs.rsc.org/en/account",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("rsc", "royal-society-of-chemistry"),
    doi_prefixes=("10.1039",),
    sample_dois=("10.1039/d5cp03829d", "10.1039/d5nj03688g"),
    base_domains=("pubs.rsc.org",),
    pdf_source_path_templates=(
        PdfSourcePathTemplate(
            domain="pubs.rsc.org",
            path_prefix="/en/content/articlelanding/",
            path_template="/en/content/articlepdf/{source_path_after_prefix}",
        ),
    ),
    pdf_url_markers=("/content/articlepdf/", "/pdf", "download", "pdf"),
)

SPRINGER_PROFILE = PublisherProfile(
    name="Springer Nature",
    article_url_template="https://doi.org/{doi}",
    pdf_url_templates=(
        "https://link.springer.com/content/pdf/{doi_quoted}.pdf",
        "https://www.nature.com/articles/{doi_suffix}.pdf",
    ),
    success_url_markers=("link.springer.com/article/", "nature.com/articles/"),
    auth_url_markers=(
        "login.openathens.net",
        "wayf.springernature.com",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Sign in", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("springer", "springer-nature", "nature"),
    doi_prefixes=("10.1007", "10.1038"),
    sample_dois=("10.1038/s41586-020-2649-2",),
    base_domains=("link.springer.com", "www.nature.com", "nature.com"),
    pdf_source_path_templates=(
        PdfSourcePathTemplate(
            domain="link.springer.com",
            path_prefix="/article/",
            path_template="/content/pdf/{doi_quoted}.pdf",
        ),
        PdfSourcePathTemplate(
            domain="nature.com",
            path_prefix="/articles/",
            path_template="{source_path}.pdf",
        ),
    ),
    pdf_url_markers=("/content/pdf/", ".pdf", "pdf"),
)

WORLD_SCIENTIFIC_PROFILE = PublisherProfile(
    name="World Scientific",
    article_url_template="https://www.worldscientific.com/doi/{doi}",
    pdf_url_templates=(
        "https://www.worldscientific.com/doi/pdf/{doi}",
    ),
    success_url_markers=("worldscientific.com/doi/",),
    auth_url_markers=(
        "login.openathens.net",
        "/action/showLogin",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("world-scientific", "worldscientific"),
    doi_prefixes=("10.1142",),
    sample_dois=("10.1142/s0218194026500348",),
    base_domains=("www.worldscientific.com", "worldscientific.com"),
    pdf_url_markers=("/doi/pdf/", "pdf"),
    institution_input_selectors=(
        "input[placeholder='Type the name of your institution']",
        "input[placeholder*='institution']",
        "input[type='text']",
        "input",
    ),
    # Tsinghua-specific (gated by _institution_query_is_tsinghua)
    institution_result_selectors=(
        "button:has-text('Tsinghua University')",
        "a:has-text('Tsinghua University')",
        "[role='option']:has-text('Tsinghua')",
        ".ui-menu-item:has-text('Tsinghua')",
        "li:has-text('Tsinghua')",
    ),
)

AIP_PROFILE = PublisherProfile(
    name="AIP Publishing",
    article_url_template="https://pubs.aip.org/doi/{doi}",
    pdf_url_templates=(
        "https://pubs.aip.org/doi/epdf/{doi}",
        "https://pubs.aip.org/doi/pdf/{doi}",
    ),
    success_url_markers=("pubs.aip.org/doi/", "pubs.aip.org/"),
    auth_url_markers=(
        "login.openathens.net",
        "/action/showLogin",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("aip", "aip-publishing", "american-institute-of-physics"),
    doi_prefixes=("10.1063",),
    sample_dois=("10.1063/5.0237567",),
    base_domains=("pubs.aip.org",),
    pdf_url_markers=("/doi/epdf/", "/doi/pdf/", "pdf"),
    supplementary_url_markers=(
        "supplementary",
        "supplemental",
        "supporting-information",
        "suppl_file",
    ),
)

AMS_PROFILE = PublisherProfile(
    name="AMS",
    article_url_template="https://journals.ametsoc.org/doi/{doi}",
    pdf_url_templates=(
        "https://journals.ametsoc.org/doi/epdf/{doi}",
        "https://journals.ametsoc.org/doi/pdf/{doi}",
    ),
    success_url_markers=("journals.ametsoc.org/",),
    auth_url_markers=(
        "login.openathens.net",
        "/action/showLogin",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("ams", "ametsoc", "american-meteorological-society"),
    doi_prefixes=("10.1175",),
    sample_dois=("10.1175/aies-d-23-0093.1",),
    base_domains=("journals.ametsoc.org", "ametsoc.org"),
    pdf_url_markers=("/doi/epdf/", "/doi/pdf/", "pdf"),
    supplementary_url_markers=(
        "supplementary",
        "supplemental",
        "supporting-information",
        "suppl_file",
    ),
)

COPERNICUS_PROFILE = PublisherProfile(
    name="Copernicus",
    article_url_template="https://doi.org/{doi}",
    pdf_url_templates=(),
    success_url_markers=(".copernicus.org/articles/", "copernicus.org/articles/"),
    auth_url_markers=(),
    auth_title_markers=(),
    sso_text_markers=(),
    aliases=("copernicus", "copernicus-publications"),
    doi_prefixes=("10.5194",),
    sample_dois=("10.5194/acp-24-1-2024",),
    base_domains=("copernicus.org",),
    pdf_url_markers=(".pdf", "/articles/", "pdf"),
    supplementary_url_markers=("assets", "supplement", "supplementary"),
)

MDPI_PROFILE = PublisherProfile(
    name="MDPI",
    article_url_template="https://doi.org/{doi}",
    pdf_url_templates=(),
    success_url_markers=("mdpi.com/",),
    auth_url_markers=(),
    auth_title_markers=(),
    sso_text_markers=(),
    aliases=("mdpi", "mdpi-ag"),
    doi_prefixes=("10.3390",),
    sample_dois=("10.3390/foods10081757",),
    base_domains=("www.mdpi.com", "mdpi.com"),
    pdf_url_markers=("/pdf", "pdf"),
    supplementary_url_markers=("supplementary", "supplement", "/s1", "table-s", "figure-s"),
)

OXFORD_ACADEMIC_PROFILE = PublisherProfile(
    name="Oxford Academic",
    article_url_template="https://doi.org/{doi}",
    pdf_url_templates=(
        "https://academic.oup.com/doi/pdf/{doi}",
        "https://academic.oup.com/doi/epdf/{doi}",
    ),
    success_url_markers=("academic.oup.com/",),
    auth_url_markers=(
        "login.openathens.net",
        "/saml/",
        "/sso/",
        "/login",
        "/my-account/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("oxfordacademic", "oxford-academic", "oup", "oxford-university-press"),
    doi_prefixes=("10.1093",),
    sample_dois=("10.1093/nar/gkaa892",),
    base_domains=("academic.oup.com",),
    pdf_url_markers=("/doi/pdf/", "/doi/epdf/", "/article-pdf/", "pdf"),
    supplementary_url_markers=("supplementary", "supplement", "suppl_file"),
)

PLOS_PROFILE = PublisherProfile(
    name="PLOS",
    article_url_template="https://doi.org/{doi}",
    pdf_url_templates=(),
    success_url_markers=("journals.plos.org/",),
    auth_url_markers=(),
    auth_title_markers=(),
    sso_text_markers=(),
    aliases=("plos", "public-library-of-science"),
    doi_prefixes=("10.1371",),
    sample_dois=("10.1371/journal.pone.0000001",),
    base_domains=("journals.plos.org", "plos.org"),
    pdf_url_markers=("type=printable", "/article/file", "pdf"),
    supplementary_url_markers=("type=supplementary", "supplementary", "figure/image", "thumbnail"),
)

PNAS_PROFILE = PublisherProfile(
    name="PNAS",
    article_url_template="https://www.pnas.org/doi/{doi}",
    pdf_url_templates=(
        "https://www.pnas.org/doi/epdf/{doi}",
        "https://www.pnas.org/doi/pdf/{doi}?download=true",
        "https://www.pnas.org/doi/pdf/{doi}",
    ),
    success_url_markers=("pnas.org/doi/", "www.pnas.org/doi/"),
    auth_url_markers=(
        "login.openathens.net",
        "/action/showLogin",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("pnas", "proceedings-national-academy-sciences"),
    doi_prefixes=("10.1073",),
    sample_dois=("10.1073/pnas.2309123120",),
    base_domains=("www.pnas.org", "pnas.org"),
    pdf_url_markers=("/doi/epdf/", "/doi/pdf/", "download=true", "pdf"),
    supplementary_url_markers=("supplementary", "suppl_file", "supporting-information"),
)

ROYAL_SOCIETY_PUBLISHING_PROFILE = PublisherProfile(
    name="Royal Society Publishing",
    article_url_template="https://royalsocietypublishing.org/doi/{doi}",
    pdf_url_templates=(
        "https://royalsocietypublishing.org/doi/pdf/{doi}",
    ),
    success_url_markers=("royalsocietypublishing.org/doi/",),
    auth_url_markers=(
        "login.openathens.net",
        "/action/showLogin",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("royalsocietypublishing", "royal-society-publishing", "the-royal-society"),
    doi_prefixes=("10.1098",),
    sample_dois=("10.1098/rsos.150470",),
    base_domains=("royalsocietypublishing.org",),
    pdf_url_markers=("/doi/pdf/", "pdf"),
    supplementary_url_markers=("supplementary", "supplement", "suppl_file"),
)

SCIENCE_PROFILE = PublisherProfile(
    name="Science",
    article_url_template="https://www.science.org/doi/{doi}",
    pdf_url_templates=(
        "https://www.science.org/doi/epdf/{doi}",
        "https://www.science.org/doi/pdf/{doi}",
        "https://www.science.org/doi/pdf/{doi}?download=true",
    ),
    success_url_markers=("science.org/doi/", "www.science.org/doi/"),
    auth_url_markers=(
        "login.openathens.net",
        "/action/showLogin",
        "/saml/",
        "/sso/",
        "/login",
    ),
    auth_title_markers=("OpenAthens", "Institutional Login", "Login", "Identity"),
    sso_text_markers=(
        "institutional login",
        "log in through your institution",
        "access through your institution",
        "openathens",
    ),
    aliases=("science", "aaas", "american-association-for-the-advancement-of-science"),
    doi_prefixes=("10.1126",),
    sample_dois=("10.1126/sciadv.adp3964",),
    base_domains=("www.science.org", "science.org"),
    pdf_url_markers=("/doi/epdf/", "/doi/pdf/", "download=true", "pdf"),
    supplementary_url_markers=("supplementary", "supplement", "suppl_file"),
)

PUBLISHER_PROFILES = {
    "acs": ACS_PROFILE,
    "aip": AIP_PROFILE,
    "ams": AMS_PROFILE,
    "acm": ACM_PROFILE,
    "annual-reviews": ANNUAL_REVIEWS_PROFILE,
    "aps": APS_PROFILE,
    "copernicus": COPERNICUS_PROFILE,
    "wiley": WILEY_PROFILE,
    "elsevier": ELSEVIER_PROFILE,
    "frontiers": FRONTIERS_PROFILE,
    "ieee": IEEE_PROFILE,
    "iop": IOP_PROFILE,
    "mdpi": MDPI_PROFILE,
    "oxfordacademic": OXFORD_ACADEMIC_PROFILE,
    "plos": PLOS_PROFILE,
    "pnas": PNAS_PROFILE,
    "royalsocietypublishing": ROYAL_SOCIETY_PUBLISHING_PROFILE,
    "rsc": RSC_PROFILE,
    "science": SCIENCE_PROFILE,
    "springer": SPRINGER_PROFILE,
    "world-scientific": WORLD_SCIENTIFIC_PROFILE,
}

_PROFILE_ALIASES = {
    alias.lower(): profile
    for profile in PUBLISHER_PROFILES.values()
    for alias in (profile.name, *profile.aliases)
}


def list_publisher_profiles() -> list[str]:
    """Return stable publisher profile keys available to deterministic workflows."""
    return sorted(PUBLISHER_PROFILES)


def get_publisher_profile(name: str) -> PublisherProfile:
    """Resolve a publisher profile by key, display name, or alias."""
    key = name.strip().lower()
    profile = PUBLISHER_PROFILES.get(key) or _PROFILE_ALIASES.get(key)
    if profile is None:
        available = ", ".join(list_publisher_profiles())
        raise ValueError(f"Unknown publisher profile '{name}'. Available: {available}")
    return profile


def infer_publisher_profile(doi: str) -> PublisherProfile | None:
    """Infer a likely publisher profile from stable DOI prefixes."""
    normalized = doi.strip().lower()
    for profile in PUBLISHER_PROFILES.values():
        if any(normalized.startswith(prefix.lower()) for prefix in profile.doi_prefixes):
            return profile
    return None


def infer_publisher_profile_from_url(url: str) -> PublisherProfile | None:
    """Infer a publisher profile from a resolved landing or publisher URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    lower_url = url.lower()
    for profile in PUBLISHER_PROFILES.values():
        domains = profile.base_domains or ()
        if any(host == domain or host.endswith(f".{domain}") for domain in domains):
            return profile
        if any(marker.lower() in lower_url for marker in profile.success_url_markers):
            return profile
    return None
