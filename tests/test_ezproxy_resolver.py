from scansci_pdf.publisher_pdf_resolver import PublisherPdfResolver


class FakePublisherPage:
    def __init__(self, url: str, html: str, candidates=None) -> None:
        self.url = url
        self._html = html
        self._candidates = candidates or []
        self.clicked = False
        self.clicked_index = None

    def content(self) -> str:
        return self._html

    def evaluate(self, script: str, *args):
        if "data-scansci-pdf-clicked" in script:
            self.clicked = True
            self.clicked_index = args[0]
            return True
        return self._candidates


def test_resolver_preserves_proxy_host_for_elsevier_metadata():
    page = FakePublisherPage(
        "https://www-sciencedirect-com.proxy.example.edu/science/article/pii/S1359645426006208",
        (
            '<script>"pdfDownload":{"isPdfFullText":true,'
            '"urlMetadata":{"queryParams":{"md5":"abc123","pid":"pid456"},'
            '"pii":"S1359645426006208","pdfExtension":".pdf",'
            '"path":"science/article/pii"}}</script>'
        ),
    )

    assert PublisherPdfResolver().resolve(page) == (
        "https://www-sciencedirect-com.proxy.example.edu/"
        "science/article/pii/S1359645426006208.pdf?md5=abc123&pid=pid456"
    )


def test_resolver_uses_springer_citation_pdf_url():
    page = FakePublisherPage(
        "https://link-springer-com.proxy.example.edu/article/10.1007/example",
        '<meta name="citation_pdf_url" content="/content/pdf/article.pdf">',
    )

    assert PublisherPdfResolver().resolve(page) == (
        "https://link-springer-com.proxy.example.edu/content/pdf/article.pdf"
    )


def test_resolver_ranks_acs_and_wiley_dom_candidates():
    acs_page = FakePublisherPage(
        "https://pubs-acs-org.proxy.example.edu/doi/10.1021/example",
        "<html></html>",
        [
            {"text": "Purchase", "href": "/purchase"},
            {"text": "Open PDF", "href": "/doi/pdf/10.1021/example", "aria": "Open PDF"},
            {"text": "Supporting information", "href": "/support.pdf"},
        ],
    )
    wiley_page = FakePublisherPage(
        "https://onlinelibrary-wiley-com.proxy.example.edu/doi/10.1002/example",
        "<html></html>",
        [{"text": "Download PDF", "href": "/doi/pdfdirect/10.1002/example"}],
    )
    resolver = PublisherPdfResolver()

    assert resolver.resolve(acs_page) == (
        "https://pubs-acs-org.proxy.example.edu/doi/pdf/10.1021/example"
    )
    assert resolver.resolve(wiley_page) == (
        "https://onlinelibrary-wiley-com.proxy.example.edu/doi/pdfdirect/10.1002/example"
    )


def test_resolver_operates_ranked_pdf_control_when_no_url_is_exposed():
    page = FakePublisherPage(
        "https://pubs-acs-org.proxy.example.edu/doi/10.1021/example",
        "<html></html>",
        [
            {"text": "Purchase PDF", "href": "", "controlIndex": 0},
            {"text": "Supporting information PDF", "href": "", "controlIndex": 1},
            {
                "text": "Download PDF",
                "href": "",
                "aria": "Download PDF",
                "controlIndex": 2,
            },
        ],
    )

    assert PublisherPdfResolver().resolve(page) == ""
    assert page.clicked is True
    assert page.clicked_index == 2
