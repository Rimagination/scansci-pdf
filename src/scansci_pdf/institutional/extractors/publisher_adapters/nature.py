"""HTML extractor for Nature/Springer articles."""

import json
import re

from bs4 import BeautifulSoup


def can_handle(url: str) -> bool:
    """Check if this adapter can handle the given URL."""
    return any(
        domain in url.lower()
        for domain in ["nature.com", "springer.com", "springerlink.com"]
    )


def extract(html: str, url: str = "") -> dict:
    """Extract paper content from Nature/Springer HTML."""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    title = _extract_title(soup)
    authors = _extract_authors(soup)
    abstract = _extract_abstract(soup)
    full_text = _extract_body(soup)
    figures = _extract_figures(soup)
    references = _extract_references(soup)

    return {
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "full_text": full_text,
        "figures": figures,
        "references": references,
    }


def _extract_title(soup: BeautifulSoup) -> str:
    for selector in [
        "h1.c-article-title",
        "h1.article-item__title",
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
    """Extract authors using three-layer pipeline: meta → JSON-LD → DOM."""
    # Layer 1: meta tags (most reliable)
    authors = []
    for meta in soup.select("meta[name='citation_author']"):
        name = meta.get("content", "").strip()
        if name:
            authors.append(name)
    if authors:
        return authors

    # Layer 2: JSON-LD
    authors = _authors_from_jsonld(soup)
    if authors:
        return authors

    # Layer 3: DOM selectors
    for el in soup.select("li.c-article-author-list__item a"):
        name = el.get_text(strip=True)
        if name:
            authors.append(name)
    if not authors:
        for el in soup.select("[data-test='author-name']"):
            name = el.get_text(strip=True)
            if name:
                authors.append(name)
    return authors


def _authors_from_jsonld(soup: BeautifulSoup) -> list[str]:
    """Extract authors from JSON-LD structured data."""
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        # Handle both single object and list
        items = data if isinstance(data, list) else [data]
        for item in items:
            author = item.get("author", [])
            if isinstance(author, dict):
                author = [author]
            names = []
            for a in author:
                if isinstance(a, dict):
                    name = a.get("name", "")
                    if name:
                        names.append(name)
            if names:
                return names
    return []


def _extract_abstract(soup: BeautifulSoup) -> str:
    for selector in [
        "#Abs1-content",
        "div.c-article-section__content[id*='Abs']",
        "#abstract",
        "section[data-title='Abstract']",
        "div.article__body p.article__teaser",
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

    # Nature uses section[data-title] for article sections
    sections = soup.select("section[data-title]")
    if sections:
        for section in sections:
            title = section.get("data-title", "")
            if title.lower() in ("abstract", "references", "supplementary information", "data availability"):
                continue
            content_div = section.select_one(".c-article-section__content")
            if content_div:
                # Extract text and tables
                text_parts = []
                for child in content_div.children:
                    if child.name == "table":
                        table_text = _extract_table(child)
                        if table_text:
                            text_parts.append(table_text)
                    elif child.name:
                        text = _clean(child.get_text())
                        if text:
                            text_parts.append(text)
                content = "\n\n".join(text_parts)
                if content:
                    parts.append(f"## {title}\n\n{content}")

    # Fallback: try article body div
    if not parts:
        body = soup.select_one("div.c-article-body") or soup.select_one("div.article__body")
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
    ref_section = soup.select_one("#Bib1") or soup.select_one("#references")
    if ref_section:
        for li in ref_section.select("li.c-article-references__item"):
            text = _clean(li.get_text())
            if text:
                refs.append(text)
        if not refs:
            for li in ref_section.find_all("li"):
                text = _clean(li.get_text())
                if text and len(text) > 20:
                    refs.append(text)
    return refs


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
