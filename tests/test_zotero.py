from pathlib import Path

from scansci_pdf import zotero


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_push_to_zotero_posts_item_as_raw_json_array(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return FakeResponse(payload={"success": {"0": "ITEMKEY"}})

    monkeypatch.setattr("requests.request", fake_request)

    result = zotero.push_to_zotero(
        doi="10.1234/example",
        pdf_path=None,
        config={
            "zotero_api_key": "test-key",
            "zotero_library_type": "user",
            "zotero_library_id": "123456",
        },
    )

    assert result["success"] is True
    assert result["zotero_key"] == "ITEMKEY"
    assert result["attachment"]["requested"] is False
    assert calls[0][0] == "POST"
    assert calls[0][1] == "https://api.zotero.org/users/123456/items"
    assert isinstance(calls[0][2]["json"], list)
    assert calls[0][2]["json"][0]["DOI"] == "10.1234/example"


def test_upload_attachment_posts_item_as_raw_json_array(monkeypatch, tmp_path):
    calls = []
    pdf_path = tmp_path / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.endswith("/items"):
            return FakeResponse(payload={"success": {"0": "ATTKEY"}})
        # The authorization call: answer that the file already exists so the
        # upload short-circuits and this test stays about the item contract.
        return FakeResponse(payload={"exists": 1})

    monkeypatch.setattr("requests.request", fake_request)

    ok = zotero._upload_attachment(
        base_url="https://api.zotero.org/users/123456",
        headers={"Zotero-API-Key": "test-key", "Content-Type": "application/json"},
        parent_key="PARENTKEY",
        pdf_path=Path(pdf_path),
    )

    assert ok["status"] == "uploaded"
    assert ok["already_attached"] is True
    assert calls[0][1] == "https://api.zotero.org/users/123456/items"
    assert isinstance(calls[0][2]["json"], list)
    assert calls[0][2]["json"][0]["parentItem"] == "PARENTKEY"
    assert calls[1][1] == "https://api.zotero.org/users/123456/items/ATTKEY/file"
