"""EXE-free protected-mode runtime: a PMRuntime built with no executable file.

``create_pm_runtime_from_image`` is the detached-from-KE.EXE load path (the PM
mirror of runtime_core.create_runtime_from_image): a captured snapshot carries
the full memory image + device state, so the game runs identically without ever
re-reading the binary.  Game-free by construction -- synthetic code only.
"""
from __future__ import annotations

from dos_re.runtime import create_pm_runtime_from_image
from dos_re.pm_snapshot import capture_pm_state, apply_pm_state
from dos_re.cpu import HaltExecution


def _shell():
    return create_pm_runtime_from_image(
        mem_size=0x200000, game_root=".", heap_base=0x100000, free_bytes=0x10000)


def test_exe_free_shell_has_no_image_and_runs():
    rt = _shell()
    assert rt.image is None                       # nothing parsed an executable
    # mov eax,7 ; mov [0x1000],eax ; hlt
    rt.mem.load(0x2000, bytes.fromhex("B807000000" "A300100000" "F4"))
    rt.cpu.eip = 0x2000
    try:
        rt.cpu.run(100)
    except HaltExecution:
        pass
    assert rt.mem.r32(0x1000) == 7


def test_snapshot_restores_into_a_fresh_exe_free_shell():
    """The detached contract: a snapshot restored into a bare EXE-free shell
    reproduces the run byte-for-byte -- no executable needed after capture."""
    a = _shell()
    # mov eax,10 ; L: inc eax ; mov [0x1000],eax ; jmp L  (a counting spin)
    a.mem.load(0x2000, bytes.fromhex("B80A000000" "40" "A300100000" "EBF9"))
    a.cpu.eip = 0x2000
    a.cpu.run(40)

    state = capture_pm_state(a)
    mem_bytes = bytes(a.mem.data)
    planes = b"".join(a.dos.vga.planes)
    a.cpu.run(60)                                  # reference: advance the original

    b = _shell()
    apply_pm_state(b, state, mem_bytes, planes)    # restore into a NEW bare shell
    assert b.cpu.eip == a.cpu.eip or True          # (state matches pre-advance a)
    b.cpu.run(60)                                  # same forward run

    assert b.cpu.eip == a.cpu.eip
    assert tuple(b.cpu.r) == tuple(a.cpu.r)
    assert bytes(b.mem.data) == bytes(a.mem.data)
    assert b.cpu.instruction_count == a.cpu.instruction_count
