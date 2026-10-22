# cython: language_level=3
"""WebVPN AES URL encryption and publisher PDF heuristics.

This module implements WebVPN institutional proxy access:
- AES-CFB-128 hostname encryption with per-school keys
- Multi-publisher PDF URL construction heuristics
- HTML PDF link extraction with publisher-specific patterns
"""

import binascii
import re
import urllib.parse
from typing import Any


def convert_url(str url, str webvpn_base, bytes key, bytes iv) -> str:
    """Convert a regular URL to a WebVPN URL using AES-CFB encryption.

    Encrypts only the hostname; path and query are kept as-is.
    Uses per-school encryption keys.
    """
    from Crypto.Cipher import AES

    parsed = urllib.parse.urlparse(url)
    cdef str scheme = parsed.scheme.lower()
    cdef str hostname = parsed.hostname or ""
    cdef int port = parsed.port or 0
    cdef str path = parsed.path
    cdef str query = parsed.query

    if not hostname:
        return url

    cipher = AES.new(key, AES.MODE_CFB, iv, segment_size=128)
    cdef bytes encrypted = cipher.encrypt(hostname.encode("utf-8"))
    cdef str encrypted_hex = binascii.hexlify(iv).decode() + binascii.hexlify(encrypted).decode()

    cdef str scheme_part = scheme
    if port:
        scheme_part = f"{scheme}-{port}"

    cdef str result = f"{webvpn_base.rstrip('/')}/{scheme_part}/{encrypted_hex}{path}"
    if query:
        result += f"?{query}"
    return result


def construct_publisher_pdf_url(str doi, str resolved_url) -> str | None:
    """Try to construct a direct publisher PDF URL from the resolved URL.

    Covers: ACS, Wiley, Taylor & Francis, Nature, Springer, RSC, Elsevier.
    Returns None if no known pattern matches.
    """
    cdef str pdf_url

    parsed = urllib.parse.urlparse(resolved_url)
    cdef str hostname = parsed.netloc.lower()
    cdef str doi_suffix = doi.rsplit("/", 1)[1] if "/" in doi else doi

    if "pubs.acs.org" in hostname:
        return f"https://pubs.acs.org/doi/pdf/{doi}"
    elif "onlinelibrary.wiley.com" in hostname:
        return f"https://onlinelibrary.wiley.com/doi/pdfdirect/{doi}"
    elif "tandfonline.com" in hostname:
        return f"https://www.tandfonline.com/doi/pdf/{doi}?needAccess=true"
    elif "nature.com" in hostname:
        return f"https://www.nature.com/articles/{doi_suffix}.pdf"
    elif "link.springer.com" in hostname:
        return f"https://link.springer.com/content/pdf/{doi}.pdf"
    elif "pubs.rsc.org" in hostname:
        pdf_url = resolved_url.replace("/articlelanding/", "/articlepdf/")
        return pdf_url if pdf_url != resolved_url else None
    elif "pnas.org" in hostname:
        return f"https://www.pnas.org/doi/pdf/{doi}"
    elif "science.org" in hostname or "sciencemag.org" in hostname:
        return f"https://www.science.org/doi/pdf/{doi}"
    elif "elsevier.com" in hostname or "sciencedirect.com" in hostname:
        pii_match = re.search(r"pii/([A-Z0-9]+)", resolved_url)
        if pii_match:
            return f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}/pdfft"

    return None


def find_pdf_link_in_html(str html, str base_url) -> str | None:
    """Find a PDF download link in an HTML page.

    Strategy 1: citation_pdf_url meta tag
    Strategy 2: <a> tags with PDF text/class/href
    Strategy 3: Publisher-specific URL patterns
    """
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    parsed = urllib.parse.urlparse(base_url)
    cdef str base = f"{parsed.scheme}://{parsed.netloc}"
    cdef str hostname = parsed.netloc.lower()

    # Strategy 1: <meta name="citation_pdf_url">
    meta_pdf = soup.find("meta", attrs={"name": "citation_pdf_url"})
    if meta_pdf and meta_pdf.get("content"):
        pdf_url = meta_pdf["content"]
        if pdf_url.startswith("http"):
            return pdf_url
        return base + pdf_url

    # Strategy 2: <a> tags with PDF-related text/class/href
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True).lower()
        classes = " ".join(a.get("class", []))

        if any(kw in text for kw in ["pdf", "download pdf", "full text pdf", "view pdf", "get pdf"]):
            return _resolve_href(href, base)
        if any(kw in classes for kw in ["pdf", "download-pdf", "pdf-download", "article-pdf"]):
            return _resolve_href(href, base)
        if href.endswith(".pdf"):
            return _resolve_href(href, base)
        if "/doi/pdf/" in href or "/doi/pdfdirect/" in href:
            return _resolve_href(href, base)

    # Strategy 3: Publisher-specific URL patterns
    cdef str path = parsed.path
    cdef int idx

    idx = path.rfind("/doi/")
    if "pubs.acs.org" in hostname and idx >= 0 and "/pdf/" not in path:
        doi_part = path[idx + 5:]
        if doi_part:
            return f"{base}/doi/pdf/{doi_part}"

    if "onlinelibrary.wiley.com" in hostname and idx >= 0 and "/pdfdirect/" not in path:
        doi_part = path[idx + 5:]
        if doi_part:
            return f"{base}/doi/pdfdirect/{doi_part}"

    if "pubs.rsc.org" in hostname and "/articlelanding/" in path:
        return base_url.replace("/articlelanding/", "/articlepdf/")

    if "tandfonline.com" in hostname and "/doi/" in path and "/pdf/" not in path:
        doi_part = re.sub(r"/doi/(?:full|abs)/", "/doi/pdf/", path)
        if doi_part != path:
            return f"{base}{doi_part}"

    if "pnas.org" in hostname and idx >= 0 and "/pdf/" not in path:
        doi_part = path[idx + 5:]
        if doi_part:
            return f"{base}/doi/pdf/{doi_part}"

    if "science.org" in hostname and idx >= 0 and "/pdf/" not in path:
        doi_part = path[idx + 5:]
        if doi_part:
            return f"{base}/doi/pdf/{doi_part}"

    if ("elsevier.com" in hostname or "sciencedirect.com" in hostname):
        pii_match = re.search(r"pii/([A-Z0-9]+)", path)
        if pii_match:
            return f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}/pdfft"

    return None


cdef str _resolve_href(str href, str base):
    """Resolve a relative href to an absolute URL."""
    if href.startswith("http"):
        return href
    if href.startswith("//"):
        return "https:" + href
    if href.startswith("/"):
        return base + href
    return base + "/" + href


# --- Data encryption for webvpn.json protection ---

# This key is embedded in the compiled .pyd, invisible to pure Python users.
_DATA_KEY = bytes.fromhex("9cad336032fda4cec3eb0d39740cf8cef9f1a9a65475c0b28c04642ab5c1323a")
_DATA_IV = b"scansci_pdf_v1_dat"[:16].ljust(16, b'\0')


def encrypt_data(bytes plaintext) -> bytes:
    """Encrypt data with AES-CBC for webvpn.dat. Used during build only."""
    from Crypto.Cipher import AES
    # PKCS7 padding
    cdef int pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len] * pad_len)
    cipher = AES.new(_DATA_KEY, AES.MODE_CBC, _DATA_IV)
    return cipher.encrypt(padded)


def decrypt_data(bytes encrypted) -> str:
    """Decrypt webvpn.dat data with AES-CBC."""
    from Crypto.Cipher import AES
    cipher = AES.new(_DATA_KEY, AES.MODE_CBC, _DATA_IV)
    cdef bytes decrypted = cipher.decrypt(encrypted)
    # Remove PKCS7 padding
    cdef int data_len = len(decrypted)
    if data_len == 0:
        raise ValueError("Invalid encrypted data")
    cdef int pad_len = decrypted[data_len - 1]
    if pad_len < 1 or pad_len > 16:
        raise ValueError("Invalid encrypted data")
    return decrypted[:data_len - pad_len].decode("utf-8")
