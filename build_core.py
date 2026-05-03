"""Build script for compiling Cython extensions.

Compiles .pyx files in src/scansci_pdf/_core/ to .pyd (Windows) or .so (Linux/macOS).
Run this before publishing to PyPI to include compiled binaries.

Usage:
    python build_core.py

Requires:
    pip install cython>=3.0
"""

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def build():
    core_dir = Path(__file__).parent / "src" / "scansci_pdf" / "_core"

    if not core_dir.exists():
        print(f"ERROR: {core_dir} not found")
        sys.exit(1)

    pyx_files = list(core_dir.glob("*.pyx"))
    if not pyx_files:
        print("ERROR: No .pyx files found in _core/")
        sys.exit(1)

    print(f"Found {len(pyx_files)} .pyx files: {[f.name for f in pyx_files]}")

    # Check Cython is available
    try:
        import Cython  # noqa: F401
        print(f"Cython version: {Cython.__version__}")
    except ImportError:
        print("ERROR: Cython not installed. Run: pip install cython>=3.0")
        sys.exit(1)

    ext_suffix = ".pyd" if platform.system() == "Windows" else ".so"
    built = []

    for pyx_file in pyx_files:
        module_name = pyx_file.stem
        print(f"\nCompiling {pyx_file.name}...")

        # Use cythonize to compile
        cmd = [
            sys.executable, "-c",
            f"from Cython.Build import cythonize; "
            f"cythonize('{pyx_file.as_posix()}', compiler_directives={{'language_level': 3}})",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  Cython compilation failed: {result.stderr}")
            continue

        # Compile C to shared library
        c_file = pyx_file.with_suffix(".c")
        if not c_file.exists():
            print(f"  ERROR: {c_file} not generated")
            continue

        # Find Python include and lib dirs
        import sysconfig
        include_dir = sysconfig.get_path("include")
        lib_dir = sysconfig.get_config_var("LIBDIR") or ""
        platlib_dir = sysconfig.get_path("platlib") or ""

        if platform.system() == "Windows":
            # MSVC compilation
            out_file = core_dir / f"{module_name}{ext_suffix}"
            cmd = [
                "cl", "/nologo", "/O2", "/MD", "/W3",
                f"/I{include_dir}",
                str(c_file),
                f"/Fe:{out_file}",
                "/link",
                f"/LIBPATH:{platlib_dir}",
                f"python{sys.version_info.major}{sys.version_info.minor}.lib",
            ]
        else:
            # GCC/Clang compilation
            out_file = core_dir / f"{module_name}{ext_suffix}"
            cmd = [
                "gcc", "-shared", "-fPIC", "-O2",
                f"-I{include_dir}",
                str(c_file),
                "-o", str(out_file),
                f"-L{lib_dir}",
                f"-lpython{sys.version_info.major}.{sys.version_info.minor}",
            ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  OK: {out_file.name} ({out_file.stat().st_size / 1024:.1f} KB)")
            built.append(out_file.name)
        else:
            print(f"  Compilation failed: {result.stderr[:200]}")

    # Clean up .c files
    for c_file in core_dir.glob("*.c"):
        c_file.unlink()

    print(f"\nBuilt {len(built)}/{len(pyx_files)} extensions: {built}")
    if len(built) == len(pyx_files):
        print("All extensions compiled successfully!")
    else:
        print("WARNING: Some extensions failed to compile.")
        sys.exit(1)


if __name__ == "__main__":
    build()
