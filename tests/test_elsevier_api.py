from __future__ import annotations

from pathlib import Path


def test_elsevier_api_downloads_main_pdf_from_xml_attachment_eid(
    monkeypatch, tmp_path: Path
):
    import requests

    from scansci_pdf.publisher_strategies import try_elsevier_api

    pdf_bytes = b"%PDF-1.4\n" + (b"0" * 1200) + b"\n%%EOF\n"
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <full-text-retrieval-response xmlns:xocs="http://www.elsevier.com/xml/xocs/dtd">
      <xocs:attachment>
        <xocs:attachment-type>PDF</xocs:attachment-type>
        <xocs:attachment-eid>1-s2.0-S0006320725007013-main.pdf</xocs:attachment-eid>
        <xocs:attachment-category>MAIN</xocs:attachment-category>
        <xocs:attachment-size>11219763</xocs:attachment-size>
        <xocs:attachment-page-count>14</xocs:attachment-page-count>
      </xocs:attachment>
      <xocs:attachment>
        <xocs:attachment-type>PDF</xocs:attachment-type>
        <xocs:attachment-eid>1-s2.0-S0006320725007013-mmc1.pdf</xocs:attachment-eid>
        <xocs:attachment-category>SUPPLEMENTARY</xocs:attachment-category>
      </xocs:attachment>
    </full-text-retrieval-response>
    """

    class FakeResponse:
        def __init__(self, content: bytes, content_type: str):
            self.status_code = 200
            self.content = content
            self.headers = {"Content-Type": content_type, "X-ELS-Status": "OK"}
            self.text = content.decode("utf-8", errors="replace")

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.cookies = []
            self.calls: list[tuple[str, dict]] = []

        def get(self, url, *, headers=None, **kwargs):
            self.calls.append((url, {"headers": dict(headers or {}), **kwargs}))
            if len(self.calls) == 1:
                return FakeResponse(xml.encode("utf-8"), "application/xml")
            return FakeResponse(pdf_bytes, "application/pdf")

    session = FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: session)

    output_path = tmp_path / "elsevier.pdf"
    result = try_elsevier_api(
        "10.1016/j.biocon.2025.111664",
        output_path,
        {"elsevier_api_key": "test-key", "min_pdf_size_bytes": 1000},
    )

    assert result is not None
    assert result["success"] is True
    assert output_path.read_bytes() == pdf_bytes
    assert session.calls[0][0] == (
        "https://api.elsevier.com/content/article/doi/"
        "10.1016/j.biocon.2025.111664"
    )
    assert session.calls[0][1]["headers"]["Accept"] == "application/xml"
    assert session.calls[0][1]["params"] == {"view": "FULL"}
    assert session.calls[1][0] == (
        "https://api.elsevier.com/content/object/eid/"
        "1-s2.0-S0006320725007013-main.pdf"
    )
    assert session.calls[1][1]["headers"]["Accept"] == "application/pdf"


def test_elsevier_api_falls_back_to_default_xml_when_full_xml_is_denied(
    monkeypatch, tmp_path: Path
):
    import requests

    from scansci_pdf.publisher_strategies import try_elsevier_api

    pdf_bytes = b"%PDF-1.4\n" + (b"1" * 1200) + b"\n%%EOF\n"
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <full-text-retrieval-response>
      <attachment>
        <attachment-type>PDF</attachment-type>
        <attachment-eid>1-s2.0-S0006320725007013-main.pdf</attachment-eid>
        <attachment-category>MAIN</attachment-category>
      </attachment>
    </full-text-retrieval-response>
    """

    class FakeResponse:
        def __init__(self, content: bytes, content_type: str, status_code: int = 200):
            self.status_code = status_code
            self.content = content
            self.headers = {"Content-Type": content_type, "X-ELS-Status": "OK"}
            self.text = content.decode("utf-8", errors="replace")

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.cookies = []
            self.calls: list[tuple[str, dict]] = []

        def get(self, url, *, headers=None, **kwargs):
            self.calls.append((url, {"headers": dict(headers or {}), **kwargs}))
            if len(self.calls) == 1:
                return FakeResponse(
                    b"<error>view denied</error>",
                    "application/xml",
                    status_code=400,
                )
            if len(self.calls) == 2:
                return FakeResponse(xml.encode("utf-8"), "application/xml")
            return FakeResponse(pdf_bytes, "application/pdf")

    session = FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: session)

    output_path = tmp_path / "elsevier.pdf"
    result = try_elsevier_api(
        "10.1016/j.biocon.2025.111664",
        output_path,
        {"elsevier_api_key": "test-key", "min_pdf_size_bytes": 1000},
    )

    assert result is not None
    assert output_path.read_bytes() == pdf_bytes
    assert [call[1]["headers"]["Accept"] for call in session.calls] == [
        "application/xml",
        "application/xml",
        "application/pdf",
    ]
    assert session.calls[0][1]["params"] == {"view": "FULL"}
    assert "params" not in session.calls[1][1]
    assert session.calls[2][0] == (
        "https://api.elsevier.com/content/object/eid/"
        "1-s2.0-S0006320725007013-main.pdf"
    )


def test_elsevier_api_requests_full_xml_view_before_object_download(
    monkeypatch, tmp_path: Path
):
    import requests

    from scansci_pdf.publisher_strategies import try_elsevier_api

    pdf_bytes = b"%PDF-1.4\n" + (b"fulltext" * 200) + b"\n%%EOF\n"
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <full-text-retrieval-response>
      <web-pdf>
        <attachment-eid>1-s2.0-S0006320725007013-main.pdf</attachment-eid>
        <filesize>10710588</filesize>
        <web-pdf-purpose>MAIN</web-pdf-purpose>
        <web-pdf-page-count>14</web-pdf-page-count>
      </web-pdf>
    </full-text-retrieval-response>
    """

    class FakeResponse:
        def __init__(self, content: bytes, content_type: str):
            self.status_code = 200
            self.content = content
            self.headers = {"Content-Type": content_type, "X-ELS-Status": "OK"}
            self.text = content.decode("utf-8", errors="replace")

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.cookies = []
            self.calls: list[tuple[str, dict]] = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if len(self.calls) == 1:
                return FakeResponse(xml.encode("utf-8"), "application/xml")
            return FakeResponse(pdf_bytes, "application/pdf")

    session = FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: session)

    result = try_elsevier_api(
        "10.1016/j.biocon.2025.111664",
        tmp_path / "elsevier.pdf",
        {"elsevier_api_key": "test-key", "min_pdf_size_bytes": 1000},
    )

    assert result is not None
    assert session.calls[0][1]["params"] == {"view": "FULL"}
    assert session.calls[0][1]["headers"]["Accept"] == "application/xml"


def test_elsevier_api_rejects_single_page_object_pdf(
    monkeypatch, tmp_path: Path
):
    import requests

    import scansci_pdf.publisher_strategies as strategies

    preview_pdf = b"%PDF-1.4\n" + (b"preview" * 200) + b"\n%%EOF\n"
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <full-text-retrieval-response>
      <attachment>
        <attachment-type>PDF</attachment-type>
        <attachment-eid>1-s2.0-S0006320725007013-main.pdf</attachment-eid>
        <attachment-category>MAIN</attachment-category>
      </attachment>
    </full-text-retrieval-response>
    """

    class FakeResponse:
        def __init__(self, content: bytes, content_type: str):
            self.status_code = 200
            self.content = content
            self.headers = {"Content-Type": content_type, "X-ELS-Status": "OK"}
            self.text = content.decode("utf-8", errors="replace")

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.cookies = []
            self.calls: list[tuple[str, dict]] = []

        def get(self, url, *, headers=None, **kwargs):
            self.calls.append((url, {"headers": dict(headers or {}), **kwargs}))
            if len(self.calls) == 1:
                return FakeResponse(xml.encode("utf-8"), "application/xml")
            return FakeResponse(preview_pdf, "application/pdf")

    monkeypatch.setattr(strategies, "_elsevier_pdf_page_count", lambda _content: 1)

    session = FakeSession()
    monkeypatch.setattr(requests, "Session", lambda: session)

    output_path = tmp_path / "elsevier.pdf"
    result = strategies.try_elsevier_api(
        "10.1016/j.biocon.2025.111664",
        output_path,
        {"elsevier_api_key": "test-key", "min_pdf_size_bytes": 1000},
    )

    assert result is None
    assert not output_path.exists()
    assert [call[1]["headers"]["Accept"] for call in session.calls] == [
        "application/xml",
        "application/pdf",
    ]


def test_elsevier_xml_uses_article_eid_main_pdf_when_attachment_eid_is_absent():
    from scansci_pdf.publisher_strategies import _extract_elsevier_pdf_attachment_eids

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <full-text-retrieval-response>
      <coredata>
        <eid>1-s2.0-S0006320725007013</eid>
        <pii>S0006-3207(25)00701-3</pii>
      </coredata>
    </full-text-retrieval-response>
    """

    assert _extract_elsevier_pdf_attachment_eids(xml) == [
        "1-s2.0-S0006320725007013-main.pdf"
    ]


def test_elsevier_api_prefers_direct_route_over_configured_proxy(
    monkeypatch, tmp_path: Path
):
    import requests

    from scansci_pdf.publisher_strategies import try_elsevier_api

    pdf_bytes = b"%PDF-1.4\n" + (b"fulltext" * 200) + b"\n%%EOF\n"
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <full-text-retrieval-response>
      <attachment>
        <attachment-type>PDF</attachment-type>
        <attachment-eid>1-s2.0-S0006320725007013-main.pdf</attachment-eid>
        <attachment-category>MAIN</attachment-category>
      </attachment>
    </full-text-retrieval-response>
    """

    class FakeResponse:
        def __init__(self, content: bytes, content_type: str, status_code: int = 200):
            self.status_code = status_code
            self.content = content
            self.headers = {"Content-Type": content_type, "X-ELS-Status": "OK"}
            self.text = content.decode("utf-8", errors="replace")

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.cookies = []
            self.proxies = {}
            self.calls = []

        def get(self, url, *, headers=None, **kwargs):
            self.calls.append((url, {"headers": dict(headers or {}), **kwargs}))
            if len(self.calls) == 1:
                return FakeResponse(xml.encode("utf-8"), "application/xml")
            return FakeResponse(pdf_bytes, "application/pdf")

    sessions: list[FakeSession] = []

    def session_factory():
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(requests, "Session", session_factory)

    result = try_elsevier_api(
        "10.1016/j.biocon.2025.111664",
        tmp_path / "elsevier.pdf",
        {
            "elsevier_api_key": "test-key",
            "network_proxy": "http://127.0.0.1:7890",
            "min_pdf_size_bytes": 1000,
        },
    )

    assert result is not None
    assert len(sessions) == 1
    assert sessions[0].trust_env is False
    assert sessions[0].proxies == {}


def test_elsevier_api_falls_back_to_configured_proxy_when_direct_not_entitled(
    monkeypatch, tmp_path: Path
):
    import requests

    from scansci_pdf.publisher_strategies import try_elsevier_api

    pdf_bytes = b"%PDF-1.4\n" + (b"fulltext" * 200) + b"\n%%EOF\n"
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <full-text-retrieval-response>
      <attachment>
        <attachment-type>PDF</attachment-type>
        <attachment-eid>1-s2.0-S0006320725007013-main.pdf</attachment-eid>
        <attachment-category>MAIN</attachment-category>
      </attachment>
    </full-text-retrieval-response>
    """

    class FakeResponse:
        def __init__(self, content: bytes, content_type: str, status_code: int = 200):
            self.status_code = status_code
            self.content = content
            self.headers = {"Content-Type": content_type, "X-ELS-Status": "OK"}
            self.text = content.decode("utf-8", errors="replace")

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.cookies = []
            self.proxies = {}
            self.calls = []
            self.index = len(sessions)

        def get(self, url, *, headers=None, **kwargs):
            self.calls.append((url, {"headers": dict(headers or {}), **kwargs}))
            if self.index == 0:
                return FakeResponse(
                    b"<service-error>not entitled</service-error>",
                    "application/xml",
                    status_code=401,
                )
            if len(self.calls) == 1:
                return FakeResponse(xml.encode("utf-8"), "application/xml")
            return FakeResponse(pdf_bytes, "application/pdf")

    sessions: list[FakeSession] = []

    def session_factory():
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(requests, "Session", session_factory)

    result = try_elsevier_api(
        "10.1016/j.biocon.2025.111664",
        tmp_path / "elsevier.pdf",
        {
            "elsevier_api_key": "test-key",
            "network_proxy": "http://127.0.0.1:7890",
            "min_pdf_size_bytes": 1000,
        },
    )

    assert result is not None
    assert len(sessions) == 2
    assert sessions[0].trust_env is False
    assert sessions[0].proxies == {}
    assert sessions[1].trust_env is False
    assert sessions[1].proxies == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
