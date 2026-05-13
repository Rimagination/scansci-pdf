"""Setup diagnostic: detect system environment and provide install commands."""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
from typing import Any


def detect_system() -> dict[str, Any]:
    """Detect OS, arch, and relevant system info."""
    sys_info: dict[str, Any] = {
        "os": platform.system(),
        "os_version": platform.version(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }

    # Windows edition
    if sys_info["os"] == "Windows":
        try:
            import ctypes
            is_64 = ctypes.sizeof(ctypes.c_voidp) == 8
            sys_info["windows_arch"] = "64-bit" if is_64 else "32-bit"
        except Exception:
            pass

    return sys_info


def check_tor() -> dict[str, Any]:
    """Check Tor installation and connectivity."""
    result: dict[str, Any] = {"installed": False, "running": False, "port": 1080, "embedded": False}

    # Check embedded Tor binary
    from .config import load_config
    config = load_config()
    from .embedded_tor import _tor_binary
    embedded = _tor_binary(config)
    if embedded:
        result["installed"] = True
        result["embedded"] = True
        result["path"] = str(embedded)

    # Check if tor binary exists in system PATH
    tor_path = shutil.which("tor")
    if tor_path:
        result["installed"] = True
        result["path"] = tor_path

    # Check if Tor is listening on common ports
    for port in (1080, 9050, 9150):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                result["running"] = True
                result["port"] = port
                break
        except (ConnectionRefusedError, OSError):
            pass

    return result


def check_docker() -> dict[str, Any]:
    """Check Docker installation and status."""
    result: dict[str, Any] = {"installed": False, "running": False}

    docker_path = shutil.which("docker")
    if not docker_path:
        return result

    result["installed"] = True
    result["path"] = docker_path

    try:
        version_out = subprocess.run(
            [docker_path, "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=5,
        )
        if version_out.returncode == 0:
            result["version"] = version_out.stdout.strip()
            result["running"] = True
    except Exception:
        pass

    # Check docker compose
    compose_path = shutil.which("docker-compose") or shutil.which("docker")
    if compose_path:
        try:
            cmd = [compose_path, "compose", "version"] if compose_path == docker_path else [compose_path, "version"]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if out.returncode == 0:
                result["compose_available"] = True
        except Exception:
            pass

    return result


def check_camofox() -> dict[str, Any]:
    """Check camofox-browser connectivity."""
    result: dict[str, Any] = {"running": False}
    try:
        from .camofox import is_available
        from .config import load_config
        result["running"] = is_available(load_config())
    except Exception:
        pass
    return result


def get_install_commands(sys_info: dict[str, Any], tor: dict, docker: dict) -> dict[str, Any]:
    """Return system-specific installation commands for missing components."""
    os_name = sys_info["os"]
    commands: dict[str, Any] = {}

    if not tor["running"]:
        # Recommend embedded Tor (no Docker needed)
        commands["tor"] = {
            "method": "embedded",
            "reason": "自动下载 Tor Expert Bundle 到 ~/.scansci-pdf/tor/，无需 Docker 或系统安装",
            "steps": [
                "1. 运行 scansci_pdf_tor_install 自动下载 Tor",
                "2. 运行 scansci_pdf_tor_start 启动 Tor SOCKS5 代理",
                "3. 下载时使用 use_tor=true",
            ],
            "bridges": "在受限网络中，使用 scansci_pdf_tor_start(use_bridges=true) 启用 obfs4 桥接",
            "alternatives": [],
        }
        if docker["installed"]:
            commands["tor"]["alternatives"].append({
                "method": "docker",
                "steps": ["docker compose up -d tor (image: shahradel/torproxy:latest)"],
            })
        if os_name == "Darwin":
            commands["tor"]["alternatives"].append({
                "method": "homebrew",
                "steps": ["brew install tor", "brew services start tor"],
            })
        elif os_name != "Windows":
            commands["tor"]["alternatives"].append({
                "method": "apt",
                "steps": ["sudo apt install tor", "sudo systemctl enable --now tor"],
            })
        elif os_name == "Windows":
            commands["tor"]["alternatives"].append({
                "method": "manual",
                "steps": ["下载 Tor Expert Bundle: https://www.torproject.org/download/tor/"],
            })

    if not docker["installed"]:
        if os_name == "Windows":
            commands["docker"] = {
                "url": "https://www.docker.com/products/docker-desktop/",
                "note": "下载 Docker Desktop for Windows (WSL2 backend)",
            }
        elif os_name == "Darwin":
            commands["docker"] = {
                "url": "https://www.docker.com/products/docker-desktop/",
                "note": "下载 Docker Desktop for Mac (Apple Silicon 或 Intel)",
            }
        else:
            commands["docker"] = {
                "steps": [
                    "curl -fsSL https://get.docker.com | sh",
                    "sudo usermod -aG docker $USER",
                ],
            }

    return commands


def setup_check() -> dict[str, Any]:
    """Full setup diagnostic."""
    sys_info = detect_system()
    tor = check_tor()
    docker = check_docker()
    camofox = check_camofox()

    commands = get_install_commands(sys_info, tor, docker)

    # Overall readiness
    issues = []
    if not tor["running"] and not docker.get("running"):
        issues.append("Tor 未运行且 Docker 未安装，Sci-Hub/LibGen 通过 Tor 访问受限")
    if not docker.get("installed"):
        issues.append("Docker 未安装，无法使用 Tor 容器化部署")
    if not camofox["running"]:
        issues.append("camofox-browser 未运行，Cloudflare 防护可能无法绕过")

    readiness = "ready" if not issues else "partial"
    if not tor["running"] and not docker.get("running") and not camofox["running"]:
        readiness = "limited"

    return {
        "system": sys_info,
        "tor": tor,
        "docker": docker,
        "camofox": camofox,
        "install_commands": commands,
        "issues": issues,
        "readiness": readiness,
    }
