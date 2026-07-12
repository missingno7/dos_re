"""DOS4GWHost — protected-mode DOS + DPMI services for a CPU386 flat program.

Origin: added for Krypton Egg (first DOS/4GW title).  Stands in for the DOS/4GW
extender's protected-mode interrupt layer: the game issues ``INT 21h`` / ``INT
31h`` / ``INT 10h`` / ``INT 33h`` in protected mode and this services them
against a flat :class:`~dos_re.cpu386.FlatMemory`, reading/writing 32-bit
registers directly.

Deliberately grown from observed calls only (dos_re/AGENTS.md): every service
this game does not exercise raises loudly with the exact AX so the next thing to
implement is always named.  No "return success" stubs.

Register contract note: in the flat DOS/4GW model, "DS:DX" / "DS:EDX" pointers
are just 32-bit linear offsets (segment base 0), so a DOS call that took DS:DX
in real mode takes EDX here.
"""
from __future__ import annotations

import os
from pathlib import Path

from .cpu386 import CPU386, EAX, EBX, ECX, EDX, ESI, EDI

# Reported environment.
DOS_MAJOR = 6
DOS_MINOR = 22


class DosError(Exception):
    """A DOS call failed; the handler sets CF and AX to the DOS error code."""
    def __init__(self, code: int):
        super().__init__(f"DOS error {code}")
        self.code = code


class DOS4GWHost:
    def __init__(self, mem, game_root: str | Path, *,
                 command_tail: bytes = b"", psp_linear: int = 0x500,
                 heap_base: int = 0x100000, free_bytes: int = 4 * 1024 * 1024):
        self.mem = mem
        self.root = Path(game_root)
        self.command_tail = command_tail
        self.psp_linear = psp_linear
        # Bump heap in the flat space above the 1 MB mark (VGA/BIOS live below).
        self._heap_next = heap_base
        self._heap_end = heap_base + free_bytes
        self.free_bytes = free_bytes
        self.pm_vectors: dict[int, tuple[int, int]] = {}   # int# -> (selector, offset)
        self.files: dict[int, object] = {}                 # DOS handle -> python file
        self._next_handle = 5                              # 0-4 reserved (stdin/out/err/aux/prn)
        self.dta = psp_linear + 0x80
        self.exit_code: int | None = None
        # Case-insensitive on-disk name resolution cache (DOS games ship
        # upper-case names; the host FS may be case-sensitive).
        self._dir_cache: dict[str, dict[str, str]] = {}
        self.unhandled: list[str] = []

    # ---- register helpers ---------------------------------------------------
    @staticmethod
    def _ax(cpu): return cpu.r[EAX] & 0xFFFF
    @staticmethod
    def _ah(cpu): return (cpu.r[EAX] >> 8) & 0xFF
    @staticmethod
    def _al(cpu): return cpu.r[EAX] & 0xFF

    def _set_cf(self, cpu, on):
        from .cpu386 import CF
        if on:
            cpu.eflags |= CF
        else:
            cpu.eflags &= ~CF

    # ---- dispatch -----------------------------------------------------------
    def interrupt(self, cpu: CPU386, num: int) -> None:
        if num == 0x21:
            self._int21(cpu)
        elif num == 0x31:
            self._int31(cpu)
        elif num == 0x10:
            self._int10(cpu)
        elif num == 0x33:
            self._int33(cpu)
        elif num == 0x2F:
            self._int2f(cpu)
        else:
            raise NotImplementedError(
                f"INT 0x{num:02X} (AX=0x{self._ax(cpu):04X}) not implemented at "
                f"eip=0x{cpu.eip:X}")

    # ---- INT 21h ------------------------------------------------------------
    def _int21(self, cpu: CPU386) -> None:
        ah = self._ah(cpu)
        self._set_cf(cpu, False)
        if ah == 0x30:                       # get DOS version
            cpu.set_reg(EAX, 2, (DOS_MINOR << 8) | DOS_MAJOR)   # AL=major, AH=minor
            cpu.set_reg(EBX, 2, 0xFF00)      # BH=OEM (0xFF), BL=0
            cpu.set_reg(ECX, 2, 0)
            return
        if ah == 0x25:                       # set interrupt vector: AL=int, EDX=handler
            self.pm_vectors[self._al(cpu)] = (cpu.seg["ds"], cpu.r[EDX])
            return
        if ah == 0x35:                       # get interrupt vector: AL=int -> ES:EBX
            sel, off = self.pm_vectors.get(self._al(cpu), (0, 0))
            cpu.seg["es"] = sel
            cpu.set_reg(EBX, 4, off)
            return
        if ah == 0x19:                       # current drive -> AL (0=A)
            cpu.set_reg(EAX, 1, 2)           # C:
            return
        if ah == 0xED:                       # DOS/4GW heap/selector query (sbrk glue)
            # The C-runtime sbrk (KE 0x2a3f8) consumes only bit 0 of the result:
            # 0 => the flat DS segment can be resized in place (which it always
            # can in the flat model), taking the AH=4A DS-resize path.
            cpu.set_reg(EAX, 1, 0)
            return
        if ah == 0x4A:                       # resize memory block (ES=sel, BX=paras)
            # Flat 16 MB space: the DS/flat segment is effectively unbounded, so
            # any in-place grow succeeds.  Return CF clear.
            self._set_cf(cpu, False)
            return
        if ah == 0xFF:                       # Watcom/DOS4GW extender-detection probe
            # AX=FF00: the C-runtime startup probes which extender/DPMI host is
            # present.  AL=0 selects the DOS/4GW-native (non-DPMI) startup path,
            # which matches our flat DOS/4GW emulation.  (See KE startup 0x24412.)
            cpu.set_reg(EAX, 1, 0)
            return
        if ah == 0x4C:                       # terminate with exit code
            self.exit_code = self._al(cpu)
            cpu.halted = True
            return
        if ah == 0x1A:                       # set DTA (EDX)
            self.dta = cpu.r[EDX]
            return
        if ah == 0x44:                       # IOCTL
            al = self._al(cpu)
            if al == 0x00:                   # get device info: BX=handle -> DX
                h = cpu.r[EBX] & 0xFFFF
                # handles 0/1/2 are the console (character device / "is a tty");
                # everything else is a disk file (bit 7 clear).
                cpu.set_reg(EDX, 2, 0x80D3 if h in (0, 1, 2) else 0x0000)
                return
            if al == 0x01:                   # set device info — accept, no effect
                return
            raise NotImplementedError(
                f"INT 21h AX=44{al:02X}h (IOCTL) not implemented at eip=0x{cpu.eip:X}")
        if ah == 0x3D:                       # open file, EDX->asciiz name, AL=mode
            return self._open(cpu)
        if ah == 0x3E:                       # close file (BX=handle)
            return self._close(cpu)
        if ah == 0x3F:                       # read (BX=handle, ECX=len, EDX=buf)
            return self._read(cpu)
        if ah == 0x40:                       # write (BX=handle, ECX=len, EDX=buf)
            return self._write(cpu)
        if ah == 0x42:                       # lseek (BX=handle, CX:DX/ECX=off, AL=whence)
            return self._seek(cpu)
        raise NotImplementedError(
            f"INT 21h AH=0x{ah:02X} (AX=0x{self._ax(cpu):04X}) not implemented at "
            f"eip=0x{cpu.eip:X}")

    # ---- file services ------------------------------------------------------
    def _read_cstr(self, addr: int) -> str:
        d = self.mem.data
        end = addr
        while d[end] != 0:
            end += 1
        return d[addr:end].decode("latin-1")

    def _resolve(self, dos_name: str) -> Path | None:
        name = dos_name.replace("\\", "/").split("/")[-1]
        direct = self.root / name
        if direct.exists():
            return direct
        # case-insensitive fallback
        lower = name.lower()
        for entry in self.root.iterdir():
            if entry.name.lower() == lower:
                return entry
        return None

    def _open(self, cpu):
        name = self._read_cstr(cpu.r[EDX])
        path = self._resolve(name)
        if path is None:
            self._set_cf(cpu, True)
            cpu.set_reg(EAX, 2, 2)          # file not found
            return
        mode = self._al(cpu) & 0x03
        pymode = "rb" if mode == 0 else ("wb" if mode == 1 else "r+b")
        h = self._next_handle
        self._next_handle += 1
        self.files[h] = open(path, pymode)
        cpu.set_reg(EAX, 2, h)
        return

    def _close(self, cpu):
        h = cpu.r[EBX] & 0xFFFF
        f = self.files.pop(h, None)
        if f:
            f.close()
        return

    def _read(self, cpu):
        h = cpu.r[EBX] & 0xFFFF
        n = cpu.r[ECX]
        buf = cpu.r[EDX]
        f = self.files.get(h)
        if f is None:
            self._set_cf(cpu, True); cpu.set_reg(EAX, 2, 6); return  # invalid handle
        data = f.read(n)
        self.mem.data[buf:buf + len(data)] = data
        cpu.set_reg(EAX, 4, len(data))
        return

    def _write(self, cpu):
        h = cpu.r[EBX] & 0xFFFF
        n = cpu.r[ECX]
        buf = cpu.r[EDX]
        if h in (1, 2):                      # stdout / stderr
            os.write(h, self.mem.data[buf:buf + n])
            cpu.set_reg(EAX, 4, n)
            return
        f = self.files.get(h)
        if f is None:
            self._set_cf(cpu, True); cpu.set_reg(EAX, 2, 6); return
        f.write(self.mem.data[buf:buf + n])
        cpu.set_reg(EAX, 4, n)
        return

    def _seek(self, cpu):
        h = cpu.r[EBX] & 0xFFFF
        off = cpu.r[EDX] | ((cpu.r[ECX] & 0xFFFF) << 16)  # CX:DX in real mode; flat uses full EDX too
        whence = self._al(cpu)
        f = self.files.get(h)
        if f is None:
            self._set_cf(cpu, True); cpu.set_reg(EAX, 2, 6); return
        pos = f.seek(off, whence)
        cpu.set_reg(EAX, 2, pos & 0xFFFF)
        cpu.set_reg(EDX, 2, (pos >> 16) & 0xFFFF)
        return

    # ---- INT 31h (DPMI) -----------------------------------------------------
    def _int31(self, cpu: CPU386) -> None:
        ax = self._ax(cpu)
        self._set_cf(cpu, False)
        raise NotImplementedError(
            f"INT 31h (DPMI) AX=0x{ax:04X} not implemented at eip=0x{cpu.eip:X}")

    # ---- INT 10h (video) ----------------------------------------------------
    def _int10(self, cpu: CPU386) -> None:
        ah = self._ah(cpu)
        if ah == 0x00:                       # set video mode (AL=mode)
            self.video_mode = self._al(cpu) & 0x7F
            return
        if ah == 0x0F:                       # get video mode -> AL=mode, AH=cols
            cpu.set_reg(EAX, 2, (40 << 8) | getattr(self, "video_mode", 0x03))
            cpu.set_reg(EBX, 1, 0)           # BH = active page 0
            return
        if ah == 0x1A:                       # display combination code
            if self._al(cpu) == 0x00:        # get: AL=1A (supported), BL=VGA+color
                cpu.set_reg(EAX, 1, 0x1A)
                cpu.set_reg(EBX, 2, 0x0008)  # BL=08 (VGA analog color), BH=00
                return
            return                            # set DCC — accept
        raise NotImplementedError(
            f"INT 10h AH=0x{ah:02X} (AX=0x{self._ax(cpu):04X}) not implemented at "
            f"eip=0x{cpu.eip:X}")

    # ---- INT 2Fh (multiplex) ------------------------------------------------
    def _int2f(self, cpu: CPU386) -> None:
        ax = self._ax(cpu)
        if ax == 0x4300:                     # XMS installation check
            # No XMS driver: DOS/4GW games get memory from the flat heap, not
            # XMS.  AL != 0x80 means "not installed".
            cpu.set_reg(EAX, 1, 0x00)
            return
        if ax == 0x1687:                      # DPMI installation check
            # Report DPMI absent (AX stays nonzero): the program is already
            # running the flat protected-mode LE image and gets memory from the
            # DOS/4GW heap path, so it does not need the DPMI mode-switch entry.
            # (Flip to "present" + INT 31h services if a real DPMI path appears.)
            return
        if ax == 0x1686:                      # "are we in protected mode?" -> AX=0 yes
            cpu.set_reg(EAX, 2, 0)
            return
        raise NotImplementedError(
            f"INT 2Fh AX=0x{ax:04X} not implemented at eip=0x{cpu.eip:X}")

    # ---- INT 33h (mouse) ----------------------------------------------------
    def _int33(self, cpu: CPU386) -> None:
        ax = self._ax(cpu)
        raise NotImplementedError(
            f"INT 33h AX=0x{ax:04X} not implemented at eip=0x{cpu.eip:X}")
