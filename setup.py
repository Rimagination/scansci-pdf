"""Build script for Cython compilation and data encryption.

Usage:
    python setup.py build_ext --inplace    # Build .pyd/.so in-place
    python setup.py bdist_wheel            # Build wheel with compiled extensions
"""

import os
from pathlib import Path
from setuptools import Extension, setup
from setuptools.command.build_py import build_py


class BuildPyWithEncryption(build_py):
    """Custom build_py that encrypts webvpn.json -> webvpn.dat."""

    def run(self):
        self._encrypt_webvpn_data()
        super().run()

    def _encrypt_webvpn_data(self):
        """Encrypt webvpn.json into webvpn.dat for IP protection."""
        data_dir = Path("src/scansci_pdf/data")
        json_file = data_dir / "webvpn.json"
        dat_file = data_dir / "webvpn.dat"

        if not json_file.exists():
            print(f"  [encrypt] {json_file} not found, skipping encryption")
            return

        # Read plaintext JSON
        plaintext = json_file.read_bytes()
        print(f"  [encrypt] Encrypting {json_file} ({len(plaintext)} bytes)...")

        # Try to use compiled core for encryption
        try:
            from scansci_pdf._core.instsci_core import encrypt_data
            encrypted = encrypt_data(plaintext)
        except ImportError:
            # Fallback: use pycryptodome directly (for first build before .pyx is compiled)
            key = bytes.fromhex("9cad336032fda4cec3eb0d39740cf8cef9f1a9a65475c0b28c04642ab5c1323a")
            iv = b"scansci_pdf_v1_dat"[:16].ljust(16, b'\0')
            from Crypto.Cipher import AES
            pad_len = 16 - (len(plaintext) % 16)
            padded = plaintext + bytes([pad_len] * pad_len)
            cipher = AES.new(key, AES.MODE_CBC, iv)
            encrypted = cipher.encrypt(padded)

        dat_file.write_bytes(encrypted)
        print(f"  [encrypt] Written {dat_file} ({len(encrypted)} bytes)")


# Determine if Cython is available for compilation
try:
    from Cython.Build import cythonize

    extensions = [
        Extension(
            "scansci_pdf._core.instsci_core",
            ["src/scansci_pdf/_core/instsci_core.pyx"],
        ),
        Extension(
            "scansci_pdf._core.scihub_core",
            ["src/scansci_pdf/_core/scihub_core.pyx"],
        ),
        Extension(
            "scansci_pdf._core.racing",
            ["src/scansci_pdf/_core/racing.pyx"],
        ),
    ]

    setup(
        ext_modules=cythonize(
            extensions,
            compiler_directives={
                "language_level": "3",
                "boundscheck": False,
                "wraparound": False,
            },
        ),
        cmdclass={"build_py": BuildPyWithEncryption},
    )
except ImportError:
    # Cython not installed - pure Python fallback
    setup(
        cmdclass={"build_py": BuildPyWithEncryption},
    )
