"""PDF auto-rename based on metadata (AuthorYear_Title format)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .chinese import has_cjk, normalize_cn_text
from .log import get_logger

log = get_logger()

_CN_TITLE_STOP_PREFIXES = ("论", "关于", "基于", "面向", "我国", "中国")
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_component(value: str) -> str:
    value = _INVALID_FILENAME_CHARS.sub("", value)
    return value.strip(" .")


def generate_filename(metadata: dict[str, Any], max_len: int = 80) -> str:
    """Generate a clean filename from paper metadata.

    Format: LastNameYear_FirstWordOfTitle.pdf
    Example: Smith2023_CRISPR.pdf, 杨国荣2025_理解人工智能.pdf
    """
    # Extract year
    year = ""
    for key in ("year", "published-print", "published-online", "created"):
        val = metadata.get(key)
        if isinstance(val, str) and val.isdigit():
            year = val
            break
        elif isinstance(val, dict):
            parts = val.get("date-parts", [[]])
            if parts and parts[0]:
                year = str(parts[0][0])
                break
        elif isinstance(val, (int, float)):
            year = str(int(val))
            break

    # Extract first author last name
    authors = metadata.get("author", metadata.get("authors", []))
    first_author = ""
    if authors and isinstance(authors, list):
        a = authors[0]
        if isinstance(a, dict):
            first_author = a.get("family", a.get("given", a.get("name", "")))
        elif isinstance(a, str):
            # "Last, First" or "First Last"
            parts = a.split(",")
            first_author = parts[0].strip().split()[-1] if parts else a

    # Extract first meaningful word from title
    titles = metadata.get("title", metadata.get("titles", []))
    title_word = ""
    if titles:
        title = titles[0] if isinstance(titles, list) else titles
        if isinstance(title, str):
            title = normalize_cn_text(title)
            if has_cjk(title):
                # Chinese titles have no spaces: use the leading CJK run and
                # drop a common discourse prefix ("论"/"基于"/"关于"...) so
                # "论人工智能报关" → 人工智能报关.
                cjk = "".join(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", title))
                for prefix in _CN_TITLE_STOP_PREFIXES:
                    if cjk.startswith(prefix) and len(cjk) - len(prefix) >= 4:
                        cjk = cjk[len(prefix):]
                        break
                title_word = cjk[:12]
            else:
                # Skip common stop words
                stop_words = {"a", "an", "the", "on", "in", "of", "for", "to", "and", "with", "from"}
                words = re.findall(r"[A-Za-z]+", title)
                for w in words:
                    if w.lower() not in stop_words and len(w) > 2:
                        title_word = w.capitalize()
                        break

    # Clean components
    if has_cjk(first_author):
        author_tokens = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]+", first_author)
        first_author = author_tokens[0][:6] if author_tokens else ""
    else:
        first_author = re.sub(r"[^A-Za-z]", "", first_author)[:20]
    year = re.sub(r"[^0-9]", "", year)[:4]
    first_author = _sanitize_component(first_author)
    title_word = _sanitize_component(title_word)[:20]

    # Build filename: LastNameYear_TitleWord
    parts = []
    if first_author:
        parts.append(first_author)
    if year:
        parts.append(year)
    if title_word:
        parts.append(title_word)

    if not parts:
        return ""

    # Join first two parts directly (AuthorYear), then underscore with title
    if len(parts) >= 3:
        name = f"{parts[0]}{parts[1]}_{parts[2]}"
    elif len(parts) == 2:
        name = f"{parts[0]}{parts[1]}"
    else:
        name = parts[0]
    # Truncate if too long
    if len(name) > max_len:
        name = name[:max_len]
    return _sanitize_component(name)


def rename_pdf(
    file_path: Path,
    metadata: dict[str, Any],
    target_dir: Path | None = None,
) -> Path | None:
    """Rename a downloaded PDF based on metadata. Returns new path or None if failed."""
    if not file_path.exists():
        return None

    new_name = generate_filename(metadata)
    if not new_name:
        return None

    new_name = f"{new_name}.pdf"
    target = (target_dir or file_path.parent) / new_name

    # Avoid overwriting existing files
    if target.exists() and target != file_path:
        # Reuse the existing file only when the bytes match. Size alone is not
        # identity: two different papers from the same journal template can be
        # within 1 KB of each other, and the freshly downloaded file used to be
        # deleted on that basis, leaving the wrong paper in place with no sign
        # that anything happened (#70). Equal sizes are the cheap filter before
        # hashing.
        try:
            identical = False
            if target.stat().st_size == file_path.stat().st_size:
                # Both handles must be closed before the unlink below: Windows
                # refuses to delete a file that is still open, which would send
                # this down the rename branch instead of reusing the file.
                with target.open("rb") as existing, file_path.open("rb") as fresh:
                    identical = (
                        hashlib.file_digest(existing, "sha256").digest()
                        == hashlib.file_digest(fresh, "sha256").digest()
                    )
            if identical:
                file_path.unlink(missing_ok=True)
                return target
        except OSError:
            pass
        counter = 1
        stem = target.stem
        while target.exists():
            target = target.parent / f"{stem}_{counter}.pdf"
            counter += 1

    try:
        if target != file_path:
            file_path.rename(target)
            log.info(f"Renamed: {file_path.name} -> {target.name}")
        return target
    except OSError as e:
        log.warning(f"Rename failed: {e}")
        return None
