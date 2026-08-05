"""HTML extractor for Wiley Online Library articles."""

import re

from bs4 import BeautifulSoup


def can_handle(url: str) -> bool:
    """Check if this adapter can handle the given URL."""
    return "wiley.com" in url.lower() or "onlinelibrary.wiley" in url.lower()


def extract(html: str, url: str = "") -> dict:
    """Extract paper content from Wiley Online Library HTML."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    return {
        "title": _extract_title(soup),
        "authors": _extract_authors(soup),
        "abstract": _extract_abstract(soup),
        "full_text": _extract_body(soup),
        "figures": _extract_figures(soup),
        "references": _extract_references(soup),
    }


def _extract_title(soup: BeautifulSoup) -> str:
    for selector in [
        "h1.citation__title",
        ".article-header__title",
        "meta[name='citation_title']",
        "meta[property='og:title']",
    ]:
        el = soup.select_one(selector)
        if el:
            if el.name == "meta":
                return el.get("content", "").strip()
            return el.get_text(strip=True)
    return ""


def _extract_authors(soup: BeautifulSoup) -> list[str]:
    """Extract authors using meta tags first, then DOM."""
    authors = []
    # Meta tags (most reliable)
    for meta in soup.select("meta[name='citation_author']"):
        name = meta.get("content", "").strip()
        if name:
            authors.append(name)
    if authors:
        return authors

    # DOM: multiple possible selectors
    for selector in [
        ".loa-authors-trunc a.author-name",
        ".loa-authors .author-name span",
        "a.author-name",
        "[data-test='author-name']",
    ]:
        for el in soup.select(selector):
            name = el.get_text(strip=True)
            if name:
                authors.append(name)
        if authors:
            return authors
    return authors


def _extract_abstract(soup: BeautifulSoup) -> str:
    for selector in [
        "section.article-section__abstract",
        "div.abstract-group",
        "#abstract",
        "meta[name='description']",
    ]:
        el = soup.select_one(selector)
        if el:
            if el.name == "meta":
                return el.get("content", "").strip()
            return _clean(el.get_text())
    return ""


def _extract_body(soup: BeautifulSoup) -> str:
    parts = []

    # Wiley uses section.article-section__content divs
    for section in soup.select("section.article-section__content"):
        heading = section.find_previous("h2")
        heading_text = heading.get_text(strip=True) if heading else ""

        if heading_text.lower() in ("abstract", "references", "supporting information", "data availability"):
            continue

        # Extract text with table support
        text_parts = []
        for child in section.children:
            if child.name == "table":
                table_text = _extract_table(child)
                if table_text:
                    text_parts.append(table_text)
            elif child.name:
                text = _clean(child.get_text())
                if text:
                    text_parts.append(text)
        content = "\n\n".join(text_parts)

        if heading_text and content:
            parts.append(f"## {heading_text}\n\n{content}")
        elif content:
            parts.append(content)

    # Fallback
    if not parts:
        body = soup.select_one("article.article__body") or soup.select_one(".article-body-section")
        if body:
            parts.append(_clean(body.get_text()))

    return "\n\n".join(parts)


def _extract_table(table) -> str:
    """Extract table content as structured text."""
    rows = []
    for tr in table.find_all("tr"):
        cells = []
        for td in tr.find_all(["td", "th"]):
            cells.append(_clean(td.get_text()))
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _extract_figures(soup: BeautifulSoup) -> list[str]:
    captions = []
    for fig in soup.select("figure"):
        cap = fig.select_one("figcaption")
        if cap:
            text = _clean(cap.get_text())
            if text and len(text) > 10:
                captions.append(text)
    return captions


def _extract_references(soup: BeautifulSoup) -> list[str]:
    refs = []
    ref_section = soup.select_one("section#references-section")
    if ref_section:
        for li in ref_section.find_all("li"):
            text = _clean(li.get_text())
            if text and len(text) > 20:
                refs.append(text)
    return refs


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
