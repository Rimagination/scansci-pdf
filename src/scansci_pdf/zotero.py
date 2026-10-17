"""Zotero integration via PyZotero API.

Transport contract: the Zotero API key is only ever sent to api.zotero.org,
API calls never follow redirects, response-supplied upload URLs must point at
allowlisted Zotero storage origins (and never receive the key), and secrets --
the key, upload keys, signed-URL authorization -- are scrubbed from returned
errors and logs. File uploads follow the documented three-step Web API
sequence (authorize, streamed multipart to storage, register) rather than
posting file bytes at the API origin.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, unquote_plus, urlsplit

from .log import get_logger

log = get_logger()

# The attachment is a PDF: nothing else may be uploaded as application/pdf.
ATTACHMENT_SUFFIXES = frozenset({".pdf"})

# Zotero write responses use a per-index envelope; a partial failure lands in
# "failed" with a code and a message worth surfacing to the user.
_OK_STATUS = (200, 201)

# The only origin that may ever receive the API key.
_API_ORIGIN = ("https", "api.zotero.org", 443)

# The only origins a response-supplied upload URL may point at (the storage
# hosts Zotero's documented upload flow returns presigned URLs for).
UPLOAD_ORIGINS = frozenset({
    ("https", "zoterofilestorage.s3.amazonaws.com", 443),
    ("https", "zoterofilestorage.s3.us-east-1.amazonaws.com", 443),
})

_UPLOAD_QUERY_SECRET = re.compile(
    r"signature|token|credential|password|policy|api.?key|access.?key|secret|authorization|^sig$|^key-pair-id$",
    re.I,
)

# Secrets observed this process -- the API key and response-provided upload
# authorization -- that must not survive into returned errors or logs.
_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def _register_secret(value: Any) -> None:
    if isinstance(value, str) and value:
        with _secrets_lock:
            _secrets.add(value)


def _scrub(text: Any) -> str:
    message = str(text)
    with _secrets_lock:
        secrets = sorted(_secrets, key=len, reverse=True)
    for secret in secrets:
        if secret in message:
            message = message.replace(secret, "***")
    return message


def _origin_of(url: str) -> tuple[str, str, int]:
    """(scheme, host, port) with the scheme's default port normalized."""
    parts = urlsplit(str(url))
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"Invalid Zotero URL: {url!r}") from None
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return (parts.scheme, (parts.hostname or "").lower(), port)


def _confined_request(method: str, url: str, *, api: bool, headers: dict[str, str] | None = None, **kwargs: Any):
    """The single network seam, so the transport contract is enforceable.

    ``api=True`` calls carry the API key and may only target the API origin;
    every other call may only target an allowlisted storage origin and never
    carries the key (header names are matched case-insensitively, because
    that is how HTTP treats them). Redirects are never followed: a redirect
    on a key-bearing request would hand the key to whatever the response
    points at, and a redirect on an upload would stream the file to an
    unlisted host.
    """
    import requests

    origin = _origin_of(url)
    headers = dict(headers or {})
    key = None
    for name in list(headers):
        if name.lower() == "zotero-api-key":
            key = headers.pop(name)
    if api:
        if origin != _API_ORIGIN:
            raise ValueError("Zotero API credentials may only be sent to api.zotero.org")
        if urlsplit(str(url)).username is not None or urlsplit(str(url)).password is not None:
            raise ValueError("Zotero API URLs may not carry embedded credentials")
        if key:
            _register_secret(key)
            headers["Zotero-API-Key"] = key
    elif origin not in UPLOAD_ORIGINS:
        raise ValueError("Zotero storage upload origin is not allowlisted")
    kwargs["allow_redirects"] = False
    return requests.request(method, url, headers=headers, **kwargs)


def validate_upload_url(url: str) -> str:
    parts = urlsplit(str(url))
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError("Invalid Zotero storage upload URL")
    if _origin_of(url) not in UPLOAD_ORIGINS:
        raise ValueError("Zotero storage upload origin is not allowlisted")
    return str(url)


def _register_upload_authorization(payload: dict[str, Any]) -> None:
    """Remember response-provided authorization before validation can fail.

    Transport errors may contain only the URL path, with query values either
    decoded or still percent-encoded, instead of the full response URL.
    """
    _register_secret(payload.get("uploadKey"))
    url = payload.get("url")
    if isinstance(url, str):
        _register_secret(url)
        query = url.partition("?")[2].partition("#")[0]
        for field in query.split("&"):
            name, separator, value = field.partition("=")
            if not separator or not _UPLOAD_QUERY_SECRET.search(unquote_plus(name)):
                continue
            _register_secret(value)
            _register_secret(unquote_plus(value))
            _register_secret(unquote(value))
            _register_secret(unquote(unquote(value)))


def _resolve_attachment(path: Path | None) -> Path:
    """Validate the value about to be opened: a regular, non-symlink PDF.

    Checked again immediately before each open; combined with the
    single-descriptor read in the upload (md5, size and streamed bytes all
    come from one open file), this keeps the hashes and the payload in
    agreement about which file was meant.
    """
    candidate = Path(path) if path is not None else None
    if candidate is None:
        raise ValueError("No attachment path")
    if candidate.suffix.lower() not in ATTACHMENT_SUFFIXES:
        raise ValueError(f"Attachment must be a PDF: {candidate.name}")
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"Attachment is not a regular file: {candidate}")
    return candidate


def _get_zotero_config(config: dict[str, Any]) -> tuple[str, str] | None:
    """Get Zotero API key and library info from config."""
    api_key = config.get("zotero_api_key", "")
    library_type = config.get("zotero_library_type", "user")  # "user" or "group"
    library_id = config.get("zotero_library_id", "")

    if not api_key:
        return None
    return api_key, f"{library_type}s/{library_id}" if library_id else ""


def _response_json(response: Any) -> dict[str, Any]:
    """Parse a Zotero response body, tolerating empty or non-dict bodies."""
    try:
        payload = response.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_failure_detail(result: dict[str, Any]) -> str:
    """Describe why a Zotero write reported no successful object."""
    failed = result.get("failed")
    if isinstance(failed, dict) and failed:
        messages = []
        for item in failed.values():
            if isinstance(item, dict):
                code = item.get("code")
                message = item.get("message") or item.get("data", {})
                messages.append(f"{code}: {message}" if code else str(message))
        if messages:
            return "; ".join(messages)
    return ""


class _UploadStream:
    """One streamed multipart envelope: prefix + file + suffix."""

    def __init__(self, handle: Any, prefix: bytes, suffix: bytes, length: int):
        self.handle, self.prefix, self.suffix, self.length = handle, prefix, suffix, length

    def __len__(self):
        # Requests must not send both Content-Length and chunked encoding.
        return self.length

    def __iter__(self):
        yield self.prefix
        yield from iter(lambda: self.handle.read(1024 * 1024), b"")
        yield self.suffix


def push_to_zotero(
    doi: str,
    pdf_path: Path | None,
    config: dict[str, Any],
    metadata: dict[str, Any] | None = None,
    zotero_key: str | None = None,
) -> dict[str, Any]:
    """Push a paper and its PDF to Zotero.

    Returns a dict that separates the three stages the caller cares about:
    whether the parent item was created, whether the attachment item was
    created, and whether the file actually reached Zotero. A requested PDF
    that did not upload is never reported as an unqualified success -- the
    already-created ``zotero_key`` is preserved, and passing it back as
    ``zotero_key`` retries only the upload against the existing item instead
    of creating a duplicate one.
    """
    requested = pdf_path is not None

    def _not_started(error: str) -> dict[str, Any]:
        return {
            "success": False,
            "error": error,
            "item_created": False,
            "attachment": {"requested": requested, "status": "not_attempted"},
        }

    creds = _get_zotero_config(config)
    if not creds:
        return _not_started("Zotero API key not configured")

    api_key, library_path = creds
    _register_secret(api_key)
    if not library_path:
        return _not_started("Zotero library_id not configured")

    base_url = f"https://api.zotero.org/{library_path}"
    headers = {
        "Zotero-API-Key": api_key,
        "Content-Type": "application/json",
    }

    # Refuse an unusable attachment before any remote item exists, so a bad
    # path never leaves an orphan item and the file is never opened.
    source: Path | None = None
    if requested:
        try:
            source = _resolve_attachment(pdf_path)
        except ValueError as exc:
            reason = _scrub(exc)
            return {
                "success": False,
                "error": f"Attachment rejected: {reason}",
                "item_created": False,
                "attachment": {
                    "requested": True,
                    "status": "rejected",
                    "filename": Path(str(pdf_path)).name,
                    "error": reason,
                },
            }

    # Build item from metadata or DOI
    if metadata:
        item_data = _metadata_to_zotero_item(metadata, doi)
    else:
        # Use DOI to create a minimal item
        item_data = {
            "itemType": "journalArticle",
            "DOI": doi,
            "url": f"https://doi.org/{doi}",
        }

    # Create the item -- or skip straight to the attachment when the caller
    # passed the key of an item that already exists (the retry path).
    if zotero_key:
        item_created = False
    else:
        try:
            resp = _confined_request("POST",
                f"{base_url}/items",
                api=True,
                headers=headers,
                json=[item_data],
                timeout=30,
            )
            if resp.status_code not in _OK_STATUS:
                return _not_started(f"Zotero API error: {resp.status_code}")

            result = _response_json(resp)
            if not (result.get("success") or result.get("successful")):
                detail = _write_failure_detail(result) or "No successful items"
                return _not_started(_scrub(detail))

            zotero_key = _created_object_key(result)
            if not zotero_key:
                return _not_started(_scrub(_write_failure_detail(result) or "No key returned"))
            item_created = True
        except Exception as e:
            return _not_started(_scrub(e))

    log.info(f"Zotero: item {zotero_key} ({'created' if item_created else 'existing'})")

    if source is None:
        return {
            "success": True,
            "zotero_key": zotero_key,
            "item_created": item_created,
            "attachment": {"requested": False, "status": "not_requested"},
        }

    upload = _upload_attachment(base_url, headers, zotero_key, source, api_key)
    attachment = {"requested": True, **upload}
    if upload["status"] == "uploaded":
        return {
            "success": True,
            "zotero_key": zotero_key,
            "item_created": item_created,
            "attachment": attachment,
        }

    # Partial failure: the parent item exists, the PDF does not. Keep the key
    # so a retry uploads the file against the existing item rather than
    # silently creating a second one.
    error = _scrub(
        f"Zotero item {zotero_key} was created but the PDF attachment was not "
        f"uploaded ({upload['status']}): {upload.get('error') or 'unknown error'}"
    )
    log.warning(error)
    return {
        "success": False,
        "partial": True,
        "error": error,
        "item_created": item_created,
        "zotero_key": zotero_key,
        "attachment": attachment,
        "retry": {
            "action": "upload_attachment_to_existing_item",
            "zotero_key": zotero_key,
            "note": "The item already exists in Zotero; retry by passing zotero_key "
            "back to the push tool so only the file upload is repeated. Re-pushing "
            "without it would create a duplicate item.",
        },
    }


def _metadata_to_zotero_item(msg: dict[str, Any], doi: str) -> dict[str, Any]:
    """Convert Crossref metadata to Zotero item format."""
    item: dict[str, Any] = {
        "itemType": "journalArticle",
        "DOI": doi,
        "url": f"https://doi.org/{doi}",
    }

    # Title
    titles = msg.get("title", [])
    if titles:
        item["title"] = titles[0] if isinstance(titles, list) else titles

    # Authors
    authors = msg.get("author", [])
    if authors:
        creators = []
        for a in authors:
            creators.append({
                "creatorType": "author",
                "firstName": a.get("given", ""),
                "lastName": a.get("family", ""),
            })
        item["creators"] = creators

    # Journal
    container = msg.get("container-title", [])
    if container:
        item["publicationTitle"] = container[0]

    # Year
    parts = msg.get("published-print", msg.get("published-online", msg.get("created", {})))
    date_parts = parts.get("date-parts", [[]]) if isinstance(parts, dict) else [[]]
    if date_parts and date_parts[0]:
        item["date"] = str(date_parts[0][0])

    # Volume, Issue, Pages
    if msg.get("volume"):
        item["volume"] = str(msg["volume"])
    if msg.get("issue"):
        item["issue"] = str(msg["issue"])
    if msg.get("page"):
        item["pages"] = msg["page"]

    # Abstract
    if msg.get("abstract"):
        import re
        item["abstractNote"] = re.sub(r"<[^>]+>", "", msg["abstract"]).strip()

    return item


def _upload_attachment(
    base_url: str,
    headers: dict[str, str],
    parent_key: str,
    pdf_path: Path,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Upload a PDF as an attachment to a Zotero item.

    Follows the Zotero Web API upload sequence so each stage can fail on its
    own terms: create the attachment item, request authorization for it, then
    push the file to the returned storage URL. Returns a stage-tagged dict
    (``uploaded`` / ``create_failed`` / ``authorize_failed`` /
    ``upload_failed`` / ``rejected``) rather than a bare bool, so the caller
    can never mistake an empty attachment for a finished one.
    """
    secret = api_key if api_key is not None else headers.get("Zotero-API-Key", "")
    _register_secret(secret)
    upload_envelope: list[str] = []

    def _fail(status: str, error: str, attachment_key: str | None = None) -> dict[str, Any]:
        # Multipart envelopes can embed authorization, but are not global
        # secrets: short ordinary boundary strings must not mask other output.
        message = str(error)
        for value in sorted(upload_envelope, key=len, reverse=True):
            message = message.replace(value, "***")
        return {
            "status": status,
            "attachment_key": attachment_key,
            "error": _scrub(message),
        }

    # Re-validate immediately before the first read: a value that changed
    # between the early check and here still cannot slip through.
    try:
        source = _resolve_attachment(pdf_path)
    except ValueError as exc:
        return _fail("rejected", str(exc))

    def _cleanup(attachment_key: str) -> None:
        # Best effort: drop a half-written attachment so a retry is clean.
        try:
            _confined_request("DELETE",
                f"{base_url}/items/{attachment_key}/file",
                api=True,
                headers={"Zotero-API-Key": secret},
                timeout=30,
            )
        except Exception:
            pass

    # 1. Create the attachment item (no file content is sent yet).
    attachment_data = {
        "itemType": "attachment",
        "linkMode": "imported_file",
        "contentType": "application/pdf",
        "filename": source.name,
        "parentItem": parent_key,
    }

    try:
        resp = _confined_request("POST",
            f"{base_url}/items",
            api=True,
            headers=headers,
            json=[attachment_data],
            timeout=30,
        )
    except Exception as e:
        return _fail("create_failed", f"attachment create request failed: {e}")

    if resp.status_code not in _OK_STATUS:
        return _fail("create_failed", f"attachment item creation returned HTTP {resp.status_code}")

    result = _response_json(resp)
    att_key = _created_object_key(result)
    if not att_key:
        return _fail("create_failed", _write_failure_detail(result) or "attachment create returned no key")

    log.info(f"Zotero: created attachment item {att_key} on {parent_key}")

    # 2. Open the file once and request upload authorization for it: the
    # md5, the size, and the streamed payload all come from this one
    # descriptor (O_NOFOLLOW where the platform has it), so they cannot
    # disagree about which file was meant.
    try:
        source = _resolve_attachment(source)
        fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except (ValueError, OSError) as exc:
        return _fail("rejected", str(exc))

    try:
        with os.fdopen(fd, "rb") as handle:
            digest = hashlib.md5(usedforsecurity=False)
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            stat = os.fstat(handle.fileno())
            form = {
                "md5": digest.hexdigest(),
                "filename": source.name,
                "filesize": str(stat.st_size),
                "mtime": str(int(stat.st_mtime * 1000)),
            }
            try:
                resp = _confined_request("POST",
                    f"{base_url}/items/{att_key}/file",
                    api=True,
                    headers={
                        "Zotero-API-Key": secret,
                        "Content-Type": "application/x-www-form-urlencoded",
                        "If-None-Match": "*",
                    },
                    data=form,
                    timeout=30,
                )
            except Exception as e:
                _cleanup(att_key)
                return _fail("authorize_failed", f"upload authorization failed: {e}", att_key)

            if resp.status_code not in _OK_STATUS:
                _cleanup(att_key)
                return _fail("authorize_failed", f"upload authorization returned HTTP {resp.status_code}", att_key)

            payload = _response_json(resp)
            _register_upload_authorization(payload)
            upload_envelope.extend(value for key in ("prefix", "suffix")
                                   if isinstance(value := payload.get(key), str) and value)
            if payload.get("exists") == 1:
                return {"status": "uploaded", "attachment_key": att_key, "error": None,
                        "filename": source.name, "already_attached": True}
            upload_key = payload.get("uploadKey")
            try:
                upload_url = validate_upload_url(payload.get("url") or "")
                if not isinstance(upload_key, str) or not upload_key:
                    raise ValueError("authorization response contained no upload key")
                prefix = payload.get("prefix", "")
                suffix = payload.get("suffix", "")
                if not isinstance(prefix, str) or not isinstance(suffix, str):
                    raise ValueError("Unsupported Zotero upload envelope")
                content_type = payload.get("contentType") or "application/pdf"
                prefix, suffix = prefix.encode("utf-8"), suffix.encode("utf-8")
            except ValueError as exc:
                _cleanup(att_key)
                return _fail("authorize_failed", str(exc), att_key)

            # 3. One multipart stream to the storage origin, then register on
            # the API origin -- never the response-supplied URL, and never a
            # second key-bearing request to storage.
            handle.seek(0)
            length = len(prefix) + stat.st_size + len(suffix)
            resp = _confined_request("POST", upload_url,
                api=False,
                headers={"Content-Type": content_type,
                         "Content-Length": str(length)},
                data=_UploadStream(handle, prefix, suffix, length), timeout=300)
            if resp.status_code != 201:
                raise ValueError(f"file upload returned HTTP {resp.status_code}")
            resp = _confined_request("POST", f"{base_url}/items/{att_key}/file",
                api=True,
                headers={"Zotero-API-Key": secret, "If-None-Match": "*"},
                data={"upload": upload_key}, timeout=30)
            if resp.status_code != 204:
                raise ValueError(f"upload registration returned HTTP {resp.status_code}")
    except Exception as exc:
        _cleanup(att_key)
        return _fail("upload_failed", f"file upload failed: {exc}", att_key)

    log.info(f"Zotero: uploaded PDF {source.name}")
    return {"status": "uploaded", "attachment_key": att_key, "error": None,
            "filename": source.name}


def _created_object_key(result: dict[str, Any]) -> str | None:
    """Extract the Zotero object key from a write response."""
    successful = result.get("successful") or result.get("success") or {}
    if not successful:
        return None

    saved_object = next(iter(successful.values()))
    if isinstance(saved_object, str):
        return saved_object
    if isinstance(saved_object, dict):
        key = saved_object.get("key")
        if isinstance(key, str):
            return key
        data = saved_object.get("data", {})
        if isinstance(data, dict) and isinstance(data.get("key"), str):
            return data["key"]

    return None
