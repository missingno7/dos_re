"""A boundary head is a re-entry point too -- not only its successor.

A park leaves IP on the head's SUCCESSOR, so that is where a resumed park comes
back, and RESUME_ENTRIES exported only that. But sitting on the successor is
not the only way to be in a tick-wait. The machine is routinely found ON the
head itself:

  * a snapshot captured while the game was spinning there -- and a game spends
    most of its wall clock in exactly this loop, so this is the LIKELIEST place
    to catch it. Every one of skyroads' gameplay demos starts at 1010:22F8,
    mid-spin.
  * an IRET returning to the head, when an IRQ landed on that instruction.

Neither could dispatch: the head was forced as a block leader but never
exported, so no hook existed at it. Behind the strict-VMless wall that is a
violation at frame 0 -- on the address the game is most often found at, in a
function that WAS lifted and does contain it, which reads like a census bug and
is not one.
"""
from __future__ import annotations

import types

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.memory import Memory

#: cmp ds:[1600],ax ; jz -6 ; ret -- the tick-wait shape (skyroads 22F8/434A).
#: The head is the cmp at offset 0; its successor is the jz at 4.
SPIN = bytes.fromhex("39060016" "74fa" "c3")
HEAD = 0x0000
SUCC = 0x0004


def _build(name: str, heads=frozenset({HEAD})):
    scan = scan_function(lambda off: SPIN[off] if off < len(SPIN) else 0x90, 0)
    src = emit_function(scan, 0x1010, name, signature=SPIN[:4],
                        boundary_heads=heads)
    mod = types.ModuleType(name)
    exec(compile(src, f"{name}.py", "exec"), mod.__dict__)
    return mod, getattr(mod, name)


def _cpu(**kw) -> CPU8086:
    st = dict(cs=0x1010, ds=0x1686, ss=0x4000, sp=0x100, flags=0x0202)
    st.update(kw)
    cpu = CPU8086(Memory(), CPUState(**st))
    cpu.mem.ww(0x4000, 0x0FE, 0xBEEF)      # a return address for the ret
    cpu.s.sp = 0xFE
    return cpu


def test_both_the_head_and_its_successor_are_resume_entries() -> None:
    mod, _fn = _build("spin_entries")
    assert f"1010:{SUCC:04X}" in mod.RESUME_ENTRIES     # the park's resume
    assert f"1010:{HEAD:04X}" in mod.RESUME_ENTRIES, (
        "the head itself must be re-enterable: a snapshot taken mid-spin sits "
        f"exactly there. got {mod.RESUME_ENTRIES}")


def test_the_head_entry_points_at_the_head_block() -> None:
    mod, _fn = _build("spin_block")
    bb = mod.RESUME_ENTRIES[f"1010:{HEAD:04X}"]
    assert mod.BLOCK_ADDRS[bb] == HEAD


def test_resuming_AT_the_head_observes_the_boundary_and_can_park() -> None:
    """The shape a mid-spin snapshot replays: enter at the head's block, the
    head executes, and the observer fires -- so a host that parks gets its
    park, rather than the wall getting a violation."""
    mod, fn = _build("spin_resume")
    cpu = _cpu()
    cpu.mem.ww(0x1686, 0x1600, 0)
    cpu.s.ax = 0                      # tick == ax: the wait cannot exit

    class Parked(Exception):
        pass
    calls = []

    def hook(c, head_cs, head_ip, resume_ip):
        calls.append((head_cs, head_ip, resume_ip))
        c.s.cs, c.s.ip = head_cs, resume_ip
        raise Parked
    cpu.boundary_hook = hook

    try:
        fn(cpu, mod.RESUME_ENTRIES[f"1010:{HEAD:04X}"])
    except Parked:
        pass
    else:
        raise AssertionError("entering at the head never reached the observer")
    assert calls == [(0x1010, HEAD, SUCC)]
    assert (cpu.s.cs, cpu.s.ip) == (0x1010, SUCC)


def test_no_head_declared_means_no_head_resume_entry() -> None:
    """The rule is scoped to declared heads; it does not invent entries."""
    mod, _fn = _build("spin_nohead", heads=frozenset())
    assert not hasattr(mod, "RESUME_ENTRIES")
