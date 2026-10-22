"""Build script for Cython compilation.

Usage:
    python setup.py build_ext --inplace    # Build .pyd/.so in-place
    python setup.py bdist_wheel            # Build wheel with compiled extensions
"""

from setuptools import Extension, setup


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
    )
except ImportError:
    # Cython not installed - pure Python fallback
    setup()
