from __future__ import annotations


def test_source_fetch_pdf_downloads_attachment_from_full_xml(monkeypatch):
    from scansci_pdf.sources import elsevier_api

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <full-text-retrieval-response xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">
      <xocs:attachment>
        <xocs:attachment-type>PDF</xocs:attachment-type>
        <xocs:attachment-eid>1-s2.0-S0006320725007013-main.pdf</xocs:attachment-eid>
        <xocs:attachment-category>MAIN</xocs:attachment-category>
        <xocs:attachment-page-count>14</xocs:attachment-page-count>
      </xocs:attachment>
    </full-text-retrieval-response>
    """
    pdf_bytes = b"%PDF-1.7\n" + (b"official" * 2000) + b"\n%%EOF\n"

    class FakeResponse:
        def __init__(self, status_code, content, content_type):
            self.status_code = status_code
            self.content = content
            self.text = content.decode("utf-8", errors="replace")
            self.headers = {"content-type": content_type}

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                return FakeResponse(200, xml.encode("utf-8"), "application/xml")
            if len(self.calls) == 2:
                return FakeResponse(200, pdf_bytes, "application/pdf")
            return FakeResponse(404, b"", "text/plain")

    session = FakeSession()
    monkeypatch.setattr(elsevier_api.requests, "Session", lambda: session)
    monkeypatch.setattr(elsevier_api, "_pdf_page_count", lambda _content: 14, raising=False)

    assert elsevier_api.fetch_pdf("10.1016/j.biocon.2025.111664", "test-key") == pdf_bytes
    assert session.trust_env is False
    assert session.calls[0][0].endswith(
        "/article/doi/10.1016/j.biocon.2025.111664"
    )
    assert session.calls[0][1]["params"] == {"view": "FULL"}
    assert session.calls[0][1]["headers"]["Accept"] == "application/xml"
    assert session.calls[1][0].endswith(
        "/object/eid/1-s2.0-S0006320725007013-main.pdf"
    )
    assert session.calls[1][1]["headers"]["Accept"] == "application/pdf"


def test_source_fetch_pdf_rejects_single_page_direct_preview(monkeypatch):
    from scansci_pdf.sources import elsevier_api

    preview_pdf = b"%PDF-1.7\n" + (b"preview" * 2000) + b"\n%%EOF\n"

    class FakeResponse:
        def __init__(self, status_code, content, content_type):
            self.status_code = status_code
            self.content = content
            self.text = content.decode("utf-8", errors="replace")
            self.headers = {"content-type": content_type}

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                return FakeResponse(400, b"<error>not entitled</error>", "application/xml")
            return FakeResponse(200, preview_pdf, "application/pdf")

    session = FakeSession()
    monkeypatch.setattr(elsevier_api.requests, "Session", lambda: session)
    monkeypatch.setattr(elsevier_api, "_pdf_page_count", lambda _content: 1, raising=False)

    assert elsevier_api.fetch_pdf("10.1016/j.biocon.2025.111664", "test-key") is None


def test_source_fetch_fulltext_requests_full_xml_view(monkeypatch):
    from scansci_pdf.sources import elsevier_api

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <full-text-retrieval-response>
      <originalText>
        <doc>
          <body>
            <section>
              <section-title>Introduction</section-title>
              <para>Full XML body.</para>
            </section>
          </body>
        </doc>
      </originalText>
    </full-text-retrieval-response>
    """

    class FakeResponse:
        status_code = 200
        content = xml.encode("utf-8")
        text = xml
        headers = {"content-type": "application/xml"}

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse()

    session = FakeSession()
    monkeypatch.setattr(elsevier_api.requests, "Session", lambda: session)

    data = elsevier_api.fetch_fulltext(
        "10.1016/j.nicl.2021.102600",
        api_key="test-key",
    )

    assert data is not None
    assert "Full XML body" in data["full_text"]
    assert session.calls[0][1]["params"] == {"view": "FULL"}
    assert session.calls[0][1]["headers"]["Accept"] == "application/xml"
