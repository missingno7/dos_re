"""Writable-file overlay + opt-in persistence sink (INT 21h create/write/close).

The 16-bit file layer always kept program writes in memory and discarded them on
close -- deterministic, but a game that saves progress (Skyroads rewrites
SKYROADS.CFG after each finished level) never remembered anything.  The overlay
now lets a run read back what it just wrote (still zero disk I/O by default), and
an opt-in ``save_dir`` flushes on close so the interactive product persists
progress.  With ``save_dir`` unset (replays/tests/headless) nothing touches disk
and the shipped assets stay pristine.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from dos_re.dos import DOSMachine


class _Mem:
    def __init__(self) -> None:
        self.b = bytearray(0x200000)

    def _lin(self, seg: int, off: int) -> int:
        return ((seg << 4) + off) & 0x1FFFFF

    def rb(self, seg: int, off: int) -> int:
        return self.b[self._lin(seg, off)]

    def wb(self, seg: int, off: int, v: int) -> None:
        self.b[self._lin(seg, off)] = v & 0xFF

    def block(self, seg: int, off: int, n: int) -> bytes:
        base = self._lin(seg, off)
        return bytes(self.b[base:base + n])


class _S:
    ax = bx = cx = dx = ds = 0


class _Cpu:
    def __init__(self) -> None:
        self.s = _S()
        self.mem = _Mem()

    def set_flag(self, flag, value) -> None:  # noqa: ANN001
        pass


def _put_name(cpu: _Cpu, name: str, at: int = 0x100) -> None:
    for i, ch in enumerate(name.encode()):
        cpu.mem.wb(0, at + i, ch)
    cpu.mem.wb(0, at + len(name), 0)
    cpu.s.ds, cpu.s.dx = 0, at


def _create(dos: DOSMachine, cpu: _Cpu, name: str) -> int:
    _put_name(cpu, name)
    cpu.s.ax = 0x3C00
    dos.int21(cpu)
    return cpu.s.ax


def _write(dos: DOSMachine, cpu: _Cpu, handle: int, data: bytes, at: int = 0x2000) -> None:
    for i, b in enumerate(data):
        cpu.mem.wb(0, at + i, b)
    cpu.s.bx, cpu.s.cx, cpu.s.ds, cpu.s.dx, cpu.s.ax = handle, len(data), 0, at, 0x4000
    dos.int21(cpu)


def _close(dos: DOSMachine, cpu: _Cpu, handle: int) -> None:
    cpu.s.bx, cpu.s.ax = handle, 0x3E00
    dos.int21(cpu)


def _open_read(dos: DOSMachine, cpu: _Cpu, name: str, n: int, at: int = 0x3000) -> "bytes | None":
    _put_name(cpu, name)
    cpu.s.ax = 0x3D00
    dos.int21(cpu)
    if cpu.s.ax in (2, 6):   # not found / bad handle -> CF path returns error code in AX
        return None
    handle = cpu.s.ax
    cpu.s.bx, cpu.s.cx, cpu.s.ds, cpu.s.dx, cpu.s.ax = handle, n, 0, at, 0x3F00
    dos.int21(cpu)
    return cpu.mem.block(0, at, n)


@pytest.fixture()
def game_dir(tmp_path: Path) -> Path:
    root = tmp_path / "assets"
    root.mkdir()
    (root / "SKYROADS.CFG").write_bytes(bytes([0x10, 0x02] + [0] * 64))
    return root


def test_default_drops_writes_on_close_so_a_reopen_reads_the_pristine_file(game_dir: Path) -> None:
    """With persistence OFF (the deterministic default) a write does NOT survive
    close: a re-open reads the original file, byte-for-byte the legacy behaviour.
    This MUST hold or a replay where the game rewrites then re-reads a file (e.g.
    Skyroads reloading SKYROADS.CFG after a level break) would replay differently
    from how it was recorded."""
    dos, cpu = DOSMachine(root=game_dir), _Cpu()      # save_dir unset
    new = bytes([0x10, 0x02, 0, 0, 0, 0, 3, 0, 1] + [0] * 57)
    h = _create(dos, cpu, "SKYROADS.CFG")
    _write(dos, cpu, h, new)
    _close(dos, cpu, h)
    got = _open_read(dos, cpu, "SKYROADS.CFG", 66)
    assert got is not None and got[6] == 0 and got[8] == 0   # pristine, not the write
    assert (game_dir / "SKYROADS.CFG").read_bytes()[6] == 0   # disk untouched too


def test_overlay_reads_back_own_write_only_when_persistence_is_on(game_dir: Path, tmp_path: Path) -> None:
    dos, cpu = DOSMachine(root=game_dir, save_dir=tmp_path / "saves"), _Cpu()
    new = bytes([0x10, 0x02, 0, 0, 0, 0, 3, 0, 1] + [0] * 57)
    h = _create(dos, cpu, "SKYROADS.CFG")
    _write(dos, cpu, h, new)
    _close(dos, cpu, h)
    got = _open_read(dos, cpu, "SKYROADS.CFG", 66)
    assert got is not None and got[6] == 3 and got[8] == 1   # sees its own bytes
    assert (game_dir / "SKYROADS.CFG").read_bytes()[6] == 0   # shipped asset pristine


def test_default_never_writes_to_disk(game_dir: Path, tmp_path: Path) -> None:
    save = tmp_path / "saves"
    dos, cpu = DOSMachine(root=game_dir), _Cpu()   # save_dir unset
    h = _create(dos, cpu, "SKYROADS.CFG")
    _write(dos, cpu, h, bytes([9] * 66))
    _close(dos, cpu, h)
    assert not save.exists()


def test_save_dir_persists_on_close_and_a_fresh_run_reads_it_back(game_dir: Path, tmp_path: Path) -> None:
    save = tmp_path / "saves"
    new = bytes([0x10, 0x02, 0, 0, 0, 0, 3, 0, 1] + [0] * 57)

    dos, cpu = DOSMachine(root=game_dir, save_dir=save), _Cpu()
    h = _create(dos, cpu, "SKYROADS.CFG")
    _write(dos, cpu, h, new)
    _close(dos, cpu, h)
    assert (save / "SKYROADS.CFG").is_file()          # flushed on close

    # a brand-new machine (a later run) with the same save_dir sees the progress,
    # NOT the pristine shipped asset.
    dos2, cpu2 = DOSMachine(root=game_dir, save_dir=save), _Cpu()
    got = _open_read(dos2, cpu2, "SKYROADS.CFG", 66)
    assert got is not None and got[6] == 3 and got[8] == 1
    # even with persistence on, the shipped asset dir is never mutated.
    assert (game_dir / "SKYROADS.CFG").read_bytes()[6] == 0


def test_overlay_wins_over_a_stale_saved_copy_within_a_run(game_dir: Path, tmp_path: Path) -> None:
    save = tmp_path / "saves"
    save.mkdir()
    (save / "SKYROADS.CFG").write_bytes(bytes([0x10, 0x02, 0, 0, 0, 0, 1] + [0] * 59))  # old save
    dos, cpu = DOSMachine(root=game_dir, save_dir=save), _Cpu()
    new = bytes([0x10, 0x02, 0, 0, 0, 0, 7] + [0] * 59)
    h = _create(dos, cpu, "SKYROADS.CFG")
    _write(dos, cpu, h, new)
    _close(dos, cpu, h)
    got = _open_read(dos, cpu, "SKYROADS.CFG", 66)
    assert got is not None and got[6] == 7   # freshest in-run write, not the old save
