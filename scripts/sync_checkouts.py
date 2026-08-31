"""Sync the repo checkout into the installed editable checkout (one command).

Replaces the error-prone manual multi-file `cp` flow that caused three
incidents today (wrong-level copies, missed files, a source file
overwritten by a test file). Usage:

    python scripts/sync_checkouts.py            # mirror + verify
    python scripts/sync_checkouts.py --test     # mirror + run the sync test

What it does:
1. Mirrors src/scansci_pdf, tests, docs from the repo into the plugins
   checkout (add/update/delete), excluding __pycache__.
2. Byte-compares every mirrored file and reports leftovers.
3. Exit 1 on any mismatch (so CI/scripts can gate on it).
"""

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGINS = Path(r"C:\Users\Liang\plugins\scansci-pdf")
TREES = ["src/scansci_pdf", "tests", "docs"]
EXCLUDE = {"__pycache__"}


def iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and not any(part in EXCLUDE for part in p.parts):
            yield p


def mirror(tree: str) -> tuple[int, int]:
    src, dst = REPO / tree, PLUGINS / tree
    copied = deleted = 0
    if not src.exists():
        return 0, 0
    for f in iter_files(src):
        rel = f.relative_to(src)
        target = dst / rel
        if not target.exists() or f.read_bytes() != target.read_bytes():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target)
            copied += 1
    if dst.exists():
        for f in iter_files(dst):
            rel = f.relative_to(dst)
            if not (src / rel).exists():
                f.unlink()
                deleted += 1
    return copied, deleted


def verify(trees) -> list[str]:
    diffs = []
    for tree in trees:
        src, dst = REPO / tree, PLUGINS / tree
        if not src.exists():
            continue
        for f in iter_files(src):
            rel = f.relative_to(src)
            t = dst / rel
            if not t.exists():
                diffs.append(f"{tree}/{rel}: missing in plugins")
            elif f.read_bytes() != t.read_bytes():
                diffs.append(f"{tree}/{rel}: differs")
        if dst.exists():
            for f in iter_files(dst):
                if not (src / f.relative_to(dst)).exists():
                    diffs.append(f"{tree}/{f.relative_to(dst)}: extra in plugins")
    return diffs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="run tests/test_checkout_sync.py after mirroring")
    args = ap.parse_args()

    trees = [t for t in TREES if (REPO / t).exists()]
    copied = deleted = 0
    for tree in trees:
        c, d = mirror(tree)
        copied += c
        deleted += d
    print(f"mirrored: {copied} updated, {deleted} deleted")
    diffs = verify(trees)
    if diffs:
        print("VERIFY FAILED:")
        for d in diffs:
            print(" ", d)
        return 1
    print("verify OK: checkouts byte-identical")
    if args.test:
        import subprocess
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/test_checkout_sync.py", "-q"],
                           cwd=REPO)
        return r.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
