"""HTML extractor for Elsevier/ScienceDirect articles."""

import re

from bs4 import BeautifulSoup


def can_handle(url: str) -> bool:
    """Check if this adapter can handle the given URL."""
    return any(
        domain in url.lower()
        for domain in ["sciencedirect.com", "elsevier.com"]
    )


def extract(html: str, url: str = "") -> dict:
    """Extract paper content from Elsevier/ScienceDirect HTML."""
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
        "span.title-text",
        "h1.article-header__title",
        "meta[name='citation_title']",
        "meta[property='og:title']",
        "meta[name='dc.title']",
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
    # Meta tags (most reliable for ScienceDirect)
    for meta in soup.select("meta[name='citation_author']"):
        name = meta.get("content", "").strip()
        if name:
            authors.append(name)
    if not authors:
        for meta in soup.select("meta[name='dc.creator']"):
            name = meta.get("content", "").strip()
            if name:
                authors.append(name)
    if authors:
        return authors

    # DOM selectors
    for selector in [
        ".author-group .author span.content",
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
        "div.abstract",
        "#abstracts",
        "div.Abstracts",
        "section#abstract",
        "meta[name='description']",
        "meta[property='og:description']",
    ]:
        el = soup.select_one(selector)
        if el:
            if el.name == "meta":
                return el.get("content", "").strip()
            return _clean(el.get_text())
    return ""


def _extract_body(soup: BeautifulSoup) -> str:
    parts = []

    # ScienceDirect article body
    body = soup.select_one("div#body") or soup.select_one("div.Body")
    if body:
        for section in body.find_all("section", recursive=False):
            heading = section.find(re.compile(r"h[2-4]"))
            heading_text = heading.get_text(strip=True) if heading else ""
            level = int(heading.name[1]) if heading else 2

            if heading_text.lower() in ("abstract", "references", "supplementary material"):
                continue

            # Extract text with tables
            text_parts = []
            for child in section.children:
                if child.name:
                    if child.name == "table":
                        table_text = _extract_table(child)
                        if table_text:
                            text_parts.append(table_text)
                    elif not child.find("section"):  # Skip nested sections
                        text = _clean(child.get_text())
                        if text:
                            text_parts.append(text)

            content = "\n\n".join(text_parts)
            if heading_text and content:
                prefix = "#" * min(level, 4)
                parts.append(f"{prefix} {heading_text}\n\n{content}")
            elif content:
                parts.append(content)

    if not parts and body:
        parts.append(_clean(body.get_text()))

    # Fallback
    if not parts:
        article = soup.select_one("article") or soup.select_one("#main-content")
        if article:
            parts.append(_clean(article.get_text()))

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
    for fig in soup.select("figure, .figure"):
        cap = fig.select_one("figcaption, .caption")
        if cap:
            text = _clean(cap.get_text())
            if text and len(text) > 10:
                captions.append(text)
    return captions


def _extract_references(soup: BeautifulSoup) -> list[str]:
    refs = []
    ref_section = soup.select_one("#bibliography") or soup.select_one("section.bibliography")
    if ref_section:
        for li in ref_section.find_all("li"):
            text = _clean(li.get_text())
            if text and len(text) > 20:
                refs.append(text)
    return refs


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
