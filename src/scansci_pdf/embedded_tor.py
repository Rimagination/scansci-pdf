"""Embedded Tor: auto-download and manage Tor binary as subprocess.

Downloads Tor Expert Bundle to ~/.scansci-pdf/tor/ on first use,
starts it as a SOCKS5 proxy subprocess, and cleans up on exit.
Supports obfs4 bridges for restricted networks.
"""

from __future__ import annotations

import io
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger()

# Default bridges for restricted networks (obfs4)
DEFAULT_OBFS4_BRIDGES = [
    "obfs4 154.35.22.13:16844 8FB268DC3037E3F5A5E64C9031B2F92384321B93 cert=bjNGcJBJqUTRqtA4q0z2CU8wmsGdDJnVBPpnrZqK+z8kfH3ZNxFbcFhsBdz2t317W77YOA iat-mode=0",
    "obfs4 192.95.36.142:443 3424DB3F0F1F5A7935872FDE7573F9FB912AAEF1 cert=nmPlqulSC4W5X+9mrrq1xLObRTFM81QjPcJm687Dsr0PQRw4UaI8OCkuNfYklA3/aWF3sA iat-mode=0",
    "obfs4 51.222.13.177:50000 1C46E0A5B76D624B99260522F818D1F0287FD4B4 cert=uxPGjNskSCqHGoODKx8j3wcllOJBuJnlkGEXXf9geUZG4YEmyrPK1sc0ePsSVuLD8P9Kg iat-mode=0",
    "obfs4 51.81.223.139:50000 B3A09F6EE345B6B246D3C462FF6643BB30D639FF cert=+yoRAwJXN3Ge1mH2kDjDRTXWbJa1yANPN4kvP1H4ozuFr3S9NbfPQ6YMclexjGqIIAFJcA iat-mode=0",
]

# Mirror URLs for downloading Tor Expert Bundle
TOR_DOWNLOAD_MIRRORS = [
    "https://dist.torproject.org",
    "https://archive.torproject.org/tor-package-archive",
]

TOR_VERSION = "14.0.4"


def _tor_dir(config: dict[str, Any]) -> Path:
    from .config import DATA_DIR
    return Path(config.get("cache_dir", str(DATA_DIR / "cache"))).parent / "tor"


def _tor_binary(config: dict[str, Any]) -> Path | None:
    """Find the tor binary path, downloading if needed."""
    tor_dir = _tor_dir(config)

    # Check for existing binary
    if platform.system() == "Windows":
        tor_exe = tor_dir / "tor" / "tor.exe"
    else:
        tor_exe = tor_dir / "tor" / "tor"

    if tor_exe.exists():
        return tor_exe

    # Check system PATH
    system_tor = shutil.which("tor")
    if system_tor:
        return Path(system_tor)

    return None


def _download_url() -> tuple[str, str]:
    """Get the download URL and filename for Tor Expert Bundle."""
    system = platform.system()
    machine = platform.machine().lower()

    if system == "Windows":
        filename = f"tor-expert-bundle-{TOR_VERSION}-windows-x86_64.zip"
    elif system == "Darwin":
        if machine in ("arm64", "aarch64"):
            filename = f"tor-expert-bundle-{TOR_VERSION}-macos-aarch64.tar.gz"
        else:
            filename = f"tor-expert-bundle-{TOR_VERSION}-macos-x86_64.tar.gz"
    else:
        if machine in ("aarch64", "arm64"):
            filename = f"tor-expert-bundle-{TOR_VERSION}-linux-aarch64.tar.gz"
        else:
            filename = f"tor-expert-bundle-{TOR_VERSION}-linux-x86_64.tar.gz"

    return filename


def download_tor(config: dict[str, Any]) -> Path | None:
    """Download and extract Tor Expert Bundle. Returns path to tor binary."""
    tor_dir = _tor_dir(config)
    tor_dir.mkdir(parents=True, exist_ok=True)

    filename = _download_url()
    is_zip = filename.endswith(".zip")
    is_targz = filename.endswith(".tar.gz")

    # Try mirrors, using proxy if configured
    import requests as req
    proxies = {}
    proxy = os.environ.get("SCANSCI_PDF_PROXY") or os.environ.get("HTTPS_PROXY") or config.get("network_proxy")
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    for mirror in TOR_DOWNLOAD_MIRRORS:
        url = f"{mirror}/{filename}"
        try:
            log.info(f"Downloading Tor from {url}")
            resp = req.get(url, timeout=300, stream=True, proxies=proxies)
            if resp.status_code != 200:
                continue

            total = int(resp.headers.get("content-length", 0))
            data = io.BytesIO()
            downloaded = 0
            for chunk in resp.iter_content(8192):
                data.write(chunk)
                downloaded += len(chunk)
                if total and downloaded % (1024 * 1024) < 8192:
                    log.info(f"  Downloaded {downloaded / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB")

            data.seek(0)
            log.info(f"Extracting Tor to {tor_dir}")

            if is_zip:
                with zipfile.ZipFile(data) as zf:
                    zf.extractall(tor_dir)
            elif is_targz:
                with tarfile.open(fileobj=data, mode="r:gz") as tf:
                    tf.extractall(tor_dir)

            # Find and set executable permission on non-Windows
            binary = _tor_binary(config)
            if binary and binary.exists():
                if platform.system() != "Windows":
                    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
                log.info(f"Tor installed: {binary}")
                return binary

        except Exception as e:
            log.warning(f"Failed to download from {mirror}: {e}")
            continue

    log.error("Failed to download Tor from all mirrors")
    return None


def _write_torrc(tor_dir: Path, socks_port: int, use_bridges: bool = False) -> Path:
    """Generate a minimal torrc configuration file."""
    torrc_path = tor_dir / "torrc"
    lines = [
        f"SocksPort {socks_port}",
        "SocksListenAddress 127.0.0.1",
        "AvoidDiskWrites 1",
        "Log notice stdout",
        "GeoIPFile unreachable",
        "GeoIPv6File unreachable",
    ]

    if use_bridges:
        lines.append("UseBridges 1")
        lines.append("ClientTransportPlugin obfs4 exec obfs4proxy")
        for bridge in DEFAULT_OBFS4_BRIDGES:
            lines.append(f"Bridge {bridge}")

    torrc_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return torrc_path


class EmbeddedTor:
    """Manages an embedded Tor subprocess as a SOCKS5 proxy."""

    def __init__(self, config: dict[str, Any], socks_port: int = 0, use_bridges: bool = False):
        self.config = config
        self.socks_port = socks_port or self._find_free_port()
        self.use_bridges = use_bridges
        self._process: subprocess.Popen | None = None
        self._binary: Path | None = None

    @staticmethod
    def _find_free_port() -> int:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @property
    def proxy_url(self) -> str:
        return f"socks5h://127.0.0.1:{self.socks_port}"

    def start(self, timeout: int = 60) -> bool:
        """Start Tor subprocess and wait for it to be ready."""
        self._binary = _tor_binary(self.config)

        # Download if not found
        if not self._binary:
            log.info("Tor binary not found, downloading...")
            self._binary = download_tor(self.config)
            if not self._binary:
                return False

        tor_dir = self._binary.parent
        torrc = _write_torrc(tor_dir.parent, self.socks_port, self.use_bridges)

        cmd = [str(self._binary), "-f", str(torrc)]
        log.info(f"Starting Tor: {' '.join(cmd[:2])}")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
            )
        except Exception as e:
            log.error(f"Failed to start Tor: {e}")
            return False

        # Wait for SOCKS port to be ready
        import socket
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.socks_port), timeout=2):
                    log.info(f"Tor ready on {self.proxy_url}")
                    return True
            except (ConnectionRefusedError, OSError):
                # Check if process died
                if self._process.poll() is not None:
                    log.error(f"Tor exited with code {self._process.returncode}")
                    stderr = self._process.stderr.read().decode("utf-8", errors="replace")[:500]
                    log.error(f"Tor stderr: {stderr}")
                    self._process = None
                    return False
                time.sleep(1)

        log.warning("Tor startup timed out")
        self.stop()
        return False

    def stop(self) -> None:
        """Stop the Tor subprocess."""
        if self._process and self._process.poll() is None:
            log.info("Stopping embedded Tor")
            try:
                if platform.system() == "Windows":
                    self._process.terminate()
                else:
                    self._process.send_signal(signal.SIGTERM)
                self._process.wait(timeout=10)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()


# Global singleton for embedded Tor process
_embedded_tor: EmbeddedTor | None = None
_tor_lock = threading.Lock()


def get_embedded_tor(config: dict[str, Any]) -> EmbeddedTor | None:
    """Get or create a global embedded Tor instance."""
    global _embedded_tor
    with _tor_lock:
        if _embedded_tor and _embedded_tor.is_running():
            return _embedded_tor

        use_bridges = config.get("tor_use_bridges", False)
        tor = EmbeddedTor(config, use_bridges=use_bridges)
        if tor.start():
            _embedded_tor = tor
            return tor
        return None


def stop_embedded_tor() -> None:
    """Stop the global embedded Tor instance."""
    global _embedded_tor
    with _tor_lock:
        if _embedded_tor:
            _embedded_tor.stop()
            _embedded_tor = None


def is_tor_installed(config: dict[str, Any]) -> bool:
    """Check if Tor binary is available (embedded or system)."""
    return _tor_binary(config) is not None


def install_tor(config: dict[str, Any]) -> dict[str, Any]:
    """Download and install Tor. Returns status dict."""
    binary = _tor_binary(config)
    if binary:
        return {"installed": True, "path": str(binary), "message": "Tor already installed"}

    binary = download_tor(config)
    if binary:
        return {"installed": True, "path": str(binary), "message": f"Tor installed to {binary}"}
    return {"installed": False, "error": "Failed to download Tor. Check network connectivity."}
