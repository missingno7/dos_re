"""Development-time import guard for a detached implementation surface.

Release detachment is proved by an ExecutionPlan and closed-world export.  This
module supplies an additional dynamic backstop for development runs: imports
that would reconnect a selected CPU-free/generated implementation to the
interpreter, EXE loader, or another forbidden adapter fail immediately.

The guard is process-global while armed. Prefer :func:`import_guard` unless the
process exists solely to run the guarded composition.
"""
from __future__ import annotations

import builtins
import contextlib
import importlib

BASE_FORBIDDEN = (
    "dos_re.cpu",
    "dos_re.cpu386",
    "dos_re.lift.install",
    "dos_re.lift.runtime",
    "dos_re.runtime",
)


class DetachedDependencyError(RuntimeError):
    """A guarded detached surface imported a forbidden dependency."""


def resolve_import(name: str, globals_, level: int) -> str:
    """Resolve an import request to its absolute dotted name."""
    if not level:
        return name
    package = (globals_ or {}).get("__package__")
    if package is None:
        module_name = (globals_ or {}).get("__name__", "")
        spec = (globals_ or {}).get("__spec__", None)
        package = getattr(spec, "parent", None)
        if package is None:
            package = module_name.rpartition(".")[0] if module_name else ""
    parts = [part for part in str(package).split(".") if part]
    if level > 1:
        parts = parts[:-(level - 1)] or []
    if name:
        parts.extend(name.split("."))
    return ".".join(parts)


def forbidden_hit(dotted: str, forbidden) -> str | None:
    """Return the forbidden package prefix matched by ``dotted``, if any."""
    components = dotted.split(".")
    for item in forbidden:
        prefix = item.split(".")
        if components[:len(prefix)] == prefix:
            return item
    return None


_GUARD_PREV = "_dos_re_prev_import"


def install_import_guard(extra_forbidden=()) -> None:
    """Arm the dynamic guard until :func:`uninstall_import_guard` is called."""
    forbidden = tuple(BASE_FORBIDDEN) + tuple(extra_forbidden)
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        dotted = resolve_import(name, globals, level)
        hit = forbidden_hit(dotted, forbidden)
        if hit is not None:
            via = f"{name!r} (relative, level={level})" if level else f"{name!r}"
            raise DetachedDependencyError(
                f"detachment guard rejected {via} -> {dotted!r} "
                f"[forbidden dependency: {hit}]")
        return real_import(name, globals, locals, fromlist, level)

    setattr(guarded, _GUARD_PREV, real_import)
    builtins.__import__ = guarded


def uninstall_import_guard() -> bool:
    """Restore the previous import function; unwind one nested guard."""
    previous = getattr(builtins.__import__, _GUARD_PREV, None)
    if previous is None:
        return False
    builtins.__import__ = previous
    return True


@contextlib.contextmanager
def import_guard(extra_forbidden=()):
    """Guard one block and always restore the previous import function."""
    install_import_guard(extra_forbidden)
    try:
        yield
    finally:
        uninstall_import_guard()

