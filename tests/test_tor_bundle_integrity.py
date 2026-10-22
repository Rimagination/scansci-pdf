"""Integrity contract for embedded Tor downloads and installs (#71 item 1).

Everything here runs against in-memory fake archives; no test touches a
mirror. The pins under test are monkeypatched to the fixture archive's real
digest, so the code path exercised is the one a genuine download takes.
"""

from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from scansci_pdf import embedded_tor

WINDOWS = {"system": "Windows", "machine": "AMD64"}

_TOR_DIR_ENTRY = tarfile.TarInfo("tor/")
_TOR_DIR_ENTRY.type = tarfile.DIRTYPE

# Directory entries like the real Expert Bundle ships; the extractor must
# skip them, not refuse them.
NORMAL_MEMBERS = [
    _TOR_DIR_ENTRY,
    ("tor/tor.exe", b"fake tor binary"),
    ("tor/obfs4proxy.exe", b"fake obfs4proxy"),
]


def _as_platform(monkeypatch, system, machine):
    monkeypatch.setattr(embedded_tor.platform, "system", lambda: system)
    monkeypatch.setattr(embedded_tor.platform, "machine", lambda: machine)


def _tar_bytes(members):
    """Build an archive from (name, payload) pairs and raw TarInfo entries."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for item in members:
            if isinstance(item, tarfile.TarInfo):
                tf.addfile(item)
            else:
                name, data = item
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, payload, status_code):
        self.status_code = status_code
        self.headers = {"content-length": str(len(payload))}
        self._payload = payload

    def iter_content(self, chunk_size):
        for i in range(0, len(self._payload), chunk_size):
            yield self._payload[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeSession:
    """Serves each queued item once; items are bytes or (status, bytes).

    An empty queue answers 404, so a test that wants no further downloads
    simply passes [].
    """

    def __init__(self, payloads):
        self._payloads = list(payloads)

    def get(self, url, stream=True, timeout=None):
        if not self._payloads:
            return _FakeResponse(b"", 404)
        item = self._payloads.pop(0)
        status, payload = item if isinstance(item, tuple) else (200, item)
        return _FakeResponse(payload, status)


def _serve(monkeypatch, payloads):
    monkeypatch.delenv("SCANSCI_PDF_PROXY", raising=False)
    monkeypatch.setattr("scansci_pdf.network.thread_session", lambda proxies=None: _FakeSession(payloads))


def _install_fake_bundle(tmp_path, monkeypatch, members=None, payloads=None):
    """Download the fake archive through the real download_tor path."""
    _as_platform(monkeypatch, WINDOWS["system"], WINDOWS["machine"])
    payload = _tar_bytes(members if members is not None else NORMAL_MEMBERS)
    monkeypatch.setitem(
        embedded_tor.TOR_HASHES, "windows-x86_64", hashlib.sha256(payload).hexdigest()
    )
    _serve(monkeypatch, payloads if payloads is not None else [payload])
    config = {"cache_dir": str(tmp_path / "cache")}
    return config, embedded_tor.download_tor(config)


@pytest.mark.parametrize("system,machine,slug", [
    ("Windows", "AMD64", "windows-x86_64"),
    ("Windows", "x86", "windows-x86_64"),
    ("Darwin", "arm64", "macos-aarch64"),
    ("Darwin", "x86_64", "macos-x86_64"),
    ("Linux", "x86_64", "linux-x86_64"),
    ("Linux", "i686", "linux-x86_64"),
])
def test_every_download_target_is_pinned(monkeypatch, system, machine, slug):
    _as_platform(monkeypatch, system, machine)
    version, filename = embedded_tor._download_url()
    assert filename == f"tor-expert-bundle-{slug}-{version}.tar.gz"
    assert slug in embedded_tor.TOR_HASHES


def test_an_unpinned_platform_fails_closed_instead_of_downloading(monkeypatch):
    # Tor ships no linux-aarch64 Expert Bundle in this release; the downloader
    # must say so rather than fetch an archive it has no pin for.
    _as_platform(monkeypatch, "Linux", "aarch64")
    with pytest.raises(ValueError):
        embedded_tor._download_url()


def test_a_download_that_fails_the_pin_is_discarded(tmp_path, monkeypatch):
    payload = _tar_bytes(NORMAL_MEMBERS)
    monkeypatch.setitem(embedded_tor.TOR_HASHES, "windows-x86_64", "0" * 64)
    _serve(monkeypatch, [payload, payload])  # both mirrors serve the bad bytes
    config = {"cache_dir": str(tmp_path / "cache")}
    assert embedded_tor.download_tor(config) is None
    assert not embedded_tor._bundle_dir(config).exists()
    assert list(tmp_path.rglob("bundle-*")) == []  # no staging leftovers


def test_a_verified_download_installs_and_reverifies(tmp_path, monkeypatch):
    config, binary = _install_fake_bundle(tmp_path, monkeypatch)
    assert binary is not None
    assert binary.name == "tor.exe"
    assert binary.is_file()
    assert (embedded_tor._bundle_dir(config) / ".bundle.tar.gz").is_file()
    # The installed bundle passes the normal lookup path, which re-verifies.
    assert embedded_tor._tor_binary(config) == binary


def test_a_tampered_installed_file_fails_closed(tmp_path, monkeypatch):
    config, binary = _install_fake_bundle(tmp_path, monkeypatch)
    assert embedded_tor._tor_binary(config) == binary  # clean before tampering
    binary.write_bytes(b"replaced after install")
    with pytest.raises(embedded_tor.TorIntegrityError):
        embedded_tor._tor_binary(config)


def test_an_extra_file_in_the_bundle_fails_closed(tmp_path, monkeypatch):
    config, binary = _install_fake_bundle(tmp_path, monkeypatch)
    assert embedded_tor._tor_binary(config) == binary  # clean before tampering
    (embedded_tor._bundle_dir(config) / "extra.txt").write_text("dropped file")
    with pytest.raises(embedded_tor.TorIntegrityError):
        embedded_tor._tor_binary(config)


def test_a_symlink_member_is_refused(tmp_path, monkeypatch):
    link = tarfile.TarInfo("tor/tor")
    link.type = tarfile.SYMTYPE
    link.linkname = "/tmp/evil"
    config, binary = _install_fake_bundle(
        tmp_path, monkeypatch,
        members=[("tor/real", b"x"), link],
        payloads=None,
    )
    assert binary is None
    assert not embedded_tor._bundle_dir(config).exists()


def test_a_symlink_member_is_structurally_refused(tmp_path):
    # Mechanism-level: the downloader returning None could also mean a
    # network failure; this pins the structural refusal itself.
    link = tarfile.TarInfo("tor/evil")
    link.type = tarfile.SYMTYPE
    link.linkname = "/tmp/evil"
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(_tar_bytes([("tor/tor.exe", b"x"), link]))
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(embedded_tor.TorIntegrityError, match="not a regular file"):
        embedded_tor._extract_verified(
            archive, staging, hashlib.sha256(archive.read_bytes()).hexdigest()
        )


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "C:/evil", "tor/NUL", "", "."])
def test_hostile_member_names_are_refused_upfront(tmp_path, name):
    with pytest.raises(embedded_tor.TorIntegrityError):
        embedded_tor._check_member_name(tmp_path, name)


def test_a_path_traversal_member_is_refused(tmp_path, monkeypatch):
    config, binary = _install_fake_bundle(
        tmp_path, monkeypatch,
        members=[("tor/tor.exe", b"x"), ("../escape", b"evil")],
    )
    assert binary is None
    assert not embedded_tor._bundle_dir(config).exists()
    assert not (tmp_path / "escape").exists()


def test_directory_members_are_skipped_not_refused(tmp_path):
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(_tar_bytes(NORMAL_MEMBERS))
    staging = tmp_path / "staging"
    staging.mkdir()
    embedded_tor._extract_verified(
        archive, staging, hashlib.sha256(archive.read_bytes()).hexdigest()
    )
    assert (staging / "tor" / "tor.exe").is_file()


def test_a_tampered_cached_archive_fails_closed(tmp_path, monkeypatch):
    config, binary = _install_fake_bundle(tmp_path, monkeypatch)
    assert embedded_tor._tor_binary(config) == binary  # clean before tampering
    archive = embedded_tor._bundle_dir(config) / ".bundle.tar.gz"
    data = bytearray(archive.read_bytes())
    data[-1] ^= 0xFF
    archive.write_bytes(bytes(data))
    with pytest.raises(embedded_tor.TorIntegrityError, match="Cached Tor archive"):
        embedded_tor._tor_binary(config)


def test_a_second_download_reverifies_instead_of_downloading(tmp_path, monkeypatch):
    config, binary = _install_fake_bundle(tmp_path, monkeypatch)
    _serve(monkeypatch, [])  # every mirror 404s; only the cache can answer
    assert embedded_tor.download_tor(config) == binary
    binary.write_bytes(b"tampered")
    # The install entry returns None on a tampered cache; it must not fall
    # through to PATH or re-download silently.
    assert embedded_tor.download_tor(config) is None


def test_a_failed_mirror_falls_through_to_the_next(tmp_path, monkeypatch):
    _as_platform(monkeypatch, WINDOWS["system"], WINDOWS["machine"])
    payload = _tar_bytes(NORMAL_MEMBERS)
    monkeypatch.setitem(
        embedded_tor.TOR_HASHES, "windows-x86_64", hashlib.sha256(payload).hexdigest()
    )
    _serve(monkeypatch, [(404, b""), payload])  # first mirror down
    config = {"cache_dir": str(tmp_path / "cache")}
    assert embedded_tor.download_tor(config) is not None


def test_a_digest_mismatch_on_one_mirror_does_not_poison_the_next(tmp_path, monkeypatch):
    _as_platform(monkeypatch, WINDOWS["system"], WINDOWS["machine"])
    good = _tar_bytes(NORMAL_MEMBERS)
    wrong = _tar_bytes([("tor/tor.exe", b"other bytes")])
    monkeypatch.setitem(
        embedded_tor.TOR_HASHES, "windows-x86_64", hashlib.sha256(good).hexdigest()
    )
    _serve(monkeypatch, [wrong, good])  # first mirror serves bad bytes
    config = {"cache_dir": str(tmp_path / "cache")}
    assert embedded_tor.download_tor(config) is not None


def test_every_pin_round_trips_through_the_filename():
    # Bumping TOR_VERSION without refreshing the pins, or renaming a slug and
    # leaving a stale key, must fail here rather than as a KeyError after a
    # full download.
    for slug, digest in embedded_tor.TOR_HASHES.items():
        filename = f"tor-expert-bundle-{slug}-{embedded_tor.TOR_VERSION}.tar.gz"
        assert embedded_tor._bundle_digest(filename) == digest


def test_the_pre_verification_layout_is_never_executed(tmp_path, monkeypatch):
    _as_platform(monkeypatch, WINDOWS["system"], WINDOWS["machine"])
    monkeypatch.setattr(embedded_tor.shutil, "which", lambda name: None)
    config = {"cache_dir": str(tmp_path / "cache")}
    legacy = tmp_path / "tor" / "tor" / "tor.exe"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"old unverified tor")
    assert embedded_tor._tor_binary(config) is None


def test_path_tor_is_still_the_last_resort(tmp_path, monkeypatch):
    _as_platform(monkeypatch, WINDOWS["system"], WINDOWS["machine"])
    fake = tmp_path / "system-tor.exe"
    fake.write_bytes(b"system tor")
    monkeypatch.setattr(embedded_tor.shutil, "which", lambda name: str(fake))
    config = {"cache_dir": str(tmp_path / "cache")}
    assert embedded_tor._tor_binary(config) == fake


def test_install_tor_reports_a_tampered_bundle(tmp_path, monkeypatch):
    config, binary = _install_fake_bundle(tmp_path, monkeypatch)
    binary.write_bytes(b"replaced after install")
    result = embedded_tor.install_tor(config)
    assert result["installed"] is False
    assert "failed verification" in result["error"]


def test_start_writes_torrc_outside_the_verified_bundle(tmp_path, monkeypatch):
    config, binary = _install_fake_bundle(tmp_path, monkeypatch)
    captured = {}

    class _FakePopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(embedded_tor.subprocess, "Popen", _FakePopen)
    # CREATE_NO_WINDOW only exists on Windows; with the platform patched to
    # Windows on a POSIX host, start() would otherwise die on the attribute.
    monkeypatch.setattr(embedded_tor.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    monkeypatch.setattr("socket.create_connection", lambda *a, **k: _Conn())

    tor = embedded_tor.EmbeddedTor(config)
    assert tor.start() is True

    cmd = captured["cmd"]
    assert cmd[0] == str(binary)
    torrc = embedded_tor._runtime_dir(config) / "torrc"
    assert torrc.is_file()
    assert cmd[cmd.index("-f") + 1] == str(torrc)
    bundle_files = {
        p.relative_to(embedded_tor._bundle_dir(config)).as_posix()
        for p in embedded_tor._bundle_dir(config).rglob("*")
        if p.is_file()
    }
    assert "torrc" not in bundle_files
