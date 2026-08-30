"""Lock the repo checkout and the installed editable checkout in byte-sync.

The package is edited in the repo working tree and installed editable from a
second checkout; edits have historically landed in one and not the other,
which is how stale-code incidents happen (a test exercising old code while
the repo holds the fix). The true installed location is discovered in a
subprocess with a neutral cwd because pytest injects the repo's ``src`` into
``sys.path``, which would make an in-process import resolve to the repo
itself. This test walks the repo's ``src/scansci_pdf`` tree and fails when
any file diverges from the installed copy.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "scansci_pdf"


def _installed_pkg_dir() -> Path | None:
    probe = subprocess.run(
        [sys.executable, "-c", "import scansci_pdf; print(scansci_pdf.__file__)"],
        cwd=tempfile.gettempdir(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        return None  # package not installed for this interpreter
    installed = Path(probe.stdout.strip()).resolve().parent
    if installed == REPO_SRC:
        return None  # running from the repo checkout itself — nothing to compare
    if "site-packages" in installed.parts or "dist-packages" in installed.parts:
        return None  # regular install (e.g. CI) — not a mirror checkout
    return installed


def test_installed_checkout_matches_repo_byte_for_byte():
    installed = _installed_pkg_dir()
    if installed is None:
        pytest.skip("no second checkout to compare against")
    repo_files = sorted(p.relative_to(REPO_SRC) for p in REPO_SRC.rglob("*.py"))
    assert repo_files, "repo source tree is empty — wrong checkout?"

    diffs: list[str] = []
    for rel in repo_files:
        repo_path = REPO_SRC / rel
        inst_path = installed / rel
        if not inst_path.exists():
            diffs.append(f"{rel}: missing in installed checkout")
        elif repo_path.read_bytes() != inst_path.read_bytes():
            diffs.append(f"{rel}: content differs")
    extra = sorted(
        str(p.relative_to(installed))
        for p in installed.rglob("*.py")
        if not (REPO_SRC / p.relative_to(installed)).exists()
    )
    diffs.extend(f"{rel}: extra file in installed checkout (deleted in repo?)" for rel in extra)

    assert not diffs, (
        "repo and installed checkouts diverged:\n  " + "\n  ".join(diffs)
        + "\nRe-run the sync step (copy repo src/ over the installed checkout)."
    )
