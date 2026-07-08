#!/usr/bin/env python3
"""Structural lint for the dos_re framework repo.

Two checks:

1. Every Python file parses (syntax).
2. The framework core stays game-agnostic and dependency-free: ``dos_re/``
   may import only the Python stdlib and other ``dos_re`` modules.  Anything
   that knows a specific game's addresses, filenames, or formats belongs in a
   game adapter built *on top of* this repo, never inside ``dos_re/``.

Game adapters that vendor this framework should extend PACKAGE_ROOTS with
their own package and add a rule that ``dos_re`` does not import it (see the
pre2_port original: scripts/lint.py).
"""
from __future__ import annotations

import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOTS = (ROOT / "dos_re", ROOT / "tools", ROOT / "examples", ROOT / "tests")

# Modules the framework core is allowed to import besides the stdlib.
CORE_ALLOWED_PREFIXES = ("dos_re",)

# Optional third-party backends the *non-core* layers may use.
KNOWN_OPTIONAL = ("pynuked_opl3", "numpy", "pygame", "pytest", "cffi")


def _stdlib_names() -> set[str]:
    return set(sys.stdlib_module_names)


def iter_py_files():
    for root in PACKAGE_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" not in p.parts:
                yield p


def main() -> int:
    stdlib = _stdlib_names()
    errors: list[str] = []
    for path in iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(ROOT)}: syntax error: {exc}")
            continue
        if not path.is_relative_to(ROOT / "dos_re"):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import stays inside dos_re
                    continue
                if node.module:
                    names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in stdlib or any(name == p or name.startswith(p + ".") for p in CORE_ALLOWED_PREFIXES):
                    continue
                errors.append(
                    f"{path.relative_to(ROOT)}:{node.lineno}: dos_re core must stay "
                    f"stdlib-only and game-agnostic; imports {name!r}"
                )
    if errors:
        print("lint failed:")
        for err in errors:
            print("  " + err)
        return 1
    print("lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
