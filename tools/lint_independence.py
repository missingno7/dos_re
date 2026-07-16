"""lint_independence.py -- STATIC proof that a strict-VMless runtime cannot
reach the original executable or the loader that parses it.

Generic, game-agnostic (docs/dos_re_2.0.md section 1a').  The runtime
file-access guard (``dos_re.independence.exe_access_guard``) is the dynamic
backstop; this is the static one.  It walks the IMPORT GRAPH rooted at the
port's VMless runner + boot module (not a flat text grep) and fails if any
module on the MODULE-LEVEL graph imports a loader/oracle symbol or names an
executable path:

    forbidden symbols   the EXE-loading + interpreter-driving entry points
                        (create_runtime, load_snapshot, load_mz_program,
                        parse_mz, load_le, plus any port-declared names)
    forbidden literals  a hard-coded ``*.exe``/``*.com`` path literal

A function-local (lazy) import is a deferred capability that does not execute
unless the function is called; the strict-VMless frontend overrides the
methods that hold such imports, so they are reported as INFO, not failures.

Usage (from a port):
    python dos_re/tools/lint_independence.py \
        --repo-root . \
        --root scripts/play_vmless.py --root mygame/vmless_boot.py \
        --forbidden create_mygame_runtime --forbidden load_mygame_snapshot

Exit code 0 = independent; nonzero = a forbidden module-level edge was found.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

# EXE-loading / loader-driving framework entry points no VMless runtime may
# import at module level.  Ports add their own adapter names via --forbidden.
DEFAULT_FORBIDDEN = {
    "create_runtime",            # the EXE MZ loader (create_runtime_from_image is fine)
    "load_snapshot",             # EXE-based restore (load_snapshot_headless is fine)
    "load_mz_program",
    "parse_mz",
    "load_le",
}

DEFAULT_LOCAL_PREFIXES = ("dos_re",)


def _module_to_path(mod: str, repo_root: Path, prefixes: tuple[str, ...]) -> Path | None:
    parts = mod.split(".")
    if parts[0] not in prefixes:
        return None
    candidates = [
        repo_root / Path(*parts).with_suffix(".py"),
        repo_root / "dos_re" / Path(*parts).with_suffix(".py"),
        repo_root / Path(*parts) / "__init__.py",
        repo_root / "dos_re" / Path(*parts) / "__init__.py",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _toplevel(tree: ast.Module):
    """Yield (node, is_module_level) for every import/constant in ``tree``."""
    def walk(node, module_level):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield from walk(child, False)
            else:
                if isinstance(child, (ast.Import, ast.ImportFrom, ast.Constant)):
                    yield child, module_level
                yield from walk(child, module_level)

    yield from walk(tree, True)


def _scan(path: Path, repo_root: Path, forbidden: set[str],
          prefixes: tuple[str, ...], offenders: list[str],
          deferred: list[str]) -> set[str]:
    """Return the local modules imported AT MODULE LEVEL by ``path``; record
    forbidden module-level edges in ``offenders``, deferred ones in
    ``deferred``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        rel = path
    local_mods: set[str] = set()
    for node, module_level in _toplevel(tree):
        bucket = offenders if module_level else deferred
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if a.name in forbidden:
                    bucket.append(f"{rel}: from {node.module} import {a.name}")
            if module_level and node.module.split(".")[0] in prefixes:
                local_mods.add(node.module)
                # `from pkg import submod` imports the SUBMODULE -- follow it too.
                for a in node.names:
                    local_mods.add(f"{node.module}.{a.name}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                if module_level and a.name.split(".")[0] in prefixes:
                    local_mods.add(a.name)
                if a.name in forbidden:
                    bucket.append(f"{rel}: import {a.name}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A real path/filename literal, not prose: no whitespace and an
            # executable suffix (docstrings that merely mention the EXE contain
            # spaces/newlines and are not code paths).
            v = node.value
            if v and not any(c.isspace() for c in v) and v.lower().endswith(
                    (".exe", ".com")):
                offenders.append(f"{rel}: executable path literal {v!r}")
    return local_mods


def run_lint(roots: list[Path], repo_root: Path, forbidden: set[str],
             prefixes: tuple[str, ...]) -> int:
    offenders: list[str] = []
    deferred: list[str] = []
    seen: set[Path] = set()
    work = list(roots)
    graph: list[Path] = []
    while work:
        path = work.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        graph.append(path)
        for mod in _scan(path, repo_root, forbidden, prefixes, offenders, deferred):
            p = _module_to_path(mod, repo_root, prefixes)
            if p and p not in seen:
                work.append(p)

    rel_roots = []
    for r in roots:
        try:
            rel_roots.append(str(r.relative_to(repo_root)))
        except ValueError:
            rel_roots.append(str(r))
    print(f"VMless independence lint: walked {len(graph)} MODULE-LEVEL edges "
          f"from {', '.join(rel_roots)}")
    if deferred:
        print(f"  ({len(set(deferred))} deferred/lazy loader import(s) present "
              f"but not on the load-time graph -- not executed by the VMless "
              f"frontend)")
    if offenders:
        print(f"FAIL -- {len(set(offenders))} forbidden module-level edge(s):")
        for o in sorted(set(offenders)):
            print(f"  {o}")
        return 1
    print("PASS -- the strict-VMless runtime import graph is EXE/loader-free.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=".",
                    help="repository root used to resolve local imports")
    ap.add_argument("--root", action="append", required=True,
                    help="entry file of the VMless runtime surface (repeatable)")
    ap.add_argument("--forbidden", action="append", default=[],
                    help="additional forbidden symbol name (repeatable; the "
                         "framework loader names are always included)")
    ap.add_argument("--local-prefix", action="append", default=[],
                    help="additional top-level package name to follow "
                         "(dos_re is always followed)")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    roots = [(repo_root / r) if not Path(r).is_absolute() else Path(r)
             for r in args.root]
    forbidden = DEFAULT_FORBIDDEN | set(args.forbidden)
    prefixes = tuple(dict.fromkeys(DEFAULT_LOCAL_PREFIXES + tuple(args.local_prefix)))
    return run_lint(roots, repo_root, forbidden, prefixes)


if __name__ == "__main__":
    raise SystemExit(main())
