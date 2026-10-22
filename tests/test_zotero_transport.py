"""Transport contract for the Zotero client: key confinement, upload origin
allowlisting, redirect refusal, secret scrubbing, and the three-step upload
sequence. All requests are faked; no test touches the network."""

from pathlib import Path

import pytest

from scansci_pdf import zotero


STORAGE_URL = (
    "https://zoterofilestorage.s3.amazonaws.com/.bucket/key"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=SECRETSIG&X-Amz-Credential=AKIASECRET%2F20261014"
)

PREFIX = (
    "--B0UNDARY\r\nContent-Disposition: form-data; name=\"key\"\r\n\r\n"
    ".bucket/key\r\n--B0UNDARY\r\nContent-Disposition: form-data; name=\"AWSAccessKeyId\"\r\n\r\nAKIASECRET"
    "\r\n--B0UNDARY\r\nContent-Disposition: form-data; name=\"acl\"\r\n\r\nprivate\r\n"
    "--B0UNDARY\r\nContent-Type: application/pdf\r\nContent-Disposition: form-data; name=\"file\"; filename=\"paper.pdf\"\r\n\r\n"
)
SUFFIX = "\r\n--B0UNDARY--"


def _auth_payload(url=STORAGE_URL, exists=0):
    return {
        "url": url,
        "uploadKey": "UPLOADKEY",
        "prefix": PREFIX,
        "suffix": SUFFIX,
        "contentType": "application/pdf",
        "exists": exists,
    }


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeTransport:
    """Routes every confined request and records what was actually sent."""

    def __init__(self, pdf_bytes=b"%PDF-1.4 fake"):
        self.calls = []  # (method, url, kwargs)
        self.pdf_bytes = pdf_bytes
        self.item_keys = ["ITEMKEY", "ATTKEY"]  # popped per /items POST
        self.storage_status = 201
        self.storage_error = None
        self.storage_body = None  # captured while the request was "in flight"
        self.register_status = 204
        self.register_error = None
        self.authorize_payload = _auth_payload()

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url == "https://api.zotero.org/users/1/items":
            key = self.item_keys.pop(0) if self.item_keys else "ATTKEY"
            return FakeResponse(payload={"success": {"0": key}})
        if url.endswith("/items/ATTKEY/file"):
            data = kwargs.get("data")
            if isinstance(data, dict) and "upload" in data:
                if self.register_error:
                    raise self.register_error
                return FakeResponse(status_code=self.register_status)
            return FakeResponse(payload=self.authorize_payload)
        host = url.split("/")[2]
        if host == "zoterofilestorage.s3.amazonaws.com":
            if self.storage_error:
                raise self.storage_error
            body = kwargs.get("data")
            if body is not None and not isinstance(body, (bytes, str)):
                self.storage_body = b"".join(body)
            return FakeResponse(status_code=self.storage_status)
        raise AssertionError(f"unexpected request to {url}")

    # helpers over the recorded calls
    def storage_call(self):
        return next(c for c in self.calls if "zoterofilestorage" in c[1])

    def register_call(self):
        return next(c for c in self.calls if c[2].get("data") == {"upload": "UPLOADKEY"})


@pytest.fixture(autouse=True)
def _fresh_secrets():
    zotero._secrets.clear()


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4 fake body")
    return path


@pytest.fixture
def transport(monkeypatch):
    fake = FakeTransport()
    monkeypatch.setattr("requests.request", fake)
    return fake


def _config():
    return {"zotero_api_key": "test-key", "zotero_library_type": "user", "zotero_library_id": "1"}


def test_the_full_upload_runs_the_three_step_sequence(transport, pdf):
    result = zotero.push_to_zotero("10.1/x", pdf, _config())

    assert result["success"] is True
    assert result["zotero_key"] == "ITEMKEY"
    assert result["attachment"]["status"] == "uploaded"
    assert result["attachment"]["attachment_key"] == "ATTKEY"

    storage_method, storage_url, storage_kwargs = transport.storage_call()
    assert storage_method == "POST"
    assert storage_url == STORAGE_URL
    # The storage request never carries the API key...
    assert "Zotero-API-Key" not in storage_kwargs["headers"]
    # ...and streams exactly prefix + file + suffix with an honest length.
    expected = PREFIX.encode() + pdf.read_bytes() + SUFFIX.encode()
    assert transport.storage_body == expected
    assert storage_kwargs["headers"]["Content-Length"] == str(len(expected))
    # Registration goes to the API origin, never the response-supplied URL.
    reg_method, reg_url, reg_kwargs = transport.register_call()
    assert reg_method == "POST"
    assert reg_url == "https://api.zotero.org/users/1/items/ATTKEY/file"
    assert reg_kwargs["headers"]["Zotero-API-Key"] == "test-key"


def test_every_request_refuses_redirects(transport, pdf):
    zotero.push_to_zotero("10.1/x", pdf, _config())
    assert transport.calls
    for _, _, kwargs in transport.calls:
        assert kwargs["allow_redirects"] is False


def test_an_upload_url_on_a_foreign_origin_is_never_contacted(transport, pdf):
    transport.authorize_payload = _auth_payload(url="https://evil.example.com/u?X-Amz-Signature=SECRETSIG")
    result = zotero.push_to_zotero("10.1/x", pdf, _config())

    assert result["success"] is False
    assert result["partial"] is True
    assert result["attachment"]["status"] == "authorize_failed"
    assert not any("evil.example.com" in url for _, url, _ in transport.calls)
    # The half-created attachment is cleaned up on the API origin.
    assert any(method == "DELETE" and url.endswith("/items/ATTKEY/file")
               for method, url, _ in transport.calls)


def test_api_credentials_refuse_a_foreign_origin():
    with pytest.raises(ValueError, match="api.zotero.org"):
        zotero._confined_request(
            "POST", "https://evil.example.com/items",
            api=True, headers={"Zotero-API-Key": "k"},
        )


@pytest.mark.parametrize("url", [
    "https://zoterofilestorage.s3.amazonaws.com/x#frag",
    "https://user:pass@zoterofilestorage.s3.amazonaws.com/x",
    "http://zoterofilestorage.s3.amazonaws.com/x",
    "https://evil.example.com/x",
])
def test_unusable_upload_urls_are_rejected(url):
    with pytest.raises(ValueError):
        zotero.validate_upload_url(url)


def test_signed_url_authorization_is_scrubbed_from_transport_errors(transport, pdf):
    transport.storage_error = RuntimeError(
        f"HTTPSConnectionPool(host='zoterofilestorage.s3.amazonaws.com'): "
        f"Max retries exceeded with url: /.bucket/key?X-Amz-Signature=SECRETSIG"
    )
    result = zotero.push_to_zotero("10.1/x", pdf, _config())

    assert result["success"] is False
    blob = str(result)
    assert "SECRETSIG" not in blob
    assert "AKIASECRET" not in blob
    assert "UPLOADKEY" not in blob


def test_the_api_key_is_scrubbed_from_errors(transport, pdf):
    import requests as _r
    transport.storage_error = _r.ConnectionError(
        "connection failed while sending headers including Zotero-API-Key: test-key"
    )
    result = zotero.push_to_zotero("10.1/x", pdf, _config())
    assert "test-key" not in str(result)


def test_an_existing_file_short_circuits_the_upload(transport, pdf):
    transport.authorize_payload = _auth_payload(exists=1)
    result = zotero.push_to_zotero("10.1/x", pdf, _config())

    assert result["success"] is True
    assert result["attachment"]["already_attached"] is True
    with pytest.raises(StopIteration):
        transport.storage_call()  # no storage request was made


def test_a_partial_upload_preserves_the_item_key_for_retry(transport, pdf):
    transport.storage_status = 500
    result = zotero.push_to_zotero("10.1/x", pdf, _config())

    assert result["success"] is False
    assert result["partial"] is True
    assert result["item_created"] is True
    assert result["zotero_key"] == "ITEMKEY"
    assert result["retry"]["action"] == "upload_attachment_to_existing_item"
    assert "zotero_key" in result["retry"]["note"]


def test_a_retry_with_the_existing_key_uploads_without_recreating_the_item(transport, pdf):
    # Only the attachment creation should hit /items, and it must attach to
    # the passed key rather than minting a new parent item.
    transport.item_keys = ["ATTKEY"]
    result = zotero.push_to_zotero("10.1/x", pdf, _config(), zotero_key="ITEMKEY")

    assert result["success"] is True
    assert result["zotero_key"] == "ITEMKEY"
    assert result["item_created"] is False
    assert result["attachment"]["status"] == "uploaded"
    item_posts = [c for c in transport.calls if c[1].endswith("/users/1/items")]
    assert len(item_posts) == 1
    assert item_posts[0][2]["json"][0]["parentItem"] == "ITEMKEY"


def test_a_non_pdf_attachment_is_rejected_before_any_request(transport, tmp_path):
    exe = tmp_path / "payload.exe"
    exe.write_bytes(b"MZ")
    result = zotero.push_to_zotero("10.1/x", exe, _config())

    assert result["success"] is False
    assert result["item_created"] is False
    assert result["attachment"]["status"] == "rejected"
    assert transport.calls == []


def test_write_failure_details_are_surfaced(monkeypatch):
    def fake_request(method, url, **kwargs):
        return FakeResponse(payload={"failed": {"0": {"code": 400, "message": "Bad JSON"}}})

    monkeypatch.setattr("requests.request", fake_request)
    result = zotero.push_to_zotero("10.1/x", None, _config())
    assert result["success"] is False
    assert "400: Bad JSON" in result["error"]


def test_symlinked_attachments_are_rejected(tmp_path, transport):
    real = tmp_path / "real.pdf"
    real.write_bytes(b"%PDF-1.4")
    try:
        link = tmp_path / "link.pdf"
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink privileges unavailable")
    result = zotero.push_to_zotero("10.1/x", link, _config())
    assert result["attachment"]["status"] == "rejected"
    assert transport.calls == []
