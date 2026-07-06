"""Guard against the NameError class of latent bugs: a name used in a never-exercised branch that was never
imported or defined. A lightweight AST check — no external linter dependency — over the framework package.

It over-approximates "defined" (module imports + every def/class + every assigned name anywhere + all arg
names + comprehension/except/with targets + builtins), so it only flags names defined *nowhere* in the
module — which is virtually always a real NameError waiting in an unexercised path.

Game adapters built on this framework should point the same check at their own recovered layers
(see the pre2_port original: tests/test_no_undefined_names.py scanning recovered/checkpoints/bridge/codecs).
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# the shipped framework package (examples are scanned too — they must stay importable)
DIRS = ("dos_re", "examples")
_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__class__", "__all__", "self", "cls"}


def _modules():
    files: list[Path] = []
    for d in DIRS:
        root = ROOT / d
        if root.exists():
            files += sorted(root.rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


def _undefined(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    defined = set(_BUILTINS)
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                defined.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            defined.add(n.id)
        elif isinstance(n, ast.arg):
            defined.add(n.arg)
        elif isinstance(n, ast.Global):
            defined.update(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
        elif isinstance(n, ast.withitem) and isinstance(n.optional_vars, ast.Name):
            defined.add(n.optional_vars.id)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(used - defined)


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_undefined_names(path):
    undefined = _undefined(path)
    assert not undefined, f"{path.relative_to(ROOT)}: undefined name(s) {undefined} (unimported/undefined — a NameError in some branch)"
