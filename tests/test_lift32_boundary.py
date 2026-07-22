"""32-bit boundary-park: a declared spin head offers ``cpu.boundary_hook`` and
is re-enterable, so an environment-wait loop lifts (park + resume) instead of
being excluded whole.  The flat mirror of ``test_lift_boundary_resume.py``.

A lifted body runs synchronously to completion -- nothing external happens
inside it -- so a loop WAITING for the world to change (an IRQ to flip a flag)
waits forever.  A declared boundary head lets the body offer that point back to
the host: the host parks (runs the spin interpreted, where the IRQ is serviced)
and later resumes INSIDE the lifted body at the exported block.
"""
from __future__ import annotations

import types

import pytest

from dos_re.cpu386 import CPU386, FlatMemory
from dos_re.lift.cfg32 import scan_function32
from dos_re.lift.emit32 import emit_function32

FUNC = 0x2000
POLL = 0x3000
STACK = 0x8000

#: cmp eax,[0x3000] ; jz FUNC ; ret -- a poll-spin.  The HEAD is the cmp at
#: 0x2000; its successor (the jz) is at 0x2006.  While [0x3000]==eax the jz
#: loops back to the head: a wait only an outside write can end.
SPIN = bytes.fromhex("3b0500300000" "74f8" "c3")
HEAD = FUNC
SUCC = FUNC + 6


def _emit(heads=frozenset({HEAD})):
    mem = FlatMemory(size=0x10000)
    mem.load(FUNC, SPIN)
    scan = scan_function32(mem.data.__getitem__, FUNC)
    assert scan.liftable, scan.refusals
    src = emit_function32(scan, "spin", signature=bytes(mem.data[FUNC:FUNC + 8]),
                          boundary_heads=heads)
    mod = types.ModuleType("spin")
    exec(compile(src, "spin.py", "exec"), mod.__dict__)
    return mod, src


def _cpu():
    mem = FlatMemory(size=0x10000)
    mem.load(FUNC, SPIN)
    mem.w32(POLL, 5)                     # [0x3000] == eax -> the jz loops
    cpu = CPU386(mem, eip=FUNC, esp=STACK)
    cpu.r[0] = 5                         # eax
    return cpu


def test_head_and_successor_are_resume_entries():
    mod, _src = _emit()
    assert f"0x{SUCC:X}" in mod.RESUME_ENTRIES        # the park's resume point
    assert f"0x{HEAD:X}" in mod.RESUME_ENTRIES, (      # a mid-spin re-entry too
        f"the head must be re-enterable; got {mod.RESUME_ENTRIES}")


def test_the_head_entry_points_at_the_head_block():
    mod, _src = _emit()
    bb = mod.RESUME_ENTRIES[f"0x{HEAD:X}"]
    assert mod.BLOCK_ADDRS[bb] == HEAD


def test_head_less_module_is_unchanged():
    """The rule costs nothing where it does not apply: a head-less emission is
    byte-for-byte the pre-boundary emitter (guards the committed graphs)."""
    mod, src = _emit(heads=frozenset())
    assert not hasattr(mod, "RESUME_ENTRIES")
    assert "RESUME_ENTRIES" not in src
    assert "boundary_hook" not in src
    assert "def spin(cpu):" in src                     # no bb parameter added


def test_boundary_offers_the_head_and_can_park():
    """Entering at the head executes it, then the observer fires -- so a host
    that parks gets its park (EIP left on the successor), rather than the body
    spinning to MAX_ITERATIONS."""
    mod, _src = _emit()
    cpu = _cpu()
    calls = []

    class Parked(Exception):
        pass

    def hook(c, head_eip, resume_eip):
        calls.append((head_eip, resume_eip))
        c.eip = resume_eip
        raise Parked
    cpu.boundary_hook = hook

    with pytest.raises(Parked):
        mod.spin(cpu, mod.RESUME_ENTRIES[f"0x{HEAD:X}"])
    assert calls == [(HEAD, SUCC)]
    assert cpu.eip == SUCC


def test_activate_registers_resume_hooks(tmp_path):
    """The graph activator installs a re-entry hook at every RESUME_ENTRIES
    address, so a parked body resumes inside the lifted code."""
    from dos_re.lift.install import activate_generated_graph32
    mem = FlatMemory(size=0x10000)
    mem.load(FUNC, SPIN)
    scan = scan_function32(mem.data.__getitem__, FUNC)
    src = emit_function32(scan, f"lift_{FUNC:x}",
                          signature=bytes(mem.data[FUNC:FUNC + 8]),
                          boundary_heads=frozenset({HEAD}))
    (tmp_path / f"lift_{FUNC:x}.py").write_text(src)

    cpu = CPU386(FlatMemory(size=0x10000), eip=0, esp=STACK)
    installed = activate_generated_graph32(cpu, tmp_path)
    assert FUNC in installed
    assert SUCC in cpu.replacement_hooks                       # the resume hook
    assert cpu.hook_names[SUCC] == f"lift_{FUNC:x}_resume_{SUCC:x}"


def test_no_observer_means_the_body_just_runs():
    """boundary_hook=None (the default) is inert: with the wait condition
    already false, the body runs straight through to its ret."""
    mod, _src = _emit()
    cpu = _cpu()
    cpu.mem.w32(POLL, 6)                 # [0x3000] != eax(5): the jz falls to ret
    cpu.mem.w32(STACK, 0xBEEF)           # return address the ret pops
    cpu.r[4] = STACK
    assert cpu.boundary_hook is None
    mod.spin(cpu)
    assert cpu.eip == 0xBEEF             # reached the ret, no park
