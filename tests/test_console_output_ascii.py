"""Console/log output must be ASCII-clean.

Windows consoles frequently decode program output as a legacy codepage (CP850/
CP1250), turning UTF-8 punctuation into mojibake — an em-dash printed by a tool
arrives as ``ÔÇö`` in the log.  Docstrings and comments never reach the console,
but every OTHER string literal can: runtime messages, argparse help, exception
text, and the templates written into generated files.

The rule enforced here: NON-DOCSTRING string literals in the framework and its
tools contain only ASCII.  Use ``--`` for an em-dash, ``...`` for an ellipsis,
``section`` for the section sign.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCAN_DIRS = [REPO / "dos_re", REPO / "tools"]


def _docstring_ids(tree: ast.Module) -> set[int]:
    ds: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                ds.add(id(node.body[0].value))
    return ds


def test_non_docstring_string_literals_are_ascii():
    offenders = []
    for d in SCAN_DIRS:
        for p in d.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            ds = _docstring_ids(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and id(node) not in ds
                        and any(ord(c) > 127 for c in node.value)):
                    bad = sorted({c for c in node.value if ord(c) > 127})
                    offenders.append(
                        f"{p.relative_to(REPO)}:{node.lineno} contains "
                        f"{[hex(ord(c)) for c in bad]}")
    assert not offenders, (
        "non-ASCII in printable string literals (mojibake on Windows consoles); "
        "use ASCII equivalents (-- ... section):\n  " + "\n  ".join(offenders))
