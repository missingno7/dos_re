"""Focused tests for INT 21h AH=1Bh (allocation info) and AH=43h (file
attributes) — the two DOS services VGA Lemmings' boot path exercised that the
DOS machine did not yet model.  Observed contracts:

- AH=1Bh: game reads only the media descriptor byte at the returned DS:BX.
- AH=43h AL=00h: existence probe (e.g. adlib.dat), branch on CF.
"""
from __future__ import annotations

from pathlib import Path

from dos_re.cpu import CPU8086, CPUState, CF
from dos_re.dos import DOSMachine
from dos_re.memory import Memory


def _cpu(mem: Memory, **regs) -> CPU8086:
    return CPU8086(mem, CPUState(**regs))


def test_int21_1b_returns_media_byte_at_ds_bx(tmp_path: Path) -> None:
    mem = Memory()
    dos = DOSMachine(tmp_path)
    cpu = _cpu(mem, ax=0x1B00)
    dos.int21(cpu)
    assert not cpu.get_flag(CF)
    assert cpu.s.ax & 0xFF == 0x04          # sectors per cluster
    assert cpu.s.cx == 512                  # bytes per sector
    # DS:BX must point at a valid media descriptor byte.
    assert mem.rb(cpu.s.ds, cpu.s.bx) == 0xF8


def _put_asciiz(mem: Memory, seg: int, off: int, name: str) -> None:
    for i, ch in enumerate(name.encode("ascii")):
        mem.wb(seg, off + i, ch)
    mem.wb(seg, off + len(name), 0)


def test_int21_43_get_existing_file_clears_carry(tmp_path: Path) -> None:
    (tmp_path / "adlib.dat").write_bytes(b"x")
    mem = Memory()
    dos = DOSMachine(tmp_path)
    _put_asciiz(mem, 0x2000, 0x0000, "ADLIB.DAT")
    cpu = _cpu(mem, ax=0x4300, ds=0x2000, dx=0x0000)
    dos.int21(cpu)
    assert not cpu.get_flag(CF)
    assert cpu.s.cx == 0x20                  # normal-file attributes


def test_int21_43_get_missing_file_sets_carry_not_found(tmp_path: Path) -> None:
    mem = Memory()
    dos = DOSMachine(tmp_path)
    _put_asciiz(mem, 0x2000, 0x0000, "NOPE.DAT")
    cpu = _cpu(mem, ax=0x4300, ds=0x2000, dx=0x0000)
    dos.int21(cpu)
    assert cpu.get_flag(CF)
    assert cpu.s.ax == 2                      # DOS "file not found"


def test_int21_43_set_attributes_succeeds(tmp_path: Path) -> None:
    mem = Memory()
    dos = DOSMachine(tmp_path)
    _put_asciiz(mem, 0x2000, 0x0000, "ANY.DAT")
    cpu = _cpu(mem, ax=0x4301, cx=0x0000, ds=0x2000, dx=0x0000)
    dos.int21(cpu)
    assert not cpu.get_flag(CF)


def test_snapshot_persists_console_input_state(tmp_path):
    """key_queue / fallback are machine state: a cold-start demo's snapshot
    must restore them or the boot menu blocks forever on replay."""
    from dos_re.runtime import create_runtime
    from dos_re.snapshot import write_snapshot, load_snapshot
    exe = tmp_path / "T.EXE"
    import struct
    hdr = bytearray(32); hdr[0:2] = b"MZ"
    struct.pack_into("<H", hdr, 2, 32 + 1); struct.pack_into("<H", hdr, 4, 1)
    struct.pack_into("<H", hdr, 8, 2)
    exe.write_bytes(bytes(hdr) + b"\xf4")
    rt = create_runtime(str(exe), game_root=str(tmp_path))
    rt.dos.key_queue = [0x0231, 0x1C0D]
    rt.dos.console_input_fallback = 0x0D
    write_snapshot(rt, tmp_path / "snap", status="t", steps=0)
    rt2 = load_snapshot(str(exe), tmp_path / "snap", game_root=str(tmp_path))
    assert rt2.dos.key_queue == [0x0231, 0x1C0D]
    assert rt2.dos.console_input_fallback == 0x0D
