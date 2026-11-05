"""Browser backend set and removal guard."""

from __future__ import annotations

from pathlib import Path

from scansci_pdf import browser_backend as bb

IGNORED = {
    ".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache",
    "runs", "_browsers",
}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".json", ".yml", ".yaml", ".txt"}


def test_backend_set_is_exact():
    assert bb.BACKENDS == ("patchright", "camoufox", "ego-lite")
    assert bb.DEFAULT_BACKEND == "patchright"
    assert not hasattr(bb, "BACKEND_" + "CLOAK" + "BROWSER")


def test_unknown_backend_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(bb, "is_available", lambda name=None, config=None: True)

    assert bb.resolve_backend({"browser_backend": "not-a-browser"}) == "patchright"


def test_camoufox_missing_falls_back_to_patchright(monkeypatch):
    monkeypatch.setattr(
        bb, "is_available", lambda name=None, config=None: name != "camoufox"
    )

    assert bb.resolve_backend({"browser_backend": "camoufox"}) == "patchright"


def test_ego_lite_is_explicit_only(monkeypatch):
    monkeypatch.setattr(bb, "is_available", lambda name=None, config=None: False)

    # Explicit selection must not silently become a different engine; launch
    # raises a setup message instead.
    assert bb.resolve_backend({"browser_backend": "ego-lite"}) == "ego-lite"


def test_repo_has_no_removed_backend_references():
    root = Path(__file__).resolve().parents[1]
    needle = "cloak" + "browser"
    offenders: list[str] = []
    for path in root.rglob("*"):
        if any(part in IGNORED for part in path.parts):
            continue
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if needle in text:
            offenders.append(str(path.relative_to(root)))

    assert offenders == []
