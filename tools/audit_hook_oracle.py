#!/usr/bin/env python3
"""Audit verifier-visible hook-boundary composition.

This is a static guardrail for the bug class where a large Python parent hook
calls a child hook's Python function directly.  A direct call can make the child
routine a shared black box inside the parent transaction, so ``--verify-hooks``
may pass even when the child is wrong.  Complete child routines should be called
through ``call_installed_hook_like_near_call`` with their real CS:IP key.

Usage:
    python tools/audit_hook_oracle.py <adapter_package_dir> [stops_module.py]

``adapter_package_dir`` is the game adapter package to scan for
``@registry.replace(cs, ip, ...)`` registrations; ``stops_module.py`` (default:
``<adapter_package_dir>/verification.py``) is where the adapter keeps its
``HookStop`` continuation-metadata dict.

Origin: adapted from the Overkill port's scripts/audit_hook_oracle.py
(game-specific constants and checks removed; package path parameterized).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

Addr = tuple[int, int]

# Adapters that register hooks via named segment constants can list them here
# (name -> value) so the AST parser resolves the address.
INT_CONSTANTS: dict[str, int] = {}


def _int_expr(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return int(node.value)
    if isinstance(node, ast.Name):
        return INT_CONSTANTS.get(node.id)
    return None


def _parse_registered_hooks(paths: list[Path]) -> dict[str, Addr]:
    out: dict[str, Addr] = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            for deco in node.decorator_list:
                if not isinstance(deco, ast.Call):
                    continue
                func = deco.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "replace"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "registry"
                ):
                    continue
                if len(deco.args) < 2:
                    continue
                cs_node, ip_node = deco.args[:2]
                cs = _int_expr(cs_node)
                ip = _int_expr(ip_node)
                if cs is not None and ip is not None:
                    out[node.name] = (cs & 0xFFFF, ip & 0xFFFF)
    return out


# The continuation-metadata class as adapters actually name it: the framework
# exports GenericHookStop (dos_re.verification); "HookStop" is kept for
# adapters that alias/subclass it under the shorter historical name.
_HOOK_STOP_CLASS_NAMES = ("HookStop", "GenericHookStop")


def _parse_hookstop_metadata(verification_py: Path) -> set[Addr]:
    tree = ast.parse(verification_py.read_text(encoding="utf-8"), filename=str(verification_py))
    out: set[Addr] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=False):
            is_hook_stop = (
                isinstance(value, ast.Call)
                and (
                    (isinstance(value.func, ast.Name) and value.func.id in _HOOK_STOP_CLASS_NAMES)
                    or (
                        isinstance(value.func, ast.Attribute)
                        and value.func.attr == "after_step"
                        and isinstance(value.func.value, ast.Name)
                        and value.func.value.id in _HOOK_STOP_CLASS_NAMES
                    )
                )
            )
            if not (
                isinstance(key, ast.Tuple)
                and len(key.elts) == 2
                and is_hook_stop
            ):
                continue
            cs = _int_expr(key.elts[0])
            ip = _int_expr(key.elts[1])
            if cs is not None and ip is not None:
                out.add((cs & 0xFFFF, ip & 0xFFFF))
    return out


def _iter_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.FunctionDef | None:
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, ast.FunctionDef):
            return cur
    return None


def _find_direct_registered_function_calls(path: Path, registered: dict[str, Addr]) -> list[str]:
    """Find Python calls that bypass a registered original CS:IP boundary.

    If a lifted parent calls ``overkill_child_xxxx(cpu)`` directly, both the
    candidate side and the ASM-oracle clone can share that child as a black box.
    Complete child routines must be reached via the generic installed-boundary
    helpers instead.
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text, filename=str(path))
    parents = _iter_parent_map(tree)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        handler = node.func.id
        if handler not in registered:
            continue
        enclosing = _enclosing_function(node, parents)
        # Recursive self-calls are not child-boundary composition.  They are not
        # used by current hooks, but this keeps the rule precise.
        if enclosing is not None and enclosing.name == handler:
            continue
        line = text.splitlines()[node.lineno - 1].strip()
        cs, ip = registered[handler]
        bad.append(
            f"{path.relative_to(ROOT)}:{node.lineno}: direct call to registered hook "
            f"{handler} ({cs:04X}:{ip:04X}); route through "
            "call_installed_hook_like_near_call or jump_installed_hook_boundary "
            f"instead: {line}"
        )
    return bad


def _find_raw_call_hook_like_registered_args(path: Path, registered: dict[str, Addr]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    bad: list[str] = []
    pattern = re.compile(r"_call_hook_like_near_call\(\s*cpu\s*,\s*([A-Za-z_][A-Za-z0-9_]*)\s*,", re.MULTILINE)
    for match in pattern.finditer(text):
        handler = match.group(1)
        if handler in registered:
            line = text.count("\n", 0, match.start()) + 1
            cs, ip = registered[handler]
            bad.append(
                f"{path.relative_to(ROOT)}:{line}: raw near-call helper to registered hook "
                f"{handler} ({cs:04X}:{ip:04X}); use call_installed_hook_like_near_call"
            )
    return bad


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    package_dir = Path(sys.argv[1])
    if not package_dir.is_absolute():
        package_dir = ROOT / package_dir
    if not package_dir.is_dir():
        print(f"adapter package directory not found: {package_dir}", file=sys.stderr)
        return 2
    verification_py = Path(sys.argv[2]) if len(sys.argv) > 2 else package_dir / "verification.py"

    adapter_paths = sorted(
        path for path in package_dir.rglob("*.py")
        if "__pycache__" not in path.parts
    )
    registered = _parse_registered_hooks(adapter_paths)
    metadata = _parse_hookstop_metadata(verification_py) if verification_py.exists() else set()

    errors: list[str] = []
    missing_metadata = sorted(set(registered.values()) - metadata)
    for cs, ip in missing_metadata:
        errors.append(f"registered hook {cs:04X}:{ip:04X} is missing HookStop metadata")

    for path in adapter_paths:
        errors.extend(_find_direct_registered_function_calls(path, registered))
        errors.extend(_find_raw_call_hook_like_registered_args(path, registered))

    if errors:
        print("Hook-oracle audit failed:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print(
        "Hook-oracle audit passed: "
        f"{len(registered)} registered hooks, {len(metadata)} metadata entries, "
        "no direct registered child calls detected."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
