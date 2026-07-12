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


class DosInputExhausted(Exception):
    """A blocking console read found the key queue empty.

    The front-end catches this to pump real input (or a demo script) and
    resume; headless runs treat it as the fail-loud boundary."""


def seed_low_memory(mem) -> None:
    """Populate the 1:1-mapped low megabyte with a power-on BIOS environment.

    DOS/4GW maps real-mode low memory into the flat address space, and programs
    read it directly: KE probes the real-mode IVT entry for INT 33h (linear
    0xCC) to detect a mouse driver.  A real machine has every vector pointing
    at a BIOS handler/IRET stub — none are null.  Mirrors
    runtime._init_bios_environment (the 16-bit power-on state).
    """
    d = mem.data
    # IVT entries -> F000:FF53 (the conventional BIOS dummy IRET) for the
    # ranges a real BIOS+DOS+mouse-driver setup populates: CPU/BIOS services
    # (00-1F), DOS (20-2F), mouse (33 — the driver KE requires and this host
    # services), and the IRQ vectors (08-0F live in 00-1F; 70-77).  EMS/VCPI
    # (67h) and other user vectors stay null: seeding them non-null makes
    # programs probe for services we do not host (observed: KE called VCPI
    # DE00h when 67h looked installed).
    for vec in (*range(0x00, 0x30), 0x33, *range(0x70, 0x78)):
        base = vec * 4
        d[base:base + 4] = b"\x53\xFF\x00\xF0"
    d[0xFFF53] = 0xCF  # the IRET itself
    # BIOS data area: CRTC base port (color) at 0040:0063.
    d[0x463], d[0x464] = 0xD4, 0x03
    # Equipment word at 0040:0010: 80x25 color, 1 floppy, FPU present.
    d[0x410], d[0x411] = 0x23, 0x44


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
        # "Conventional memory" for DPMI DOS-block allocation (INT 31h AX=0100):
        # paragraph-aligned low-linear space between the LE image (ends well
        # below 0x60000 for typical titles) and the VGA aperture at 0xA0000.
        self.dos_next = 0x60000
        self.dos_end = 0xA0000
        self.dos_blocks: dict[int, tuple[int, int]] = {}   # selector -> (base, size)
        self._next_selector = 0x80
        self.files: dict[int, object] = {}                 # DOS handle -> python file
        self._next_handle = 5                              # 0-4 reserved (stdin/out/err/aux/prn)
        self.dta = psp_linear + 0x80
        self.exit_code: int | None = None
        # Console input queue (ASCII codes) consumed by INT 21h AH=01/07/08.
        # The front-end/probe seeds it; an empty queue on a blocking read fails
        # loud rather than spinning or inventing a key.
        self.key_queue: list[int] = []
        # Case-insensitive on-disk name resolution cache (DOS games ship
        # upper-case names; the host FS may be case-sensitive).
        self._dir_cache: dict[str, dict[str, str]] = {}
        self.unhandled: list[str] = []
        # VGA input-status reads (03DAh): deterministic per-read toggle of the
        # vertical-retrace bit, the same model DOSMachine._vga_status proved on
        # the 16-bit ports — busy-wait loops make progress in headless runs; an
        # interactive front-end can install a time source later.
        self.vga_status_reads = 0
        self.time_source = None
        self.vga_retrace_active_fraction = 0.28
        # DAC (palette) write state, mirroring the VGA 3C8/3C9 protocol.
        self.dac_write_index = 0
        self.dac_rgb_phase = 0
        self.dac = bytearray(768)
        self.unmodeled_port_reads: dict[int, int] = {}
        self.unmodeled_port_writes: dict[int, int] = {}

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
        if ah == 0x48:                       # allocate DOS memory (BX=paragraphs)
            # DOS/4G maps the low megabyte 1:1 into the flat space, so the
            # returned real-mode segment is directly usable at seg*16 via the
            # flat DS.  BX=0xFFFF is the classic "largest block?" probe.
            paras = cpu.r[EBX] & 0xFFFF
            base = (self.dos_next + 15) & ~15
            avail = (self.dos_end - base) // 16
            if paras > avail:
                self._set_cf(cpu, True)
                cpu.set_reg(EAX, 2, 8)       # insufficient memory
                cpu.set_reg(EBX, 2, avail)
                return
            self.dos_next = base + paras * 16
            cpu.set_reg(EAX, 2, base >> 4)
            return
        if ah == 0x49:                       # free DOS memory block — pool model, no-op
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
        if ah in (0x01, 0x07, 0x08):         # console input (blocking) -> AL
            # 01 echoes, 07/08 don't (08 honors Ctrl-C; not modelled).  A real
            # DOS blocks; an empty queue here is a driver bug, so fail loud.
            if not self.key_queue:
                raise DosInputExhausted(
                    f"INT 21h AH={ah:02X}h console read with empty key_queue "
                    f"at eip=0x{cpu.eip:X}")
            cpu.set_reg(EAX, 1, self.key_queue.pop(0) & 0xFF)
            return
        if ah == 0x0B:                       # console input status -> AL=FF/00
            cpu.set_reg(EAX, 1, 0xFF if self.key_queue else 0x00)
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
    def _alloc_selector(self, cpu: CPU386, base: int) -> int:
        sel = self._next_selector
        self._next_selector += 8
        cpu.selector_bases[sel & 0xFFFC] = base
        return sel

    def _int31(self, cpu: CPU386) -> None:
        ax = self._ax(cpu)
        self._set_cf(cpu, False)
        if ax == 0x0100:                     # allocate DOS memory block (BX=paragraphs)
            paras = cpu.r[EBX] & 0xFFFF
            size = paras * 16
            base = (self.dos_next + 15) & ~15
            if base + size > self.dos_end:
                self._set_cf(cpu, True)
                cpu.set_reg(EAX, 2, 0x0008)              # insufficient memory
                cpu.set_reg(EBX, 2, (self.dos_end - base) // 16)
                return
            self.dos_next = base + size
            sel = self._alloc_selector(cpu, base)
            self.dos_blocks[sel] = (base, size)
            cpu.set_reg(EAX, 2, base >> 4)               # real-mode segment
            cpu.set_reg(EDX, 2, sel)                     # protected-mode selector
            return
        if ax == 0x0101:                     # free DOS memory block (DX=selector)
            self.dos_blocks.pop(cpu.r[EDX] & 0xFFFF, None)
            return
        if ax == 0x0500:                     # get free memory information (ES:EDI buf)
            # 0x30-byte block; first dword = largest available free block in
            # bytes (the field games gate their minimum-RAM check on — KE's
            # box says 2 MB minimum, we report the extended-heap size, 4 MB by
            # default).  Unsupported fields are -1 per the DPMI spec.
            buf = cpu.sbase["es"] + cpu.r[EDI]
            self.mem.data[buf:buf + 0x30] = b"\xFF" * 0x30
            free = self._heap_end - self._heap_next
            self.mem.w32(buf + 0x00, free)               # largest free block
            self.mem.w32(buf + 0x14, free >> 12)         # free pages
            self.mem.w32(buf + 0x18, (self._heap_end - 0x100000) >> 12)  # total pages
            return
        if ax == 0x0006:                     # get segment base address (BX=selector)
            base = cpu.selector_bases.get(cpu.r[EBX] & 0xFFFC, 0)
            cpu.set_reg(ECX, 2, (base >> 16) & 0xFFFF)
            cpu.set_reg(EDX, 2, base & 0xFFFF)
            return
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

    # ---- I/O ports -----------------------------------------------------------
    def port_read(self, cpu: CPU386, port: int, bits: int) -> int:
        if port in (0x3DA, 0x3BA):           # VGA input status 1
            ts = self.time_source
            if ts is None:
                self.vga_status_reads += 1
                # bit 3 = vertical retrace, bit 0 = display-enable NOT active;
                # both track the toggle so either wait style makes progress.
                return 0x09 if (self.vga_status_reads & 1) else 0x00
            phase = (ts() * 70.0) % 1.0
            return 0x09 if phase >= (1.0 - self.vga_retrace_active_fraction) else 0x00
        if port == 0x3C7:                     # DAC state
            return 0x00
        self.unmodeled_port_reads[port] = self.unmodeled_port_reads.get(port, 0) + 1
        return 0

    def port_write(self, cpu: CPU386, port: int, value: int, bits: int) -> None:
        if port == 0x3C8:                     # DAC write index
            self.dac_write_index = value & 0xFF
            self.dac_rgb_phase = 0
            return
        if port == 0x3C9:                     # DAC data (r, g, b per index)
            self.dac[(self.dac_write_index * 3 + self.dac_rgb_phase) % 768] = value & 0x3F
            self.dac_rgb_phase += 1
            if self.dac_rgb_phase == 3:
                self.dac_rgb_phase = 0
                self.dac_write_index = (self.dac_write_index + 1) & 0xFF
            return
        self.unmodeled_port_writes[port] = self.unmodeled_port_writes.get(port, 0) + 1

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
    # Minimal Microsoft mouse driver: state fed by the front-end (or left at
    # rest for headless runs).  Services grown as the game issues them.
    def _int33(self, cpu: CPU386) -> None:
        ax = self._ax(cpu)
        if ax == 0x0000:                     # reset/detect -> AX=FFFF, BX=#buttons
            cpu.set_reg(EAX, 2, 0xFFFF)
            cpu.set_reg(EBX, 2, 2)
            self.mouse_x, self.mouse_y, self.mouse_buttons = 320, 100, 0
            return
        if ax in (0x0001, 0x0002):           # show / hide cursor
            return
        if ax == 0x0003:                     # get position + buttons
            cpu.set_reg(EBX, 2, getattr(self, "mouse_buttons", 0))
            cpu.set_reg(ECX, 2, getattr(self, "mouse_x", 320))
            cpu.set_reg(EDX, 2, getattr(self, "mouse_y", 100))
            return
        if ax == 0x0004:                     # set position (CX, DX)
            self.mouse_x = cpu.r[ECX] & 0xFFFF
            self.mouse_y = cpu.r[EDX] & 0xFFFF
            return
        if ax in (0x0007, 0x0008):           # set horizontal / vertical range
            return
        if ax == 0x000B:                     # read motion counters -> CX/DX deltas
            cpu.set_reg(ECX, 2, 0)
            cpu.set_reg(EDX, 2, 0)
            return
        if ax == 0x0024:                     # get driver version/type/IRQ
            cpu.set_reg(EBX, 2, 0x0814)      # version 8.20 (MS MOUSE.COM convention)
            cpu.set_reg(ECX, 2, 0x0400)      # CH=4 (PS/2), CL=0 (no IRQ for PS/2)
            return
        if ax in (0x0005, 0x0006):           # button press/release data
            cpu.set_reg(EAX, 2, getattr(self, "mouse_buttons", 0))
            cpu.set_reg(EBX, 2, 0)           # count since last query
            cpu.set_reg(ECX, 2, getattr(self, "mouse_x", 320))
            cpu.set_reg(EDX, 2, getattr(self, "mouse_y", 100))
            return
        if ax in (0x000C, 0x000F, 0x0010, 0x001A):  # set handler/ratio/region/sens.
            return
        if ax == 0x001B:                     # get sensitivity -> BX/CX/DX
            cpu.set_reg(EBX, 2, 50)
            cpu.set_reg(ECX, 2, 50)
            cpu.set_reg(EDX, 2, 50)          # double-speed threshold
            return
        raise NotImplementedError(
            f"INT 33h AX=0x{ax:04X} not implemented at eip=0x{cpu.eip:X}")
