"""Run a CPUless recovered corpus STANDALONE -- the shared host every port needs.

A promoted corpus (DOS_RE 2.0 stage 3) is a package of pure
``func_<cs>_<ip>(mem, plat, *, <regs>) -> (outputs, _compat)`` modules that import only their siblings.
Turning that into a running program takes the same four things in every port, and each port had grown
its own copy of them (lemmings' ``play_cpuless``, skyroads', overkill's ``cpuless_host``). They live
here once:

* :func:`install_import_guard` -- THE WALL. Arms ``builtins.__import__`` so any import of the
  interpreter / CPU carrier / VMless graph / EXE runtime raises instead of silently re-attaching the VM.
  Resolves RELATIVE imports to their absolute name first: ``from .cpu import X`` reaches ``__import__``
  as ``name='cpu', level=1`` WITHOUT the package, so a guard that skips that step has a blind spot
  exactly where the framework's own intra-package imports live (this is how ``dos_re.cpu`` reached
  lemmings' runner silently on every boot).
* :func:`load_recovered` / :func:`run_recovered` -- resolve a ``'CS:IP'`` key to its recovered module in
  the port's corpus package and run it. A missing module (the function, or a recovered callee it
  imports, is on the frontier) fails LOUD; there is no interpreter fallback, by construction.
* :class:`FailLoudPlatform` -- the honest default device model. Every ``intr``/``inp``/``outp`` raises
  and NAMES the missing service, so an unimplemented platform effect is a visible work item rather than
  a silent wrong answer. A port subclasses it and overrides only what it has really implemented.
* :func:`run_deep` -- headroom for tail-dispatch loops. A machine ``jmp`` is a TAIL transfer that reuses
  the frame, but the emitter models a dynamic tail dispatch as a NESTED ``_dyn`` call, so a tail-dispatch
  LOOP grows the Python stack instead of iterating. Such loops are bounded (a blitter walking rows
  terminates), so they complete given a big enough stack.

  This is a RUNTIME ACCOMMODATION, not a fix, and is documented as one: the correct repairs are to emit
  an intra-routine dispatch as a block goto (see ``dispatch.absorb_dispatch_arms`` -- an absorbed arm's
  jump table resolves as an intra-function ``_LOCAL`` landing) and to trampoline CROSS-routine tail
  cycles. Until a port's corpus is free of cross-routine tail cycles it needs this; with it, a genuinely
  UNBOUNDED cycle still terminates in a ``RecursionError`` rather than hanging.

Nothing here imports the CPU, the interpreter, or a port: it is the framework side of the standalone
contract, parameterised by the port's corpus package.
"""
from __future__ import annotations

import builtins
import importlib
import sys

#: Modules a standalone CPUless runtime must NEVER import -- the interpreter / CPU carrier, the VMless
#: graph installer and its lifted-call support, and the EXE/VM runtime builder. A recovered program that
#: reaches for any of these has not actually detached from the VM. A port adds its own CPU-ABI adapter
#: package via ``extra_forbidden`` (those adapters are verification shims, never runtime source).
BASE_FORBIDDEN = (
    "dos_re.cpu",                 # the interpreter / CPU8086 carrier
    "dos_re.cpu386",
    "dos_re.lift.install",        # the VMless graph installer
    "dos_re.lift.runtime",        # the VMless lifted-call support (emulate_*)
    "dos_re.runtime",             # the EXE loader / VM runtime builder
)


class CpuStandaloneWitness(RuntimeError):
    """The standalone CPUless runtime cannot proceed without the VM: an unpromoted function on the
    frontier, a reached platform effect with no host implementation, or an attempt to import a
    forbidden CPU-carrier module (the wall was breached). A structured witness, never a fallback."""


def resolve_import(name: str, globals_, level: int) -> str:
    """The ABSOLUTE dotted name of an import request, resolving relative imports (``level > 0``)."""
    if not level:
        return name
    pkg = (globals_ or {}).get("__package__")
    if pkg is None:
        modname = (globals_ or {}).get("__name__", "")
        spec = (globals_ or {}).get("__spec__", None)
        pkg = getattr(spec, "parent", None)
        if pkg is None:
            pkg = modname.rpartition(".")[0] if modname else ""
    parts = [p for p in str(pkg).split(".") if p]
    if level > 1:
        parts = parts[:-(level - 1)] or []
    if name:
        parts = parts + name.split(".")
    return ".".join(parts)


def forbidden_hit(dotted: str, forbidden) -> "str | None":
    """The forbidden PACKAGE PREFIX ``dotted`` falls under, or None. Prefix-matched on dotted
    components so ``dos_re.cpu.x`` hits ``dos_re.cpu`` while ``dos_re.cpuxyz`` does not."""
    base = dotted.split(".")
    for forb in forbidden:
        fparts = forb.split(".")
        if base[:len(fparts)] == fparts:
            return forb
    return None


def install_import_guard(extra_forbidden=()) -> None:
    """Arm the CPUless wall for this process. Fires only on an EXECUTED import, so pair it with a
    STATIC import-graph lint for paths a given run does not take."""
    forbidden = tuple(BASE_FORBIDDEN) + tuple(extra_forbidden)
    real_import = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        dotted = resolve_import(name, globals, level)
        hit = forbidden_hit(dotted, forbidden)
        if hit is not None:
            via = f"{name!r} (relative, level={level})" if level else f"{name!r}"
            raise CpuStandaloneWitness(
                f"standalone CPUless runtime attempted to import {via} -> {dotted!r} "
                f"[forbidden: {hit}] -- it must not depend on the interpreter, the VMless graph, "
                f"the VM runtime, or the CPU-ABI adapters.")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = guarded


class FailLoudPlatform:
    """The honest default device model: every platform effect raises and NAMES the missing service.

    A pure-memory recovered function never calls it; one that does reports exactly which device the
    port still owes. Subclass and override only what is really implemented -- anything left inherited
    stays a visible frontier item instead of a silent no-op."""

    def intr(self, num, regs, cost):
        raise CpuStandaloneWitness(
            f"INT {num & 0xFF:#04x} reached with no host platform implementation "
            f"(bind the device that services it before running this path)")

    def inp(self, port, width, cost):
        raise CpuStandaloneWitness(
            f"IN from port {port & 0xFFFF:#06x} with no host platform implementation")

    def outp(self, port, value, width, cost):
        raise CpuStandaloneWitness(
            f"OUT to port {port & 0xFFFF:#06x} with no host platform implementation")


def module_name(key: str) -> str:
    """The recovered module basename for a ``'CS:IP'`` key: ``'1010:5F61'`` -> ``'func_1010_5f61'``."""
    cs, ip = key.split(":")
    return f"func_{int(cs, 16):04x}_{int(ip, 16):04x}"


def load_recovered(package: str, key: str):
    """Import promoted recovered function ``key`` from the port's corpus ``package``.

    Fails LOUD when the function -- or any recovered callee it imports -- has no module, so the CPUless
    frontier stays visible instead of being papered over."""
    name = module_name(key)
    try:
        mod = importlib.import_module(f"{package}.{name}")
    except ModuleNotFoundError as exc:
        raise CpuStandaloneWitness(
            f"{key}: no recovered module ({name}) in {package} -- it (or a recovered callee) is on "
            f"the CPUless frontier; promote it or bind a native override.") from exc
    return getattr(mod, name)


def run_recovered(package: str, key: str, mem, plat=None, **regs):
    """Run recovered function ``key`` over ``mem`` with ``plat``, returning its live-output register
    dict. Composition is implicit: the function calls its recovered callees directly. With ``plat``
    omitted a :class:`FailLoudPlatform` is used, so a reached effect fails loud rather than no-oping."""
    fn = load_recovered(package, key)
    outputs, _compat = fn(mem, FailLoudPlatform() if plat is None else plat, **regs)
    return outputs


#: 512MB is rejected by Windows' thread API; 64MB is portable and deep enough for observed blit loops.
DEEP_STACK_BYTES = 64 * 1024 * 1024
DEEP_RECURSION = 300_000


def run_deep(fn, *args, stack_bytes: int = DEEP_STACK_BYTES,
             recursion: int = DEEP_RECURSION, **kwargs):
    """Run ``fn`` on a thread with a large stack and a raised recursion limit, so a BOUNDED
    tail-dispatch loop completes instead of dying on Python's frame limit (see the module notes: this
    is an accommodation, not a fix). Result and exception propagate to the caller unchanged.

    The big stack is the load-bearing half: raising the recursion limit alone lets CPython run past
    what the C stack can hold, which crashes the process instead of raising."""
    import threading

    box: dict = {}

    def _target():
        sys.setrecursionlimit(recursion)
        try:
            box["value"] = fn(*args, **kwargs)
        except BaseException as exc:            # noqa: BLE001 -- propagated verbatim
            box["error"] = exc

    prev = threading.stack_size(stack_bytes)
    try:
        t = threading.Thread(target=_target)
        t.start()
        t.join()
    finally:
        threading.stack_size(prev)
    if "error" in box:
        raise box["error"]
    return box.get("value")
