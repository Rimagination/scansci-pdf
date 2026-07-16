"""Shared browser runtime discovery for ScanSci PDF.

The doctor in this module is intentionally diagnostic only: it does not install
packages, download browser binaries, launch browsers, or write browser profiles.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

PROFILE_DIR = "D:/Dev/browser-profiles/scansci"
CACHE_DIR = "D:/Dev/cache/browser"


def _default_system_browser_paths() -> List[str]:
    return [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ]


def _default_current_module_paths() -> List[str]:
    base = Path(__file__).resolve().parent
    return [
        str(base / "browser_cookies.py"),
        str(base / "browser_engine.py"),
        str(base / "browser_login.py"),
    ]


def _split_command(value: str) -> List[str]:
    value = value.strip()
    if not value:
        return []
    if value.startswith("["):
        parsed = json.loads(value)
        return [str(item) for item in parsed]
    return shlex.split(value, posix=os.name != "nt")


def _http_available(url: str) -> bool:
    if not url:
        return False
    try:
        request = urllib.request.Request(url.rstrip("/") + "/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=0.25) as response:
            return response.status < 500
    except Exception:
        return False


class BrowserRuntimeResolver:
    def __init__(
        self,
        *,
        env: Optional[Dict[str, str]] = None,
        path_lookup: Optional[Callable[[str], Optional[str]]] = None,
        import_exists: Optional[Callable[[str], bool]] = None,
        path_exists: Optional[Callable[[str], bool]] = None,
        http_available: Optional[Callable[[str], bool]] = None,
        current_module_paths: Optional[Iterable[str]] = None,
        default_system_browser_paths: Optional[Iterable[str]] = None,
    ) -> None:
        self.env = dict(os.environ if env is None else env)
        self.path_lookup = path_lookup or shutil.which
        self.import_exists = import_exists or (lambda name: importlib.util.find_spec(name) is not None)
        self.path_exists = path_exists or (lambda path: Path(path).exists())
        self.http_available = http_available or _http_available
        self.current_module_paths = list(
            _default_current_module_paths() if current_module_paths is None else current_module_paths
        )
        self.system_browser_paths = list(
            _default_system_browser_paths() if default_system_browser_paths is None else default_system_browser_paths
        )

    def doctor(self) -> Dict[str, Any]:
        candidates = self._candidates()
        selected = next((item for item in candidates if item.get("available")), None)
        if selected:
            result = dict(selected)
            result["install_needed"] = False
            result["install_hint"] = ""
        else:
            result = {
                "selected": "",
                "source": "",
                "available": False,
                "install_needed": True,
                "install_hint": "No reusable browser runtime found. Suggested explicit install: pip install cloakbrowser, or configure SCANSCI_BROWSER_COMMAND.",
            }
        result["profile_dir"] = PROFILE_DIR
        result["cache_dir"] = CACHE_DIR
        result["candidates"] = candidates
        return result

    def _candidates(self) -> List[Dict[str, Any]]:
        candidates: List[Dict[str, Any]] = []
        configured = self.env.get("SCANSCI_BROWSER_COMMAND", "").strip()
        if configured:
            candidates.append(
                {
                    "selected": "configured_command",
                    "source": "SCANSCI_BROWSER_COMMAND",
                    "available": True,
                    "command": _split_command(configured),
                }
            )

        command_path = self.path_lookup("scansci-browser")
        candidates.append(
            {
                "selected": "scansci_browser_command",
                "source": "PATH",
                "available": bool(command_path),
                "path": command_path or "",
            }
        )
        candidates.append(
            {
                "selected": "scansci_browser_package",
                "source": "python_package",
                "available": bool(self.import_exists("scansci_browser")),
                "package": "scansci_browser",
            }
        )
        candidates.append(self._current_scansci_pdf())
        for package in ("cloakbrowser", "playwright"):
            candidates.append(
                {
                    "selected": f"{package}_package",
                    "source": "python_package",
                    "available": bool(self.import_exists(package)),
                    "package": package,
                }
            )
        system_browser = self._system_browser()
        candidates.append(
            {
                "selected": "system_browser",
                "source": "system_browser",
                "available": bool(system_browser),
                "browser_binary": system_browser or "",
            }
        )
        return candidates

    def _current_scansci_pdf(self) -> Dict[str, Any]:
        modules = [path for path in self.current_module_paths if self.path_exists(path)]
        return {
            "selected": "scansci_pdf_browser",
            "source": "current_scansci_pdf",
            "available": bool(modules),
            "modules": [path.replace("\\", "/") for path in modules],
        }

    def _system_browser(self) -> str:
        for path in self.system_browser_paths:
            if self.path_exists(path):
                return path.replace("\\", "/")
        for name in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
            found = self.path_lookup(name)
            if found:
                return found.replace("\\", "/")
        return ""


def doctor() -> Dict[str, Any]:
    result = BrowserRuntimeResolver().doctor()
    # Surface cloakbrowser version freshness. cloakbrowser iterates on
    # anti-detection patches fast; surfacing staleness here lets users
    # diagnose silent bot-detection failures via scansci-pdf browser-doctor.
    try:
        from .cli import _cloakbrowser_version_status, CLOAKBROWSER_RECOMMENDED_MIN
        cb_status, cb_detail = _cloakbrowser_version_status()
        result["cloakbrowser_version"] = {
            "status": cb_status,
            "detail": cb_detail,
            "recommended_min": CLOAKBROWSER_RECOMMENDED_MIN,
        }
    except Exception as exc:  # pragma: no cover - defensive
        result["cloakbrowser_version"] = {"status": "unknown", "detail": str(exc)}
    return result


def main(argv: Optional[List[str]] = None) -> int:
    print(json.dumps(doctor(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
