"""Cache management for ScanSci PDF."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any


def cache_key(identifier: str) -> str:
    return hashlib.sha256(identifier.lower().encode("utf-8")).hexdigest()[:24]


def cache_path(identifier: str, config: dict[str, Any]) -> Path:
    return Path(config["cache_dir"]) / f"{cache_key(identifier)}.json"


def cache_get(identifier: str, config: dict[str, Any]) -> dict[str, Any] | None:
    path = cache_path(identifier, config)
    if not path.exists():
        return None
    ttl = float(config.get("cache_ttl_hours", 168)) * 3600
    if ttl > 0 and time.time() - path.stat().st_mtime > ttl:
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def cache_set(identifier: str, result: dict[str, Any], config: dict[str, Any]) -> None:
    cache_dir = Path(config["cache_dir"])
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_path(identifier, config)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
        tmp.replace(target)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def cache_clear(identifier: str | None, config: dict[str, Any]) -> int:
    cache_dir = Path(config["cache_dir"])
    if not cache_dir.exists():
        return 0
    if identifier:
        path = cache_path(identifier, config)
        if path.exists():
            path.unlink()
            return 1
        return 0
    count = 0
    for f in cache_dir.glob("*.json"):
        f.unlink()
        count += 1
    return count
