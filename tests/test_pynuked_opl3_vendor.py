from __future__ import annotations

from pathlib import Path

import pytest

import pynuked_opl3


ROOT = Path(__file__).resolve().parents[1]


def test_vendored_pynuked_opl3_package_is_present_without_build_artifacts():
    """The vendored package ships source-only: built extensions may exist
    locally (``python -m pynuked_opl3._ffi_build`` builds in-package — the
    relative import requires it there — and .gitignore covers the result)
    but must never be committed."""
    pkg = ROOT / "pynuked_opl3"
    assert (pkg / "__init__.py").is_file()
    assert (pkg / "_ffi_build.py").is_file()
    assert (pkg / "vendor" / "opl3.c").is_file()
    assert (pkg / "vendor" / "opl3.h").is_file()
    assert (pkg / "LICENSE").is_file()
    import subprocess
    tracked = subprocess.run(
        ["git", "ls-files", "pynuked_opl3"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout
    for ext in (".pyd", ".so", ".dylib"):
        assert not [line for line in tracked.splitlines()
                    if "_opl3_cffi" in line and line.endswith(ext)], (
            f"built extension ({ext}) must not be committed"
        )


def test_vendored_pynuked_opl3_import_is_lazy_until_extension_is_built():
    assert hasattr(pynuked_opl3, "OPL3")
    assert isinstance(pynuked_opl3.is_available(), bool)
    if not pynuked_opl3.is_available():
        with pytest.raises(pynuked_opl3.NukedOpl3Unavailable):
            pynuked_opl3.OPL3()
