"""Data models for institutional paper fetching."""

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Paper:
    """Represents a fetched academic paper."""

    doi: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    journal: str = ""
    year: int | None = None
    abstract: str = ""
    full_text: str = ""
    figures: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    source: str = ""
    pdf_path: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_markdown(self, include_pdf_path: bool = False) -> str:
        lines = []
        lines.append(f"# {self.title or 'Untitled'}")
        lines.append("")
        if self.authors:
            lines.append(f"**Authors:** {', '.join(self.authors)}")
        if self.journal:
            lines.append(f"**Journal:** {self.journal}")
        if self.year:
            lines.append(f"**Year:** {self.year}")
        if self.doi:
            lines.append(f"**DOI:** {self.doi}")
        if self.source:
            lines.append(f"**Source:** {self.source}")
        if include_pdf_path and self.pdf_path:
            lines.append(f"**PDF saved to:** {self.pdf_path}")
        lines.append("")

        if self.abstract:
            lines.append("## Abstract")
            lines.append("")
            lines.append(self.abstract)
            lines.append("")

        if self.full_text:
            lines.append("## Full Text")
            lines.append("")
            lines.append(self.full_text)
            lines.append("")

        if self.figures:
            lines.append("## Figures")
            lines.append("")
            for i, fig in enumerate(self.figures, 1):
                lines.append(f"**Figure {i}:** {fig}")
            lines.append("")

        if self.references:
            lines.append("## References")
            lines.append("")
            for ref in self.references:
                lines.append(f"- {ref}")
            lines.append("")

        return "\n".join(lines)

    def to_text(self) -> str:
        parts = []
        if self.title:
            parts.append(self.title)
            parts.append("")
        if self.abstract:
            parts.append(self.abstract)
            parts.append("")
        if self.full_text:
            parts.append(self.full_text)
        return "\n".join(parts)

    @classmethod
    def from_json(cls, data: str | dict) -> "Paper":
        if isinstance(data, str):
            data = json.loads(data)
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class NextAction:
    """Actionable next step for users or agents."""

    kind: str
    message: str
    command: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class FetchResult:
    """Structured fetch outcome for agent-facing workflows."""

    status: str
    quality: str
    paper: Paper = field(default_factory=Paper)
    reason: str = ""
    next_action: NextAction | None = None
    attempts: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def from_paper(
        cls,
        paper: Paper,
        *,
        min_fulltext_len: int = 1000,
        institution_configured: bool = False,
        identifier: str = "",
    ) -> "FetchResult":
        full_text_len = len(paper.full_text or "")
        if full_text_len >= min_fulltext_len:
            return cls(status="success", quality="full_text", paper=paper)

        quality = _content_quality(paper)
        if quality == "pdf_only":
            return cls(
                status="blocked",
                quality="pdf_only",
                paper=paper,
                reason="pdf_extraction_failed",
                next_action=_inspect_pdf_action(paper.pdf_path),
            )

        if quality == "none" and not institution_configured:
            return cls(
                status="config_needed",
                quality="none",
                paper=paper,
                reason="institution_not_configured",
                next_action=_configure_institution_action(),
            )

        if quality == "none":
            return cls(
                status="auth_required",
                quality="none",
                paper=paper,
                reason="institution_login_required",
                next_action=_login_action(identifier or paper.doi or paper.url),
            )

        next_action = (
            _login_action(identifier or paper.doi or paper.url)
            if institution_configured
            else _configure_institution_action()
        )
        return cls(
            status="partial",
            quality=quality,
            paper=paper,
            reason="insufficient_full_text",
            next_action=next_action,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "quality": self.quality,
            "reason": self.reason,
            "paper": self.paper.to_dict(),
            "next_action": self.next_action.to_dict() if self.next_action else None,
            "attempts": self.attempts,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    def to_markdown(self, include_pdf_path: bool = False) -> str:
        lines = [
            f"**Status:** {self.status}",
            f"**Quality:** {self.quality}",
        ]
        if self.reason:
            lines.append(f"**Reason:** {self.reason}")
        if self.next_action:
            lines.append(f"**Next action:** {self.next_action.message}")
            if self.next_action.command:
                lines.append(f"**Command:** `{self.next_action.command}`")
        if self.attempts:
            lines.append("**Attempts:**")
            for attempt in self.attempts:
                item = f"- {attempt.get('stage', 'unknown')}: {attempt.get('status', 'unknown')}"
                if attempt.get("reason"):
                    item += f" ({attempt['reason']})"
                if attempt.get("detail"):
                    item += f" - {attempt['detail']}"
                lines.append(item)
        lines.append("")
        lines.append(self.paper.to_markdown(include_pdf_path=include_pdf_path))
        return "\n".join(lines)

    def to_text(self) -> str:
        text = self.paper.to_text()
        if self.status == "success":
            return text
        parts = [text] if text else []
        if self.next_action:
            parts.append(f"Next action: {self.next_action.message}")
            if self.next_action.command:
                parts.append(f"Command: {self.next_action.command}")
        return "\n\n".join(parts)


def _content_quality(paper: Paper) -> str:
    if paper.full_text:
        return "short_text"
    if paper.pdf_path:
        return "pdf_only"
    if paper.abstract:
        return "abstract_only"
    if paper.title or paper.authors or paper.journal or paper.year:
        return "metadata_only"
    return "none"


def _configure_institution_action() -> NextAction:
    return NextAction(
        kind="configure_institution",
        command="scansci-pdf setup --school YOUR_SCHOOL",
        message="Configure your school or institution before retrying institutional access.",
    )


def _login_action(identifier: str = "") -> NextAction:
    command = "scansci-pdf login --force"
    if identifier:
        command = f"{command}  # then retry {identifier}"
    return NextAction(
        kind="login",
        command=command,
        message="Complete or refresh institutional login, then retry the paper.",
    )


def _inspect_pdf_action(pdf_path: str) -> NextAction:
    return NextAction(
        kind="inspect_pdf",
        message=(
            f"PDF was saved to {pdf_path}, but no usable full text was extracted. "
            "Inspect the PDF or retry with another source/version."
        ),
    )
