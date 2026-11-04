#!/usr/bin/env python3
"""Build the Qoder plugin ZIP with a deterministic runtime allowlist.

The bundle directory also contains development-only files (``scripts/`` and
``RELEASE.md``) that must not reach the marketplace. Copying the directory with
``Compress-Archive -Path .\\qoder-plugin\\*`` includes them, so this script
copies only the runtime payload, wraps it in a top-level ``scansci-pdf/``
folder (matching the layout of the successful local install), and writes the
ZIP under ``dist/``.

Usage:
    python qoder-plugin/scripts/build-bundle.py
    python qoder-plugin/scripts/build-bundle.py --output dist/qoder.zip
    python qoder-plugin/scripts/build-bundle.py --flat   # installer layout test
"""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import tomllib
import zipfile
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent.parent
REPO = BUNDLE.parent

# Exact runtime payload. Anything not listed here is excluded, so adding a new
# development file cannot leak into the marketplace ZIP by accident.
ALLOW_FILES = (
    ".mcp.json",
    ".qoder-plugin/plugin.json",
    "README.md",
    "CONNECTORS.md",
    "LICENSE",
    "icon.png",
    "skills/scansci-pdf/SKILL.md",
)
ALLOW_DIRS = ("commands/scansci",)

# These must never appear in the ZIP. The allowlist already excludes them; the
# post-build assertion is a second line of defense.
FORBIDDEN_PARTS = ("scripts/", "RELEASE.md", "references/", "scansci-sort/")


def _package_version() -> str:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def _manifest_version() -> str:
    data = json.loads((BUNDLE / ".qoder-plugin" / "plugin.json").read_text(encoding="utf-8"))
    return str(data["version"])


def _runtime_files() -> list[Path]:
    files: list[Path] = []
    for rel in ALLOW_FILES:
        src = BUNDLE / rel
        if not src.is_file():
            raise SystemExit(f"missing bundle file: {rel}")
        files.append(src)
    for rel in ALLOW_DIRS:
        directory = BUNDLE / rel
        if not directory.is_dir():
            raise SystemExit(f"missing bundle directory: {rel}")
        matches = sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix == ".md")
        if not matches:
            raise SystemExit(f"no .md files in bundle directory: {rel}")
        files.extend(matches)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="", help="ZIP path (default: dist/scansci-pdf-qoder-plugin-<version>.zip)")
    parser.add_argument("--root", default="scansci-pdf", help="Top-level folder inside the ZIP")
    parser.add_argument("--flat", action="store_true", help="Put the payload at the ZIP root (installer test)")
    args = parser.parse_args()

    package_version = _package_version()
    manifest_version = _manifest_version()
    if manifest_version != package_version:
        raise SystemExit(
            f"version drift: plugin.json has {manifest_version}, pyproject.toml has {package_version}"
        )

    output = (
        Path(args.output)
        if args.output
        else REPO / "dist" / f"scansci-pdf-qoder-plugin-{package_version}.zip"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()

    files = _runtime_files()
    with tempfile.TemporaryDirectory(prefix="qoder-bundle-") as tmp:
        tmp_root = Path(tmp)
        stage = tmp_root if args.flat else tmp_root / args.root
        stage.mkdir(parents=True, exist_ok=True)
        for src in files:
            dst = stage / src.relative_to(BUNDLE)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        base = stage if args.flat else tmp_root
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(base).as_posix())

    with zipfile.ZipFile(output) as zf:
        names = zf.namelist()
    leaked = [name for name in names if any(part in name for part in FORBIDDEN_PARTS)]
    if leaked:
        output.unlink(missing_ok=True)
        raise SystemExit(f"refusing to ship forbidden files: {', '.join(leaked)}")

    print(f"Built {output} ({len(names)} files, root={'flat' if args.flat else args.root})")
    for name in names:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
