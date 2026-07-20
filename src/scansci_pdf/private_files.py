"""Private local-file persistence for credential-bearing runtime state."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_private(path: Path, content: str) -> None:
    """Atomically write text in the target directory with owner-only mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
