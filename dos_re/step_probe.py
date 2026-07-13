"""Per-instance CPU step observers with an address-set TRAP — the cheap demo-scale probe primitive.

A probe typically wants a callback at a handful of ``(cs, ip)`` addresses over a replay of tens of
millions of interpreted instructions (a frame boundary, a routine entry, a write leaf).  The two
naive shapes both make that prohibitively slow:

* patching ``CPU8086.step`` at CLASS level charges every runtime a Python wrapper per instruction —
  including a candidate side that never observes — and defeats PyPy's JIT on the hot loop;
* calling the callback unconditionally charges ~120M Python calls on a cold boot for the few
  thousand that matter.

This module owns the proven shape (promoted from the first completed port's probe harness, where it
took a two-address cold-boot probe from 120M Python calls to a few thousand):

* the observer is installed on ONE cpu INSTANCE (``cpu.step = closure``) — the other side stays hot;
* with ``trap`` (an iterable of ``(cs, ip)`` pairs), the closure makes the address check itself with
  default-arg locals, so the callback runs ONLY at those addresses;
* observers NEST: installing over an existing observer chains to it, and uninstalling restores
  exactly what was visible before (LIFO — enforced fail-loud).

Usage::

    from dos_re.step_probe import step_observer

    with step_observer(cpu, on_step, trap={(0x1010, 0x9B2E), (0x2032, 0x0063)}):
        ...  # run the replay; on_step(cpu) fires only at the trapped addresses

The callback runs BEFORE the trapped instruction executes (entry semantics), so registers and
memory hold the routine's inputs.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, Tuple

Addr = Tuple[int, int]

_MISSING = object()


def install_step_observer(cpu, callback: Callable, *, trap: "Iterable[Addr] | None" = None,
                          ) -> Callable[[], None]:
    """Install ``callback(cpu)`` as a step observer on this cpu INSTANCE; return an uninstaller.

    With ``trap`` the callback fires only when ``(cs, ip)`` is in the set (checked with 16-bit
    masking); without it, before every instruction.  The uninstaller restores exactly what was
    installed before and fails loud if a later observer is still stacked on top (unwind LIFO).
    """
    inner = cpu.step                      # bound class method, or the prior observer's closure
    prior = cpu.__dict__.get("step", _MISSING)
    if trap is None:
        def step(_cpu=cpu, _cb=callback, _inner=inner):
            _cb(_cpu)
            return _inner()
    else:
        trapset = frozenset((cs & 0xFFFF, ip & 0xFFFF) for cs, ip in trap)
        def step(_cpu=cpu, _cb=callback, _inner=inner, _s=cpu.s, _trap=trapset):
            if (_s.cs & 0xFFFF, _s.ip & 0xFFFF) in _trap:
                _cb(_cpu)
            return _inner()
    cpu.step = step

    def uninstall() -> None:
        if cpu.__dict__.get("step") is not step:
            raise RuntimeError(
                "step observer is not the top layer (uninstall observers LIFO)")
        if prior is _MISSING:
            del cpu.__dict__["step"]      # back to the plain class method
        else:
            cpu.step = prior
    return uninstall


@contextmanager
def step_observer(cpu, callback: Callable, *, trap: "Iterable[Addr] | None" = None) -> Iterator:
    """Context-manager form of :func:`install_step_observer` (uninstalls on exit)."""
    uninstall = install_step_observer(cpu, callback, trap=trap)
    try:
        yield cpu
    finally:
        uninstall()
