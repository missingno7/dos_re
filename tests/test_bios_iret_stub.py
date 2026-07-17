"""The power-on BIOS dummy-IRET stub must be reachable WITHOUT interpretation.

``_init_bios_environment`` points every unused hardware-IRQ vector at
F000:FF53 and writes a single 0xCF (IRET) there.  An interpreted runtime just
executes that byte -- but a strict-VMless runtime may not interpret ANY x86,
and chaining an IRQ vector to "the previous handler" is the universal DOS
idiom (SkyRoads' timer ISR does exactly this: its far jump to the saved old
vector is patched in by its installer, and it fires on EVERY timer tick).
Without a native form of the stub the EXE-free runtime cannot service a single
chained interrupt.
"""
from __future__ import annotations

from pathlib import Path

from dos_re.cpu import CPU8086, CPUState
from dos_re.memory import Memory
from dos_re.runtime_core import (BIOS_IRET_ENTRY, bios_iret_stub,
                                 create_runtime_from_image)


def test_stub_performs_a_real_iret() -> None:
    cpu = CPU8086(Memory(), CPUState(cs=0x1000, ss=0x2000, sp=0x0100))
    cpu.push(0x0246)          # flags
    cpu.push(0x1010)          # cs
    cpu.push(0x4321)          # ip
    sp_before = cpu.s.sp
    bios_iret_stub(cpu)
    assert (cpu.s.cs, cpu.s.ip) == (0x1010, 0x4321)
    assert cpu.s.flags == (0x0246 | 0x0002)
    assert cpu.s.sp == (sp_before + 6) & 0xFFFF     # three words popped


def test_image_runtime_installs_the_stub_as_a_native_hook() -> None:
    # The EXE-free load path must carry the whole power-on BIOS environment as
    # hooks: without this, the first chained IRQ fails the VMless wall.
    rt = create_runtime_from_image(bytes(Memory().data),
                                   CPUState(cs=0x1010, ip=0x0000),
                                   game_root=Path("."))
    assert BIOS_IRET_ENTRY in rt.cpu.replacement_hooks
    assert rt.cpu.hook_names[BIOS_IRET_ENTRY] == "bios_iret_stub"


def test_chained_irq_returns_through_the_stub_with_no_interpretation() -> None:
    """The end-to-end shape: an ISR that chains to the previous vector reaches
    the stub as a HOOK and returns -- the sequence a VMless timer tick makes."""
    rt = create_runtime_from_image(bytes(Memory().data),
                                   CPUState(cs=0x1010, ip=0x0000, ss=0x2000,
                                            sp=0x0100, flags=0x0202),
                                   game_root=Path("."))
    cpu = rt.cpu
    cpu.interp_forbidden = True            # arm the wall
    # Stand in for the game's ISR: push an IRQ frame, then jump to the stub.
    cpu.push(0x0202)                       # flags
    cpu.push(0x1010)                       # cs  (return into "game code")
    cpu.push(0x0500)                       # ip
    cpu.s.cs, cpu.s.ip = BIOS_IRET_ENTRY
    cpu.step()                             # dispatches the hook, never interprets
    assert (cpu.s.cs, cpu.s.ip) == (0x1010, 0x0500)
