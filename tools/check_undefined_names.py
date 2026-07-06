"""Static guard against the undefined-name bug class (Python F821).

Usage:
    python tools/check_undefined_names.py [package_dir]     # default: dos_re

``tools/lint.py`` checks imports and layering but not name *resolution*, so a
reference to a name that is never bound -- a forgotten import, a copy/paste that drops
the local binding -- sails through until that branch happens to run.  In the Overkill
port (where this tool originates) two such latent ``NameError`` landmines shipped this
way, each hidden in a recovered-code path with no oracle coverage.

This scanner walks every function with proper per-scope binding (parameters, local
assignments, ``for`` / ``with`` / ``except`` / comprehension targets, nested-def names,
``global`` / ``nonlocal`` declarations, enclosing-function closures, module globals,
builtins) and flags any ``Load`` of a name bound in none of them.  ``from X import *`` is
resolved by importing ``X`` and reading its real exports (``__all__`` or public ``dir``),
which is why this lives alongside lint's import machinery rather than guessing
statically -- several modules build ``__all__`` dynamically.
"""
from __future__ import annotations

import ast
import builtins
import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Package directory to scan; game adapters pass their own package name.
PKG = ROOT / (sys.argv[1] if len(sys.argv) > 1 else "dos_re")

BUILTIN_NAMES = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__class__", "__qualname__", "__annotations__",
    "__dict__", "__module__",
}

_NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _store_names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}


def _package_for(rel: str) -> str:
    """The dotted package a relative import in ``rel`` resolves against."""
    # overkill/a/b.py -> package overkill.a ; overkill/a/__init__.py -> overkill.a
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    else:
        parts = parts[:-1]
    return ".".join(parts)


def _star_exports(module: str) -> set[str]:
    """Names that ``from <module> import *`` would bind, via a real import."""
    mod = importlib.import_module(module)
    explicit = getattr(mod, "__all__", None)
    if explicit is not None:
        return set(explicit)
    return {n for n in dir(mod) if not n.startswith("_")}


def _module_star_names(tree: ast.AST, rel: str) -> set[str]:
    names: set[str] = set()
    pkg = _package_for(rel)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            if node.level:
                base = pkg
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0]
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module
            names |= _star_exports(target)
    return names


def _scope_bound(scope: ast.AST) -> set[str]:
    """Names bound directly in ``scope`` (not descending into nested scopes)."""
    bound: set[str] = set()
    args = getattr(scope, "args", None)
    if args:
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            bound.add(a.arg)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)
    body = scope.body if isinstance(scope.body, list) else [scope.body]
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, _NESTED):
            bound.add(getattr(n, "name", ""))
            continue
        if isinstance(n, ast.Assign):
            for t in n.targets:
                bound |= _store_names(t)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            bound |= _store_names(n.target)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            bound |= _store_names(n.target)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars:
                    bound |= _store_names(item.optional_vars)
        elif isinstance(n, ast.ExceptHandler):
            if n.name:
                bound.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                if a.name != "*":
                    bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound |= set(n.names)
        elif isinstance(n, ast.NamedExpr):
            bound |= _store_names(n.target)
        elif isinstance(n, ast.comprehension):
            bound |= _store_names(n.target)
        for ch in ast.iter_child_nodes(n):
            stack.append(ch)
    return bound


def _loads_excluding_nested(scope: ast.AST):
    body = scope.body if isinstance(scope.body, list) else [scope.body]
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, _NESTED):
            continue
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            yield n
        for ch in ast.iter_child_nodes(n):
            stack.append(ch)


def _nested_funcs(scope: ast.AST):
    body = scope.body if isinstance(scope.body, list) else [scope.body]
    stack = list(body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield n
            continue
        if isinstance(n, ast.ClassDef):
            continue
        for ch in ast.iter_child_nodes(n):
            stack.append(ch)


def _check_scope(scope, enclosing: set[str], findings: list, rel: str) -> None:
    scope_bound = enclosing | _scope_bound(scope)
    for nm in _loads_excluding_nested(scope):
        if nm.id not in scope_bound and nm.id not in BUILTIN_NAMES:
            findings.append((rel, nm.lineno, nm.id))
    for fn in _nested_funcs(scope):
        _check_scope(fn, scope_bound, findings, rel)


def main() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    findings: list[tuple[str, int, str]] = []
    for path in sorted(PKG.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        module_scope = _scope_bound(tree) | _module_star_names(tree, rel) | BUILTIN_NAMES
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                _check_scope(node, module_scope, findings, rel)
            elif isinstance(node, ast.ClassDef):
                class_scope = module_scope | _scope_bound(node)
                for m in node.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        _check_scope(m, class_scope, findings, rel)

    for rel, line, name in sorted(set(findings)):
        print(f"{rel}:{line}: undefined name {name!r}")
    if findings:
        print(f"\ncheck_undefined_names FAILED: {len(set(findings))} undefined-name reference(s)")
        return 1
    print("check_undefined_names passed: no undefined-name references found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
