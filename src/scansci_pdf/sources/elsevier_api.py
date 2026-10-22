"""Elsevier RetrievalAPI integration for fetching full-text articles."""

import logging
import os
import xml.etree.ElementTree as ET
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

ELSEVIER_API = "https://api.elsevier.com/content"


def get_api_key(config_key: str = "") -> str:
    """Get Elsevier API key from config or environment."""
    return config_key or os.environ.get("ELSEVIER_API_KEY", "")


def _resolve_proxies(config: dict | None) -> dict[str, str] | None:
    """Explicit ScanSci proxy → requests proxies dict (None = direct).

    Entitlement for subscription full text follows the request's egress IP:
    off-campus direct requests 403 (NOT_ENTITLED) even with a valid key.
    HTTP_PROXY/HTTPS_PROXY are ignored project-wide, so a campus-bound proxy
    must be set via SCANSCI_PDF_PROXY / network_proxy — the same setting the
    entitlement probe (elsevier_check) routes through.
    """
    from ..network import configured_proxy

    proxy = configured_proxy(config)
    return {"http": proxy, "https": proxy} if proxy else None


def fetch_pdf(doi: str, api_key: str, inst_token: str = "", config: dict | None = None) -> bytes | None:
    """Download full PDF via Elsevier API using the attachment EID approach.

    Strategy (from successful 32-paper batch experience):
    1. GET /article/doi/{doi}?view=FULL → XML with attachment metadata
    2. Parse XML to find MAIN PDF attachment-eid (main.pdf or mainext.pdf)
    3. GET /object/eid/{attachment-eid} → official publisher PDF

    This bypasses ScienceDirect web pages, Cloudflare, and CAPTCHAs entirely.
    Direct PDF endpoint (/article/doi/{doi} + Accept: application/pdf) only
    returns a 1-page preview for paywalled articles.
    """
    if not api_key:
        return None

    proxies = _resolve_proxies(config)

    # Step 1: Get FULL XML with attachment metadata
    eids = _fetch_attachment_eids(doi, api_key, inst_token, proxies)
    if not eids:
        # Fallback: try direct PDF endpoint (works for OA articles)
        return _fetch_pdf_direct(doi, api_key, inst_token, proxies)

    # Step 2: Try each attachment EID until we get a valid PDF
    for eid in eids:
        pdf_bytes = _fetch_pdf_by_eid(eid, api_key, inst_token, proxies)
        if pdf_bytes:
            return pdf_bytes

    logger.info("Elsevier API: all attachment EIDs failed for %s, trying direct", doi)
    return _fetch_pdf_direct(doi, api_key, inst_token, proxies)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1].lower()


def _element_text(el: ET.Element) -> str:
    return " ".join(" ".join(el.itertext()).split())


def _header(headers: dict, name: str) -> str:
    value = headers.get(name)
    if value is not None:
        return str(value)
    needle = name.lower()
    for key, value in headers.items():
        if str(key).lower() == needle:
            return str(value)
    return ""


def _response_is_pdf(resp: requests.Response) -> bool:
    content_type = _header(resp.headers, "content-type").lower()
    return "pdf" in content_type or resp.content[:5] == b"%PDF-"


def _looks_like_pdf_eid(value: str) -> bool:
    lowered = value.strip().lower()
    return bool(lowered) and (lowered.endswith(".pdf") or ".pdf" in lowered)


def _article_eid_to_main_pdf(value: str) -> str:
    candidate = value.strip()
    if candidate.lower().startswith("eid:"):
        candidate = candidate.split(":", 1)[1].strip()
    if not candidate.startswith("1-s2.0-"):
        return ""
    if candidate.lower().endswith(".pdf"):
        return candidate
    return f"{candidate}-main.pdf"


# Maximum characters of attachment metadata handed to the scorer. The scorer
# pattern-matches that string, so a container that resolved to a shared
# ancestor contributes every sibling attachment's markers -- and the
# ancestor's own tag name, "full-text-retrieval-response" among them, reads
# as a version-of-record claim (#89). A few KB is far more than one
# attachment's own metadata needs.
_ATTACHMENT_METADATA_WINDOW = 4096

# Maximum characters taken from a single element's text. Without it one long
# element (a 5,000-character label) fills the whole window and pushes the
# block's *own* markers out of it -- which costs the version of record its
# own "full-text" claim.
_ATTACHMENT_METADATA_PART_CAP = 512

# Elements that name an object id rather than hold attachment content.
_ATTACHMENT_ID_TAGS = frozenset({"attachment-eid", "object-eid", "eid", "identifier"})

# Elements that describe one attachment. The loose "attachment" in the tag
# name would also match "attachment-size" and "attachment-metadata", which
# describe nothing, so the index uses this explicit list.
_ATTACHMENT_BLOCK_TAGS = frozenset({
    "attachment", "attachments", "object", "objects", "web-pdf",
})


def _attachment_key(eid: str) -> str:
    """Normalise an object id to the identity of the attachment it names.

    ``1-s2.0-X-main.pdf`` and the bare article id ``1-s2.0-X`` name the same
    version of record and share a key; ``1-s2.0-X-am.pdf`` and
    ``1-s2.0-X-author_2017.pdf`` are different attachments and do not.
    """
    key = eid.strip().lower()
    if key.endswith(".pdf"):
        key = key[:-4]
    if key.endswith("-main"):
        key = key[:-5]
    return key


def _eids_in(el: ET.Element) -> list[str]:
    """Object ids carried by one element: its own eid text, plus eid attributes."""
    found: list[str] = []
    local = _local_name(str(el.tag))
    if local in {"attachment-eid", "object-eid"}:
        text = _element_text(el)
        if _looks_like_pdf_eid(text):
            found.append(text)
    elif local in {"eid", "identifier"}:
        main_pdf = _article_eid_to_main_pdf(_element_text(el))
        if main_pdf:
            found.append(main_pdf)

    for attr_name, attr_value in el.attrib.items():
        attr_local = _local_name(str(attr_name))
        if attr_local in {"attachment-eid", "object-eid", "eid"}:
            value = str(attr_value).strip()
            if _looks_like_pdf_eid(value):
                found.append(value)
            else:
                main_pdf = _article_eid_to_main_pdf(value)
                if main_pdf:
                    found.append(main_pdf)
    return found


def _is_attachment_block(node: ET.Element) -> bool:
    """True for a node that holds attachment content rather than naming an id.

    ``<attachment-eid>`` and ``<object-eid>`` name an object; they do not
    hold one, and treating them as blocks would stop the walk at an element
    whose metadata is nothing but the id.
    """
    local = _local_name(str(node.tag))
    if local in _ATTACHMENT_ID_TAGS:
        return False
    return "attachment" in local or "object" in local or local == "web-pdf"


def _is_block_element(node: ET.Element) -> bool:
    """True for the element that describes one attachment.

    The loose test above would also match ``<attachment-size>`` and
    ``<attachment-metadata>``, which describe nothing, so the block index
    uses this explicit list instead.
    """
    return _local_name(str(node.tag)) in _ATTACHMENT_BLOCK_TAGS


def _has_block_descendant(node: ET.Element) -> bool:
    """True when the node holds another block: it is a holder, not a block.

    ``<attachments><attachment>...</attachment></attachments>`` is a holder.
    Its metadata belongs to every attachment inside it, so it must never be
    used as the container of one of them (#89).
    """
    for sub in node.iter():
        if sub is node:
            continue
        if _is_block_element(sub):
            return True
    return False


def _declared_eids(node: ET.Element) -> list[str]:
    """Every object id declared inside ``node``."""
    found: list[str] = []
    for sub in node.iter():
        found.extend(_eids_in(sub))
    return found


def _declares_file_id(node: ET.Element) -> bool:
    """True when the node names a file id itself.

    The article id (``<eid>1-s2.0-X</eid>``) is turned into ``-main.pdf`` on
    the way in, so a block that only carries the article id describes the
    file less precisely than one that names the file id itself.
    """
    for sub in node.iter():
        local = _local_name(str(sub.tag))
        if local in {"attachment-eid", "object-eid"}:
            if _looks_like_pdf_eid(_element_text(sub)):
                return True
        for attr_name, attr_value in sub.attrib.items():
            attr_local = _local_name(str(attr_name))
            if attr_local not in {"attachment-eid", "object-eid"}:
                continue
            if _looks_like_pdf_eid(str(attr_value).strip()):
                return True
    return False


def _block_is_tight(node: ET.Element) -> bool:
    """True when the node declares exactly one attachment.

    Declarations are counted, not normalised keys: the article-level id and
    the ``-main.pdf`` id name the same version of record, so a holder of two
    blocks that normalise to one key would pass a key count while still
    carrying the sibling's metadata.
    """
    return len(_declared_eids(node)) == 1


def _block_is_scoped(node: ET.Element, key: str) -> bool:
    """True when the node declares no attachment other than ``key``.

    A holder of several attachments -- the plural ``attachments`` element, or
    an ancestor spanning the whole article -- carries every sibling's
    metadata, so a sibling's markers leak into this attachment's score (#89).
    """
    for eid in _declared_eids(node):
        if _attachment_key(eid) != key:
            return False
    return True


def _block_declares(node: ET.Element, key: str) -> bool:
    """True when the node declares the attachment ``key``.

    Unlike :func:`_block_is_scoped` this tolerates a block that also
    holds something else -- a nested ``<object>``, say. The block's own label
    still names the file, and losing it would turn an accepted manuscript
    back into an ordinary candidate.
    """
    for eid in _declared_eids(node):
        if _attachment_key(eid) == key:
            return True
    return False


def _attachment_blocks(root: ET.Element) -> tuple[dict[str, ET.Element], dict[str, ET.Element]]:
    """Index the tight attachment blocks by the attachment they declare.

    Two indexes come back:

    - by the exact object id a block declares, which names the file itself
      and is therefore the strongest match;
    - by the normalised attachment key, for the blocks that declare the
      article id instead of a file id.

    Only *tight* blocks are indexed, and never a holder: a node that
    declares more than one attachment describes more than one attachment,
    and a node that holds other blocks describes all of them (#89). A block
    that names the file id wins over one that merely carries the article
    id, because it describes the file more precisely.
    """
    found: list[tuple[str, ET.Element, bool]] = []
    for node in root.iter():
        if not _is_block_element(node):
            continue
        if _has_block_descendant(node):
            continue
        eids = _declared_eids(node)
        if len(eids) != 1:
            continue
        found.append((eids[0], node, _declares_file_id(node)))

    by_eid: dict[str, ET.Element] = {}
    by_key: dict[str, ET.Element] = {}
    for names_file in (True, False):
        for eid, node, is_file_id in found:
            if is_file_id != names_file:
                continue
            by_eid.setdefault(eid.strip().lower(), node)
            by_key.setdefault(_attachment_key(eid), node)
    return by_eid, by_key


def _attachment_container(
    el: ET.Element,
    parent_map: dict[ET.Element, ET.Element],
    eid: str,
    blocks: tuple[dict[str, ET.Element], dict[str, ET.Element]],
) -> ET.Element:
    """The node whose metadata describes the attachment named by ``eid``.

    Five candidates, in order of how well they describe *this* attachment:

    1. the tight block that declares this exact object id -- the strongest
       match, because it names the file rather than the article;
    2. the nearest ancestor of the element that carried the id which holds
       exactly one attachment, and that one;
    3. the nearest ancestor that declares this attachment at all -- the block
       may also hold a nested object, but its own label still names the file;
    4. the tight block that declares this attachment's key, for the article
       id that sits outside every attachment block;
    5. the element that carried the id, which is the tightest scope there is.

    Falling back to the id's *parent*, as this used to, was the defect: the
    parent can be a node spanning the whole article, and the metadata of such
    a node belongs to every attachment in it (#89).
    """
    key = _attachment_key(eid)

    by_eid, by_key = blocks
    block = by_eid.get(eid.strip().lower())
    if block is not None:
        return block

    node = el
    while node is not None:
        if (
            _is_attachment_block(node)
            and _block_is_tight(node)
            and _block_is_scoped(node, key)
        ):
            return node
        node = parent_map.get(node)

    node = el
    while node is not None:
        if _is_attachment_block(node) and _block_declares(node, key):
            return node
        node = parent_map.get(node)

    block = by_key.get(key)
    if block is not None:
        return block
    return el


_ATTACHMENT_METADATA_MARKERS = (
    # The claims the scorer reads out of the metadata. The window and the
    # per-element cap bound how much of a container reaches the scorer, so a
    # marker they cut away would change the score by up to 100 points. They
    # are therefore collected over the full text and appended.
    "full-text", "fulltext",
    "page-count", "pages",
    "attachment-size", "filesize", "file-size",
    "supplement", "supplementary", "mmc", "appendix", "graphical", "thumbnail",
    "-am.pdf", "aam-pdf", "accepted manuscript", "accepted-manuscript",
    "author manuscript", "manuscript version", "submitted to",
)


def _marker_hits(text: str) -> list[str]:
    """The decisive markers present in `text`."""
    low = text.lower()
    return [marker for marker in _ATTACHMENT_METADATA_MARKERS if marker in low]


def _marker_survivors(text: str) -> str:
    """The decisive markers present in `text`, as a compact tail.

    A marker that falls outside the metadata window, or one the per-element
    cap cuts in half, would silently change the score -- up to 100 points for
    the version of record's own "full-text" claim. Collecting the markers
    over the full text and appending them keeps the window a bound on the
    size of the metadata rather than on what it says.
    """
    hits = _marker_hits(text)
    if not hits:
        return ""
    return "[markers: " + " ".join(hits) + "]"


def _name_survivors(el: ET.Element) -> str:
    """The decisive markers carried by the container's element names.

    An element's name -- "page-count", "attachment-size" -- reaches the
    haystack only through the part it produces, and the builder produces a
    part only for an element that has text. Reading the name of an element
    that produced no part would add a marker no version of the metadata
    ever held: an empty ``<supplementary-material/>`` would demote the
    version of record on its own.

    The attribute names and values are read the same way -- the builder
    emits an attribute part only for a non-empty value -- so the survivors
    are exactly the markers the unbounded metadata holds.
    """
    hits: list[str] = []
    for node in el.iter():
        local = _local_name(str(node.tag))
        name_hits = _marker_hits(local)
        if name_hits and _element_text(node):
            hits.extend(name_hits)
        for attr_name, attr_value in node.attrib.items():
            attr_local = _local_name(str(attr_name))
            if attr_value:
                hits.extend(_marker_hits(attr_local))
                hits.extend(_marker_hits(str(attr_value)))
    if not hits:
        return ""
    return "[markers: " + " ".join(sorted(set(hits))) + "]"


def _cap_text(text: str) -> str:
    """Shorten one element's text to the per-element cap.

    The beginning is kept: the markers the scorer reads ("supplementary",
    "thumbnail", "accepted manuscript") sit at the head of a label, and a cut
    through the middle of one would leave the scorer a word it cannot match.
    A cut that does land inside a marker is repaired by appending the
    markers the full text holds, so a marker is never truncated into
    something the scorer cannot read.
    """
    if len(text) <= _ATTACHMENT_METADATA_PART_CAP:
        return text
    cut = text[:_ATTACHMENT_METADATA_PART_CAP]
    space = cut.rfind(" ")
    if space > _ATTACHMENT_METADATA_PART_CAP // 2:
        cut = cut[:space]
    survivors = _marker_survivors(text)
    return f"{cut} {survivors}" if survivors else cut


def _attachment_metadata(el: ET.Element) -> str:
    """Metadata of an attachment's container, bounded to a few KB.

    Two bounds apply. The total keeps a pathological container from filling
    the scorer's haystack; the per-element cap keeps one long element from
    pushing the block's own markers out of the window, which would cost the
    version of record its own "full-text" claim.

    The bounds are a bound on size, not on meaning: the markers the scorer
    reads are collected over the whole container and appended, so a marker
    the window or the cap would have cut away cannot change the score.
    """
    parts: list[str] = []
    size = 0
    for node in el.iter():
        local = _local_name(str(node.tag))
        text = _element_text(node)
        if text:
            part = f"{local}:{_cap_text(text)}"
            if size + len(part) + 1 > _ATTACHMENT_METADATA_WINDOW:
                break
            parts.append(part)
            size += len(part) + 1
        for attr_name, attr_value in node.attrib.items():
            attr_local = _local_name(str(attr_name))
            if attr_value:
                part = f"{attr_local}:{attr_value}"
                if size + len(part) + 1 > _ATTACHMENT_METADATA_WINDOW:
                    break
                parts.append(part)
                size += len(part) + 1
        if size >= _ATTACHMENT_METADATA_WINDOW:
            break
    names = _name_survivors(el)
    if names:
        parts.append(names)
    return " ".join(parts).lower()


_ACCEPTED_MANUSCRIPT_MARKERS = (
    "-am.pdf", "aam-pdf", "accepted manuscript", "accepted-manuscript",
    "author manuscript", "manuscript version", "submitted to",
)


def _attachment_score(eid: str, metadata: str) -> int:
    eid_lower = eid.lower()
    haystack = f"{eid} {metadata}".lower()
    score = 0
    if eid_lower.endswith(".pdf"):
        score += 20
    if "pdf" in haystack:
        score += 10
    # Scope the version-of-record boost to the object id. Matching "main"
    # anywhere in the haystack also catches accepted-manuscript attachments:
    # their storage URLs carry a "/main/" path segment, so an `-am.pdf` picked
    # up the same +100 and outranked the `-main.pdf` it accompanies (#73).
    if eid_lower.endswith("-main.pdf") or "full-text" in haystack or "fulltext" in haystack:
        score += 100
    if "page-count" in haystack or "pages" in haystack:
        score += 5
    if "attachment-size" in haystack or "filesize" in haystack or "file-size" in haystack:
        score += 5
    if any(
        marker in haystack
        for marker in ("supplement", "supplementary", "mmc", "appendix", "graphical", "thumbnail")
    ):
        score -= 100
    if any(marker in haystack for marker in _ACCEPTED_MANUSCRIPT_MARKERS):
        # Below the version of record even if the manuscript claims
        # "full-text" (+100), yet still above supplementary material (-100) so
        # a manuscript-only article keeps its best candidate.
        score -= 60
    return score


def _extract_pdf_attachment_eids(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Failed to parse Elsevier XML attachments: %s", e)
        return []

    parent_map = {child: parent for parent in root.iter() for child in parent}

    # Index the tight attachment blocks: a node that names an attachment and
    # declares exactly one of them. The article-level id sits outside every
    # block, so this index is what describes the version of record it names.
    blocks = _attachment_blocks(root)

    candidates: list[tuple[int, int, str]] = []
    seen: set[str] = set()

    for el in root.iter():
        for eid in _eids_in(el):
            # De-duplicate on the attachment identity, not on the spelling:
            # "1-S2.0-X-MAIN.PDF" and "1-s2.0-x-main.pdf" are one file, and
            # asking the object store for it twice wastes a request.
            key = _attachment_key(eid)
            if key in seen:
                continue
            seen.add(key)
            container = _attachment_container(el, parent_map, eid, blocks)
            metadata = _attachment_metadata(container)
            candidates.append((_attachment_score(eid, metadata), len(candidates), eid))

    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [eid for _, _, eid in candidates]


def _pdf_page_count(content: bytes) -> int | None:
    try:
        import fitz  # type: ignore[import-not-found]
    except Exception:
        return None

    try:
        with fitz.open(stream=content, filetype="pdf") as doc:
            return int(doc.page_count)
    except Exception:
        return None


def _valid_pdf_bytes(content: bytes, label: str, *, reject_single_page: bool) -> bool:
    if content[:5] != b"%PDF-":
        logger.info("Elsevier API: %s returned non-PDF", label)
        return False
    if len(content) < 10000:
        logger.warning("Elsevier API: %s too small (%d bytes)", label, len(content))
        return False
    if reject_single_page and _pdf_page_count(content) == 1:
        logger.info("Elsevier API: %s is a 1-page preview", label)
        return False
    return True


def _fetch_attachment_eids(
    doi: str, api_key: str, inst_token: str = "", proxies: dict[str, str] | None = None,
) -> list[str]:
    """Fetch FULL XML and extract MAIN PDF attachment EIDs."""
    url = f"{ELSEVIER_API}/article/doi/{doi}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/xml",
    }
    if inst_token:
        headers["X-ELS-InstToken"] = inst_token

    resp = _api_request(url, headers, params={"view": "FULL"}, proxies=proxies)
    if not resp or resp.status_code != 200:
        # The XML → attachment-EID chain starts here; a 403 on this request
        # means the requestor (key + egress IP) is not configured for full
        # text at all — no EID can exist. Name the two real causes so the
        # next step is actionable instead of a re-diagnosis.
        if resp is not None and resp.status_code == 403:
            text = _error_status_text(resp)
            if "requestor configuration settings insufficient" in text.lower():
                logger.warning(
                    "Elsevier API: 403 '%s' on view=FULL for %s — requestor (key + egress IP) "
                    "has no full-text configuration. Either the egress IP is outside the "
                    "institution's registered range (off-campus: route via SCANSCI_PDF_PROXY / "
                    "network_proxy to campus egress), or the institution has not enabled API "
                    "full-text for this key (contact the institution's Elsevier admin; "
                    "insttoken is the off-campus alternative).",
                    text, doi,
                )
        return []

    eids = _extract_pdf_attachment_eids(resp.text)
    for eid in eids:
        logger.info("Elsevier API: found PDF attachment %s", eid)
    return eids


def _fetch_pdf_by_eid(
    eid: str, api_key: str, inst_token: str = "", proxies: dict[str, str] | None = None,
) -> bytes | None:
    """Download PDF via Content Object API using attachment EID."""
    url = f"{ELSEVIER_API}/object/eid/{quote(eid, safe='')}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/pdf",
    }
    if inst_token:
        headers["X-ELS-InstToken"] = inst_token

    resp = _api_request(url, headers, proxies=proxies)
    if not resp:
        return None

    if resp.status_code == 404:
        logger.info("Elsevier API: attachment %s not found (404)", eid)
        return None

    if resp.status_code != 200:
        logger.info("Elsevier API: HTTP %d for attachment %s", resp.status_code, eid)
        return None

    if not _response_is_pdf(resp):
        logger.info("Elsevier API: attachment %s returned non-PDF", eid)
        return None

    if not _valid_pdf_bytes(resp.content, f"attachment {eid}", reject_single_page=True):
        return None

    logger.info("Elsevier API: downloaded %d bytes via attachment %s", len(resp.content), eid)
    return resp.content


def _fetch_pdf_direct(
    doi: str, api_key: str, inst_token: str = "", proxies: dict[str, str] | None = None,
) -> bytes | None:
    """Fallback: download PDF directly from article endpoint (works for OA)."""
    url = f"{ELSEVIER_API}/article/doi/{doi}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/pdf",
    }
    if inst_token:
        headers["X-ELS-InstToken"] = inst_token

    resp = _api_request(url, headers, proxies=proxies)
    if not resp or resp.status_code != 200:
        return None

    content_type = _header(resp.headers, "content-type")
    if not _response_is_pdf(resp):
        logger.info("Elsevier API: direct endpoint returned non-PDF (%s)", content_type[:50])
        return None

    if not _valid_pdf_bytes(resp.content, "direct PDF", reject_single_page=True):
        return None

    logger.info("Elsevier API: downloaded %d bytes directly for %s", len(resp.content), doi)
    return resp.content


def _error_status_text(resp: requests.Response) -> str:
    """Extract <statusText> from an Elsevier error XML body ("" if absent)."""
    try:
        root = ET.fromstring(resp.text)
    except (ET.ParseError, ValueError):
        return ""
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "statusText":
            return (el.text or "").strip()
    return ""


def _api_request(
    url: str,
    headers: dict,
    *,
    params: dict[str, str] | None = None,
    proxies: dict[str, str] | None = None,
) -> requests.Response | None:
    """Make an Elsevier API request with error handling."""
    try:
        session = requests.Session()
        session.trust_env = False
        if proxies:
            session.proxies = proxies
        resp = session.get(
            url,
            headers=headers,
            params=params,
            timeout=30,
            allow_redirects=True,
        )
    except requests.exceptions.SSLError as exc:
        # A campus proxy that terminates TLS presents its own certificate, so a
        # retry without verification is the one legitimate case -- but it has to
        # be asked for. Retrying unverified by default turned a certificate error
        # into a silent success (#70).
        from ..config import load_config
        from ..network import allow_self_signed as _allow_self_signed
        if not _allow_self_signed(load_config()):
            logger.warning(
                "Elsevier API: TLS verification failed and no retry was made "
                "(set institutional_allow_self_signed for a self-signed campus connector): %s",
                exc,
            )
            return None
        logger.warning(
            "Elsevier API: retrying without TLS verification (institutional_allow_self_signed is on)"
        )
        try:
            session = requests.Session()
            session.trust_env = False
            if proxies:
                session.proxies = proxies
            resp = session.get(
                url,
                headers=headers,
                params=params,
                timeout=30,
                allow_redirects=True,
                verify=False,
            )
        except requests.RequestException as e:
            logger.warning("Elsevier API request failed: %s", e)
            return None
    except requests.RequestException as e:
        logger.warning("Elsevier API request failed: %s", e)
        return None

    if resp.status_code in (401, 403):
        # 403 ≠ key invalid: valid keys 403 on unentitled resources (#56) —
        # surface Elsevier's own statusText instead of guessing.
        detail = _error_status_text(resp) or "key invalid or not entitled"
        logger.warning("Elsevier API: HTTP %d (%s)", resp.status_code, detail)
    elif resp.status_code == 429:
        logger.warning("Elsevier API: rate limited")

    return resp


def fetch_fulltext(doi: str, api_key: str, inst_token: str = "", config: dict | None = None) -> dict | None:
    """Fetch article full text via Elsevier RetrievalAPI."""
    if not api_key:
        return None

    url = f"{ELSEVIER_API}/article/doi/{doi}"
    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/xml",
    }
    if inst_token:
        headers["X-ELS-Insttoken"] = inst_token

    resp = _api_request(url, headers, params={"view": "FULL"}, proxies=_resolve_proxies(config))
    if not resp:
        return None

    if resp.status_code == 401:
        logger.warning("Elsevier API: invalid API key")
        return None
    if resp.status_code == 404:
        logger.info("Elsevier API: DOI %s not found", doi)
        return None
    if resp.status_code != 200:
        logger.info("Elsevier API: HTTP %d for %s", resp.status_code, doi)
        return None

    return _parse_xml(resp.text)


def _parse_xml(xml_text: str) -> dict | None:
    """Parse Elsevier XML response into structured data."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("Failed to parse Elsevier XML: %s", e)
        return None

    result = {
        "title": "",
        "authors": [],
        "abstract": "",
        "full_text": "",
        "figures": [],
        "references": [],
    }

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "title" and el.text and el.text.strip():
            result["title"] = el.text.strip()
            break

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local in ("creator", "author"):
            if el.text and el.text.strip():
                result["authors"].append(el.text.strip())
    if not result["authors"]:
        for el in root.iter():
            local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
            if local == "author":
                given = ""
                surname = ""
                for child in el:
                    child_local = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                    if "given" in child_local:
                        given = (child.text or "").strip()
                    elif "surname" in child_local or "last" in child_local:
                        surname = (child.text or "").strip()
                if given or surname:
                    result["authors"].append(f"{given} {surname}".strip())

    result["abstract"] = _extract_abstract(root)
    result["full_text"] = _extract_body(root)
    result["references"] = _extract_references(root)

    return result


def _extract_abstract(root: ET.Element) -> str:
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local in ("abstract", "description") and _collect_text(el).strip():
            return _collect_text(el).strip()
    return ""


def _extract_body(root: ET.Element) -> str:
    parts = []

    body = None
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "body":
            body = el
            break

    if body is None:
        return ""

    def _find_sections(el):
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "section":
            yield el
        for child in el:
            yield from _find_sections(child)

    for section in _find_sections(body):
        heading = ""
        content_parts = []

        for child in section:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag in ("section-title", "sectiontitle", "heading"):
                heading = _collect_text(child).strip()
            elif tag == "para":
                text = _collect_text(child).strip()
                if text:
                    content_parts.append(text)

        if heading and content_parts:
            parts.append(f"## {heading}\n\n{' '.join(content_parts)}")
        elif content_parts:
            parts.append(" ".join(content_parts))

    if not parts:
        for child in body.iter():
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "para":
                text = _collect_text(child).strip()
                if text:
                    parts.append(text)

    return "\n\n".join(parts)


def _extract_references(root: ET.Element) -> list[str]:
    refs = []

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local in ("bib-reference", "reference"):
            text = " ".join(_collect_text(el).split())
            if text and len(text) > 10:
                refs.append(text)

    if refs:
        return refs

    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if local == "bibliography":
            for ref in el:
                text = " ".join(_collect_text(ref).split())
                if text and len(text) > 10:
                    refs.append(text)
            if refs:
                return refs

    return refs


def _collect_text(el: ET.Element) -> str:
    parts = []
    if el.text:
        parts.append(el.text)
    for child in el:
        parts.append(_collect_text(child))
        if child.tail:
            parts.append(child.tail)
    return " ".join(parts)
