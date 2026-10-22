"""Chinese DOI resolution, title normalisation, and landing-page parsing.

China runs its own DOI registration agencies (ISTIC/China DOI, CNKI) rather
than Crossref.  A ``10.3969`` DOI resolves to Wanfang, CNKI-registered DOIs
(``10.16235/j.cnki...``) resolve through ``chndoi.org`` to ``link.cnki.net``,
and journal-registered DOIs can land on the journal's own website.  Crossref
knows none of them, so the normal metadata path has to fall back to these
sources.

This module is deliberately dependency-light: it only talks HTTP and parses
HTML, so it can be used by the free-source race as well as by metadata and
filename code.
"""

from __future__ import annotations

import html as _html
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests

from .log import get_logger
from .network import CHROME_UA, proxy_dict, request_timeout, select_proxy_for_url

log = get_logger()

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2028-\u202f\ufeff]")

#: DOI prefixes / suffixes registered through Chinese agencies.  ``10.3969``
#: is the big ISTIC/China DOI prefix used by Wanfang; CNKI registers many
#: small prefixes but always puts ``j.cnki`` (journal) or ``b.cnki`` (book)
#: into the suffix.
_CN_DOI_PREFIXES = ("10.3969/", "10.12074/")
_CN_DOI_MARKERS = (
    "/j.cnki", "/b.cnki", ".cnki.", "/j.issn.", "/b.issn.",
    "/sp.j.", "/cjstp.", "/j.aas.", "/j.cjstp.", "/j.cs.",
)

_FULLWIDTH_PUNCT = {
    "（": "(", "）": ")", "［": "[", "］": "]", "｛": "{", "｝": "}",
    "，": ",", "：": ":", "；": ";", "？": "?", "！": "!",
    "“": '"', "”": '"', "‘": "'", "’": "'", "、": ",",
    "【": "[", "】": "]", "《": "<", "》": ">", "——": "-",
    "–": "-", "—": "-", "～": "~", "　": " ",
}

_WANFANG_TITLE_SUFFIX_RE = re.compile(
    r"\s*[-_|—]\s*(?:期刊-)?(?:万方数据知识服务平台|万方数据|中国知网|中国知网CNKI|"
    r"维普期刊官网.*|维普网.*|国家哲学社会科学文献中心.*)\s*$",
    re.I,
)

_RESOLUTION_TTL = 6 * 3600.0
_RESOLUTION_CACHE: dict[str, tuple[float, "DoiResolution"]] = {}
_RESOLUTION_LOCK = threading.Lock()


def has_cjk(text: str) -> bool:
    return bool(text and _CJK_RE.search(text))


def to_halfwidth(text: str) -> str:
    """Convert full-width ASCII/punctuation to its half-width form.

    Handles the full ``\uff01-\uff5e`` range plus the ideographic space and
    the common Chinese punctuation that users paste along with titles/DOIs.
    """
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    for src, dst in _FULLWIDTH_PUNCT.items():
        out = out.replace(src, dst)
    return out


def normalize_cn_text(text: str) -> str:
    """Normalise a Chinese title/abstract for display and matching.

    - strips HTML tags (NCPSSD highlights matches with ``<font>`` tags)
    - decodes HTML entities
    - full-width brackets/punctuation → half-width
    - collapses whitespace / zero-width characters
    """
    if not text:
        return ""
    text = _html.unescape(str(text))
    text = _TAG_RE.sub("", text)
    text = _ZERO_WIDTH_RE.sub("", text)
    text = to_halfwidth(text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


def normalize_title_for_match(title: str) -> str:
    """Aggressive normalisation used only for title similarity."""
    text = normalize_cn_text(title).lower()
    # Keep CJK and alphanumerics; punctuation differences are noise.
    return "".join(ch for ch in text if ch.isalnum() or _CJK_RE.match(ch))


def title_similarity(left: str, right: str) -> float:
    """Similarity in [0, 1] that works for Chinese and Latin titles."""
    a = normalize_title_for_match(left)
    b = normalize_title_for_match(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # One title embedded in the other (common for subtitles) is a strong
    # signal, but require enough shared characters to avoid one-word matches.
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 6 and shorter in longer:
        return 0.95
    return SequenceMatcher(None, a, b).ratio()


def is_chinese_doi(doi: str) -> bool:
    """Whether *doi* belongs to a Chinese registration agency/pattern."""
    value = normalize_doi_loose(doi).lower()
    if not value:
        return False
    if value.startswith(_CN_DOI_PREFIXES):
        return True
    return any(marker in value for marker in _CN_DOI_MARKERS)


def normalize_doi_loose(value: str) -> str:
    """Small local DOI cleaner (avoids importing identifiers.py → cycles)."""
    value = (value or "").strip()
    value = to_halfwidth(value)
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I)
    value = re.sub(r"^doi\s*:\s*", "", value, flags=re.I)
    value = re.sub(r"[。，；：、]+$", "", value).strip()
    return unquote(value).strip()


def parse_chinese_doi(doi: str) -> dict[str, str] | None:
    """Parse the structured parts of common Chinese DOI shapes.

    Examples::

        10.3969/j.issn.1000-0135.2023.01.001
        10.16235/j.cnki.33-1005/c.2025.03.002

    Returns a dict with ``kind`` and whatever fields could be recovered.
    """
    value = normalize_doi_loose(doi)
    match = re.match(
        r"^10\.3969/j\.issn\.(?P<issn>\d{4}-\d{3}[\dXx])\.(?P<year>\d{4})\."
        r"(?P<issue>\d{2,3})\.(?P<seq>\d{3,4})$",
        value,
        re.I,
    )
    if match:
        return {"kind": "istic", **{k: v.upper() if k == "issn" else v
                                     for k, v in match.groupdict().items()}}
    if re.match(r"^10\.\d+/[jb]\.(?:cnki|issn)", value, re.I):
        return {"kind": "cnki", "doi": value}
    if value.startswith("10.3969/"):
        return {"kind": "istic", "doi": value}
    return None


@dataclass
class DoiResolution:
    """Everything we learned while resolving a Chinese DOI."""

    doi: str
    landing_url: str = ""
    host: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    publisher: str = ""
    registration_agency: str = ""
    urls: list[str] = field(default_factory=list)
    cnki: dict[str, Any] = field(default_factory=dict)
    wanfang_id: str = ""
    wanfang_file_id: str = ""
    ncpssd_id: str = ""
    status: str = ""

    def as_metadata(self) -> dict[str, Any]:
        """Return the fields in the Crossref-ish shape used by rename.py."""
        if not self.title:
            return {}
        return {
            "title": [self.title],
            "author": [{"family": name} for name in self.authors],
            "DOI": self.doi,
            "URL": f"https://doi.org/{self.doi}",
            "type": "journal-article",
            "publisher": self.publisher or _publisher_for(self.host),
        }


def _publisher_for(host: str) -> str:
    if "wanfang" in host:
        return "万方数据"
    if "cnki" in host or "chndoi" in host:
        return "中国知网"
    if "cqvip" in host:
        return "维普"
    if "ncpssd" in host:
        return "国家哲学社会科学文献中心"
    return ""


def _session(config: dict[str, Any] | None = None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update({
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    if config:
        session.headers.setdefault("Accept-Encoding", "gzip, deflate")
    return session


def _get(
    session: requests.Session,
    url: str,
    config: dict[str, Any] | None,
    *,
    allow_redirects: bool = True,
    timeout: int | None = None,
    retries: int = 2,
) -> requests.Response | None:
    cfg = config or {}
    timeout = timeout or max(10, int(request_timeout(cfg)[1]))
    proxies = proxy_dict(select_proxy_for_url(url, cfg)) if cfg else None
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(
                url,
                timeout=timeout,
                allow_redirects=allow_redirects,
                proxies=proxies,
                headers={"Referer": "https://www.ncpssd.cn/"} if "ncpssd" in url else None,
            )
            if resp.status_code < 500:
                return resp
            last_error = requests.HTTPError(f"HTTP {resp.status_code}")
        except requests.RequestException as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(0.6 * (attempt + 1))
    if last_error:
        log.debug("Chinese DOI request failed for %s: %s", url[:80], last_error)
    return None


def response_text(resp: requests.Response) -> str:
    """Decode Chinese publisher responses without mojibake."""
    encoding = (resp.encoding or "").lower()
    if encoding in ("gb2312", "gbk", "gb18030"):
        try:
            return resp.content.decode("gb18030", "ignore")
        except Exception:
            pass
    if encoding and encoding != "iso-8859-1":
        try:
            return resp.content.decode(encoding, "ignore")
        except Exception:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return resp.content.decode(enc)
        except UnicodeDecodeError:
            continue
    return resp.text


def _handle_urls(doi: str, session: requests.Session,
                 config: dict[str, Any] | None) -> list[str]:
    """Ask the DOI Handle API for a Chinese DOI's registered URL(s).

    This is faster and more reliable than following ``doi.org`` redirects:
    the redirector sometimes returns HTTP 600 for Chinese multi-resolution
    DOIs, while the Handle API still returns the URL records.
    """
    url = f"https://doi.org/api/handles/{quote(doi, safe='/._-()')}"
    resp = _get(session, url, config, timeout=12, retries=1)
    if not resp or resp.status_code >= 400:
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []
    urls: list[str] = []
    for item in payload.get("values", []) or []:
        if item.get("type") != "URL":
            continue
        value = (item.get("data") or {}).get("value") or ""
        value = value.strip()
        if value.startswith(("http://", "https://")):
            urls.append(value)
    return urls


def _read_varint(buf: bytes, index: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while index < len(buf):
        byte = buf[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 63:
            break
    raise ValueError("truncated varint")


def _proto_fields(buf: bytes) -> list[tuple[int, int, Any]]:
    """Decode one protobuf message into ``(field, wire_type, value)`` triples."""
    fields: list[tuple[int, int, Any]] = []
    index = 0
    while index < len(buf):
        key, index = _read_varint(buf, index)
        number, wire = key >> 3, key & 0x07
        if number <= 0:
            break
        if wire == 0:
            value, index = _read_varint(buf, index)
        elif wire == 2:
            length, index = _read_varint(buf, index)
            value = buf[index:index + length]
            index += length
        elif wire == 5:
            value = buf[index:index + 4]
            index += 4
        elif wire == 1:
            value = buf[index:index + 8]
            index += 8
        else:
            break
        fields.append((number, wire, value))
    return fields


def _find_proto_message(buf: bytes, target: int, depth: int = 0) -> bytes | None:
    if depth > 3:
        return None
    try:
        fields = _proto_fields(buf)
    except ValueError:
        return None
    for number, wire, value in fields:
        if number == target and wire == 2:
            return value
        if wire == 2:
            found = _find_proto_message(value, target, depth + 1)
            if found is not None:
                return found
    return None


def _text(value: bytes) -> str:
    for encoding in ("utf-8", "gb18030"):
        try:
            text = value.decode(encoding)
        except UnicodeDecodeError:
            continue
        if text and all(ch.isprintable() or ch in "\r\n\t" for ch in text):
            return text
    return ""


def _wanfang_detail_frame(wanfang_id: str) -> bytes:
    """gRPC-web frame for Wanfang's ``getDetailInFormation`` request."""
    def varint(value: int) -> bytes:
        out = bytearray()
        while True:
            chunk = value & 0x7F
            value >>= 7
            if value:
                out.append(chunk | 0x80)
            else:
                out.append(chunk)
                return bytes(out)

    def field(number: int, data: bytes) -> bytes:
        return bytes([(number << 3) | 2]) + varint(len(data)) + data

    payload = b"".join([
        field(1, b"Periodical"),
        field(2, wanfang_id.encode("utf-8")),
        field(3, b"AI_READ"),
        field(3, b"AI_EXTRACT"),
        field(3, b"ONLINE_READ"),
        field(3, b"DOWNLOAD"),
    ])
    return b"\x00" + len(payload).to_bytes(4, "big") + payload


def parse_wanfang_detail(data: bytes) -> dict[str, Any]:
    """Decode Wanfang's gRPC-web detail response into article metadata.

    The article message is field 103 (nested under 1/2).  Field numbers were
    verified against live 2026 responses: 1 file id, 2 title(s), 3 authors,
    24 journal, 33 year, 34 issue, 35 volume, 36 pages, 41 DOI, 45 ISSN,
    43 NCPSSD URL.
    """
    if not data:
        return {}
    payload = data[5:] if data[:1] == b"\x00" and len(data) >= 5 else data
    article = _find_proto_message(payload, 103)
    if article is None:
        return {}
    result: dict[str, Any] = {}
    titles: list[str] = []
    journals: list[str] = []
    authors: list[str] = []
    try:
        fields = _proto_fields(article)
    except ValueError:
        return {}
    for number, wire, value in fields:
        if wire != 2:
            if wire == 0 and number in (33, 34, 35, 37):
                result.setdefault({33: "year", 34: "issue", 35: "volume", 37: "pages"}[number],
                                  str(value))
            continue
        text = _text(value)
        if number == 1:
            result.setdefault("file_id", text)
        elif number == 2:
            if text:
                titles.append(text)
        elif number == 3:
            if text:
                authors.append(text)
        elif number == 24:
            if text:
                journals.append(text)
        elif number == 34:
            result.setdefault("issue", text)
        elif number == 35:
            result.setdefault("volume", text)
        elif number == 36:
            result.setdefault("pages", text)
        elif number == 41:
            result.setdefault("doi", text)
        elif number == 43:
            result.setdefault("ncpssd_url", text)
        elif number == 45:
            result.setdefault("issn", text)
        elif number == 20 and text:
            result.setdefault("abstract", text)
    if titles:
        # First title is Chinese; the second (if any) is the English title.
        chinese = [t for t in titles if _CJK_RE.search(t)]
        result["title"] = chinese[0] if chinese else titles[0]
        if len(titles) > 1:
            result["title_en"] = titles[1]
    if journals:
        chinese = [j for j in journals if _CJK_RE.search(j)]
        result["journal"] = chinese[0] if chinese else journals[0]
    result["authors"] = authors
    ncpssd_url = result.get("ncpssd_url") or ""
    match = re.search(r"[?&]id=([A-Za-z0-9]+)", ncpssd_url)
    if match:
        result["ncpssd_id"] = match.group(1)
    return {k: v for k, v in result.items() if v not in ("", [], None)}


def fetch_wanfang_detail(
    wanfang_id: str,
    landing_url: str,
    config: dict[str, Any] | None = None,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Call Wanfang's gRPC-web detail endpoint and return parsed metadata."""
    if not wanfang_id:
        return {}
    client = session or _session(config)
    proxies = proxy_dict(select_proxy_for_url("https://d.wanfangdata.com.cn/", config or {}))
    try:
        resp = client.post(
            "https://d.wanfangdata.com.cn/Detail.DetailService/getDetailInFormation",
            data=_wanfang_detail_frame(wanfang_id),
            timeout=25,
            proxies=proxies,
            headers={
                "Content-Type": "application/grpc-web+proto",
                "x-grpc-web": "1",
                "x-user-agent": "grpc-web-javascript/0.1",
                "Origin": "https://d.wanfangdata.com.cn",
                "Referer": landing_url or "https://d.wanfangdata.com.cn/",
            },
        )
    except requests.RequestException as exc:
        log.debug("Wanfang detail request failed: %s", exc)
        return {}
    if resp.status_code >= 400:
        return {}
    return parse_wanfang_detail(resp.content)


def resolve_chinese_doi(
    doi: str,
    config: dict[str, Any] | None = None,
    *,
    refresh: bool = False,
) -> DoiResolution:
    """Resolve a Chinese DOI to its landing page and identifiers.

    The result is cached for six hours per process; resolution hits
    ``doi.org`` (Handle API), ``chndoi.org``, CNKI, and Wanfang.
    """
    value = normalize_doi_loose(doi)
    now = time.time()
    if not refresh:
        with _RESOLUTION_LOCK:
            cached = _RESOLUTION_CACHE.get(value)
            if cached and cached[0] > now:
                return cached[1]

    result = DoiResolution(doi=value)
    session = _session(config)

    urls = _handle_urls(value, session, config)
    if not urls:
        # Fall back to the normal redirector (some proxies block the API host).
        resp = _get(session, f"https://doi.org/{quote(value, safe='/._-()')}",
                    config, timeout=25, retries=1)
        if resp is not None and resp.status_code < 400:
            urls = [resp.url]
    result.urls = urls

    if not urls:
        result.status = "unresolved"
        _cache_resolution(result)
        return result

    primary = urls[0]
    primary_host = (urlparse(primary).hostname or "").lower()

    if "chndoi.org" in primary_host:
        _resolve_chndoi(result, primary, session, config)
    elif "wanfangdata" in primary_host:
        _resolve_wanfang(result, primary, session, config)
    elif "cnki" in primary_host:
        _resolve_cnki(result, primary, session, config)
    else:
        _resolve_generic(result, primary, session, config)

    if not result.title and result.landing_url:
        resp = _get(session, result.landing_url, config, timeout=20, retries=1)
        if resp is not None and resp.status_code < 400:
            result.title = title_from_html(response_text(resp))

    if not result.host and result.landing_url:
        result.host = (urlparse(result.landing_url).hostname or "").lower()
    result.status = "ok" if result.landing_url or result.cnki else "partial"
    _cache_resolution(result)
    return result


def _cache_resolution(result: DoiResolution) -> None:
    with _RESOLUTION_LOCK:
        _RESOLUTION_CACHE[result.doi] = (time.time() + _RESOLUTION_TTL, result)


def _resolve_chndoi(result: DoiResolution, handler_url: str,
                    session: requests.Session,
                    config: dict[str, Any] | None) -> None:
    result.landing_url = handler_url
    result.host = "www.chndoi.org"
    resp = _get(session, handler_url, config, timeout=25)
    if not resp or resp.status_code >= 400:
        # The handler occasionally answers 600 for multi-resolution DOIs;
        # the Handle API still gave us the CNKI link, so use it directly.
        cnki_url = _pick_cnki_url(result.urls)
        if cnki_url:
            _resolve_cnki(result, cnki_url, session, config)
        return
    meta = parse_chndoi_handler(response_text(resp))
    if meta.get("title"):
        result.title = meta["title"]
    if meta.get("authors"):
        result.authors = meta["authors"]
    if meta.get("publisher"):
        result.publisher = meta["publisher"]
    urls = meta.get("urls") or []
    result.urls = list(dict.fromkeys(result.urls + urls))
    cnki_url = _pick_cnki_url(urls)
    if cnki_url:
        _resolve_cnki(result, cnki_url, session, config)
        return
    # Some Chinese journals register a multi-resolution DOI that points at
    # their own Magtech/官网 page instead of CNKI.  Follow that URL so the
    # journal-site lane can scrape its PDF link.
    for url in urls:
        host = (urlparse(url).hostname or "").lower()
        if any(skip in host for skip in ("chndoi.org", "doi.org", "cnki.net")):
            continue
        resp = _get(session, url, config, timeout=20, retries=1)
        if resp is not None and resp.status_code < 400:
            result.landing_url = resp.url or url
            result.host = (urlparse(result.landing_url).hostname or "").lower()
            result.title = result.title or title_from_html(response_text(resp))
            return


def _pick_cnki_url(urls: list[str]) -> str:
    """Prefer the domestic CNKI link; fall back to the overseas mirror."""
    candidates = [u for u in urls if "cnki.net" in urlparse(u).hostname.lower()]
    for host in ("link.cnki.net", "kns.cnki.net", "link.oversea.cnki.net"):
        for url in candidates:
            if urlparse(url).hostname.lower() == host:
                return url
    return candidates[0] if candidates else ""


def _resolve_cnki(result: DoiResolution, url: str, session: requests.Session,
                  config: dict[str, Any] | None) -> None:
    resp = _get(session, url, config, timeout=25, retries=1)
    if not resp or resp.status_code >= 400:
        return
    detail = parse_cnki_detail(response_text(resp))
    detail.setdefault("landing_url", resp.url or url)
    # The canonical /doi/ URL does not carry the expiring session token, so
    # it is the right URL to re-fetch later with institutional cookies.
    detail.setdefault("canonical_url", f"https://link.cnki.net/doi/{result.doi}")
    result.cnki = detail
    result.landing_url = detail.get("landing_url") or url
    result.host = (urlparse(result.landing_url).hostname or "").lower()
    result.title = result.title or detail.get("title", "")
    if not result.authors and detail.get("authors"):
        result.authors = detail["authors"]


def _resolve_wanfang(result: DoiResolution, url: str,
                     session: requests.Session,
                     config: dict[str, Any] | None) -> None:
    resp = _get(session, url, config, timeout=25, retries=1)
    if not resp or resp.status_code >= 400:
        result.landing_url = url
        result.host = (urlparse(url).hostname or "").lower()
        return
    result.landing_url = resp.url or url
    result.host = (urlparse(result.landing_url).hostname or "").lower()
    result.wanfang_id = extract_wanfang_id(result.landing_url)
    result.title = title_from_html(response_text(resp))
    result.publisher = result.publisher or "万方数据"

    # The static HTML title is just the site name; the SPA loads the article
    # from a gRPC-web detail call.  That call also returns the internal file
    # id used by the download endpoint.
    detail = fetch_wanfang_detail(
        result.wanfang_id, result.landing_url, config, session=session,
    )
    if detail:
        if detail.get("title"):
            result.title = detail["title"]
        if detail.get("authors"):
            result.authors = detail["authors"]
        result.wanfang_file_id = detail.get("file_id", "")
        result.ncpssd_id = detail.get("ncpssd_id", "")
    if result.title.strip() in ("万方数据知识服务平台", "万方数据"):
        result.title = ""


def _resolve_generic(result: DoiResolution, url: str,
                     session: requests.Session,
                     config: dict[str, Any] | None) -> None:
    resp = _get(session, url, config, timeout=25, retries=1)
    result.landing_url = (resp.url if resp is not None and resp.status_code < 400 else url)
    result.host = (urlparse(result.landing_url).hostname or "").lower()
    if resp is not None and resp.status_code < 400:
        result.title = title_from_html(response_text(resp))


def parse_chndoi_handler(html: str) -> dict[str, Any]:
    """Parse ``chndoi.org/Resolution/Handler`` multi-resolution pages."""
    text = html or ""
    title = _label_value(text, "题名")
    authors = _label_value(text, "作者")
    publisher = _label_value(text, "出版机构")
    urls = re.findall(r'href="(https?://[^"]+)"', text)
    urls = [u.replace("&amp;", "&") for u in urls]
    return {
        "title": normalize_cn_text(title),
        "authors": [normalize_cn_text(a) for a in re.split(r"[;；]", authors) if a.strip()],
        "publisher": normalize_cn_text(publisher),
        "urls": urls,
    }


def _label_value(html_text: str, label: str) -> str:
    match = re.search(
        rf"<label>\s*{re.escape(label)}\s*[:：]\s*</label>\s*([^<]+)",
        html_text,
        re.I,
    )
    if not match:
        return ""
    return _html.unescape(match.group(1)).strip()


def parse_cnki_detail(html: str) -> dict[str, Any]:
    """Extract CNKI's internal IDs and download links from a detail page."""
    text = html or ""
    detail: dict[str, Any] = {}
    title = ""
    meta_title = re.search(
        r'<meta[^>]+name=["\']citation_title["\'][^>]+content=["\']([^"\']+)',
        text, re.I,
    )
    if meta_title:
        title = _html.unescape(meta_title.group(1))
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        if match:
            title = _html.unescape(_TAG_RE.sub("", match.group(1)))
    detail["title"] = normalize_cn_text(_WANFANG_TITLE_SUFFIX_RE.sub("", title))

    for field_name in ("filename", "dbcode", "tablename"):
        match = re.search(rf"{field_name}\s*=\s*[\"']?([A-Za-z0-9_]+)", text, re.I)
        if match:
            detail[field_name] = match.group(1)

    pdf_url = _anchor_by_id(text, "pdfDown")
    caj_url = _anchor_by_id(text, "cajDown")
    if pdf_url:
        detail["pdf_url"] = _html.unescape(pdf_url).replace("&amp;", "&")
    if caj_url:
        detail["caj_url"] = _html.unescape(caj_url).replace("&amp;", "&")

    authors = re.findall(
        r'<meta[^>]+name=["\']citation_author["\'][^>]+content=["\']([^"\']+)',
        text, re.I,
    )
    if authors:
        detail["authors"] = [normalize_cn_text(a) for a in authors if a.strip()]
    return detail


def _anchor_by_id(html_text: str, element_id: str) -> str:
    match = re.search(
        rf'<a[^>]*id=["\']{re.escape(element_id)}["\'][^>]*>',
        html_text,
        re.I,
    )
    if not match:
        return ""
    href = re.search(r'href=["\']([^"\']+)["\']', match.group(0), re.I)
    return href.group(1) if href else ""


def extract_wanfang_id(url: str) -> str:
    """Extract the opaque Wanfang article token from a landing URL."""
    match = re.search(r"/periodical/([^/?#]+)", url or "")
    return unquote(match.group(1)) if match else ""


def _collect_meta(html_text: str) -> dict[str, list[str]]:
    metas: dict[str, list[str]] = {}
    for match in re.finditer(r"<meta\b[^>]*>", html_text or "", re.I):
        tag = match.group(0)
        name = re.search(r'(?:name|property)=["\']([^"\']+)["\']', tag, re.I)
        content = re.search(r'content=["\']([^"\']*)["\']', tag, re.I)
        if not name or not content:
            continue
        metas.setdefault(name.group(1).strip().lower(), []).append(
            _html.unescape(content.group(1)).strip()
        )
    return metas


def _first_meta(metas: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = metas.get(name)
        if values:
            for value in values:
                if value:
                    return value
    return ""


def parse_journal_meta(html_text: str) -> dict[str, Any]:
    """Parse citation/dc meta tags from a Chinese journal landing page.

    Magtech and most Chinese journal platforms emit both ``citation_*`` and
    ``dc.*`` tags.  The result uses Crossref's message shape so the existing
    rename/BibTeX code can consume it directly.
    """
    metas = _collect_meta(html_text)
    title = _first_meta(metas, "citation_title", "dc.title", "og:title")
    if not title:
        return {}
    authors: list[str] = []
    for key in ("citation_author", "citation_authors", "dc.creator", "dc.creator.personalname"):
        for value in metas.get(key, []):
            for part in re.split(r"[;；]", value):
                part = normalize_cn_text(part)
                if part and part not in authors:
                    authors.append(part)
    journal = _first_meta(metas, "citation_journal_title", "dc.source", "dc.relation.is")
    date = _first_meta(metas, "citation_publication_date", "citation_date", "dc.date")
    year_match = re.search(r"(19|20)\d{2}", date or "")
    volume = _first_meta(metas, "citation_volume", "dc.citation.volume")
    issue = _first_meta(metas, "citation_issue", "dc.citation.issue")
    first_page = _first_meta(metas, "citation_firstpage", "dc.citation.spage")
    last_page = _first_meta(metas, "citation_lastpage", "dc.citation.epage")
    doi = _first_meta(metas, "citation_doi", "dc.identifier")
    doi = re.sub(r"^doi\s*:\s*", "", doi, flags=re.I)
    abstract = _first_meta(metas, "citation_abstract", "dc.description")
    message: dict[str, Any] = {
        "title": [normalize_cn_text(title)],
        "author": [{"family": name} for name in authors],
        "container-title": [normalize_cn_text(journal)] if journal else [],
        "type": "journal-article",
    }
    if year_match:
        message["published-print"] = {"date-parts": [[int(year_match.group(0))]]}
    if volume:
        message["volume"] = volume
    if issue:
        message["issue"] = issue
    if first_page:
        message["page"] = f"{first_page}-{last_page}" if last_page else first_page
    if doi:
        message["DOI"] = doi
        message["URL"] = f"https://doi.org/{doi}"
    if journal:
        message["publisher"] = normalize_cn_text(journal)
    if abstract:
        message["abstract"] = normalize_cn_text(abstract)
    return message


def _is_aggregator_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(
        host == item or host.endswith("." + item)
        for item in ("cnki.net", "cnki.com.cn", "chndoi.org", "wanfangdata.com.cn",
                     "cqvip.com", "ncpssd.cn", "ncpssd.org")
    )


def journal_metadata_from_url(
    url: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fetch a journal landing page and return citation meta if present."""
    if not url or _is_aggregator_url(url) or urlparse(url).path.lower().endswith(".pdf"):
        return {}
    session = _session(config)
    resp = _get(session, url, config, timeout=20, retries=1)
    if resp is None or resp.status_code >= 400:
        return {}
    return parse_journal_meta(response_text(resp))


def title_from_html(html_text: str) -> str:
    """Best-effort article title from citation meta / og:title / <title>."""
    text = html_text or ""
    for pattern in (
        r'<meta[^>]+name=["\']citation_title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+name=["\']dc.title["\'][^>]+content=["\']([^"\']+)',
    ):
        match = re.search(pattern, text, re.I)
        if match:
            return normalize_cn_text(_WANFANG_TITLE_SUFFIX_RE.sub("", _html.unescape(match.group(1))))
    match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if match:
        raw = _html.unescape(_TAG_RE.sub("", match.group(1)))
        return normalize_cn_text(_WANFANG_TITLE_SUFFIX_RE.sub("", raw))
    return ""


def normalize_title(title: str) -> str:
    """Display-friendly title normalisation (public alias)."""
    return normalize_cn_text(title)
