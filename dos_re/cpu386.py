"""CPU386 — a 32-bit flat protected-mode interpreter for DOS/4GW-style games.

Origin: added for Krypton Egg (the first DOS/4GW / Watcom LE title in the
ecosystem).  The existing :class:`~dos_re.cpu.CPU8086` is a 16-bit real-mode
core; a flat 32-bit program needs a different register/operand/address model,
so this is a separate class (sharing flag semantics and the same integration
surface — ``interrupt_handler``/``port_reader``/``port_writer``/
``coverage_telemetry``/``instruction_count``) rather than a mode bolted onto
the hot 16-bit path.

Model — deliberately narrow (dos_re/AGENTS.md: "do not make the emulator more
general than a real target requires"):

* **Flat segmentation.** DOS/4GW maps every selector to base 0, limit 4 GB.
  Segment registers hold selector *values* (some code stores/compares them),
  but every effective address is the plain 32-bit offset into one linear image.
  No descriptor tables, no paging, no privilege checks — none of which the
  target exercises.
* **Default 32-bit** operand and address size (the code object's D bit = 1);
  a ``0x66`` prefix selects 16-bit operands, ``0x67`` 16-bit addressing.
* **Interrupts are serviced like syscalls**: ``INT n`` calls
  ``interrupt_handler(cpu, n)`` which reads/writes registers and returns — the
  DOS/4GW host (dos4gw.py) stands in for the extender's PM interrupt layer.

Every unimplemented opcode/prefix raises :class:`UnsupportedInstruction` with
the byte and linear address, so the next thing to add is always named.
Stdlib-only, scalar hot path.
"""
from __future__ import annotations

import struct
from typing import Any

from .cpu import CF, PF, AF, ZF, SF, TF, IF, DF, OF, _PARITY, UnsupportedInstruction, HaltExecution

# GP register indices (ModRM reg/rm order).
EAX, ECX, EDX, EBX, ESP, EBP, ESI, EDI = range(8)
_REG32 = ("eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi")
_REG16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")
_REG8 = ("al", "cl", "dl", "bl", "ah", "ch", "dh", "bh")
_SEG = ("es", "cs", "ss", "ds", "fs", "gs")
_SEG_PREFIX = {0x26: "es", 0x2E: "cs", 0x36: "ss", 0x3E: "ds", 0x64: "fs", 0x65: "gs"}
# opcode -> (segment name, is_push).  FS/GS use 0F A0/A1/A8/A9 (see _two_byte).
_SEG_PUSHPOP = {
    0x06: ("es", True), 0x07: ("es", False),
    0x0E: ("cs", True),
    0x16: ("ss", True), 0x17: ("ss", False),
    0x1E: ("ds", True), 0x1F: ("ds", False),
}


def _sign(v: int, bits: int) -> int:
    m = 1 << (bits - 1)
    return (v & (m - 1)) - (v & m)


class FlatMemory:
    """A single flat linear byte space (little-endian).  base 0 == index 0.

    Large enough to hold the loaded image, the low-1 MB region DOS/4GW maps for
    VGA (A0000h) / BIOS, and the runtime heap the DOS host hands out above the
    program.  ``data`` is a plain bytearray so snapshot/renderers can view it.

    When ``vga`` is attached AND unchained (Mode X), accesses to the A000h
    aperture route through the planar model; otherwise (the common case and
    plain mode 13h) every access is a direct bytearray hit — the hot path pays
    one attribute test per access only while a VGA device is attached.
    """

    def __init__(self, size: int = 16 * 1024 * 1024):
        self.data = bytearray(size)
        self.size = size
        self.vga = None    # VGASequencer, attached by the host when unchained

    def r8(self, a: int) -> int:
        v = self.vga
        if v is not None and 0xA0000 <= a < 0xB0000:
            return v.read(a - 0xA0000)
        return self.data[a]

    def r16(self, a: int) -> int:
        v = self.vga
        if v is not None and 0xA0000 <= a < 0xB0000:
            return v.read(a - 0xA0000) | (v.read(a - 0xA0000 + 1) << 8)
        d = self.data
        return d[a] | (d[a + 1] << 8)

    def r32(self, a: int) -> int:
        v = self.vga
        if v is not None and 0xA0000 <= a < 0xB0000:
            o = a - 0xA0000
            return (v.read(o) | (v.read(o + 1) << 8) |
                    (v.read(o + 2) << 16) | (v.read(o + 3) << 24))
        d = self.data
        return d[a] | (d[a + 1] << 8) | (d[a + 2] << 16) | (d[a + 3] << 24)

    def read(self, a: int, size: int) -> int:
        return self.r8(a) if size == 1 else (self.r16(a) if size == 2 else self.r32(a))

    def w8(self, a: int, v: int) -> None:
        g = self.vga
        if g is not None and 0xA0000 <= a < 0xB0000:
            g.write(a - 0xA0000, v & 0xFF)
            return
        self.data[a] = v & 0xFF

    def w16(self, a: int, v: int) -> None:
        g = self.vga
        if g is not None and 0xA0000 <= a < 0xB0000:
            o = a - 0xA0000
            g.write(o, v & 0xFF)
            g.write(o + 1, (v >> 8) & 0xFF)
            return
        d = self.data
        d[a] = v & 0xFF
        d[a + 1] = (v >> 8) & 0xFF

    def w32(self, a: int, v: int) -> None:
        g = self.vga
        if g is not None and 0xA0000 <= a < 0xB0000:
            o = a - 0xA0000
            g.write(o, v & 0xFF)
            g.write(o + 1, (v >> 8) & 0xFF)
            g.write(o + 2, (v >> 16) & 0xFF)
            g.write(o + 3, (v >> 24) & 0xFF)
            return
        d = self.data
        d[a] = v & 0xFF
        d[a + 1] = (v >> 8) & 0xFF
        d[a + 2] = (v >> 16) & 0xFF
        d[a + 3] = (v >> 24) & 0xFF

    def write(self, a: int, size: int, v: int) -> None:
        if size == 1:
            self.w8(a, v)
        elif size == 2:
            self.w16(a, v)
        else:
            self.w32(a, v)

    def block(self, a: int, n: int) -> bytes:
        return bytes(self.data[a:a + n])

    def load(self, a: int, payload: bytes) -> None:
        self.data[a:a + len(payload)] = payload


class CPU386:
    def __init__(self, mem: FlatMemory, eip: int, esp: int,
                 cs: int = 0x000C, ds: int = 0x0014):
        self.mem = mem
        self.r = [0, 0, 0, 0, esp & 0xFFFFFFFF, 0, 0, 0]   # eax..edi (esp at index 4)
        self.eip = eip & 0xFFFFFFFF
        self.eflags = 0x0202
        self.seg = {"es": ds, "cs": cs, "ss": ds, "ds": ds, "fs": 0, "gs": 0}
        # Mini descriptor table: selector (RPL masked) -> linear base.  The
        # LE's own flat selectors resolve to 0; DPMI-allocated DOS-memory
        # selectors (base = paragraph*16) are registered here by the host.
        # ``sbase`` caches the resolved base per segment register so the hot
        # path pays one attribute read + add, not a dict probe per access.
        self.selector_bases: dict[int, int] = {}
        self.sbase = {"es": 0, "cs": 0, "ss": 0, "ds": 0, "fs": 0, "gs": 0}
        self.halted = False
        self.instruction_count = 0
        # x87 FPU: st[-1] is ST(0).  Doubles stand in for the 80-bit registers
        # (same precision caveat as CPU8086.execute_fpu).  Grown on demand.
        self.st: list[float] = []
        self.fcw = 0x037F
        self.fsw = 0x0000
        # Control registers.  Flat DOS/4GW: protected mode (PE), 387 present
        # (MP|ET), paging bit tracked but not modelled.  Writes are stored so
        # reads stay consistent; their real effects (EM/paging) do not apply to
        # the flat model.
        self.cr = {0: 0x00000013, 2: 0, 3: 0, 4: 0}
        # Integration surface shared with CPU8086 (see module docstring).
        self.interrupt_handler = None
        self.port_reader = None
        self.port_writer = None
        self.coverage_telemetry = None
        # Hardware-IRQ source: polled each step while IF is set; returning an
        # IRQ number delivers it through ``idt`` (the protected-mode vectors
        # the program installed via the DOS/4GW host's AH=25).  None on the
        # deterministic default path, same contract as CPU8086.pending_irq.
        self.pending_irq = None
        # Called with the IRQ number when an acknowledged hardware interrupt has
        # no installed handler.  A real PIC has set the in-service bit by the
        # time the CPU vectors; the BIOS unexpected-interrupt stub still sends
        # EOI, so without this the orphaned in-service bit would wedge the PIC
        # (one-in-service rule) and block every later IRQ — e.g. a Sound
        # Blaster whose block-complete IRQ arrives while its vector is briefly
        # uninstalled would play exactly one block and then go silent.
        self.irq_eoi = None
        self.idt: dict[int, tuple[int, int]] = {}
        # Replacement hooks, keyed by flat linear EIP (the PM analogue of
        # CPU8086's (cs, ip) keys).  Same dispatch contract: when the next
        # instruction's address has a hook, the handler runs INSTEAD of the
        # original code and counts as one instruction; a hook_verifier wraps
        # the call for differential proof (pm_verification.py).
        self.replacement_hooks: dict[int, Any] = {}
        self.hook_names: dict[int, str] = {}
        self.hook_verifier = None
        self.hook_verifier_passthrough: set[int] = set()
        # Observation seam: {flat EIP: callable(cpu)} fired BEFORE the
        # instruction (or hook) at that address executes.  Unlike a
        # replacement hook it never replaces execution, never counts an
        # instruction, and never re-enters the IRQ poll — so observing an
        # execution cannot perturb it (virtual time and IRQ delivery points
        # stay identical with probes installed or absent).  Evidence
        # collection (replay function visits, transfer observation) hangs
        # here; None keeps the per-step cost to one attribute test.
        self.entry_probes: dict[int, Any] | None = None
        # Decode scratch reset each instruction.
        self._opsize = 4
        self._adsize = 4
        self._segovr = None
        self._defseg = "ds"
        self._irq_decim = 0

    # ---- segments ------------------------------------------------------------
    def set_seg(self, name: str, sel: int) -> None:
        """Load a segment register: store the selector and cache its base.

        RPL (low 2 bits) is masked for the base lookup, like real descriptor
        resolution; unknown selectors are flat (base 0) — the LE model."""
        sel &= 0xFFFF
        self.seg[name] = sel
        self.sbase[name] = self.selector_bases.get(sel & 0xFFFC, 0)

    # ---- registers ----------------------------------------------------------
    def reg(self, i: int, size: int) -> int:
        if size == 4:
            return self.r[i]
        if size == 2:
            return self.r[i] & 0xFFFF
        # 8-bit: 0..3 low byte, 4..7 high byte of r[i-4]
        if i < 4:
            return self.r[i] & 0xFF
        return (self.r[i - 4] >> 8) & 0xFF

    def set_reg(self, i: int, size: int, v: int) -> None:
        if size == 4:
            self.r[i] = v & 0xFFFFFFFF
        elif size == 2:
            self.r[i] = (self.r[i] & 0xFFFF0000) | (v & 0xFFFF)
        elif i < 4:
            self.r[i] = (self.r[i] & 0xFFFFFF00) | (v & 0xFF)
        else:
            self.r[i - 4] = (self.r[i - 4] & 0xFFFF00FF) | ((v & 0xFF) << 8)

    # ---- flags --------------------------------------------------------------
    def get_flag(self, f: int) -> bool:
        return bool(self.eflags & f)

    def set_flag(self, f: int, on: bool) -> None:
        if on:
            self.eflags |= f
        else:
            self.eflags &= ~f
        self.eflags |= 0x0002

    def _flags_logic(self, r: int, bits: int) -> None:
        mask = (1 << bits) - 1
        r &= mask
        f = self.eflags & ~(CF | PF | ZF | SF | OF)
        if r == 0:
            f |= ZF
        if r & (1 << (bits - 1)):
            f |= SF
        if _PARITY[r & 0xFF]:
            f |= PF
        self.eflags = f | 0x0002

    def _flags_add(self, a: int, b: int, res: int, bits: int, carry: int = 0) -> None:
        mask = (1 << bits) - 1
        sign = 1 << (bits - 1)
        r = res & mask
        f = self.eflags & ~(CF | PF | AF | ZF | SF | OF)
        if res > mask:
            f |= CF
        if r == 0:
            f |= ZF
        if r & sign:
            f |= SF
        if _PARITY[r & 0xFF]:
            f |= PF
        if ((a & 0xF) + (b & 0xF) + carry) > 0xF:
            f |= AF
        if (~(a ^ b) & (a ^ r)) & sign:
            f |= OF
        self.eflags = f | 0x0002

    def _flags_sub(self, a: int, b: int, res: int, bits: int, carry: int = 0) -> None:
        mask = (1 << bits) - 1
        sign = 1 << (bits - 1)
        r = res & mask
        f = self.eflags & ~(CF | PF | AF | ZF | SF | OF)
        if res < 0:
            f |= CF
        if r == 0:
            f |= ZF
        if r & sign:
            f |= SF
        if _PARITY[r & 0xFF]:
            f |= PF
        if ((a & 0xF) - (b & 0xF) - carry) < 0:
            f |= AF
        if ((a ^ b) & (a ^ r)) & sign:
            f |= OF
        self.eflags = f | 0x0002

    # ---- fetch --------------------------------------------------------------
    def _fetch8(self) -> int:
        v = self.mem.data[self.eip]
        self.eip = (self.eip + 1) & 0xFFFFFFFF
        return v

    def _fetch16(self) -> int:
        a = self.eip
        self.eip = (a + 2) & 0xFFFFFFFF
        return self.mem.r16(a)

    def _fetch32(self) -> int:
        a = self.eip
        self.eip = (a + 4) & 0xFFFFFFFF
        return self.mem.r32(a)

    def _fetch_imm(self, size: int) -> int:
        return self._fetch8() if size == 1 else (self._fetch16() if size == 2 else self._fetch32())

    # ---- ModRM --------------------------------------------------------------
    # Returns (is_reg, value): is_reg True -> value is register index;
    # False -> value is a linear address.  reg field returned separately.
    def _modrm(self):
        modrm = self._fetch8()
        mod = modrm >> 6
        reg = (modrm >> 3) & 7
        rm = modrm & 7
        if mod == 3:
            return reg, True, rm
        return reg, False, self._memaddr(mod, rm)

    def _memaddr(self, mod: int, rm: int) -> int:
        """Decode a memory operand into a final linear address: offset plus the
        segment base (override prefix, else the addressing-form default —
        SS for EBP/ESP-based forms, DS otherwise)."""
        if self._adsize == 4:
            off = self._addr32(mod, rm)
        else:
            off = self._addr16(mod, rm)
        seg = self._segovr or self._defseg
        return (self.sbase[seg] + off) & 0xFFFFFFFF

    def _addr32(self, mod: int, rm: int) -> int:
        self._defseg = "ds"
        if rm == 4:  # SIB
            sib = self._fetch8()
            scale = sib >> 6
            index = (sib >> 3) & 7
            base = sib & 7
            addr = 0
            if index != 4:
                addr += self.r[index] << scale
            if base == 5 and mod == 0:
                addr += self._fetch32()
            else:
                addr += self.r[base]
                if base in (ESP, EBP):
                    self._defseg = "ss"
        elif rm == 5 and mod == 0:
            return self._fetch32()
        else:
            addr = self.r[rm]
            if rm == EBP:
                self._defseg = "ss"
        if mod == 1:
            addr += _sign(self._fetch8(), 8)
        elif mod == 2:
            addr += self._fetch32()
        return addr & 0xFFFFFFFF

    def _addr16(self, mod: int, rm: int) -> int:
        self._defseg = "ss" if rm in (2, 3) or (rm == 6 and mod != 0) else "ds"
        bx, bp, si, di = self.r[EBX] & 0xFFFF, self.r[EBP] & 0xFFFF, self.r[ESI] & 0xFFFF, self.r[EDI] & 0xFFFF
        base = (
            (bx + si), (bx + di), (bp + si), (bp + di), si, di, bp, bx,
        )[rm]
        if rm == 6 and mod == 0:
            self._defseg = "ds"
            return self._fetch16()
        if mod == 1:
            base += _sign(self._fetch8(), 8)
        elif mod == 2:
            base += self._fetch16()
        return base & 0xFFFF

    def _rm_read(self, is_reg, val, size):
        return self.reg(val, size) if is_reg else self.mem.read(val, size)

    def _rm_write(self, is_reg, val, size, v):
        if is_reg:
            self.set_reg(val, size, v)
        else:
            self.mem.write(val, size, v)

    # ---- stack --------------------------------------------------------------
    def push(self, v: int, size: int = 4) -> None:
        self.r[ESP] = (self.r[ESP] - size) & 0xFFFFFFFF
        self.mem.write(self.sbase["ss"] + self.r[ESP], size, v)

    def pop(self, size: int = 4) -> int:
        v = self.mem.read(self.sbase["ss"] + self.r[ESP], size)
        self.r[ESP] = (self.r[ESP] + size) & 0xFFFFFFFF
        return v

    def call_through(self, target: int, args=(), *, max_steps: int = 2_000_000) -> int:
        """Execute a sub-routine through the interpreter and return, as if the
        current instruction stream had ``call target`` with cdecl ``args``.

        For recovered *composed* hooks that must delegate a not-yet-recovered
        sub-call to the interpreter: push the args and a sentinel return
        address, run until the callee's final ``ret`` pops the sentinel, then
        clean up the args (cdecl).  Async IRQ delivery is suppressed for the
        duration so the delegated call stays atomic — matching the composition
        verifier's oracle.  Returns EAX.
        """
        SENTINEL = 0xFFFFFFF0            # not a valid code address in this image
        saved_irq = self.pending_irq
        self.pending_irq = None
        try:
            for a in reversed(tuple(args)):
                self.push(a & 0xFFFFFFFF, 4)
            self.push(SENTINEL, 4)
            self.eip = target & 0xFFFFFFFF
            steps = 0
            while self.eip != SENTINEL:
                self.step()
                steps += 1
                if steps > max_steps:
                    raise RuntimeError(
                        f"call_through(0x{target:X}) did not return after "
                        f"{steps} steps (eip=0x{self.eip:X})")
            if args:
                self.r[ESP] = (self.r[ESP] + 4 * len(args)) & 0xFFFFFFFF
            return self.r[EAX]
        finally:
            self.pending_irq = saved_irq

    # ---- run loop -----------------------------------------------------------
    def run(self, max_instructions: int = 1_000_000) -> None:
        for _ in range(max_instructions):
            if self.halted:
                return
            self.step()

    def addr(self):
        return self.seg["cs"], self.eip

    def deliver_interrupt(self, vec: int) -> bool:
        """Hardware-interrupt entry into an installed protected-mode handler.

        Pushes EFLAGS/CS/EIP (32-bit frames, matching the IRET(D) this core
        implements), clears IF/TF, and jumps to the ``idt`` vector.  Returns
        False when no handler is installed (the BIOS stub would IRET — a
        no-op)."""
        handler = self.idt.get(vec)
        if handler is None:
            return False
        sel, off = handler
        self.push(self.eflags, 4)
        self.push(self.seg["cs"], 4)
        self.push(self.eip, 4)
        self.eflags &= ~(IF | TF)
        self.set_seg("cs", sel)
        self.eip = off & 0xFFFFFFFF
        return True

    def step(self) -> None:
        if self.pending_irq is not None and (self.eflags & IF):
            # Poll the IRQ source every 16 instructions, not every one: the
            # dict/PIC probe was ~10% of the whole interpreter (measured).
            # Delivery granularity stays deterministic (a fixed decimation of
            # the instruction count), and 16 instructions is far finer than
            # any real IRQ latency the era's software could observe.
            self._irq_decim += 1
            if self._irq_decim >= 16:
                self._irq_decim = 0
                irq = self.pending_irq()
                if irq is not None:
                    # Master PIC base 08h, slave 70h — DOS/4GW's mapping.
                    vec = 0x08 + irq if irq < 8 else 0x70 + irq - 8
                    if not self.deliver_interrupt(vec) and self.irq_eoi is not None:
                        # No handler installed: the acknowledge already marked
                        # the IRQ in service, so EOI it (as the BIOS stub would)
                        # or the in-service bit wedges the PIC forever.
                        self.irq_eoi(irq)
        start = self.eip
        if self.entry_probes is not None:
            probe = self.entry_probes.get(start)
            if probe is not None:
                probe(self)
        hooks = self.replacement_hooks
        if hooks and start in hooks:
            handler = hooks[start]
            name = self.hook_names.get(start, "replacement")
            if self.hook_verifier is not None and start not in self.hook_verifier_passthrough:
                self.hook_verifier(self, start, handler, name)
            else:
                try:
                    handler(self)
                finally:
                    if self.coverage_telemetry is not None:
                        self.coverage_telemetry.record_hook_unverified(start, name)
            self.instruction_count += 1
            return
        if self.coverage_telemetry is not None:
            self.coverage_telemetry.record_interpreted_instruction((self.seg["cs"], start))
        self.instruction_count += 1
        self._opsize = 4
        self._adsize = 4
        self._segovr = None
        # prefixes
        rep = 0
        while True:
            op = self._fetch8()
            if op == 0x66:
                self._opsize = 2
            elif op == 0x67:
                self._adsize = 2
            elif op in _SEG_PREFIX:
                self._segovr = _SEG_PREFIX[op]
            elif op == 0xF3:
                rep = 0xF3
            elif op == 0xF2:
                rep = 0xF2
            elif op == 0xF0:
                pass  # LOCK — no effect on a single-threaded interpreter
            else:
                break
        self._exec(op, rep)

    def _exec(self, op: int, rep: int) -> None:
        osz = self._opsize
        bits = osz * 8

        # ---- segment push/pop (0x06/07/0E/16/17/1E/1F) ----------------------
        segpp = _SEG_PUSHPOP.get(op)
        if segpp is not None:
            sname, is_push = segpp
            if is_push:
                self.push(self.seg[sname], osz)
            else:
                self.set_seg(sname, self.pop(osz))
            return

        # ---- ALU family 0x00..0x3B (add/or/adc/sbb/and/sub/xor/cmp) ---------
        if op < 0x40 and (op & 0xC0) == 0 and (op & 0x07) < 6:
            self._alu(op)
            return

        hi = op & 0xF8
        lo = op & 0x07

        # ---- 0x40..0x5F inc/dec/push/pop reg -------------------------------
        if 0x40 <= op <= 0x47:
            r = self.reg(lo, osz)
            res = (r + 1) & ((1 << bits) - 1)
            cf = self.eflags & CF
            self._flags_add(r, 1, r + 1, bits)
            self.eflags = (self.eflags & ~CF) | cf  # INC preserves CF
            self.set_reg(lo, osz, res)
            return
        if 0x48 <= op <= 0x4F:
            r = self.reg(lo, osz)
            res = (r - 1) & ((1 << bits) - 1)
            cf = self.eflags & CF
            self._flags_sub(r, 1, r - 1, bits)
            self.eflags = (self.eflags & ~CF) | cf  # DEC preserves CF
            self.set_reg(lo, osz, res)
            return
        if 0x50 <= op <= 0x57:
            self.push(self.reg(lo, osz), osz)
            return
        if 0x58 <= op <= 0x5F:
            self.set_reg(lo, osz, self.pop(osz))
            return

        if op == 0x60:  # PUSHAD/PUSHA
            self._pusha(osz)
            return
        if op == 0x61:  # POPAD/POPA
            self._popa(osz)
            return
        if op == 0x68:  # push imm(v)
            self.push(self._fetch_imm(osz), osz)
            return
        if op == 0x6A:  # push imm8 (sign-extended)
            self.push(_sign(self._fetch8(), 8) & ((1 << bits) - 1), osz)
            return
        if op == 0x69 or op == 0x6B:  # imul r, r/m, imm
            reg, is_reg, val = self._modrm()
            src = _sign(self._rm_read(is_reg, val, osz), bits)
            imm = _sign(self._fetch8(), 8) if op == 0x6B else _sign(self._fetch_imm(osz), bits)
            self._imul_store(reg, osz, src, imm)
            return

        # ---- 0x70..0x7F jcc short ------------------------------------------
        if 0x70 <= op <= 0x7F:
            disp = _sign(self._fetch8(), 8)
            if self._cond(op & 0x0F):
                self.eip = (self.eip + disp) & 0xFFFFFFFF
            return

        # ---- grp1 0x80/81/83 ------------------------------------------------
        if op in (0x80, 0x81, 0x83):
            sz = 1 if op == 0x80 else osz
            b = sz * 8
            reg, is_reg, val = self._modrm()
            if op == 0x83:
                imm = _sign(self._fetch8(), 8) & ((1 << b) - 1)
            else:
                imm = self._fetch_imm(sz)
            self._alu_op(reg, self._rm_read(is_reg, val, sz), imm, b,
                         None if reg == 7 else (is_reg, val, sz))
            return

        if op in (0x84, 0x85):  # test r/m, r
            sz = 1 if op == 0x84 else osz
            reg, is_reg, val = self._modrm()
            self._flags_logic(self._rm_read(is_reg, val, sz) & self.reg(reg, sz), sz * 8)
            return
        if op in (0x86, 0x87):  # xchg r/m, r
            sz = 1 if op == 0x86 else osz
            reg, is_reg, val = self._modrm()
            a = self._rm_read(is_reg, val, sz)
            self._rm_write(is_reg, val, sz, self.reg(reg, sz))
            self.set_reg(reg, sz, a)
            return

        # ---- mov 0x88..0x8B -------------------------------------------------
        if op in (0x88, 0x89, 0x8A, 0x8B):
            sz = 1 if op in (0x88, 0x8A) else osz
            reg, is_reg, val = self._modrm()
            if op in (0x88, 0x89):  # r/m <- reg
                self._rm_write(is_reg, val, sz, self.reg(reg, sz))
            else:                   # reg <- r/m
                self.set_reg(reg, sz, self._rm_read(is_reg, val, sz))
            return
        if op == 0x8D:  # lea
            reg, is_reg, val = self._modrm()
            self.set_reg(reg, osz, val)
            return
        if op == 0x8C:  # mov r/m16, sreg
            reg, is_reg, val = self._modrm()
            self._rm_write(is_reg, val, 2, self.seg[_SEG[reg]])
            return
        if op == 0x8E:  # mov sreg, r/m16
            reg, is_reg, val = self._modrm()
            self.set_seg(_SEG[reg], self._rm_read(is_reg, val, 2))
            return
        if op == 0x8F:  # pop r/m
            reg, is_reg, val = self._modrm()
            self._rm_write(is_reg, val, osz, self.pop(osz))
            return

        if op == 0x9B:  # fwait/wait — no-op in a scalar interpreter
            return
        if 0xD8 <= op <= 0xDF:  # x87 escape
            self._fpu(op)
            return

        if op == 0x90:  # nop (xchg eax,eax)
            return
        if 0x91 <= op <= 0x97:  # xchg eax, reg
            a = self.reg(EAX, osz)
            self.set_reg(EAX, osz, self.reg(lo, osz))
            self.set_reg(lo, osz, a)
            return
        if op == 0x98:  # cbw/cwde
            self.set_reg(EAX, osz, _sign(self.reg(EAX, osz // 2), bits // 2) & ((1 << bits) - 1))
            return
        if op == 0x99:  # cwd/cdq
            v = self.reg(EAX, osz)
            self.set_reg(EDX, osz, ((1 << bits) - 1) if (v & (1 << (bits - 1))) else 0)
            return
        if op == 0x9E:  # sahf — AH -> SF/ZF/AF/PF/CF
            ah = (self.r[EAX] >> 8) & 0xFF
            self.eflags = (self.eflags & ~0xD5) | (ah & 0xD5) | 0x0002
            return
        if op == 0x9F:  # lahf
            self.set_reg(4, 1, (self.eflags & 0xD5) | 0x0002)
            return
        if op == 0x9C:  # pushf(d)
            self.push(self.eflags, osz)
            return
        if op == 0x9D:  # popf(d)
            self.eflags = (self.pop(osz) & 0x0FD5) | 0x0002
            return

        # ---- mov moffs 0xA0..0xA3 ------------------------------------------
        if op in (0xA0, 0xA1, 0xA2, 0xA3):
            sz = 1 if op in (0xA0, 0xA2) else osz
            disp = self._fetch32() if self._adsize == 4 else self._fetch16()
            addr = (self.sbase[self._segovr or "ds"] + disp) & 0xFFFFFFFF
            if op in (0xA0, 0xA1):
                self.set_reg(EAX, sz, self.mem.read(addr, sz))
            else:
                self.mem.write(addr, sz, self.reg(EAX, sz))
            return
        if op in (0xA8, 0xA9):  # test al/eax, imm
            sz = 1 if op == 0xA8 else osz
            self._flags_logic(self.reg(EAX, sz) & self._fetch_imm(sz), sz * 8)
            return
        if op in (0xA4, 0xA5, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF):
            self._string(op, rep)
            return

        # ---- mov imm 0xB0..0xBF --------------------------------------------
        if 0xB0 <= op <= 0xB7:
            self.set_reg(lo, 1, self._fetch8())
            return
        if 0xB8 <= op <= 0xBF:
            self.set_reg(lo, osz, self._fetch_imm(osz))
            return

        # ---- grp2 shifts 0xC0/C1/D0..D3 ------------------------------------
        if op in (0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3):
            sz = 1 if op in (0xC0, 0xD0, 0xD2) else osz
            reg, is_reg, val = self._modrm()
            if op in (0xC0, 0xC1):
                cnt = self._fetch8()
            elif op in (0xD0, 0xD1):
                cnt = 1
            else:
                cnt = self.reg(ECX, 1)
            self._shift(reg, is_reg, val, sz, cnt)
            return

        if op == 0xC2:  # ret imm16
            n = self._fetch16()
            self.eip = self.pop(osz)
            self.r[ESP] = (self.r[ESP] + n) & 0xFFFFFFFF
            return
        if op == 0xC3:  # ret
            self.eip = self.pop(osz)
            return
        if op in (0xC6, 0xC7):  # mov r/m, imm
            sz = 1 if op == 0xC6 else osz
            reg, is_reg, val = self._modrm()
            self._rm_write(is_reg, val, sz, self._fetch_imm(sz))
            return
        if op == 0xC9:  # leave
            self.r[ESP] = self.r[EBP]
            self.set_reg(EBP, osz, self.pop(osz))
            return

        if op == 0xCC:  # int3
            self._interrupt(3)
            return
        if op == 0xCD:  # int imm8
            self._interrupt(self._fetch8())
            return
        if op == 0xCF:  # iret(d)
            self.eip = self.pop(osz)
            self.set_seg("cs", self.pop(osz))
            self.eflags = (self.pop(osz) & 0x0FD5) | 0x0002
            return

        # ---- 0xE0..0xE3 loop/jcxz ------------------------------------------
        if op in (0xE0, 0xE1, 0xE2):
            disp = _sign(self._fetch8(), 8)
            self.set_reg(ECX, self._adsize, self.reg(ECX, self._adsize) - 1)
            c = self.reg(ECX, self._adsize)
            take = c != 0
            if op == 0xE0:
                take = take and not self.get_flag(ZF)
            elif op == 0xE1:
                take = take and self.get_flag(ZF)
            if take:
                self.eip = (self.eip + disp) & 0xFFFFFFFF
            return
        if op == 0xE3:  # jcxz/jecxz
            disp = _sign(self._fetch8(), 8)
            if self.reg(ECX, self._adsize) == 0:
                self.eip = (self.eip + disp) & 0xFFFFFFFF
            return

        if op in (0xE4, 0xE5, 0xEC, 0xED):  # in
            sz = 1 if op in (0xE4, 0xEC) else osz
            port = self._fetch8() if op in (0xE4, 0xE5) else (self.reg(EDX, 2))
            v = self.port_reader(self, port, sz * 8) if self.port_reader else 0
            self.set_reg(EAX, sz, v)
            return
        if op in (0xE6, 0xE7, 0xEE, 0xEF):  # out
            sz = 1 if op in (0xE6, 0xEE) else osz
            port = self._fetch8() if op in (0xE6, 0xE7) else (self.reg(EDX, 2))
            if self.port_writer:
                self.port_writer(self, port, self.reg(EAX, sz), sz * 8)
            return

        if op == 0xE8:  # call rel(v)
            disp = _sign(self._fetch_imm(osz), bits)
            self.push(self.eip, osz)
            self.eip = (self.eip + disp) & 0xFFFFFFFF
            return
        if op == 0xE9:  # jmp rel(v)
            disp = _sign(self._fetch_imm(osz), bits)
            self.eip = (self.eip + disp) & 0xFFFFFFFF
            return
        if op == 0xEB:  # jmp rel8
            disp = _sign(self._fetch8(), 8)
            self.eip = (self.eip + disp) & 0xFFFFFFFF
            return

        # ---- flag ops -------------------------------------------------------
        if op == 0xF4:  # hlt
            raise HaltExecution("HLT")
        if op == 0xF5:  # cmc
            self.eflags ^= CF
            return
        if op == 0xF8:
            self.set_flag(CF, False); return
        if op == 0xF9:
            self.set_flag(CF, True); return
        if op == 0xFA:
            self.set_flag(IF, False); return
        if op == 0xFB:
            self.set_flag(IF, True); return
        if op == 0xFC:
            self.set_flag(DF, False); return
        if op == 0xFD:
            self.set_flag(DF, True); return

        if op in (0xF6, 0xF7):  # grp3
            self._grp3(op)
            return
        if op in (0xFE, 0xFF):  # grp4/5
            self._grp5(op)
            return

        if op == 0x0F:
            self._two_byte(self._fetch8())
            return

        raise UnsupportedInstruction(
            f"opcode 0x{op:02X} at linear 0x{(self.eip - 1) & 0xFFFFFFFF:X} "
            f"(opsize={osz}, cs:eip start)"
        )

    # ---- ALU ---------------------------------------------------------------
    def _alu(self, op: int) -> None:
        aluop = op >> 3            # 0..7
        form = op & 0x07
        if form in (0, 1, 2, 3):
            sz = 1 if form in (0, 2) else self._opsize
            reg, is_reg, val = self._modrm()
            if form in (0, 1):     # r/m, reg  (dst = r/m)
                a = self._rm_read(is_reg, val, sz)
                dst = (is_reg, val, sz)
            else:                  # reg, r/m  (dst = reg)
                a = self.reg(reg, sz)
                dst = None
            if form in (0, 1):
                b = self.reg(reg, sz)
            else:
                b = self._rm_read(is_reg, val, sz)
            if form in (0, 1):
                self._alu_op(aluop, a, b, sz * 8, None if aluop == 7 else dst)
            else:
                self._alu_op(aluop, a, b, sz * 8, None if aluop == 7 else (True, reg, sz))
        else:                      # 4: al,imm8 ; 5: eax,imm(v)
            sz = 1 if form == 4 else self._opsize
            a = self.reg(EAX, sz)
            b = self._fetch_imm(sz)
            self._alu_op(aluop, a, b, sz * 8, None if aluop == 7 else (True, EAX, sz))

    def _alu_op(self, aluop, a, b, bits, dst):
        """dst is (is_reg, val, sz) to write back, or None for cmp/no write."""
        cf = 1 if (self.eflags & CF) else 0
        if aluop == 0:      # add
            res = a + b
            self._flags_add(a, b, res, bits)
        elif aluop == 1:    # or
            res = a | b
            self._flags_logic(res, bits)
        elif aluop == 2:    # adc
            res = a + b + cf
            self._flags_add(a, b, res, bits, cf)
        elif aluop == 3:    # sbb
            res = a - b - cf
            self._flags_sub(a, b, res, bits, cf)
        elif aluop == 4:    # and
            res = a & b
            self._flags_logic(res, bits)
        elif aluop == 5:    # sub
            res = a - b
            self._flags_sub(a, b, res, bits)
        elif aluop == 6:    # xor
            res = a ^ b
            self._flags_logic(res, bits)
        else:               # cmp
            self._flags_sub(a, b, a - b, bits)
            return
        if dst is not None:
            is_reg, val, sz = dst
            self._rm_write(is_reg, val, sz, res & ((1 << bits) - 1))

    def _imul_store(self, reg, sz, a, b):
        bits = sz * 8
        full = a * b
        res = full & ((1 << bits) - 1)
        signed = _sign(res, bits)
        of = signed != full
        self.set_reg(reg, sz, res)
        self.set_flag(CF, of)
        self.set_flag(OF, of)

    # ---- shifts ------------------------------------------------------------
    def _shift(self, sub, is_reg, val, sz, cnt):
        bits = sz * 8
        mask = (1 << bits) - 1
        cnt &= 0x1F
        v = self._rm_read(is_reg, val, sz)
        if cnt == 0:
            return
        if sub == 4 or sub == 6:      # shl / sal
            res = (v << cnt) & mask
            cf = (v >> (bits - cnt)) & 1
            self._rm_write(is_reg, val, sz, res)
            self._flags_logic(res, bits)
            self.set_flag(CF, bool(cf))
            if cnt == 1:
                self.set_flag(OF, bool(((res >> (bits - 1)) & 1) ^ cf))
        elif sub == 5:                # shr
            cf = (v >> (cnt - 1)) & 1
            res = (v & mask) >> cnt
            self._rm_write(is_reg, val, sz, res)
            self._flags_logic(res, bits)
            self.set_flag(CF, bool(cf))
            if cnt == 1:
                self.set_flag(OF, bool((v >> (bits - 1)) & 1))
        elif sub == 7:                # sar
            sv = _sign(v, bits)
            cf = (sv >> (cnt - 1)) & 1
            res = (sv >> cnt) & mask
            self._rm_write(is_reg, val, sz, res)
            self._flags_logic(res, bits)
            self.set_flag(CF, bool(cf))
            if cnt == 1:
                self.set_flag(OF, False)
        elif sub in (0, 1):           # rol / ror
            rc = cnt % bits
            if rc == 0:
                return
            if sub == 0:
                res = ((v << rc) | (v >> (bits - rc))) & mask
                self.set_flag(CF, bool(res & 1))
            else:
                res = ((v >> rc) | (v << (bits - rc))) & mask
                self.set_flag(CF, bool((res >> (bits - 1)) & 1))
            self._rm_write(is_reg, val, sz, res)
        else:                          # rcl (2) / rcr (3): rotate through carry
            cf = 1 if (self.eflags & CF) else 0
            for _ in range(cnt % (bits + 1)):
                if sub == 2:
                    newcf = (v >> (bits - 1)) & 1
                    v = ((v << 1) | cf) & mask
                else:
                    newcf = v & 1
                    v = (v >> 1) | (cf << (bits - 1))
                cf = newcf
            self._rm_write(is_reg, val, sz, v)
            self.set_flag(CF, bool(cf))

    # ---- grp3 (test/not/neg/mul/imul/div/idiv) -----------------------------
    def _grp3(self, op):
        sz = 1 if op == 0xF6 else self._opsize
        bits = sz * 8
        mask = (1 << bits) - 1
        reg, is_reg, val = self._modrm()
        v = self._rm_read(is_reg, val, sz)
        if reg in (0, 1):   # test imm
            self._flags_logic(v & self._fetch_imm(sz), bits)
        elif reg == 2:      # not
            self._rm_write(is_reg, val, sz, ~v & mask)
        elif reg == 3:      # neg
            res = (-v) & mask
            self._flags_sub(0, v, -v, bits)
            self._rm_write(is_reg, val, sz, res)
        elif reg == 4:      # mul (unsigned)
            self._muldiv_mul(v, sz, signed=False)
        elif reg == 5:      # imul
            self._muldiv_mul(v, sz, signed=True)
        elif reg == 6:      # div
            self._muldiv_div(v, sz, signed=False)
        else:               # idiv
            self._muldiv_div(v, sz, signed=True)

    def _muldiv_mul(self, v, sz, signed):
        bits = sz * 8
        a = self.reg(EAX, sz)
        if signed:
            prod = _sign(a, bits) * _sign(v, bits)
        else:
            prod = a * v
        mask = (1 << bits) - 1
        if sz == 1:
            self.set_reg(EAX, 2, prod & 0xFFFF)
            hi = (prod >> 8) & 0xFF
        else:
            self.set_reg(EAX, sz, prod & mask)
            self.set_reg(EDX, sz, (prod >> bits) & mask)
            hi = (prod >> bits) & mask
        overflow = hi != 0 if not signed else (prod != _sign(prod & mask, bits))
        self.set_flag(CF, bool(overflow))
        self.set_flag(OF, bool(overflow))

    def _muldiv_div(self, v, sz, signed):
        if v == 0:
            self._interrupt(0)  # #DE divide error
            return
        bits = sz * 8
        if sz == 1:
            dividend = self.reg(EAX, 2)
            if signed:
                dividend = _sign(dividend, 16); d = _sign(v, 8)
                q = int(dividend / d); r = dividend - q * d
            else:
                q, r = divmod(dividend, v)
            self.set_reg(0, 1, q & 0xFF)   # AL = quotient
            self.set_reg(4, 1, r & 0xFF)   # AH = remainder
        else:
            dividend = self.reg(EDX, sz) << bits | self.reg(EAX, sz)
            d = v
            if signed:
                dividend = _sign(dividend, bits * 2)
                d = _sign(v, bits)
                q = int(dividend / d); r = dividend - q * d
            else:
                q, r = divmod(dividend, d)
            mask = (1 << bits) - 1
            self.set_reg(EAX, sz, q & mask)
            self.set_reg(EDX, sz, r & mask)

    # ---- grp5 (inc/dec/call/jmp/push) --------------------------------------
    def _grp5(self, op):
        sz = 1 if op == 0xFE else self._opsize
        bits = sz * 8
        reg, is_reg, val = self._modrm()
        if reg == 0:    # inc
            v = self._rm_read(is_reg, val, sz)
            cf = self.eflags & CF
            self._flags_add(v, 1, v + 1, bits)
            self.eflags = (self.eflags & ~CF) | cf
            self._rm_write(is_reg, val, sz, (v + 1) & ((1 << bits) - 1))
        elif reg == 1:  # dec
            v = self._rm_read(is_reg, val, sz)
            cf = self.eflags & CF
            self._flags_sub(v, 1, v - 1, bits)
            self.eflags = (self.eflags & ~CF) | cf
            self._rm_write(is_reg, val, sz, (v - 1) & ((1 << bits) - 1))
        elif reg == 2:  # call r/m
            self.push(self.eip, self._opsize)
            self.eip = self._rm_read(is_reg, val, self._opsize)
        elif reg == 4:  # jmp r/m
            self.eip = self._rm_read(is_reg, val, self._opsize)
        elif reg == 6:  # push r/m
            self.push(self._rm_read(is_reg, val, self._opsize), self._opsize)
        else:
            raise UnsupportedInstruction(f"grp5 /{reg} (op 0x{op:02X}) not implemented "
                                         f"(far call/jmp?) at 0x{self.eip:X}")

    def _pusha(self, osz):
        temp = self.r[ESP]
        for i in (EAX, ECX, EDX, EBX):
            self.push(self.reg(i, osz), osz)
        self.push(temp, osz)
        for i in (EBP, ESI, EDI):
            self.push(self.reg(i, osz), osz)

    def _popa(self, osz):
        for i in (EDI, ESI, EBP):
            self.set_reg(i, osz, self.pop(osz))
        self.pop(osz)  # skip esp
        for i in (EBX, EDX, ECX, EAX):
            self.set_reg(i, osz, self.pop(osz))

    # ---- string ops --------------------------------------------------------
    def _string(self, op, rep):
        sz = 1 if op in (0xA4, 0xAA, 0xAC, 0xAE) else self._opsize
        asz = self._adsize
        step = sz if not self.get_flag(DF) else -sz
        sbase = self.sbase[self._segovr or "ds"]   # source segment (overridable)
        dbase = self.sbase["es"]                    # destination is always ES
        # Bulk fast path for forward REP MOVS/STOS: byte-identical semantics
        # to the per-unit loop (equivalence-tested), orders of magnitude
        # faster for the clears/copies that dominate rendering.
        if (rep and step > 0 and asz == 4 and op in (0xA4, 0xA5, 0xAA, 0xAB)
                and self._bulk_string(op, sz, sbase, dbase)):
            return
        def once():
            si = self.reg(ESI, asz)
            di = self.reg(EDI, asz)
            if op in (0xA4, 0xA5):      # movs
                self.mem.write(dbase + di, sz, self.mem.read(sbase + si, sz))
                self.set_reg(ESI, asz, si + step); self.set_reg(EDI, asz, di + step)
            elif op in (0xAA, 0xAB):    # stos
                self.mem.write(dbase + di, sz, self.reg(EAX, sz))
                self.set_reg(EDI, asz, di + step)
            elif op in (0xAC, 0xAD):    # lods
                self.set_reg(EAX, sz, self.mem.read(sbase + si, sz))
                self.set_reg(ESI, asz, si + step)
            elif op in (0xAE, 0xAF):    # scas
                a = self.reg(EAX, sz); b = self.mem.read(dbase + di, sz)
                self._flags_sub(a, b, a - b, sz * 8)
                self.set_reg(EDI, asz, di + step)
            else:                        # cmps A6/A7
                a = self.mem.read(sbase + si, sz); b = self.mem.read(dbase + di, sz)
                self._flags_sub(a, b, a - b, sz * 8)
                self.set_reg(ESI, asz, si + step); self.set_reg(EDI, asz, di + step)
        if not rep:
            once()
            return
        while self.reg(ECX, asz) != 0:
            once()
            self.set_reg(ECX, asz, self.reg(ECX, asz) - 1)
            if op in (0xA6, 0xA7, 0xAE, 0xAF):
                z = self.get_flag(ZF)
                if rep == 0xF3 and not z:
                    break
                if rep == 0xF2 and z:
                    break

    # ---- two-byte 0x0F -----------------------------------------------------
    def _two_byte(self, op2):
        osz = self._opsize
        if 0x80 <= op2 <= 0x8F:      # jcc near
            disp = _sign(self._fetch_imm(osz), osz * 8)
            if self._cond(op2 & 0x0F):
                self.eip = (self.eip + disp) & 0xFFFFFFFF
            return
        if 0x90 <= op2 <= 0x9F:      # setcc
            reg, is_reg, val = self._modrm()
            self._rm_write(is_reg, val, 1, 1 if self._cond(op2 & 0x0F) else 0)
            return
        if op2 in (0xB6, 0xB7, 0xBE, 0xBF):  # movzx/movsx
            srcsz = 1 if op2 in (0xB6, 0xBE) else 2
            reg, is_reg, val = self._modrm()
            v = self._rm_read(is_reg, val, srcsz)
            if op2 in (0xBE, 0xBF):
                v = _sign(v, srcsz * 8) & ((1 << (osz * 8)) - 1)
            self.set_reg(reg, osz, v)
            return
        if op2 == 0xAF:              # imul r, r/m
            reg, is_reg, val = self._modrm()
            self._imul_store(reg, osz, _sign(self.reg(reg, osz), osz * 8),
                             _sign(self._rm_read(is_reg, val, osz), osz * 8))
            return
        if op2 == 0x01:              # SGDT/SIDT/SMSW/... group
            self._grp7()
            return
        if op2 in (0x20, 0x22):      # mov reg,crN / mov crN,reg
            modrm = self._fetch8()
            crn = (modrm >> 3) & 7
            gpr = modrm & 7
            if op2 == 0x20:
                self.set_reg(gpr, 4, self.cr.get(crn, 0))
            else:
                self.cr[crn] = self.reg(gpr, 4)
            return
        if op2 == 0xA0:              # push fs
            self.push(self.seg["fs"], osz); return
        if op2 == 0xA1:              # pop fs
            self.set_seg("fs", self.pop(osz)); return
        if op2 == 0xA8:              # push gs
            self.push(self.seg["gs"], osz); return
        if op2 == 0xA9:              # pop gs
            self.set_seg("gs", self.pop(osz)); return
        if op2 in (0xA3, 0xAB, 0xB3, 0xBB):   # bt/bts/btr/btc r/m, reg
            self._bit_op({0xA3: "bt", 0xAB: "bts", 0xB3: "btr", 0xBB: "btc"}[op2])
            return
        if op2 == 0xBA:              # grp8: bt/bts/btr/btc r/m, imm8
            self._bit_op(None, imm=True)
            return
        if op2 in (0xA4, 0xA5, 0xAC, 0xAD):   # shld/shrd
            self._shldrd(op2)
            return
        if op2 in (0xBC, 0xBD):      # bsf / bsr
            reg, is_reg, val = self._modrm()
            src = self._rm_read(is_reg, val, osz)
            if src == 0:
                self.set_flag(ZF, True)
            else:
                self.set_flag(ZF, False)
                if op2 == 0xBC:
                    i = (src & -src).bit_length() - 1
                else:
                    i = src.bit_length() - 1
                self.set_reg(reg, osz, i)
            return
        if op2 == 0x31:              # rdtsc
            self.set_reg(EAX, 4, self.instruction_count & 0xFFFFFFFF)
            self.set_reg(EDX, 4, 0)
            return
        if op2 == 0xA2:              # cpuid
            self.set_reg(EAX, 4, 0); self.set_reg(EBX, 4, 0)
            self.set_reg(ECX, 4, 0); self.set_reg(EDX, 4, 0)
            return
        raise UnsupportedInstruction(
            f"opcode 0x0F 0x{op2:02X} at linear 0x{(self.eip - 2) & 0xFFFFFFFF:X}")

    # ---- bit ops -----------------------------------------------------------
    def _bit_op(self, kind, imm=False):
        osz = self._opsize
        reg, is_reg, val = self._modrm()
        if imm:
            sub = reg  # reg field selects op for grp8
            bit = self._fetch8()
            kind = {4: "bt", 5: "bts", 6: "btr", 7: "btc"}[sub]
        else:
            bit = self.reg(reg, osz)
        if is_reg:
            bits = osz * 8
            b = bit % bits
            v = self.reg(val, osz)
            cf = (v >> b) & 1
            if kind != "bt":
                if kind == "bts":
                    v |= (1 << b)
                elif kind == "btr":
                    v &= ~(1 << b)
                else:
                    v ^= (1 << b)
                self.set_reg(val, osz, v)
        else:
            addr = val + (bit >> 3)
            b = bit & 7
            v = self.mem.r8(addr)
            cf = (v >> b) & 1
            if kind != "bt":
                if kind == "bts":
                    v |= (1 << b)
                elif kind == "btr":
                    v &= ~(1 << b)
                else:
                    v ^= (1 << b)
                self.mem.w8(addr, v)
        self.set_flag(CF, bool(cf))

    def _shldrd(self, op2):
        osz = self._opsize
        bits = osz * 8
        left = op2 in (0xA4, 0xA5)
        reg, is_reg, val = self._modrm()
        src = self.reg(reg, osz)
        cnt = (self._fetch8() if op2 in (0xA4, 0xAC) else self.reg(ECX, 1)) & 0x1F
        if cnt == 0:
            return
        dst = self._rm_read(is_reg, val, osz)
        if left:
            res = ((dst << cnt) | (src >> (bits - cnt))) & ((1 << bits) - 1)
            cf = (dst >> (bits - cnt)) & 1
        else:
            res = ((dst >> cnt) | (src << (bits - cnt))) & ((1 << bits) - 1)
            cf = (dst >> (cnt - 1)) & 1
        self._rm_write(is_reg, val, osz, res)
        self._flags_logic(res, bits)
        self.set_flag(CF, bool(cf))

    # ---- conditions --------------------------------------------------------
    def _cond(self, cc: int) -> bool:
        f = self.eflags
        of = bool(f & OF); sf = bool(f & SF); zf = bool(f & ZF)
        cf = bool(f & CF); pf = bool(f & PF)
        return (
            of, not of, cf, not cf, zf, not zf, cf or zf, not (cf or zf),
            sf, not sf, pf, not pf, sf != of, sf == of,
            zf or (sf != of), not zf and (sf == of),
        )[cc]

    def _bulk_string(self, op, sz, sbase, dbase) -> bool:
        """Forward REP MOVS/STOS as slice operations.  Returns False when any
        byte of the transfer would need per-byte semantics the slices don't
        reproduce (planar write modes other than 0, partial bit mask, ranges
        straddling the A000h aperture boundary) — the caller then runs the
        exact per-unit loop."""
        r = self.r
        count = r[1]
        if count == 0:
            r[1] = 0
            return True
        n = count * sz
        mem = self.mem
        vga = mem.vga
        dst = (dbase + r[7]) & 0xFFFFFFFF
        movs = op in (0xA4, 0xA5)

        def region(a):
            if vga is not None and 0xA0000 <= a < 0xB0000:
                return 1
            return 0

        if region(dst) != region(dst + n - 1):
            return False
        dst_planar = region(dst)
        if movs:
            src = (sbase + r[6]) & 0xFFFFFFFF
            if region(src) != region(src + n - 1):
                return False
            src_planar = region(src)
        else:
            src = 0
            src_planar = 0

        if (dst_planar or src_planar):
            if vga.bit_mask != 0xFF:
                return False
            wm = vga.write_mode
            if dst_planar and wm not in (0, 1):
                return False

        # Same-aperture copies must retain element-order semantics when the
        # ranges overlap.  VGASequencer.bulk_copy owns both the bulk and the
        # sequential fallback, including latch state, so do not rebuild its
        # plane rules here.
        if movs and src_planar and dst_planar:
            vga.bulk_copy(src - 0xA0000, dst - 0xA0000, n)
            r[6] = (r[6] + n) & 0xFFFFFFFF
            r[7] = (r[7] + n) & 0xFFFFFFFF
            r[1] = 0
            return True

        # REP MOVS executes a read and write for each element.  A forward
        # overlapping RAM destination can therefore become the next source
        # element; a snapshot/slice copy would incorrectly have memmove
        # semantics.  Let _string's per-unit loop model that case exactly.
        if movs and not src_planar and not dst_planar and src < dst < src + n:
            return False

        # ---- gather source bytes -------------------------------------------
        if movs:
            if src_planar:
                off = src - 0xA0000
                if off + n > 0x10000:
                    return False
                data = bytes(vga.planes[vga.read_map & 3][off:off + n])
                vga.latches = [vga.planes[pi][off + n - 1] for pi in range(4)]
            else:
                data = bytes(mem.data[src:src + n])
        else:
            v = self.reg(EAX, sz)
            data = v.to_bytes(sz, "little") * count

        # ---- scatter to destination ----------------------------------------
        if dst_planar:
            if vga.write_mode == 1:
                return False                      # wm1 with RAM source: per-byte
            doff = dst - 0xA0000
            if doff + n > 0x10000:
                return False
            m = vga.map_mask
            for pi in range(4):
                if m & (1 << pi):
                    vga.planes[pi][doff:doff + n] = data
        else:
            mem.data[dst:dst + n] = data

        if movs:
            r[6] = (r[6] + n) & 0xFFFFFFFF
        r[7] = (r[7] + n) & 0xFFFFFFFF
        r[1] = 0
        return True

    # ---- x87 FPU -----------------------------------------------------------
    # Semantics ported from CPU8086.execute_fpu (doubles stand in for the 80-bit
    # registers; long dependent chains can diverge in the low mantissa bits).
    # Grown from the ops KE actually issues; unhandled forms fail loud.
    def _fpush(self, v: float) -> None:
        if len(self.st) >= 8:
            raise UnsupportedInstruction("x87 stack overflow")
        self.st.append(v)

    def _fpop(self) -> float:
        if not self.st:
            raise UnsupportedInstruction("x87 stack underflow")
        return self.st.pop()

    def _fst_i(self, i: int) -> float:
        return self.st[-1 - i]

    def _fround(self, v: float) -> int:
        import math
        rc = (self.fcw >> 10) & 3
        if rc == 0:                       # round to nearest even
            f = math.floor(v); d = v - f
            if d > 0.5 or (d == 0.5 and int(f) & 1):
                f += 1
            return int(f)
        if rc == 1:
            return math.floor(v)
        if rc == 2:
            return math.ceil(v)
        return math.trunc(v)

    def _fcompare(self, a: float, b: float) -> None:
        self.fsw &= ~0x4700
        if a != a or b != b:
            self.fsw |= 0x4500
        elif a == b:
            self.fsw |= 0x4000
        elif a < b:
            self.fsw |= 0x0100

    @staticmethod
    def _fdiv(num: float, den: float) -> float:
        # x87 masked division: n/0 -> signed inf, 0/0 -> NaN (default fcw masks
        # #Z/#IA).  Krypton's cstart relies on 1.0/0.0 == +inf (infinity probe).
        import math
        if den == 0.0:
            if num == 0.0 or num != num:
                return float("nan")
            return math.copysign(1.0, num) * math.copysign(1.0, den) * math.inf
        return num / den

    def _farith(self, sub: int, other: float) -> None:
        # Memory-operand arithmetic: destination is always ST(0); ``other`` is
        # the loaded memory value (D8 m32 / DC m64 mapping).
        a = self._fst_i(0)
        if sub == 0:
            self.st[-1] = a + other
        elif sub == 1:
            self.st[-1] = a * other
        elif sub == 2:                  # FCOM
            self._fcompare(a, other)
        elif sub == 3:                  # FCOMP
            self._fcompare(a, other); self._fpop()
        elif sub == 4:
            self.st[-1] = a - other     # FSUB
        elif sub == 5:
            self.st[-1] = other - a     # FSUBR
        elif sub == 6:
            self.st[-1] = self._fdiv(a, other)   # FDIV
        else:
            self.st[-1] = self._fdiv(other, a)   # FDIVR

    def _fpu_arith_reg(self, op, reg, rm):
        # Register-form arithmetic.  D8: dest ST(0), other ST(i).  DC/DE: dest
        # ST(i), other ST(0) — and the SUB/DIV vs reversed sense swaps between
        # D8 and DC/DE (the classic x87 encoding quirk).  DE also pops.
        st0 = self._fst_i(0)
        sti = self._fst_i(rm)
        if reg == 2:
            self._fcompare(st0, sti); return
        if reg == 3:
            self._fcompare(st0, sti); self._fpop(); return
        if op == 0xD8:
            a, b, dest_i = st0, sti, 0
        else:
            a, b, dest_i = sti, st0, rm
        if reg == 0:
            r = a + b
        elif reg == 1:
            r = a * b
        elif reg == 4:
            r = (a - b) if op == 0xD8 else (b - a)
        elif reg == 5:
            r = (b - a) if op == 0xD8 else (a - b)
        elif reg == 6:
            r = self._fdiv(a, b) if op == 0xD8 else self._fdiv(b, a)
        else:
            r = self._fdiv(b, a) if op == 0xD8 else self._fdiv(a, b)
        self.st[-1 - dest_i] = r
        if op == 0xDE:
            self._fpop()

    def _fpu(self, op: int) -> None:
        modrm = self._fetch8()
        mod = modrm >> 6
        reg = (modrm >> 3) & 7
        rm = modrm & 7
        if mod == 3:
            self._fpu_reg(op, reg, rm, modrm)
            return
        self._fpu_mem(op, reg, self._memaddr(mod, rm))

    def _fpu_reg(self, op, reg, rm, modrm):
        if op == 0xDB and reg == 4:
            if rm == 3:                 # FNINIT
                self.st = []; self.fsw = 0; self.fcw = 0x037F; return
            if rm == 2:                 # FNCLEX
                self.fsw &= 0x7F00; return
            if rm in (0, 1):            # FNENI / FNDISI (8087 no-ops)
                return
        if op == 0xD9:
            if modrm == 0xEE:
                self._fpush(0.0); return                 # FLDZ
            if modrm == 0xE8:
                self._fpush(1.0); return                 # FLD1
            if modrm == 0xE0:
                self.st[-1] = -self.st[-1]; return        # FCHS
            if modrm == 0xE1:
                self.st[-1] = abs(self.st[-1]); return    # FABS
            if reg == 0:                                  # FLD ST(i)
                self._fpush(self._fst_i(rm)); return
            if reg == 1:                                  # FXCH ST(i)
                self.st[-1], self.st[-1 - rm] = self.st[-1 - rm], self.st[-1]; return
        if op == 0xDD and reg == 3:                       # FSTP ST(i)
            self.st[-1 - rm] = self._fst_i(0); self._fpop(); return
        if op == 0xDD and reg == 0:                       # FFREE — model as no-op
            return
        if op == 0xDF and modrm == 0xE0:                  # FNSTSW AX
            self.set_reg(EAX, 2, self.fsw); return
        if op == 0xDE and modrm == 0xD9:                  # FCOMPP
            self._fcompare(self._fst_i(0), self._fst_i(1)); self._fpop(); self._fpop(); return
        if op in (0xD8, 0xDC, 0xDE):                       # arith with ST(i)
            self._fpu_arith_reg(op, reg, rm)
            return
        raise UnsupportedInstruction(
            f"x87 reg-form op 0x{op:02X} modrm 0x{modrm:02X} (reg={reg} rm={rm}) at 0x{self.eip:X}")

    def _fpu_mem(self, op, reg, addr):
        m = self.mem
        if op == 0xD9:
            if reg == 5:
                self.fcw = m.r16(addr); return            # FLDCW
            if reg == 7:
                m.w16(addr, self.fcw); return             # FNSTCW
            if reg == 0:
                self._fpush(struct.unpack("<f", m.block(addr, 4))[0]); return   # FLD m32
            if reg in (2, 3):
                m.data[addr:addr + 4] = struct.pack("<f", self._fst_i(0))       # FST/FSTP m32
                if reg == 3:
                    self._fpop()
                return
        if op == 0xDD:
            if reg == 0:
                self._fpush(struct.unpack("<d", m.block(addr, 8))[0]); return   # FLD m64
            if reg in (2, 3):
                m.data[addr:addr + 8] = struct.pack("<d", self._fst_i(0))       # FST/FSTP m64
                if reg == 3:
                    self._fpop()
                return
            if reg == 7:
                m.w16(addr, self.fsw); return             # FNSTSW m16
        if op == 0xDB:
            if reg == 0:
                self._fpush(float(_sign(m.r32(addr), 32))); return              # FILD m32
            if reg == 3:
                m.w32(addr, self._fround(self._fst_i(0)) & 0xFFFFFFFF); self._fpop(); return  # FISTP m32
        if op == 0xDF:
            if reg == 0:
                self._fpush(float(_sign(m.r16(addr), 16))); return              # FILD m16
            if reg == 5:
                self._fpush(float(_sign(m.r32(addr) | (m.r32(addr + 4) << 32), 64))); return  # FILD m64
            if reg == 7:
                v = self._fround(self._fst_i(0))
                m.w32(addr, v & 0xFFFFFFFF); m.w32(addr + 4, (v >> 32) & 0xFFFFFFFF)
                self._fpop(); return                       # FISTP m64
        if op == 0xD8:                                     # arith m32 with ST(0)
            self._farith(reg, struct.unpack("<f", m.block(addr, 4))[0]); return
        if op == 0xDC:                                     # arith m64 with ST(0)
            self._farith(reg, struct.unpack("<d", m.block(addr, 8))[0]); return
        raise UnsupportedInstruction(
            f"x87 mem-form op 0x{op:02X} /{reg} at 0x{self.eip:X}")

    # ---- 0F 01 group (SGDT/SIDT/SMSW/LGDT/LIDT/LMSW) -----------------------
    def _grp7(self):
        reg, is_reg, val = self._modrm()
        if reg == 4:                    # SMSW: PE|MP|ET (protected, 387 present)
            self._rm_write(is_reg, val, 2, 0x0013)
        elif reg in (0, 1):             # SGDT/SIDT -> [limit:2][base:4]
            self.mem.w16(val, 0x03FF)
            self.mem.w32(val + 2, 0)
        elif reg in (2, 3, 6):          # LGDT/LIDT/LMSW: accepted, no effect (flat model)
            pass
        else:
            raise UnsupportedInstruction(f"0F 01 /{reg} not implemented at 0x{self.eip:X}")

    # ---- interrupts --------------------------------------------------------
    def _interrupt(self, num: int) -> None:
        if self.interrupt_handler is None:
            raise UnsupportedInstruction(f"INT 0x{num:02X} with no handler at 0x{self.eip:X}")
        self.interrupt_handler(self, num)
