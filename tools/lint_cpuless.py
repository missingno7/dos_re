"""lint_cpuless.py -- audit a selected source closure for CPU independence.

Generic and game-agnostic. The runtime
import guard is the DYNAMIC backstop; this is the static one, and it is
strictly stronger in the ways that matter:

  * the guard arms inside main(), so importing the runner as a library leaves
    it unarmed;
  * the guard hooks __import__, so it only fires on a path actually EXECUTED
    -- an untaken branch that imports the CPU stays invisible until hit;
  * the guard is deliberately lifted around host-side debug actions.

This walks the IMPORT GRAPH (AST, not a text grep) under two rules that
differ, on purpose, from the VMless independence lint:

  RECOVERED PURITY (--recovered-root)
      The recovered corpus is the port itself: pure computation over
      (mem[, plat], *regs).  It may import NOTHING from the framework, at ANY
      level -- module-level or function-local.  A lazy import here is not a
      "deferred capability", it is the carrier sneaking back in.  Only sibling
      recovered modules and the generated dispatch/dyncall support are allowed.

  RUNNER CLOSURE (--root)
      The runner's function-local imports ARE its real dependencies -- they are
      lazy only so the guard can arm first -- so unlike the VMless lint they
      are FOLLOWED, not excused.  From every module thus reached, module-level
      edges are followed transitively.  No forbidden module may appear
      anywhere on that closure: an ALLOWED module that itself imports the CPU
      would hand the runner a carrier through the back door.

A forbidden MODULE is matched by dotted prefix, so it catches both
``import dos_re.cpu`` and ``from dos_re.cpu import CPUState`` -- the symbol
form the module-name check in lint_independence.py misses.

Usage (from a port):
    python dos_re/tools/lint_cpuless.py \
        --repo-root . \
        --root product/launcher.py \
        --recovered-root mygame/recovered \
        --recovered-prefix mygame.recovered \
        --forbidden-module dos_re.cpu --forbidden-module mygame.lifted \
        --local-prefix dos_re --local-prefix mygame

Exit code 0 = CPU-free; nonzero = a forbidden edge was found.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lint_independence import _module_to_path, _toplevel  # noqa: E402

#: CPU-carrier symbols: naming one is reaching for the MACHINE even if the
#: module it lives in was imported by an allowed path.
#:
#: CPUState is deliberately NOT here: a register record is a VALUE (it lives in
#: the ISA leaf dos_re.x86), and the CPUless runner legitimately holds a
#: register file -- in its boundary park.  What it must never hold is something
#: that EXECUTES.
CPU_SYMBOLS = (
    "CPU8086", "CPU386",
    "interpret_current_instruction", "interpret_current_instruction_without_hook",
    "replacement_hooks", "emulate_int", "emulate_call",
)


def _is_type_checking(test) -> bool:
    """``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` -- the body never
    executes at runtime, so an import there is an annotation dependency, not a
    carrier edge (dos_re.dos type-annotates ``cpu: CPU8086`` this way)."""
    return ((isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
            or (isinstance(test, ast.Attribute)
                and test.attr == "TYPE_CHECKING"))


def _walk_imports(node, module_level: bool):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            yield from _walk_imports(child, False)
        elif isinstance(child, ast.If) and _is_type_checking(child.test):
            for sub in child.orelse:          # the else branch DOES run
                yield from _walk_imports(sub, module_level)
        else:
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child, module_level
            yield from _walk_imports(child, module_level)


def rel_target_path(path: Path, module: str | None, level: int) -> Path | None:
    """The FILE a relative import resolves to.

    Dotted names cannot be reconstructed reliably from a path (dos_re's package
    lives at dos_re/dos_re/, so the naive join yields 'dos_re.dos_re.cpu' and a
    prefix match against 'dos_re.cpu' silently misses).  Relative imports are
    resolved by the filesystem, so resolve them the same way and compare FILES.
    """
    base = path.resolve().parent
    for _ in range(max(0, level - 1)):
        base = base.parent
    if module:
        base = base / Path(*module.split("."))
    for c in (base.with_suffix(".py"), base / "__init__.py"):
        if c.is_file():
            return c.resolve()
    return None


def forbidden_paths(forbidden: tuple[str, ...], repo_root: Path,
                    prefixes: tuple[str, ...],
                    package_dirs: dict[str, Path] | None) -> dict[Path, str]:
    """Every forbidden module resolved to a file (and package dir) so a
    relative import can be judged by identity, not by name."""
    out: dict[Path, str] = {}
    for f in forbidden:
        p = _module_to_path(f, repo_root, prefixes, package_dirs)
        if p is not None:
            out[p.resolve()] = f
        parts = Path(*f.split("."))
        for d in (repo_root / parts, repo_root / "dos_re" / parts):
            if d.is_dir():
                out[d.resolve()] = f       # a forbidden package tree
    return out


def _under_forbidden(target: Path, fpaths: dict[Path, str]) -> str | None:
    if target in fpaths:
        return fpaths[target]
    for p, name in fpaths.items():
        if p.is_dir():
            try:
                target.relative_to(p)
                return name
            except ValueError:
                pass
    return None


def _imports(path: Path, repo_root: Path | None = None):
    """Yield (module, symbol|None, module_level, relative) for every import in
    ``path`` that actually executes at runtime."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node, module_level in _walk_imports(tree, True):
        if isinstance(node, ast.ImportFrom):
            # A RELATIVE import (level > 0) is resolved by the FILESYSTEM;
            # `relative` carries the target path so the caller can judge it by
            # identity.  For the recovered corpus a relative import is a
            # sibling by construction (`from .dispatch import DISPATCH`).
            if node.level:
                tgt = rel_target_path(path, node.module, node.level)
                for a in node.names:
                    yield node.module, a.name, module_level, tgt or True
                continue
            if node.module:
                for a in node.names:
                    yield node.module, a.name, module_level, False
        elif isinstance(node, ast.Import):
            for a in node.names:
                yield a.name, None, module_level, False


def _is_forbidden(mod: str, forbidden: tuple[str, ...]) -> str | None:
    parts = mod.split(".")
    for f in forbidden:
        fp = f.split(".")
        if parts[:len(fp)] == fp:
            return f
    return None


def _rel(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def check_recovered(recovered_dirs: list[Path], repo_root: Path,
                    allowed_prefixes: tuple[str, ...],
                    offenders: list[str]) -> int:
    """RECOVERED PURITY: no framework import at any level."""
    n = 0
    for d in recovered_dirs:
        for path in sorted(d.rglob("*.py")):
            n += 1
            for mod, sym, module_level, relative in _imports(path, repo_root):
                if relative is not False:
                    continue        # intra-package by construction: a sibling
                if _is_forbidden(mod, allowed_prefixes) is not None:
                    continue        # an absolute sibling reference: fine
                root = mod.split(".")[0]
                if root == "__future__" or _is_stdlib(root):
                    continue
                where = "module-level" if module_level else "function-local"
                what = f"from {mod} import {sym}" if sym else f"import {mod}"
                offenders.append(
                    f"{_rel(path, repo_root)}: {what}  ({where}) -- recovered "
                    f"code must import nothing but sibling recovered modules")
    return n


_STDLIB = set(getattr(sys, "stdlib_module_names", ()))


def _is_stdlib(root: str) -> bool:
    return root in _STDLIB


def walk_runner(roots: list[Path], repo_root: Path,
                forbidden: tuple[str, ...], prefixes: tuple[str, ...],
                offenders: list[str],
                package_dirs: dict[str, Path] | None = None):
    """RUNNER CLOSURE: follow the runner's own imports (lazy ones too -- they
    execute), then module-level edges transitively.  ``seen`` is returned so
    the caller can report the graph size.

    NOTE for packaging: ``seen`` is an audit closure, not a shipping list.
    It deliberately stops following function-local imports below the roots, so
    a module a reached library lazily imports is absent from it. Use
    :func:`runtime_payload` to
    decide what a release package must carry -- an APK built from ``seen``
    died with ModuleNotFoundError on the first device it ran on."""
    seen: set[Path] = set()
    # (path, follow_lazy): only the runner roots' lazy imports are real
    # dependencies; a library module's lazy import is a deferred capability.
    work: list[tuple[Path, bool]] = [(r, True) for r in roots]
    unresolved: set[str] = set()
    fpaths = forbidden_paths(forbidden, repo_root, prefixes, package_dirs)
    while work:
        path, follow_lazy = work.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for mod, sym, module_level, relative in _imports(path, repo_root):
            if not (module_level or follow_lazy):
                continue
            if relative is not False:
                # judged by file identity, not by dotted name
                tgt = relative if isinstance(relative, Path) else None
                if tgt is None:
                    continue
                hit = _under_forbidden(tgt, fpaths)
                if hit is not None:
                    what = f"from .{mod or ''} import {sym}"
                    where = "module-level" if module_level else "function-local"
                    offenders.append(
                        f"{_rel(path, repo_root)}: {what}  ({where}) -- "
                        f"reaches the forbidden carrier {hit}")
                elif tgt not in seen:
                    work.append((tgt, False))
                continue
            hit = _is_forbidden(mod, forbidden)
            if hit is not None:
                what = f"from {mod} import {sym}" if sym else f"import {mod}"
                where = "module-level" if module_level else "function-local"
                offenders.append(
                    f"{_rel(path, repo_root)}: {what}  ({where}) -- reaches "
                    f"the forbidden carrier {hit}")
                continue
            if sym is not None and sym in CPU_SYMBOLS:
                offenders.append(
                    f"{_rel(path, repo_root)}: from {mod} import {sym} -- "
                    f"CPU-carrier symbol")
            if mod.split(".")[0] not in prefixes:
                continue
            p = _module_to_path(mod, repo_root, prefixes, package_dirs)
            if p is None:
                if sym is None:
                    unresolved.add(mod)
                continue
            if p not in seen:
                work.append((p, False))
            if sym is not None:
                sp = _module_to_path(f"{mod}.{sym}", repo_root, prefixes,
                                     package_dirs)
                if sp is not None and sp not in seen:
                    work.append((sp, False))
    return seen, unresolved


def runtime_payload(roots: list[Path], repo_root: Path,
                    forbidden: tuple[str, ...], prefixes: tuple[str, ...],
                    package_dirs: dict[str, Path] | None = None) -> list:
    """Every repo module the standalone runtime can IMPORT, as sorted
    repo-relative POSIX paths: the shipping list for a release package.

    A different question from :func:`walk_runner`'s, and it needs a different
    walk -- getting this wrong breaks a release in one of two ways:

      * follow only module-level edges below the roots (the audit walk's rule)
        and you MISS what a reached module lazily imports and then calls:
        a runtime helper imports a deferred input service inside the
        function, and an APK packaged from that closure crashed on device.
        So: follow function-local imports at every level.
      * follow them into FORBIDDEN modules and you would ship the interpreter
        itself -- dos_re.snapshot's load path lazily imports dos_re.cpu, for
        the VM callers it also serves.  Those paths cannot execute in a
        release (the runner never calls them, and the import guard would fire
        if it did), so prune at the forbidden dependency boundary. The package contains a
        module whose unexecuted branch names an absent one -- that is the
        declared policy being enforced, not a packaging defect.

    Package ``__init__.py`` files are part of the WALK, not an afterthought:
    importing a module executes its packages' ``__init__`` first, so they are
    both required on disk AND edges in the graph -- ``dos_re.lift.__init__``
    imports ``.decode``, and a payload that merely appended the __init__ paths
    without following them shipped an APK that died on device with
    ``ModuleNotFoundError: dos_re.lift.decode``.
    """
    seen: set[Path] = set()
    work: list[Path] = list(roots)
    fpaths = forbidden_paths(forbidden, repo_root, prefixes, package_dirs)
    while work:
        path = work.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        # the packages this module lives in: their __init__ runs on import
        try:
            d = path.resolve().parent
            while d != repo_root and repo_root in d.parents:
                init = d / "__init__.py"
                if init.is_file() and init not in seen:
                    work.append(init)
                d = d.parent
        except (ValueError, OSError):
            pass
        for mod, sym, _module_level, relative in _imports(path, repo_root):
            targets = []
            if relative is not False:
                if isinstance(relative, Path):
                    targets.append(relative)
            elif mod.split(".")[0] in prefixes:
                p = _module_to_path(mod, repo_root, prefixes, package_dirs)
                if p is not None:
                    targets.append(p)
                if sym is not None:
                    sp = _module_to_path(f"{mod}.{sym}", repo_root, prefixes,
                                         package_dirs)
                    if sp is not None:
                        targets.append(sp)
            for tgt in targets:
                if _under_forbidden(tgt, fpaths) is not None:
                    continue                      # prune forbidden dependency
                if tgt not in seen:
                    work.append(tgt)
    out = set()
    for p in seen:
        try:
            out.add(Path(p).resolve().relative_to(repo_root).as_posix())
        except ValueError:
            continue                              # outside the repo
    return sorted(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--root", action="append", default=[],
                    help="the standalone runner entry point(s)")
    ap.add_argument("--recovered-root", action="append", default=[],
                    help="directory of recovered modules (purity-checked)")
    ap.add_argument("--recovered-prefix", action="append", default=[],
                    help="import prefix recovered modules may use")
    ap.add_argument("--forbidden-module", action="append", default=[],
                    help="module prefix the runtime must never reach")
    ap.add_argument("--local-prefix", action="append", default=[])
    ap.add_argument("--package-dir", action="append", default=[],
                    help="PREFIX=DIR")
    ap.add_argument("--print-payload", action="store_true",
                    help="also print the RELEASE PAYLOAD (runtime_payload): "
                         "every repo module the runtime can import, one path "
                         "per line -- what a release package must carry")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    forbidden = tuple(args.forbidden_module)
    prefixes = tuple(args.local_prefix)
    pkg_dirs = {}
    for spec in args.package_dir:
        k, _, v = spec.partition("=")
        pkg_dirs[k] = (repo_root / v).resolve()

    offenders: list[str] = []
    print("CPUless independence lint (static): the standalone runtime must "
          "never reach a CPU")

    n_rec = 0
    if args.recovered_root:
        dirs = [(repo_root / d).resolve() for d in args.recovered_root]
        n_rec = check_recovered(dirs, repo_root, tuple(args.recovered_prefix),
                                offenders)
        print(f"  recovered purity : {n_rec} module(s) checked "
              f"({', '.join(args.recovered_root)})")

    seen: set[Path] = set()
    unresolved: set[str] = set()
    if args.root:
        roots = [(repo_root / r).resolve() for r in args.root]
        seen, unresolved = walk_runner(roots, repo_root, forbidden, prefixes,
                                       offenders, pkg_dirs)
        print(f"  runner closure   : {len(seen)} module(s) reached from "
              f"{', '.join(args.root)} (function-local imports FOLLOWED)")
        if args.print_payload:
            print("PAYLOAD:")
            for p in runtime_payload(roots, repo_root, forbidden, prefixes,
                                     pkg_dirs):
                print(p)

    if unresolved:
        print(f"FAIL -- {len(unresolved)} local module(s) not resolvable to "
              f"files (add --package-dir PREFIX=DIR); the walk has a hole:")
        for m in sorted(unresolved):
            print(f"  {m}")
        return 1
    if offenders:
        print(f"FAIL -- {len(set(offenders))} forbidden edge(s):")
        for o in sorted(set(offenders)):
            print(f"  {o}")
        return 1
    print(f"PASS -- no path from the standalone runner or the recovered "
          f"corpus reaches {', '.join(forbidden)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
