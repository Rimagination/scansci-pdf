"""Generic HTML extractor - fallback for unknown publishers."""

import re

from bs4 import BeautifulSoup, Tag


def extract(html: str, url: str = "") -> dict:
    """Extract paper content from generic HTML.

    Args:
        html: Raw HTML content.
        url: The URL the HTML was fetched from.

    Returns:
        Dict with title, abstract, full_text, figures, references.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove script, style, nav, header, footer elements
    for tag in soup.find_all(["script", "style", "nav", "header", "footer", "aside"]):
        tag.decompose()

    title = _extract_title(soup)
    abstract = _extract_abstract(soup)
    full_text = _extract_body(soup)
    figures = _extract_figures(soup)
    references = _extract_references(soup)
    authors = _extract_authors(soup)

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "full_text": full_text,
        "figures": figures,
        "references": references,
    }


def _extract_title(soup: BeautifulSoup) -> str:
    """Extract article title."""
    # Try common title selectors
    for selector in [
        "h1.article-title",
        "h1.c-article-title",
        "h1#title",
        ".article-header h1",
        "article h1",
        "h1",
    ]:
        el = soup.select_one(selector)
        if el and el.get_text(strip=True):
            return el.get_text(strip=True)

    # Fallback to <title> tag
    title_tag = soup.find("title")
    if title_tag:
        return title_tag.get_text(strip=True)
    return ""


def _extract_abstract(soup: BeautifulSoup) -> str:
    """Extract abstract."""
    for selector in [
        "#abstract",
        ".abstract",
        "[data-title='Abstract']",
        "section.abstract",
        "div.abstractSection",
    ]:
        el = soup.select_one(selector)
        if el:
            return _clean_text(el.get_text())

    # Try meta tag
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        return meta["content"].strip()

    return ""


def _extract_body(soup: BeautifulSoup) -> str:
    """Extract main body text."""
    # Try common article body selectors
    for selector in [
        "article",
        "[role='main']",
        "main",
        ".article-body",
        ".article-content",
        "#body",
        ".body",
    ]:
        el = soup.select_one(selector)
        if el:
            text = _clean_text(el.get_text())
            if len(text) > 500:  # Sanity check - body should be substantial
                return text

    # Fallback: find the largest text block
    best = ""
    for tag in soup.find_all(["div", "section"]):
        text = _clean_text(tag.get_text())
        if len(text) > len(best):
            best = text

    return best


def _extract_figures(soup: BeautifulSoup) -> list[str]:
    """Extract figure captions."""
    captions = []
    for fig in soup.find_all("figure"):
        cap = fig.find("figcaption")
        if cap:
            text = _clean_text(cap.get_text())
            if text:
                captions.append(text)

    # Also try common caption classes
    if not captions:
        for selector in [".figure-caption", ".caption", ".fig-caption"]:
            for el in soup.select(selector):
                text = _clean_text(el.get_text())
                if text and len(text) > 10:
                    captions.append(text)

    return captions


def _extract_references(soup: BeautifulSoup) -> list[str]:
    """Extract reference list."""
    refs = []

    # Try common reference section selectors
    ref_section = None
    for selector in [
        "#references",
        ".references",
        "#bibliography",
        "[data-title='References']",
        "section.ref-list",
    ]:
        ref_section = soup.select_one(selector)
        if ref_section:
            break

    if ref_section:
        for li in ref_section.find_all("li"):
            text = _clean_text(li.get_text())
            if text and len(text) > 20:
                refs.append(text)

        # If no <li>, try <p> or <div> items
        if not refs:
            for el in ref_section.find_all(["p", "div"], class_=re.compile(r"ref|citation")):
                text = _clean_text(el.get_text())
                if text and len(text) > 20:
                    refs.append(text)

    return refs


def _extract_authors(soup: BeautifulSoup) -> list[str]:
    """Extract author names."""
    authors = []
    for selector in [
        "meta[name='citation_author']",
        "meta[name='dc.creator']",
    ]:
        for meta in soup.select(selector):
            name = meta.get("content", "").strip()
            if name:
                authors.append(name)

    if not authors:
        for selector in [".author-name", ".authors a", ".contrib-author"]:
            for el in soup.select(selector):
                name = el.get_text(strip=True)
                if name:
                    authors.append(name)

    return authors


def _clean_text(text: str) -> str:
    """Clean extracted text."""
    text = re.sub(r"\s+", " ", text)
    return text.strip()
