#!/usr/bin/env python3
"""Audit selected native implementation layers for machine-runtime leakage.

Use this when an implementation declares that it is detached from the machine
runtime: no ``dos_re`` imports, no ``cpu``/``mem`` names or CPU/Memory types,
and no bare memory-layout constants. The rule applies only to directories
assigned that property; DOS-memory-backed implementations remain valid.

Usage:
    python tools/audit_layers.py <pure_dir> [<pure_dir> ...]
                                 [--forbid PKG]... [--layout-const 0xADDR]...

e.g. (from an adapter repo):
    python tools/audit_layers.py mygame/recovered \\
        --forbid mygame.hooks --forbid mygame.bridge \\
        --layout-const 0x1010 --layout-const 0x2B5C

``--forbid`` adds forbidden import prefixes beyond the default (``dos_re``).
``--layout-const`` registers segment/table-base addresses that must not appear
as bare literals in pure code; a genuinely-justified value may carry a
``# layout-justified`` comment on its line. Run it with your test suite.

"""
from __future__ import annotations

import argparse
import ast
import pathlib
from dataclasses import dataclass

FORBIDDEN_NAMES = {"cpu", "mem", "memory"}
# Capitalised VM/CPU *types* must not be referenced (annotations or bare names)
# in the pure layers either: the future native core never sees these.
FORBIDDEN_TYPE_NAMES = {"CPU", "CPU8086", "CPUState", "Memory", "Mem", "Registers", "Register", "Runtime", "DOSMachine"}
LAYOUT_JUSTIFIED_MARKER = "layout-justified"


@dataclass(frozen=True)
class Issue:
    path: pathlib.Path
    lineno: int
    message: str


def _import_targets(node: ast.Import | ast.ImportFrom) -> list[tuple[int, str]]:
    if isinstance(node, ast.Import):
        return [(node.lineno, alias.name) for alias in node.names]
    if node.level:
        # Relative imports within the pure layer are allowed; a repo lint checks
        # that they resolve.  Avoid guessing package names here.
        return []
    if node.module is None:
        return []
    return [(node.lineno, node.module)]


class PureLayerVisitor(ast.NodeVisitor):
    def __init__(self, path: pathlib.Path, source_lines: list[str],
                 forbidden_imports: tuple[str, ...], layout_constants: set[int]) -> None:
        self.path = path
        self.source_lines = source_lines
        self.forbidden_imports = forbidden_imports
        self.layout_constants = layout_constants
        self.issues: list[Issue] = []

    def _is_forbidden_import(self, target: str) -> bool:
        return any(target == f or target.startswith(f + ".") for f in self.forbidden_imports)

    def visit_Import(self, node: ast.Import) -> None:
        for lineno, target in _import_targets(node):
            if self._is_forbidden_import(target):
                self.issues.append(Issue(self.path, lineno, f"pure layer must not import {target!r}"))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for lineno, target in _import_targets(node):
            if self._is_forbidden_import(target):
                self.issues.append(Issue(self.path, lineno, f"pure layer must not import {target!r}"))

    def visit_arg(self, node: ast.arg) -> None:
        if node.arg in FORBIDDEN_NAMES:
            self.issues.append(Issue(self.path, node.lineno, f"pure layer argument {node.arg!r} looks VM-bound"))
        self.generic_visit(node)  # annotations on the argument (e.g. ``x: CPU``)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_NAMES:
            self.issues.append(Issue(self.path, node.lineno, f"pure layer name {node.id!r} looks VM-bound"))
        elif node.id in FORBIDDEN_TYPE_NAMES:
            self.issues.append(Issue(self.path, node.lineno, f"pure layer references VM/CPU type {node.id!r}"))

    def _line_is_layout_justified(self, lineno: int) -> bool:
        if 1 <= lineno <= len(self.source_lines):
            return LAYOUT_JUSTIFIED_MARKER in self.source_lines[lineno - 1]
        return False

    def visit_Constant(self, node: ast.Constant) -> None:
        if (isinstance(node.value, int) and not isinstance(node.value, bool)
                and node.value in self.layout_constants
                and not self._line_is_layout_justified(node.lineno)):
            self.issues.append(Issue(
                self.path, node.lineno,
                f"pure layer uses memory-layout constant {node.value:#06x} "
                f"(add a '# {LAYOUT_JUSTIFIED_MARKER}' comment if this is a real domain value)",
            ))


def audit_file(path: pathlib.Path, forbidden_imports: tuple[str, ...],
               layout_constants: set[int]) -> list[Issue]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    visitor = PureLayerVisitor(path, source.splitlines(), forbidden_imports, layout_constants)
    visitor.visit(tree)
    return visitor.issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", help="pure-layer directories to audit")
    parser.add_argument("--forbid", action="append", default=[],
                        help="forbidden import prefix (repeatable); dos_re is always forbidden")
    parser.add_argument("--layout-const", action="append", default=[], metavar="0xADDR",
                        help="memory-layout constant that must not appear bare (repeatable)")
    args = parser.parse_args(argv)

    forbidden = tuple(dict.fromkeys(["dos_re", *args.forbid]))
    layout_constants = {int(v, 0) for v in args.layout_const}

    files: list[pathlib.Path] = []
    for root in args.roots:
        root_path = pathlib.Path(root)
        if not root_path.is_dir():
            print(f"audit_layers: no such directory: {root_path}")
            return 2
        files.extend(p for p in sorted(root_path.rglob("*.py")) if "__pycache__" not in p.parts)

    issues: list[Issue] = []
    for path in files:
        issues.extend(audit_file(path, forbidden, layout_constants))
    if issues:
        print("PURE LAYER AUDIT FAILED")
        for issue in sorted(issues, key=lambda item: (str(item.path), item.lineno, item.message)):
            print(f"{issue.path}:{issue.lineno}: {issue.message}")
        return 1
    print(f"pure layer audit passed for {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
