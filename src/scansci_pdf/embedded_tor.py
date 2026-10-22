"""Embedded Tor: auto-download and manage Tor binary as subprocess.

Downloads Tor Expert Bundle to ~/.scansci-pdf/tor/ on first use,
starts it as a SOCKS5 proxy subprocess, and cleans up on exit.
Supports obfs4 bridges for restricted networks.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
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

# Mirror URLs for downloading Tor Expert Bundle.
# Files live under {mirror}/torbrowser/{version}/ on both mirrors.
TOR_DOWNLOAD_MIRRORS = [
    "https://dist.torproject.org/torbrowser",
    "https://archive.torproject.org/tor-package-archive/torbrowser",
]

# Tor Browser version shipping the Expert Bundle. Bump when torproject.org
# cuts a new stable release -- together with TOR_HASHES, in the same change:
# the pins are what make a download verifiable, not the version string.
# 15.0.25 pins confirmed 2026-10-19: the four digests below match the
# Expert Bundle entries in the signed manifest
# https://tor.eff.org/dist/torbrowser/15.0.25/sha256sums-signed-build.txt,
# whose GPG signature verifies against the Tor Browser signing key
# (EF6E 286D DA85 EA2A 4BA7 DE68 4E2C 6E87 9329 8290). The key's identity
# rests on that fingerprint being Tor's publicly documented signing key.
# The values are confirmed, not transcribed.
# 15.0.17 has since been dropped from the dist mirror root, leaving
# archive.torproject.org as its only surviving mirror.
TOR_VERSION = "15.0.25"

# SHA-256 of each Expert Bundle archive for TOR_VERSION, keyed by the os-arch
# slug used in the archive filename. A release upgrade must update these
# source-controlled pins, never fetch new ones at runtime (#71 item 1).
TOR_HASHES = {
    "linux-x86_64": "19cf18726eecb362e0f21d27afa9ea9efe5da928bfdfbb1ceb5fe54519e87f4a",
    "macos-aarch64": "a3febb6179152b0b4f56a0f5cc57bf5cc8ecef7db35e7c3e9d5a044c8158c26c",
    "macos-x86_64": "e1ae4d4a734d28bb590839188511deb3cebc26fcbce9df66d01b2c452060fe95",
    "windows-x86_64": "c1988c701de2f24f7d098d1d6d7f507b58f27740d097f29c246e0524d639b252",
}

# Name under which the verified archive is kept inside an installed bundle
# directory, so the pins stay checkable against what is on disk.
_BUNDLE_ARCHIVE = ".bundle.tar.gz"

# Sanity ceiling for a download: pinned Expert Bundles are tens of MB, so
# anything this size is a compromised mirror trying to fill the disk before
# the digest check ever runs.
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024

# Windows reserved device basenames (stem before the first extension dot);
# writing such a member would create or hit a device instead of a real file.
_RESERVED_BASENAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# So the pre-verification layout notice logs once per process, not per probe.
_legacy_layout_warned = False


class TorIntegrityError(RuntimeError):
    """A Tor bundle failed pinned verification; nothing from it may be run."""


def _download_url() -> tuple[str, str]:
    """Get the download URL path and filename for the Tor Expert Bundle.

    Returns ``(url_path, filename)`` where ``url_path`` is the relative path
    under a mirror root (the Tor Browser version) and ``filename`` is the
    archive name. All Expert Bundle archives are .tar.gz on every platform
    (the legacy .zip was retired with 0.4.7.x).

    Raises ValueError for platforms with no pinned bundle: this release ships
    no linux-aarch64 Expert Bundle, and downloading an archive we cannot
    verify is worse than failing with a clear message (#71 item 1).
    """
    system = platform.system()
    machine = platform.machine().lower()

    # Map (system, machine) → the os-arch slug used in the filename.
    if system == "Windows":
        os_arch = "windows-x86_64"  # i686 build retired; x86_64 covers all supported Windows
    elif system == "Darwin":
        os_arch = "macos-aarch64" if machine in ("arm64", "aarch64") else "macos-x86_64"
    elif machine in ("aarch64", "arm64"):
        raise ValueError(
            "No Tor Expert Bundle is published for linux-aarch64 in this release; "
            "set tor_binary_path or put your own tor on PATH"
        )
    else:
        os_arch = "linux-x86_64"

    filename = f"tor-expert-bundle-{os_arch}-{TOR_VERSION}.tar.gz"
    return TOR_VERSION, filename


def _tor_dir(config: dict[str, Any]) -> Path:
    from .config import DATA_DIR
    return Path(config.get("cache_dir", str(DATA_DIR / "cache"))).parent / "tor"


def _bundle_dir(config: dict[str, Any]) -> Path:
    """The version-scoped directory a verified bundle is installed into."""
    return _tor_dir(config) / f"bundle-{TOR_VERSION}"


def _runtime_dir(config: dict[str, Any]) -> Path:
    """Generated state (torrc) lives here, outside the verified bundle."""
    return _tor_dir(config) / "runtime"


def _tor_binary(config: dict[str, Any]) -> Path | None:
    """Find the tor binary: explicit config, then the embedded install, then PATH.

    The PATH fallback stays -- people do install Tor themselves -- but it is now
    the last resort and says so, because PATH lookup order silently deciding
    which binary gets executed is not a decision worth leaving implicit (#71).

    The embedded install is trusted only after `_verify_bundle` re-checks it
    against the pinned archive; a bundle that fails verification raises instead
    of returning None, so a tampered cache can never fall through to PATH (#71
    item 1). The pre-verification layout (``<tor>/tor/tor``) is ignored for
    the same reason: those files were never verified.
    """
    configured = str(config.get("tor_binary_path") or "").strip()
    if configured:
        pinned = Path(configured).expanduser()
        if pinned.is_file():
            return pinned
        log.warning("tor_binary_path is set but is not a file: %s", pinned)
        return None

    tor_dir = _tor_dir(config)
    legacy = tor_dir / "tor" / ("tor.exe" if platform.system() == "Windows" else "tor")
    global _legacy_layout_warned
    if legacy.exists() and not _legacy_layout_warned:
        _legacy_layout_warned = True
        log.info(
            "Ignoring Tor at %s: installed by an older version and never verified; "
            "a pinned bundle takes its place on the next install",
            legacy,
        )

    bundle = _bundle_dir(config)
    if bundle.exists():
        return _verify_bundle(bundle)

    system_tor = shutil.which("tor")
    if system_tor:
        log.info(
            "Using tor found on PATH (%s); set tor_binary_path to pin which binary runs",
            system_tor,
        )
        return Path(system_tor)

    return None


def tor_installed(config: dict[str, Any]) -> bool:
    """Whether a tor binary is already available (pinned, embedded, or on PATH).

    Starting Tor installs on demand, so this is what tells a caller whether that
    would reach the network for the bundle (#71).
    """
    return _tor_binary(config) is not None


def _bundle_digest(filename: str) -> str:
    slug = filename.removeprefix("tor-expert-bundle-").removesuffix(f"-{TOR_VERSION}.tar.gz")
    return TOR_HASHES[slug]


def _check_member_name(staging: Path, name: str) -> Path:
    """Resolve one archive member name inside `staging`, or refuse it."""
    member = PurePosixPath(name)
    if member.is_absolute() or ".." in member.parts:
        raise TorIntegrityError(f"Archive member escapes the install directory: {name}")
    parts = [p for p in member.parts if p not in ("", ".")]
    if not parts:
        raise TorIntegrityError(f"Archive member has an empty name: {name!r}")
    for part in parts:
        if len(part) == 2 and part[1] == ":" and part[0].isalpha():
            # "C:/x" resolves inside staging on POSIX hosts but replaces the
            # drive on Windows; no legitimate archive member looks like this.
            raise TorIntegrityError(f"Archive member looks like a Windows drive path: {name}")
        stem = part.split(".", 1)[0].rstrip(" ").upper()
        if stem in _RESERVED_BASENAMES:
            raise TorIntegrityError(f"Archive member uses a reserved device name: {name}")
    target = staging.joinpath(*parts)
    if not target.resolve().is_relative_to(staging.resolve()):
        raise TorIntegrityError(f"Archive member escapes the install directory: {name}")
    return target


def _extract_verified(archive_path: Path, staging: Path, expected_digest: str) -> None:
    """Extract into `staging` from the archive, verifying it as it is read.

    The archive is read into memory and hashed here, and extraction runs from
    that same in-memory copy: the bytes that get extracted are exactly the
    bytes that matched the pin, leaving no disk-resident gap between the
    download's streaming digest and what tarfile opens (#71 item 1). Only
    regular files are written; directories are implied by their parents, and
    any other member type (symlink, device, hard link) is refused rather than
    followed.
    """
    data = archive_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_digest:
        raise TorIntegrityError("Archive on disk does not match its pinned digest")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.isfile():
                raise TorIntegrityError(f"Archive member is not a regular file: {member.name}")
            target = _check_member_name(staging, member.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def _verify_bundle(bundle: Path) -> Path:
    """Re-verify an installed bundle against the pinned archive.

    Checks the cached archive digest, every installed file (SHA-256) against
    the archive member it came from, and that the directory holds nothing
    else. Returns the tor executable; raises TorIntegrityError on any
    mismatch so the caller never falls through to a different binary.
    """
    archive_path = bundle / _BUNDLE_ARCHIVE
    if archive_path.is_symlink() or not archive_path.is_file():
        raise TorIntegrityError(f"Verified bundle is missing its archive: {archive_path}")
    try:
        _, filename = _download_url()
    except ValueError as e:
        raise TorIntegrityError(str(e)) from e
    digest = hashlib.sha256()
    with archive_path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != _bundle_digest(filename):
        raise TorIntegrityError("Cached Tor archive failed pinned verification")

    expected = {_BUNDLE_ARCHIVE}
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive:
            if member.isdir():
                continue
            if not member.isfile():
                raise TorIntegrityError(f"Archive member is not a regular file: {member.name}")
            target = _check_member_name(bundle, member.name)
            if target.is_symlink() or not target.is_file():
                raise TorIntegrityError(f"Installed bundle file is missing or not regular: {member.name}")
            with archive.extractfile(member) as original:
                if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(original.read()).digest():
                    raise TorIntegrityError(f"Installed file differs from the verified archive: {member.name}")
            expected.add(str(target.relative_to(bundle)).replace("\\", "/"))

    actual = {
        str(path.relative_to(bundle)).replace("\\", "/")
        for path in bundle.rglob("*")
        if path.is_symlink() or not path.is_dir()
    }
    if actual != expected:
        raise TorIntegrityError("Unexpected or missing file in the Tor bundle directory")

    executable = bundle / "tor" / ("tor.exe" if platform.system() == "Windows" else "tor")
    if not executable.is_file():
        raise TorIntegrityError("Verified bundle lacks the Tor executable")
    return executable


def download_tor(config: dict[str, Any]) -> Path | None:
    """Download and install the pinned Tor Expert Bundle. Returns path to tor binary.

    The archive digest must match TOR_HASHES before anything is extracted, and
    extraction lands in a staging directory that is renamed into place only
    after every member checks out -- a partial or tampered download never
    becomes the binary we execute (#71 item 1).
    """
    try:
        version_path, filename = _download_url()
    except ValueError as e:
        log.error(str(e))
        return None

    bundle = _bundle_dir(config)
    if bundle.exists():
        try:
            return _verify_bundle(bundle)
        except TorIntegrityError as e:
            log.error("Existing Tor bundle failed verification: %s", e)
            return None

    tor_dir = _tor_dir(config)
    tor_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for leftover in tor_dir.glob(f"bundle-{TOR_VERSION}.staging-*"):
        # Stale only: a fresh staging directory may belong to a concurrent
        # download in another process.
        try:
            if now - leftover.stat().st_mtime > 3600:
                shutil.rmtree(leftover, ignore_errors=True)
        except OSError:
            pass
    staging = Path(tempfile.mkdtemp(dir=tor_dir, prefix=f"bundle-{TOR_VERSION}.staging-"))
    try:
        from .network import configured_proxy, thread_session

        proxy = configured_proxy(config)
        proxies = {"http": proxy, "https": proxy} if proxy else None
        session = thread_session(proxies)

        for mirror in TOR_DOWNLOAD_MIRRORS:
            url = f"{mirror}/{version_path}/{filename}"
            try:
                log.info(f"Downloading Tor from {url}")
                digest = hashlib.sha256()
                archive_path = staging / _BUNDLE_ARCHIVE
                with session.get(url, stream=True, timeout=(10, 300)) as resp:
                    if resp.status_code != 200:
                        log.warning(f"Mirror returned {resp.status_code}: {url}")
                        continue
                    total = int(resp.headers.get("content-length", 0))
                    downloaded = 0
                    with archive_path.open("wb") as fh:
                        for chunk in resp.iter_content(64 * 1024):
                            fh.write(chunk)
                            digest.update(chunk)
                            downloaded += len(chunk)
                            if downloaded > _MAX_ARCHIVE_BYTES:
                                raise TorIntegrityError(
                                    "Tor archive exceeds any plausible pinned bundle size"
                                )
                            if total and downloaded % (1024 * 1024) < 65536:
                                log.info(f"  Downloaded {downloaded / 1024 / 1024:.1f}/{total / 1024 / 1024:.1f} MB")

                if digest.hexdigest() != _bundle_digest(filename):
                    log.warning(f"Digest mismatch, discarding download from {url}")
                    archive_path.unlink(missing_ok=True)
                    continue

                # Re-reads and re-hashes the file before extracting from the
                # same in-memory copy, so the promoted bytes are the pinned
                # bytes.
                _extract_verified(archive_path, staging, _bundle_digest(filename))

                try:
                    bundle = staging.rename(_bundle_dir(config))
                except OSError:
                    # Another process won the install race; use its result.
                    winner = _verify_bundle(_bundle_dir(config))
                    log.info(f"Tor bundle already installed by another process: {winner}")
                    return winner

                executable = bundle / "tor" / ("tor.exe" if platform.system() == "Windows" else "tor")
                if platform.system() != "Windows":
                    executable.chmod(executable.stat().st_mode | stat.S_IEXEC)
                log.info(f"Tor installed: {executable}")
                return executable

            except TorIntegrityError as e:
                # A digest-matching archive that fails structural verification
                # means the pin itself points at hostile bytes -- that is a
                # release compromise, not a flaky mirror, and must not be
                # retried elsewhere or parked in the failure backoff.
                log.error(f"Pinned Tor archive from {url} failed structural verification: {e}")
                return None
            except Exception as e:
                log.warning(f"Failed to download from {mirror}: {e}")
                continue

        log.error("Failed to download a verifiable Tor bundle from all mirrors")
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)


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

    torrc_path.parent.mkdir(parents=True, exist_ok=True)
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

        # Warn when something else already holds our port — typically a
        # foreign/orphaned tor from a recycled MCP server process. Our own
        # tor would then die on bind and start() reports failure while the
        # proxy actually still works on that port (#61).
        import socket as _socket
        try:
            with _socket.create_connection(("127.0.0.1", self.socks_port), timeout=1):
                log.warning(
                    f"Tor: port {self.socks_port} is already occupied by another process — "
                    "if this is an old embedded Tor, either reuse it via tor_proxy "
                    f"({self.proxy_url}) or stop it before starting a new instance"
                )
        except OSError:
            pass

        # torrc is generated state; it lives outside the verified bundle so
        # _verify_bundle's strict directory check keeps holding (#71 item 1).
        torrc = _write_torrc(_runtime_dir(self.config), self.socks_port, self.use_bridges)

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
_tor_unavailable_since: float = 0.0  # timestamp when Tor last failed
_TOR_UNAVAILABLE_TTL = 3600  # 1 hour before retrying Tor download


def get_embedded_tor(config: dict[str, Any]) -> EmbeddedTor | None:
    """Get or create a global embedded Tor instance.

    If Tor download previously failed, skips retry for 1 hour to avoid
    blocking on unreachable torproject.org mirrors.
    """
    global _embedded_tor, _tor_unavailable_since
    with _tor_lock:
        if _embedded_tor and _embedded_tor.is_running():
            return _embedded_tor
        # Fast skip: Tor download previously failed within the TTL
        if _tor_unavailable_since and (time.time() - _tor_unavailable_since) < _TOR_UNAVAILABLE_TTL:
            return None

        use_bridges = config.get("tor_use_bridges", False)
        tor = EmbeddedTor(config, use_bridges=use_bridges)
        if tor.start():
            _embedded_tor = tor
            _tor_unavailable_since = 0.0  # reset on success
            return tor
        _tor_unavailable_since = time.time()  # cache failure timestamp
        return None


def running_embedded_tor() -> "EmbeddedTor | None":
    """Return the embedded instance only if it is ALREADY running.

    Unlike get_embedded_tor this never starts Tor or downloads a binary —
    safe to call from a health probe that must stay side-effect free (#61).
    """
    with _tor_lock:
        if _embedded_tor and _embedded_tor.is_running():
            return _embedded_tor
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
    try:
        binary = _tor_binary(config)
    except TorIntegrityError as e:
        log.error("Existing Tor bundle failed verification: %s", e)
        return {
            "installed": False,
            "error": f"Installed Tor bundle failed verification ({e}); "
            f"remove {_bundle_dir(config)} and install again",
        }
    if binary:
        return {"installed": True, "path": str(binary), "message": "Tor already installed"}

    binary = download_tor(config)
    if binary:
        return {"installed": True, "path": str(binary), "message": f"Tor installed to {binary}"}
    return {"installed": False, "error": "Failed to download Tor. Check network connectivity."}
