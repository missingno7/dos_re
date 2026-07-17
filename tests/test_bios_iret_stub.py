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

import struct
from pathlib import Path

from dos_re.cpu import CPU8086, CPUState
from dos_re.memory import Memory
from dos_re.runtime_core import (BIOS_INT9_ENTRY, BIOS_IRET_ENTRY,
                                 bios_iret_stub, create_runtime_from_image)

#: Every power-on BIOS entry a game can vector to. EVERY load path must install
#: all of them: which handlers exist is a property of the MACHINE, not of how
#: the program happened to be loaded.
BIOS_HOOKS = {BIOS_INT9_ENTRY: "bios_int9_keyboard",
              BIOS_IRET_ENTRY: "bios_iret_stub"}


def _tiny_exe(tmp_path: Path) -> str:
    exe = tmp_path / "T.EXE"
    hdr = bytearray(32)
    hdr[0:2] = b"MZ"
    struct.pack_into("<H", hdr, 2, 32 + 1)
    struct.pack_into("<H", hdr, 4, 1)
    struct.pack_into("<H", hdr, 8, 2)
    exe.write_bytes(bytes(hdr) + b"\xf4")
    return str(exe)


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


def test_exe_runtime_installs_the_same_bios_hooks(tmp_path: Path) -> None:
    """LOAD-PATH PARITY. Which BIOS handlers exist is a property of the
    machine, not of how the program was loaded -- so the EXE path must install
    exactly what the EXE-free path does.

    It did not. The EXE path had INT 09h but not the IRET stub, and nothing
    noticed because that path is normally interpreted, where the 0xCF byte
    works fine. It surfaced only once a snapshot -- restored THROUGH this path
    -- was run behind the VMless wall: first chained timer IRQ, violation at
    F000:FF53, and no game code anywhere near the blame.
    """
    from dos_re.runtime import create_runtime
    rt = create_runtime(_tiny_exe(tmp_path), game_root=str(tmp_path))
    for entry, name in BIOS_HOOKS.items():
        assert rt.cpu.hook_names.get(entry) == name, (
            f"the EXE path is missing the native {name} at "
            f"{entry[0]:04X}:{entry[1]:04X}")


def test_snapshot_resume_installs_the_same_bios_hooks(tmp_path: Path) -> None:
    """A snapshot resume is a THIRD load path (it goes through create_runtime
    and then overwrites memory + CPU state), and it is the one the VMless demo
    differential uses. Restoring the image must not cost the machine its BIOS
    hooks."""
    from dos_re.runtime import create_runtime
    from dos_re.snapshot import load_snapshot, write_snapshot
    exe = _tiny_exe(tmp_path)
    rt = create_runtime(exe, game_root=str(tmp_path))
    write_snapshot(rt, tmp_path / "snap", status="t", steps=0)
    rt2 = load_snapshot(exe, tmp_path / "snap", game_root=str(tmp_path))
    for entry, name in BIOS_HOOKS.items():
        assert rt2.cpu.hook_names.get(entry) == name, (
            f"a snapshot resume lost the native {name}")


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
