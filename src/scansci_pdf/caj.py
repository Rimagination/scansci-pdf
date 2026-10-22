"""CAJ/KDH/NH/TEB handling helpers.

CNKI often serves Chinese theses/journal articles as CAJ (or KDH/NH/TEB)
rather than PDF.  A CAJ file is a valid full-text artifact but cannot be
opened by ordinary PDF readers and cannot be parsed by PyMuPDF.  Rather than
silently renaming it to ``.pdf`` (which produces a corrupt-looking file), the
downloader keeps the original extension and returns an explicit conversion
hint.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

_CAJ_MAGIC = (b"CAJ", b"KDH", b"NH", b"TEB")
_CAJ_SUFFIXES = {".caj", ".kdh", ".nh", ".teb"}


def is_caj_bytes(head: bytes) -> bool:
    """Whether the first bytes look like a CAJ-family container."""
    if not head:
        return False
    return any(head.startswith(magic) for magic in _CAJ_MAGIC)


def is_caj_file(path: str | Path) -> bool:
    file_path = Path(path)
    if file_path.suffix.lower() in _CAJ_SUFFIXES:
        return True
    try:
        with file_path.open("rb") as handle:
            return is_caj_bytes(handle.read(8))
    except OSError:
        return False


def guess_format(head: bytes, content_type: str = "", filename: str = "") -> str:
    """Classify the first bytes of a download: ``pdf``, ``caj``, or ``other``."""
    if head.startswith(b"%PDF-"):
        return "pdf"
    if is_caj_bytes(head):
        return "caj"
    lowered = (content_type or "").lower()
    suffix = Path(filename).suffix.lower() if filename else ""
    if "pdf" in lowered or suffix == ".pdf":
        return "pdf"
    if "caj" in lowered or suffix in _CAJ_SUFFIXES:
        return "caj"
    return "other"


def caj_hints(path: str | Path | None = None) -> dict[str, Any]:
    """Actionable guidance for a CAJ-family file (Chinese output for users)."""
    hints: dict[str, Any] = {
        "format": "caj",
        "reason": "该文件是 CAJ/KDH 格式（知网专用电子书格式），不是 PDF",
        "how_to_open": [
            "方式一：用 CAJViewer 打开（Windows，知网官方免费）",
            "方式二：转换为 PDF（CAJViewer 中“另存为 PDF”，或开源工具 caj2pdf）",
            "方式三：如果知网详情页同时有“PDF下载”按钮，优先重新下载 PDF",
        ],
        "cajviewer_url": "https://cajviewer.cnki.net/",
        "caj2pdf_url": "https://github.com/caj2pdf/caj2pdf",
    }
    if path:
        hints["file"] = str(path)
        hints["agent_hint"] = (
            f"已下载到 CAJ 文件：{path}。请提示用户用 CAJViewer 打开，或使用 caj2pdf "
            "转换为 PDF。不要把这个文件当作 PDF 解析；重试下载时可优先选择 PDF 按钮。"
        )
    else:
        hints["agent_hint"] = (
            "知网仅返回 CAJ 格式。请提示用户用 CAJViewer 打开或转换为 PDF；"
            "若详情页有 PDF 下载按钮，优先重试 PDF。"
        )
    return hints


def try_convert_caj(path: str | Path, output_pdf: str | Path | None = None) -> Path | None:
    """Convert CAJ→PDF if an external converter is installed.

    Supports the open-source ``caj2pdf`` command line.  No Python dependency
    is added: conversion is opportunistic and returns ``None`` when the tool
    is missing or fails.
    """
    source = Path(path)
    if not source.exists() or not is_caj_file(source):
        return None
    target = Path(output_pdf) if output_pdf else source.with_suffix(".pdf")
    converter = shutil.which("caj2pdf")
    if not converter:
        return None
    try:
        completed = subprocess.run(
            [converter, "convert", str(source), "-o", str(target)],
            capture_output=True,
            timeout=180,
            check=False,
        )
        if completed.returncode == 0 and target.exists() and target.stat().st_size > 1000:
            return target
    except (OSError, subprocess.SubprocessError):
        return None
    return None
