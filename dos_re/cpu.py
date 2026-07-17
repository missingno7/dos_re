from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .memory import (
    BIOS_ROM_BASE,
    EGA_CPU_APERTURE,
    EGA_PLANE_WINDOW,
    Memory,
    linear,
)

# One-past-the-end of the EGA CPU aperture: fetches/accesses below or above
# this window are plain RAM even while the planar shadow is active.
_EGA_WINDOW_END = EGA_CPU_APERTURE + EGA_PLANE_WINDOW


REG16 = ["ax", "cx", "dx", "bx", "sp", "bp", "si", "di"]
REG8 = ["al", "cl", "dl", "bl", "ah", "ch", "dh", "bh"]
# Backing 16-bit register per REG8 index (idx >= 4 = the high byte).  Module
# level so get_reg8/set_reg8 don't rebuild the name by string concat per call.
_REG8_BASE = ("ax", "cx", "dx", "bx", "ax", "cx", "dx", "bx")
SREG = ["es", "cs", "ss", "ds"]
# Segment-override prefix byte -> register name.  Module-level so the hot
# prefix-decode loop in step() does not rebuild the dict on every instruction.
_SEG_OVERRIDE = {0x26: "es", 0x2E: "cs", 0x36: "ss", 0x3E: "ds"}
JCC_NAMES = ["jo", "jno", "jb", "jnb", "jz", "jnz", "jbe", "ja", "js", "jns", "jp", "jnp", "jl", "jge", "jle", "jg"]
_ALU_NAMES = ("add", "or", "adc", "sbb", "and", "sub", "xor", "cmp")

# x86 FLAGS bits + control exceptions live in the shared leaf module so the
# device model (dos_re.dos) need not import the interpreter (dos_re.x86).
#: CPUState now lives in .x86 (the ISA leaf): a register record is a VALUE,
#: and needing one must not drag this interpreter into the import graph -- the
#: same reason the flag bits and control exceptions moved there.  Re-exported
#: so every existing `from .cpu import CPUState` importer is unaffected.
from .x86 import (CF, PF, AF, ZF, SF, TF, IF, DF, OF, PARITY as _PARITY,  # noqa: F401
                  UnsupportedInstruction, HaltExecution, CPUState)


class EffectiveAddress:
    """Decoded r/m effective address.

    ``text`` (the disassembly rendering) is built ON DEMAND: it only feeds
    trace lines and error messages, and every trace consumer is gated on
    ``trace_enabled`` — so the hot trace-off path never pays the disp string
    formatting this used to do eagerly on every memory operand.
    ``base_text`` is None for the direct-address form ([1234]), else the
    static "[bx+si]"-style literal; ``disp`` is the signed displacement to
    render (None when the form has none).
    """
    __slots__ = ("segment", "offset", "base_text", "disp")

    def __init__(self, segment: str, offset: int, base_text: str | None, disp: int | None = None) -> None:
        self.segment = segment
        self.offset = offset
        self.base_text = base_text
        self.disp = disp

    @property
    def text(self) -> str:
        if self.base_text is None:
            return f"[{self.disp:04X}]"
        if self.disp is None:
            return self.base_text
        return self.base_text[:-1] + f"{self.disp:+d}]"


class _RegOperand:
    """Register r/m operand.  Module-level so it is not rebuilt every instruction."""
    __slots__ = ("cpu", "rm", "bits", "text")

    def __init__(self, cpu: "CPU8086", rm: int, bits: int) -> None:
        self.cpu = cpu
        self.rm = rm
        self.bits = bits
        self.text = REG8[rm] if bits == 8 else REG16[rm]

    def read(self) -> int:
        return self.cpu.get_reg8(self.rm) if self.bits == 8 else self.cpu.get_reg16(self.rm)

    def write(self, value: int) -> None:
        if self.bits == 8:
            self.cpu.set_reg8(self.rm, value)
        else:
            self.cpu.set_reg16(self.rm, value)


class _MemOperand:
    """Memory r/m operand bound to a decoded effective address.

    The segment register VALUE is captured at decode time (one getattr here
    instead of one per read/write): no 8086 instruction modifies a segment
    register before accessing its own r/m operand — the only segment writers
    (MOV sreg / POP sreg / LES / LDS) read the operand first.  ``text`` is a
    lazy property for the same trace-off reason as EffectiveAddress.
    """
    __slots__ = ("cpu", "ea", "segv", "offset", "bits")

    def __init__(self, cpu: "CPU8086", ea: EffectiveAddress, bits: int) -> None:
        self.cpu = cpu
        self.ea = ea
        self.segv = getattr(cpu.s, ea.segment)
        self.offset = ea.offset
        self.bits = bits

    @property
    def text(self) -> str:
        return f"{self.ea.segment}:{self.ea.text}"

    def read(self) -> int:
        return self.cpu.mem.rb(self.segv, self.offset) if self.bits == 8 else self.cpu.mem.rw(self.segv, self.offset)

    def write(self, value: int) -> None:
        if self.bits == 8:
            self.cpu.mem.wb(self.segv, self.offset, value)
        else:
            self.cpu.mem.ww(self.segv, self.offset, value)

    def read_far(self) -> tuple[int, int]:
        return self.cpu.mem.rw(self.segv, self.offset), self.cpu.mem.rw(self.segv, (self.offset + 2) & 0xFFFF)


@dataclass
class CPU8086:
    mem: Memory
    s: CPUState = field(default_factory=CPUState)
    halted: bool = False
    trace: list[str] = field(default_factory=list)
    # Tracing defaults OFF: the per-instruction trace list is appended once per
    # executed instruction and is only drained by snapshot.run_until.  Any other
    # run loop (the interactive viewer, headless demo replay, liftverify drives)
    # that left it ON would grow `self.trace` without bound — ~1 formatted string
    # per instruction, i.e. gigabytes within seconds of gameplay (found 2026-07-15,
    # the Lemmings pilot's runaway-RAM investigation).  The paths that WANT a trace
    # (run_until, the differential verifier, OK_TRACE_HOOK) enable it explicitly
    # and consume it; the hot path must never pay for it.
    trace_enabled: bool = False
    instruction_count: int = 0
    call_depth: int = 0
    interrupt_handler: Callable[["CPU8086", int], None] | None = None
    port_reader: Callable[["CPU8086", int, int], int] | None = None
    port_writer: Callable[["CPU8086", int, int, int], None] | None = None
    replacement_hooks: dict[tuple[int, int], Callable[["CPU8086"], None]] = field(default_factory=dict)
    hook_names: dict[tuple[int, int], str] = field(default_factory=dict)
    #: boundary observer for lifted code (lift/emit ``boundary_heads``): the
    #: emitted event call ``cpu.boundary_hook(cpu, head_cs, head_ip,
    #: resume_ip)`` fires after each observed head instruction; the port's
    #: clock arms it to consume per-head park costs and park exactly
    #: (re-pointing CS:IP at the RESUME entry before raising).  None = no
    #: observation cost.
    boundary_hook: Callable[["CPU8086", int, int, int], None] | None = None
    #: THE VMLESS WALL POISON (docs/dos_re_2.0.md §1a): when True, any step
    #: that would fetch/decode/execute an original instruction (i.e. no
    #: replacement hook at CS:IP) raises immediately — interpretation is
    #: IMPOSSIBLE, not merely unused.  Armed by wall-gated runners on the
    #: candidate; never on the oracle.
    interp_forbidden: bool = False
    #: diagnostic: when a set, ``interp_forbidden`` RECORDS uncovered
    #: (cs,ip) here and interprets instead of raising — one run enumerates
    #: the whole interpreted frontier (the census-closure work list).
    interp_frontier: set | None = None
    #: EXE-INDEPENDENCE (docs/dos_re_2.0.md §"The EXE-independence wall"): when
    #: True, the lifted entry guard ``self_disable_if_patched`` is a no-op.  A
    #: data-only boot image has the recovered code ZEROED (poisoned), so an
    #: entry-signature comparison against the live bytes is meaningless — the
    #: lifted host function IS the authoritative implementation, and the byte
    #: check would false-alarm on the intentionally-poisoned bytes.  Set only
    #: by the strict-VMless boot path (lemmings.vmless_boot); never armed when
    #: the original code is present.
    code_poisoned: bool = False
    hook_verifier: Callable[["CPU8086", tuple[int, int], Callable[["CPU8086"], None], str], None] | None = None
    hook_verifier_passthrough: set[tuple[int, int]] = field(default_factory=set)
    # Optional live-side replacements used only while a differential hook
    # transaction is executing the replacement handler.  Interactive front-ends
    # use this to keep UI presenter/timer hooks publishing frames without letting
    # their normal frame-boundary exceptions interrupt the verified routine.
    hook_verifier_live_passthrough_overrides: dict[tuple[int, int], Callable[["CPU8086"], None]] = field(default_factory=dict)
    # Interactive front-ends sometimes need a publish/pacing boundary while a
    # verified parent hook is still running.  The live-side passthrough wrapper
    # cannot raise the normal UI boundary exception immediately, because that
    # would abort the differential transaction before the ASM-vs-hook diff is
    # computed.  Instead it sets this flag; HookVerifier raises the optional
    # callback after the verified hook has reached its continuation and compared
    # cleanly.
    hook_verifier_live_yield_requested: bool = False
    hook_verifier_live_yield_callback: Callable[[], None] | None = None
    # When a lifted parent executes an original bounded CALL or directly invokes
    # an installed child hook, keep differential verification active at the
    # nested hook boundary.  This makes child addresses real oracle checkpoints
    # instead of shared black boxes inside a larger parent transaction.
    hook_verifier_verify_nested_calls: bool = True
    # Optional real-time pacer invoked once per modelled timer tick (the game's
    # PIT/timer wait).  Left None for headless/deterministic runs; an interactive
    # front-end sets it to throttle the game to real time.
    timer_pacer: Callable[[], None] | None = None
    timer_ticks_elapsed: int = 0
    # Optional hardware-interrupt source (a PIC).  When set, it is polled at each
    # instruction boundary with IF set; returning an IRQ number delivers it inline
    # (real hardware-interrupt entry into the IVT handler).  Left None on the
    # deterministic demo/test path so that timing there is unchanged.
    pending_irq: "Callable[[], int | None] | None" = None
    max_rep_count: int = 1_000_000
    # Optional generic execution telemetry sink. The CPU emits only raw events;
    # game-specific island classification lives outside the interpreter.
    coverage_telemetry: Any | None = None

    def addr(self) -> tuple[int, int]:
        return self.s.cs & 0xFFFF, self.s.ip & 0xFFFF

    def set_flag(self, flag: int, value: bool) -> None:
        if value:
            self.s.flags |= flag
        else:
            self.s.flags &= ~flag
        self.s.flags |= 0x0002
        self.s.flags &= 0x0FFF

    def get_flag(self, flag: int) -> bool:
        return bool(self.s.flags & flag)

    def parity(self, value: int) -> bool:
        return _PARITY[value & 0xFF]

    # The three flag helpers below are extremely hot.  They compute the whole
    # flags word in one assignment instead of 5-6 set_flag() calls each.  The bits
    # they touch (and the ones they preserve) match the original set_flag-based
    # versions exactly; the regression suite checks the resulting flags.
    def set_logic_flags(self, result: int, bits: int) -> None:
        sign = 1 << (bits - 1)
        r = result & ((1 << bits) - 1)
        # Clear CF, PF, ZF, SF, OF (leave AF, like the original); CF=OF=0.
        f = self.s.flags & ~0x08C5
        if r == 0:
            f |= ZF
        if r & sign:
            f |= SF
        if _PARITY[r & 0xFF]:
            f |= PF
        self.s.flags = (f | 0x0002) & 0x0FFF

    def set_add_flags(self, a: int, b: int, result: int, bits: int, carry: int = 0) -> None:
        # ``a`` and ``b`` are the *original* operands; ``carry`` is the incoming
        # carry for ADC (0 for plain ADD).  ``result`` is the full unmasked
        # a+b+carry.  Folding carry into b before this call would destroy the
        # nibble-carry (AF) and sign (OF) information, so it is kept separate.
        mask = (1 << bits) - 1
        sign = 1 << (bits - 1)
        r = result & mask
        f = self.s.flags & ~0x08D5  # clear CF, PF, AF, ZF, SF, OF
        if result > mask:
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
        self.s.flags = (f | 0x0002) & 0x0FFF

    def set_sub_flags(self, a: int, b: int, result: int, bits: int, carry: int = 0) -> None:
        # ``a`` and ``b`` are the *original* operands; ``carry`` is the incoming
        # borrow for SBB (0 for plain SUB/CMP).  ``result`` is the full signed
        # a-b-carry (may be negative).  CF is the true borrow (result < 0); AF and
        # OF use the original operands so the borrow does not corrupt them.
        mask = (1 << bits) - 1
        sign = 1 << (bits - 1)
        r = result & mask
        f = self.s.flags & ~0x08D5  # clear CF, PF, AF, ZF, SF, OF
        if result < 0:
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
        self.s.flags = (f | 0x0002) & 0x0FFF

    def set_incdec_flags(self, old: int, res: int, bits: int, *, dec: bool) -> None:
        """Flags for INC/DEC: ZF/SF/PF/AF/OF from old±1, CF PRESERVED.

        Replaces the get_flag(CF) / set_add_flags / set_flag(CF) dance the
        INC/DEC forms used (three calls per instruction).  Equivalences to
        set_add_flags/set_sub_flags with b=1: AF = low nibble 0xF (inc) or 0
        (dec); OF = result exactly the sign bit (inc 0x7FFF) or old exactly
        the sign bit (dec 0x8000)."""
        sign = 1 << (bits - 1)
        f = self.s.flags & ~0x08D4  # clear PF, AF, ZF, SF, OF — keep CF
        if res == 0:
            f |= ZF
        if res & sign:
            f |= SF
        if _PARITY[res & 0xFF]:
            f |= PF
        if dec:
            if (old & 0xF) == 0:
                f |= AF
            if old == sign:
                f |= OF
        else:
            if (old & 0xF) == 0xF:
                f |= AF
            if res == sign:
                f |= OF
        self.s.flags = (f | 0x0002) & 0x0FFF

    def get_reg16(self, idx: int) -> int:
        return getattr(self.s, REG16[idx]) & 0xFFFF

    def set_reg16(self, idx: int, value: int) -> None:
        setattr(self.s, REG16[idx], value & 0xFFFF)

    def get_reg8(self, idx: int) -> int:
        v = getattr(self.s, _REG8_BASE[idx])
        return (v >> 8) & 0xFF if idx >= 4 else v & 0xFF

    def set_reg8(self, idx: int, value: int) -> None:
        base = _REG8_BASE[idx]
        cur = getattr(self.s, base)
        if idx >= 4:
            cur = (cur & 0x00FF) | ((value & 0xFF) << 8)
        else:
            cur = (cur & 0xFF00) | (value & 0xFF)
        setattr(self.s, base, cur & 0xFFFF)

    def get_sreg(self, idx: int) -> int:
        return getattr(self.s, SREG[idx]) & 0xFFFF

    def set_sreg(self, idx: int, value: int) -> None:
        setattr(self.s, SREG[idx], value & 0xFFFF)

    def fetch8(self) -> int:
        # Hot path: an opcode/operand byte from CS:IP.  Inline the memory
        # selector fast-path to save ~2.5M rb() method calls per profiled run,
        # and the real-mode fast path (a code fetch is then a plain array
        # read).  With the EGA planar shadow active, only a fetch whose linear
        # address lands INSIDE the A000h aperture goes through rb() (aperture
        # reads have latch side effects); code executes from plain RAM, so in
        # practice this keeps every fetch on the direct path.
        s = self.s
        off = s.ip & 0xFFFF
        mem = self.mem
        sb = mem.sel_base
        if sb is not None:
            seg = s.cs & 0xFFFF
            if seg >= mem.sel_min:
                base = sb.get(seg)
                v = mem.data[base + off] if base is not None \
                    else mem.data[(seg << 4) + off]
            else:
                v = mem.data[(seg << 4) + off]
        else:
            a = (((s.cs & 0xFFFF) << 4) + off) & 0xFFFFF
            if not mem.ega_planar or a < EGA_CPU_APERTURE or a >= _EGA_WINDOW_END:
                v = mem.data[a]
            else:
                v = mem.rb(s.cs, off)
        s.ip = (off + 1) & 0xFFFF
        return v

    def fetch16(self) -> int:
        # Real-mode fast path mirrors fetch8 (one call frame instead of two
        # per operand word); selector mode and aperture-crossing fetches keep
        # the fetch8 pair.
        s = self.s
        mem = self.mem
        if mem.sel_base is None:
            data = mem.data
            base = ((s.cs & 0xFFFF) << 4) & 0xFFFFF
            ip = s.ip & 0xFFFF
            a0 = (base + ip) & 0xFFFFF
            a1 = (base + ((ip + 1) & 0xFFFF)) & 0xFFFFF
            if not mem.ega_planar or (
                    (a0 < EGA_CPU_APERTURE or a0 >= _EGA_WINDOW_END)
                    and (a1 < EGA_CPU_APERTURE or a1 >= _EGA_WINDOW_END)):
                s.ip = (ip + 2) & 0xFFFF
                return data[a0] | (data[a1] << 8)
        lo = self.fetch8()
        hi = self.fetch8()
        return lo | (hi << 8)

    def push(self, value: int) -> None:
        self.s.sp = (self.s.sp - 2) & 0xFFFF
        self.mem.ww(self.s.ss, self.s.sp, value)

    def pop(self) -> int:
        v = self.mem.rw(self.s.ss, self.s.sp)
        self.s.sp = (self.s.sp + 2) & 0xFFFF
        return v

    def sign8(self, v: int) -> int:
        return v - 0x100 if v & 0x80 else v

    def sign16(self, v: int) -> int:
        return v - 0x10000 if v & 0x8000 else v

    def decode_ea(self, mod: int, rm: int, seg_override: str | None = None) -> EffectiveAddress:
        if mod == 0 and rm == 6:
            disp = self.fetch16()
            # base_text=None marks the direct-address form; EffectiveAddress
            # renders "[%04X]" from disp on demand.
            return EffectiveAddress(seg_override or "ds", disp & 0xFFFF, None, disp)
        if rm == 0:
            base = self.s.bx + self.s.si; text = "[bx+si]"; default_seg = "ds"
        elif rm == 1:
            base = self.s.bx + self.s.di; text = "[bx+di]"; default_seg = "ds"
        elif rm == 2:
            base = self.s.bp + self.s.si; text = "[bp+si]"; default_seg = "ss"
        elif rm == 3:
            base = self.s.bp + self.s.di; text = "[bp+di]"; default_seg = "ss"
        elif rm == 4:
            base = self.s.si; text = "[si]"; default_seg = "ds"
        elif rm == 5:
            base = self.s.di; text = "[di]"; default_seg = "ds"
        elif rm == 6:
            base = self.s.bp; text = "[bp]"; default_seg = "ss"
        else:
            base = self.s.bx; text = "[bx]"; default_seg = "ds"
        if mod == 0:
            return EffectiveAddress(seg_override or default_seg, base & 0xFFFF, text)
        # Inline the displacement fetch (fetch8/fetch16 fast path): one call
        # frame per displacement-carrying memory operand.
        s = self.s
        mem = self.mem
        if mem.sel_base is None:
            ip = s.ip & 0xFFFF
            cs_base = ((s.cs & 0xFFFF) << 4) & 0xFFFFF
            a0 = (cs_base + ip) & 0xFFFFF
            direct = not mem.ega_planar or a0 < EGA_CPU_APERTURE or a0 >= _EGA_WINDOW_END
            if direct and mod == 1:
                d = mem.data[a0]
                s.ip = (ip + 1) & 0xFFFF
                disp = d - 0x100 if d & 0x80 else d
                return EffectiveAddress(seg_override or default_seg, (base + disp) & 0xFFFF, text, disp)
            a1 = (cs_base + ((ip + 1) & 0xFFFF)) & 0xFFFFF
            if direct and mod == 2 and (
                    not mem.ega_planar or a1 < EGA_CPU_APERTURE or a1 >= _EGA_WINDOW_END):
                d = mem.data[a0] | (mem.data[a1] << 8)
                s.ip = (ip + 2) & 0xFFFF
                disp = d - 0x10000 if d & 0x8000 else d
                return EffectiveAddress(seg_override or default_seg, (base + disp) & 0xFFFF, text, disp)
        if mod == 1:
            disp = self.sign8(self.fetch8())
        else:
            disp = self.sign16(self.fetch16())
        return EffectiveAddress(seg_override or default_seg, (base + disp) & 0xFFFF, text, disp)


    def decode_rm_operand(self, mod: int, rm: int, bits: int, seg_override: str | None = None):
        if mod == 3:
            return _RegOperand(self, rm, bits)
        return _MemOperand(self, self.decode_ea(mod, rm, seg_override), bits)

    def read_rm(self, mod: int, rm: int, bits: int, seg_override: str | None = None) -> tuple[int, str]:
        if mod == 3:
            return (self.get_reg8(rm) if bits == 8 else self.get_reg16(rm)), (REG8[rm] if bits == 8 else REG16[rm])
        ea = self.decode_ea(mod, rm, seg_override)
        seg = getattr(self.s, ea.segment)
        v = self.mem.rb(seg, ea.offset) if bits == 8 else self.mem.rw(seg, ea.offset)
        # The text return only feeds trace lines (all gated on trace_enabled).
        return v, (f"{ea.segment}:{ea.text}" if self.trace_enabled else "")

    def write_rm(self, mod: int, rm: int, bits: int, value: int, seg_override: str | None = None) -> str:
        if mod == 3:
            if bits == 8:
                self.set_reg8(rm, value)
                return REG8[rm]
            self.set_reg16(rm, value)
            return REG16[rm]
        ea = self.decode_ea(mod, rm, seg_override)
        seg = getattr(self.s, ea.segment)
        if bits == 8:
            self.mem.wb(seg, ea.offset, value)
        else:
            self.mem.ww(seg, ea.offset, value)
        return f"{ea.segment}:{ea.text}" if self.trace_enabled else ""

    def peek_modrm(self) -> tuple[int, int, int, int]:
        # Inline of fetch8()'s fast path: this runs once per r/m instruction
        # (~half of all instructions), so the extra call frame was measurable.
        s = self.s
        off = s.ip & 0xFFFF
        mem = self.mem
        sb = mem.sel_base
        if sb is not None:
            m = self.fetch8()
            return m, (m >> 6) & 3, (m >> 3) & 7, m & 7
        a = (((s.cs & 0xFFFF) << 4) + off) & 0xFFFFF
        if not mem.ega_planar or a < EGA_CPU_APERTURE or a >= _EGA_WINDOW_END:
            m = mem.data[a]
        else:
            m = mem.rb(s.cs, off)
        s.ip = (off + 1) & 0xFFFF
        return m, (m >> 6) & 3, (m >> 3) & 7, m & 7

    def _enter_hardware_interrupt(self, irq: int) -> None:
        """Real IRQ entry: push flags/cs/ip, clear IF/TF, jump to the IVT handler."""
        vec = (0x08 + irq) if irq < 8 else (0x70 + irq - 8)
        off = self.mem.rw(0, vec * 4)
        seg = self.mem.rw(0, vec * 4 + 2)
        self.push(self.s.flags)
        self.push(self.s.cs & 0xFFFF)
        self.push(self.s.ip & 0xFFFF)
        self.set_flag(IF, False)
        self.set_flag(TF, False)
        self.s.cs, self.s.ip = seg & 0xFFFF, off & 0xFFFF

    def step(self) -> None:
        s = self.s
        if self.halted:
            raise HaltExecution()

        # Deliver a pending hardware interrupt at this instruction boundary.
        if self.pending_irq is not None and (s.flags & IF):
            irq = self.pending_irq()
            if irq is not None:
                self._enter_hardware_interrupt(irq)
                return

        start_cs, start_ip = s.cs & 0xFFFF, s.ip & 0xFFFF
        hooks = self.replacement_hooks
        # Only pay the (cs, ip) tuple alloc + dict probe when hooks exist.
        if hooks and (start_cs, start_ip) in hooks:
            hook_key = (start_cs, start_ip)
            before = s.snapshot() if self.trace_enabled else ""
            name = self.hook_names.get(hook_key, "replacement")
            handler = hooks[hook_key]
            if self.hook_verifier is not None and hook_key not in self.hook_verifier_passthrough:
                self.hook_verifier(self, hook_key, handler, name)
            else:
                try:
                    handler(self)
                finally:
                    if self.coverage_telemetry is not None:
                        self.coverage_telemetry.record_hook_unverified(hook_key, name)
            # Virtual-time preservation (lift/emit count_instructions): a hook
            # that accounts its own instruction_count per block declares
            # owns_time — adding the dispatch +1 on top would overcount by one
            # per call vs the interpreted oracle, skewing every time-derived
            # observable (PIT reads first among them).
            if not getattr(handler, "owns_time", False):
                self.instruction_count += 1
            if self.trace_enabled:
                self.trace.append(f"{start_cs:04X}:{start_ip:04X}  HOOK {name:<23} {before} -> {self.s.snapshot()}")
            return

        if self.interp_forbidden:
            frontier = self.interp_frontier
            if frontier is not None:
                # DIAGNOSTIC collect mode: record the uncovered address and
                # interpret it (no raise), so a single run enumerates the
                # whole interpreted frontier instead of stopping at the first.
                frontier.add((start_cs, start_ip))
            else:
                raise RuntimeError(
                    f"VMLESS WALL VIOLATION: attempted to interpret an original "
                    f"instruction at {start_cs:04X}:{start_ip:04X} -- no lifted "
                    f"hook covers this address.  The candidate must never "
                    f"fetch/decode/execute x86; register a resume entry, lift "
                    f"the code, or record the recovery fact that explains this "
                    f"address (docs/dos_re_2.0.md section 1a).")

        seg_override: str | None = None
        rep: int | None = None
        mem = self.mem
        while True:
            # Inline of fetch8()'s fast path (same branches, same semantics):
            # saves one method-call frame on every instruction executed.
            off = s.ip & 0xFFFF
            sb = mem.sel_base
            if sb is not None:
                seg = s.cs & 0xFFFF
                if seg >= mem.sel_min:
                    base = sb.get(seg)
                    op = mem.data[base + off] if base is not None \
                        else mem.data[(seg << 4) + off]
                else:
                    op = mem.data[(seg << 4) + off]
            else:
                a = (((s.cs & 0xFFFF) << 4) + off) & 0xFFFFF
                if not mem.ega_planar or a < EGA_CPU_APERTURE or a >= _EGA_WINDOW_END:
                    op = mem.data[a]
                else:
                    op = mem.rb(s.cs, off)
            s.ip = (off + 1) & 0xFFFF
            if op == 0x26 or op == 0x2E or op == 0x36 or op == 0x3E:
                seg_override = _SEG_OVERRIDE[op]
                continue
            if op == 0xF2 or op == 0xF3:
                rep = op
                continue
            if op == 0x66:
                # PRE2 contains a small 386 CPU-probe path using operand-size
                # prefixes (notably ``66 33 C0`` / xor eax,eax).  The VM is a
                # 16-bit source-recovery oracle, so we deliberately execute the
                # following instruction with the visible 16-bit register state.
                # That preserves the game-observable low word while avoiding a
                # full 80386 register model during bootstrap bring-up.
                continue
            break
        asm = self.execute_opcode(op, seg_override, rep)
        if self.coverage_telemetry is not None:
            self.coverage_telemetry.record_interpreted_instruction((start_cs, start_ip))
        self.instruction_count += 1
        if self.trace_enabled:
            self.trace.append(f"{start_cs:04X}:{start_ip:04X}  d{self.call_depth:02d} {asm:<34} {self.s.snapshot()}")

    def run(self, max_steps: int = 1000) -> int:
        steps = 0
        while steps < max_steps and not self.halted:
            self.step()
            steps += 1
        return steps

    # ---- x87 floating point (ESC opcodes D8-DF) --------------------------
    # Grown for Win16 inline-8087 code.  Registers are Python doubles: the
    # real 8087 computes in 80-bit extended precision, so long dependent
    # chains can diverge in the low mantissa bits.  Documented caveat; revisit
    # with an extended-precision model only if a target proves it matters.

    def _fpush(self, v: float) -> None:
        if len(self.s.fst) >= 8:
            raise UnsupportedInstruction("x87 stack overflow")
        self.s.fst.append(v)

    def _fpop(self) -> float:
        if not self.s.fst:
            raise UnsupportedInstruction("x87 stack underflow")
        return self.s.fst.pop()

    def _fst(self, i: int) -> float:
        return self.s.fst[-1 - i]

    def _fst_set(self, i: int, v: float) -> None:
        self.s.fst[-1 - i] = v

    def _fcompare(self, a: float, b: float) -> None:
        # C3 (bit14) = equal, C2 (bit10) = unordered, C0 (bit8) = a < b.
        self.s.fsw &= ~0x4700
        if a != a or b != b:
            self.s.fsw |= 0x4500
        elif a == b:
            self.s.fsw |= 0x4000
        elif a < b:
            self.s.fsw |= 0x0100

    def _fxam(self) -> None:
        # FXAM: classify ST(0) into C3/C2/C0 and set C1 = sign (FSW bits
        # C0=0x100, C1=0x200, C2=0x400, C3=0x4000).
        import math
        s = self.s
        s.fsw &= ~0x4700
        if not s.fst:
            s.fsw |= 0x4100                         # empty: C3,C0
            return
        v = self._fst(0)
        if math.copysign(1.0, v) < 0:
            s.fsw |= 0x0200                         # C1 = sign
        if v != v:
            s.fsw |= 0x0100                         # NaN: C0
        elif math.isinf(v):
            s.fsw |= 0x0500                         # inf: C2,C0
        elif v == 0.0:
            s.fsw |= 0x4000                         # zero: C3
        else:
            s.fsw |= 0x0400                         # normal: C2

    def _fround(self, v: float) -> int:
        rc = (self.s.fcw >> 10) & 3
        import math
        if rc == 0:  # round to nearest even
            f = math.floor(v)
            d = v - f
            if d > 0.5 or (d == 0.5 and int(f) & 1):
                f += 1
            return int(f)
        if rc == 1:
            return math.floor(v)
        if rc == 2:
            return math.ceil(v)
        return math.trunc(v)

    @staticmethod
    def _f80_to_double(b: bytes) -> float:
        import math
        man = int.from_bytes(b[:8], "little")
        se = int.from_bytes(b[8:10], "little")
        sign = -1.0 if se & 0x8000 else 1.0
        exp = se & 0x7FFF
        if exp == 0:
            return sign * math.ldexp(man, -16382 - 63) if man else sign * 0.0
        if exp == 0x7FFF:
            return sign * float("inf") if man in (0, 1 << 63) else float("nan")
        return sign * math.ldexp(man / (1 << 63), exp - 16383)

    @staticmethod
    def _double_to_f80(v: float) -> bytes:
        import math
        sign = 0x8000 if math.copysign(1.0, v) < 0 else 0
        if v != v:
            return (0xC000000000000000).to_bytes(8, "little") + (0x7FFF | sign).to_bytes(2, "little")
        if math.isinf(v):
            return (1 << 63).to_bytes(8, "little") + (0x7FFF | sign).to_bytes(2, "little")
        if v == 0.0:
            return b"\x00" * 8 + sign.to_bytes(2, "little")
        m, e = math.frexp(abs(v))               # v = m * 2**e, 0.5 <= m < 1
        man = int(m * (1 << 64)) & ((1 << 64) - 1)
        return man.to_bytes(8, "little") + ((e - 1 + 16383) | sign).to_bytes(2, "little")

    def _fmem(self, mod: int, rm: int, seg_override: str | None):
        ea = self.decode_ea(mod, rm, seg_override)
        return getattr(self.s, ea.segment), ea.offset, f"{ea.segment}:{ea.text}"

    def _fread(self, seg: int, off: int, nbytes: int) -> bytes:
        return bytes(self.mem.rb(seg, (off + i) & 0xFFFF) for i in range(nbytes))

    def _fwrite(self, seg: int, off: int, data: bytes) -> None:
        for i, byte in enumerate(data):
            self.mem.wb(seg, (off + i) & 0xFFFF, byte)

    def execute_fpu(self, op: int, seg_override: str | None) -> str:
        """Decode the ESC instruction's modrm/EA from the stream, then delegate.

        FP semantics live in :meth:`fpu_reg_op` / :meth:`fpu_mem_op` so the
        lifter's emitted code can call the SAME helpers with a natively
        computed effective address (dos_re/lift/emit.py) -- one source of
        truth for x87 behaviour, zero drift between interpreter and lift.
        """
        _, mod, reg, rm = self.peek_modrm()
        if mod == 3:
            return self.fpu_reg_op(op, reg, rm)
        seg, off, text = self._fmem(mod, rm, seg_override)
        return self.fpu_mem_op(op, reg, seg, off, text)

    def fpu_reg_op(self, op: int, reg: int, rm: int) -> str:
        """Register-form ESC instruction (mod == 3): ST(i)/control operands.

        The interpreter's own semantics, callable directly by lifted code.
        Raises UnsupportedInstruction on any form this model does not
        implement and on FP-stack over/underflow -- identically on both
        paths (fail loud, never guess)."""
        import math
        s = self.s
        if op == 0xD8 and reg == 3:                          # FCOMP ST(i)
            self._fcompare(self._fst(0), self._fst(rm))
            self._fpop()
            return f"fcomp st({rm})"
        if op == 0xD9 and reg == 0:                          # FLD ST(i)
            self._fpush(self._fst(rm))
            return f"fld st({rm})"
        if op == 0xDB and reg == 4:
            if rm == 2:                                      # FCLEX
                s.fsw &= 0x7F00
                return "fclex"
            if rm == 3:                                      # FINIT
                s.fst = []
                s.fsw = 0
                s.fcw = 0x037F
                return "finit"
            if rm in (0, 1):                                 # FENI/FDISI (8087)
                return "feni" if rm == 0 else "fdisi"
        if op == 0xDD and reg == 3:                          # FSTP ST(i)
            v = self._fst(0)
            self._fst_set(rm, v)
            self._fpop()
            return f"fstp st({rm})"
        if op == 0xDE:
            if reg == 0:                                     # FADDP ST(i),ST
                self._fst_set(rm, self._fst(rm) + self._fst(0))
                self._fpop()
                return f"faddp st({rm})"
            if reg == 1:                                     # FMULP ST(i),ST
                self._fst_set(rm, self._fst(rm) * self._fst(0))
                self._fpop()
                return f"fmulp st({rm})"
            if reg == 4:                                     # FSUBRP ST(i),ST
                self._fst_set(rm, self._fst(0) - self._fst(rm))
                self._fpop()
                return f"fsubrp st({rm})"
            if reg == 5:                                     # FSUBP ST(i),ST
                self._fst_set(rm, self._fst(rm) - self._fst(0))
                self._fpop()
                return f"fsubp st({rm})"
            if reg == 6:                                     # FDIVRP ST(i),ST
                self._fst_set(rm, self._fst(0) / self._fst(rm))
                self._fpop()
                return f"fdivrp st({rm})"
            if reg == 7:                                     # FDIVP ST(i),ST
                self._fst_set(rm, self._fst(rm) / self._fst(0))
                self._fpop()
                return f"fdivp st({rm})"
        # -- register-form arithmetic + specials (grown for SimAnt's x87) --
        if op == 0xD8:                                       # ST(0) op= ST(i)
            a, b = self._fst(0), self._fst(rm)
            if reg == 0: self._fst_set(0, a + b); return f"fadd st,st({rm})"
            if reg == 1: self._fst_set(0, a * b); return f"fmul st,st({rm})"
            if reg == 2: self._fcompare(a, b); return f"fcom st({rm})"
            if reg == 4: self._fst_set(0, a - b); return f"fsub st,st({rm})"
            if reg == 5: self._fst_set(0, b - a); return f"fsubr st,st({rm})"
            if reg == 6: self._fst_set(0, a / b); return f"fdiv st,st({rm})"
            if reg == 7: self._fst_set(0, b / a); return f"fdivr st,st({rm})"
        if op == 0xDC:                                       # ST(i) op= ST(0)
            a, b = self._fst(rm), self._fst(0)
            if reg == 0: self._fst_set(rm, a + b); return f"fadd st({rm}),st"
            if reg == 1: self._fst_set(rm, a * b); return f"fmul st({rm}),st"
            if reg == 4: self._fst_set(rm, b - a); return f"fsubr st({rm}),st"
            if reg == 5: self._fst_set(rm, a - b); return f"fsub st({rm}),st"
            if reg == 6: self._fst_set(rm, b / a); return f"fdivr st({rm}),st"
            if reg == 7: self._fst_set(rm, a / b); return f"fdiv st({rm}),st"
        if op == 0xD9:
            if reg == 1:                                     # FXCH ST(i)
                v0, vi = self._fst(0), self._fst(rm)
                self._fst_set(0, vi); self._fst_set(rm, v0)
                return f"fxch st({rm})"
            if reg == 4:                                     # FCHS/FABS/FTST/FXAM
                if rm == 0: self._fst_set(0, -self._fst(0)); return "fchs"
                if rm == 1: self._fst_set(0, abs(self._fst(0))); return "fabs"
                if rm == 4: self._fcompare(self._fst(0), 0.0); return "ftst"
                if rm == 5: self._fxam(); return "fxam"
            if reg == 5:                                     # FLD1/FLDZ/FLDPI/...
                c = {0: 1.0, 1: math.log2(10.0), 2: math.log2(math.e),
                     3: math.pi, 4: math.log10(2.0), 5: math.log(2.0),
                     6: 0.0}.get(rm)
                if c is not None:
                    self._fpush(c); return f"fldconst{rm}"
            if reg == 7:                                     # FSQRT/FSIN/FCOS/...
                v = self._fst(0)
                if rm == 2: self._fst_set(0, math.sqrt(v)); return "fsqrt"
                if rm == 4: self._fst_set(0, float(self._fround(v))); return "frndint"
                if rm == 5:                                  # FSCALE
                    self._fst_set(0, math.ldexp(v, int(self._fst(1)))); return "fscale"
                if rm == 6: self._fst_set(0, math.sin(v)); return "fsin"
                if rm == 7: self._fst_set(0, math.cos(v)); return "fcos"
        if op == 0xDF and reg == 4 and rm == 0:              # FNSTSW AX
            s.ax = s.fsw & 0xFFFF; return "fnstsw ax"
        raise UnsupportedInstruction(
            f"x87 opcode {op:02X} /{reg} rm={rm} (register form) at {s.cs:04X}:{s.ip:04X}")

    def fpu_mem_op(self, op: int, reg: int, seg: int, off: int,
                   text: str = "mem") -> str:
        """Memory-form ESC instruction with a pre-computed effective address
        (segment VALUE + offset, exactly what ``_fmem`` produces).

        The interpreter's own semantics, callable directly by lifted code;
        same fail-loud contract as :meth:`fpu_reg_op`, and the same 80-bit
        read/write conversions (``_f80_to_double``/``_double_to_f80``) with
        the documented doubles-for-80-bit precision caveat."""
        import struct
        s = self.s
        if op == 0xD9:
            if reg == 5:                                         # FLDCW m16
                s.fcw = self.mem.rw(seg, off)
                return f"fldcw {text}"
            if reg == 7:                                         # FSTCW m16
                self.mem.ww(seg, off, s.fcw)
                return f"fstcw {text}"
        if op == 0xDB:
            if reg == 0:                                         # FILD m32int
                v = self.mem.rw(seg, off) | (self.mem.rw(seg, (off + 2) & 0xFFFF) << 16)
                if v & 0x80000000:
                    v -= 1 << 32
                self._fpush(float(v))
                return f"fild dword {text}"
            if reg == 5:                                         # FLD m80
                self._fpush(self._f80_to_double(self._fread(seg, off, 10)))
                return f"fld tbyte {text}"
            if reg == 7:                                         # FSTP m80
                self._fwrite(seg, off, self._double_to_f80(self._fpop()))
                return f"fstp tbyte {text}"
        if op == 0xDC and reg == 2:                              # FCOM m64
            (v,) = struct.unpack("<d", self._fread(seg, off, 8))
            self._fcompare(self._fst(0), v)
            return f"fcom qword {text}"
        if op == 0xDD:
            if reg == 0:                                         # FLD m64
                (v,) = struct.unpack("<d", self._fread(seg, off, 8))
                self._fpush(v)
                return f"fld qword {text}"
            if reg == 2 or reg == 3:                             # FST/FSTP m64
                self._fwrite(seg, off, struct.pack("<d", self._fst(0)))
                if reg == 3:
                    self._fpop()
                return f"fst{'p' if reg == 3 else ''} qword {text}"
            if reg == 7:                                         # FNSTSW m16
                self.mem.ww(seg, off, s.fsw)
                return f"fnstsw {text}"
        if op == 0xDF and reg == 7:                              # FISTP m64int
            v = self._fround(self._fpop()) & ((1 << 64) - 1)
            self._fwrite(seg, off, v.to_bytes(8, "little"))
            return f"fistp qword {text}"
        # -- single-precision (m32) + integer (m16/m32) mem forms (SimAnt x87) --
        if op == 0xD9:
            if reg == 0:                                         # FLD m32
                (v,) = struct.unpack("<f", self._fread(seg, off, 4))
                self._fpush(v)
                return f"fld dword {text}"
            if reg in (2, 3):                                    # FST/FSTP m32
                self._fwrite(seg, off, struct.pack("<f", self._fst(0)))
                if reg == 3:
                    self._fpop()
                return f"fst{'p' if reg == 3 else ''} dword {text}"
        if op == 0xDB and reg in (2, 3):                         # FIST/FISTP m32int
            v = self._fround(self._fst(0)) & 0xFFFFFFFF
            self._fwrite(seg, off, v.to_bytes(4, "little"))
            if reg == 3:
                self._fpop()
            return f"fist{'p' if reg == 3 else ''} dword {text}"
        if op == 0xDF:
            if reg == 0:                                         # FILD m16int
                v = self.mem.rw(seg, off)
                if v & 0x8000:
                    v -= 1 << 16
                self._fpush(float(v))
                return f"fild word {text}"
            if reg in (2, 3):                                    # FIST/FISTP m16int
                v = self._fround(self._fst(0)) & 0xFFFF
                self.mem.ww(seg, off, v)
                if reg == 3:
                    self._fpop()
                return f"fist{'p' if reg == 3 else ''} word {text}"
        if op in (0xD8, 0xDC):                    # FADD/FMUL/FCOM/FSUB/FDIV m32/m64
            nbytes, fmt = (4, "<f") if op == 0xD8 else (8, "<d")
            (b,) = struct.unpack(fmt, self._fread(seg, off, nbytes))
            a = self._fst(0)
            if reg == 0: self._fst_set(0, a + b); return f"fadd {text}"
            if reg == 1: self._fst_set(0, a * b); return f"fmul {text}"
            if reg == 2: self._fcompare(a, b); return f"fcom {text}"
            if reg == 3: self._fcompare(a, b); self._fpop(); return f"fcomp {text}"
            if reg == 4: self._fst_set(0, a - b); return f"fsub {text}"
            if reg == 5: self._fst_set(0, b - a); return f"fsubr {text}"
            if reg == 6: self._fst_set(0, a / b); return f"fdiv {text}"
            if reg == 7: self._fst_set(0, b / a); return f"fdivr {text}"
        if op in (0xDA, 0xDE):        # FIADD/FIMUL/FICOM/FISUB/FIDIV m32int/m16int
            if op == 0xDA:                                       # m32int operand
                v = self.mem.rw(seg, off) | (self.mem.rw(seg, (off + 2) & 0xFFFF) << 16)
                if v & 0x80000000:
                    v -= 1 << 32
            else:                                                # m16int operand
                v = self.mem.rw(seg, off)
                if v & 0x8000:
                    v -= 1 << 16
            a, b = self._fst(0), float(v)
            if reg == 0: self._fst_set(0, a + b); return f"fiadd {text}"
            if reg == 1: self._fst_set(0, a * b); return f"fimul {text}"
            if reg == 2: self._fcompare(a, b); return f"ficom {text}"
            if reg == 3: self._fcompare(a, b); self._fpop(); return f"ficomp {text}"
            if reg == 4: self._fst_set(0, a - b); return f"fisub {text}"
            if reg == 5: self._fst_set(0, b - a); return f"fisubr {text}"
            if reg == 6: self._fst_set(0, a / b); return f"fidiv {text}"
            if reg == 7: self._fst_set(0, b / a); return f"fidivr {text}"
        raise UnsupportedInstruction(
            f"x87 opcode {op:02X} /{reg} mem at {s.cs:04X}:{s.ip:04X}")

    def execute_opcode(self, op: int, seg_override: str | None, rep: int | None) -> str:
        if 0xD8 <= op <= 0xDF:
            return self.execute_fpu(op, seg_override)
        s = self.s
        # Disassembly text is DEBUG-ONLY: it is returned to step(), which appends
        # it to the trace ONLY when trace_enabled.  The text never feeds back into
        # CPU/memory state, so building it every instruction is pure waste on the
        # hot (trace-off) path.  Every string return below is `T and f"..."`: when
        # T is False the f-string is never evaluated (short-circuit) and step()
        # discards the False; when True the exact same text is produced as before.
        # Only execute_fpu (above) and string_op (below) have side effects and are
        # returned directly.  ~2x fewer allocations per instruction with trace off.
        T = self.trace_enabled
        # --- frequency-ordered fast path -----------------------------------
        # The ladder below is a top-to-bottom chain of mutually-exclusive `if`
        # tests, so an opcode's cost includes every preceding comparison.  These
        # are the hottest opcodes in real Win16 gameplay and sat deepest in the
        # chain (Jcc/XCHG were ~35 tests in); hoisting them removes those probes
        # from the hot path.  Safe to reorder: each branch is exclusive by opcode
        # value and returns.  Verified byte-exact by the state-digest gate.
        if 0x70 <= op <= 0x7F:                          # Jcc rel8
            rel = self.sign8(self.fetch8()); take = self.condition(op & 0xF)
            target = (s.ip + rel) & 0xFFFF
            if take: s.ip = target
            # Trace always shows the encoded branch target, taken or not --
            # showing the fall-through IP instead reads as "the target is here",
            # which is not what the not-taken case means (regression fix ported
            # from overkill_port, reintroduced when this path was hoisted for perf).
            return T and f"{JCC_NAMES[op & 0xF]} -> {s.cs:04X}:{target:04X} {'taken' if take else 'not'}"
        # Shift/rotate group 2 (hoisted: ~20%+ of real workloads — codec loops)
        if op in (0xC0,0xC1,0xD0,0xD1,0xD2,0xD3):
            bits = 8 if op in (0xC0,0xD0,0xD2) else 16
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            if op in (0xD0,0xD1):
                count = 1
            elif op in (0xD2,0xD3):
                count = s.cx & 0xFF
            else:
                count = self.fetch8()
            val = operand.read()
            res = self.shift(reg, val, count, bits)
            operand.write(res)
            return T and f"shift/{reg} {operand.text},{count}"
        # Arithmetic r/m with reg, directions 00-03,08-0B,10-13,18-1B,20-23,28-2B,30-33,38-3B (hoisted)
        if op < 0x40 and (op & 0x04) == 0 and (op & 0x07) in (0,1,2,3):
            group = (op >> 3) & 7
            bits = 8 if (op & 1) == 0 else 16
            direction_to_reg = bool(op & 2)
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            rmv = operand.read()
            regv = self.get_reg8(reg) if bits == 8 else self.get_reg16(reg)
            if direction_to_reg:
                res, name = self.alu(group, regv, rmv, bits, write=False)
                if group != 7:
                    if bits == 8: self.set_reg8(reg, res)
                    else: self.set_reg16(reg, res)
                return T and f"{name} {REG8[reg] if bits==8 else REG16[reg]},{operand.text}"
            res, name = self.alu(group, rmv, regv, bits, write=False)
            if group != 7:
                operand.write(res)
            return T and f"{name} {operand.text},{REG8[reg] if bits==8 else REG16[reg]}"
        # Group FE/FF: INC/DEC r/m + indirect call/jmp/push (hoisted)
        if op in (0xFE, 0xFF):
            bits = 8 if op == 0xFE else 16
            _, mod, reg, rm = self.peek_modrm()
            if reg in (0,1):
                operand = self.decode_rm_operand(mod, rm, bits, seg_override)
                old = operand.read()
                if reg == 0:
                    res = (old + 1) & ((1 << bits)-1); self.set_incdec_flags(old, res, bits, dec=False); opn = "inc"
                else:
                    res = (old - 1) & ((1 << bits)-1); self.set_incdec_flags(old, res, bits, dec=True); opn = "dec"
                operand.write(res); return T and f"{opn} {operand.text}"
            if op == 0xFF and reg == 2:
                operand = self.decode_rm_operand(mod, rm, 16, seg_override); target = operand.read(); self.push(s.ip); s.ip = target; return T and f"call {operand.text}"
            if op == 0xFF and reg == 3:
                if mod == 3:
                    raise UnsupportedInstruction("far indirect call requires memory operand")
                operand = self.decode_rm_operand(mod, rm, 16, seg_override)
                off, farseg = operand.read_far()
                self.push(s.cs); self.push(s.ip); s.cs = farseg; s.ip = off
                return T and f"call far {operand.text}"
            if op == 0xFF and reg == 4:
                operand = self.decode_rm_operand(mod, rm, 16, seg_override); target = operand.read(); s.ip = target; return T and f"jmp {operand.text}"
            if op == 0xFF and reg == 5:
                if mod == 3:
                    raise UnsupportedInstruction("far indirect jmp requires memory operand")
                operand = self.decode_rm_operand(mod, rm, 16, seg_override)
                off, farseg = operand.read_far()
                s.cs = farseg; s.ip = off
                return T and f"jmp far {operand.text} -> {farseg:04X}:{off:04X}"
            if op == 0xFF and reg == 6:
                operand = self.decode_rm_operand(mod, rm, 16, seg_override); val = operand.read(); self.push(val); return T and f"push {operand.text}"
            raise UnsupportedInstruction(f"group FE/FF /{reg}")
        # CALL/RET/LOOP/JMP short + string ops (hoisted control-flow tail)
        if op == 0xE8:
            rel = self.sign16(self.fetch16())
            ret = s.ip
            target = (s.ip + rel) & 0xFFFF
            self.push(ret)
            s.ip = target
            self.call_depth += 1
            return T and f"call near -> {s.cs:04X}:{target:04X} ret={ret:04X}"
        if op == 0xC3:
            target = self.pop(); s.ip = target; self.call_depth = max(0, self.call_depth - 1); return T and f"ret near -> {s.cs:04X}:{target:04X}"
        if op in (0xE0, 0xE1, 0xE2):
            rel = self.sign8(self.fetch8()); s.cx = (s.cx - 1) & 0xFFFF
            take = s.cx != 0 and (op == 0xE2 or (op == 0xE1 and self.get_flag(ZF)) or (op == 0xE0 and not self.get_flag(ZF)))
            target = (s.ip + rel) & 0xFFFF
            if take: s.ip = target
            name = {0xE0: 'loopne', 0xE1: 'loope', 0xE2: 'loop'}[op]
            return T and f"{name} -> {s.cs:04X}:{target:04X} {'taken' if take else 'not'} cx={s.cx:04X}"
        if op == 0xEB:
            rel = self.sign8(self.fetch8()); target = (s.ip + rel) & 0xFFFF; s.ip = target; return T and f"jmp short -> {s.cs:04X}:{target:04X}"
        if op in (0x6C, 0x6D, 0x6E, 0x6F, 0xA4, 0xA5, 0xA6, 0xA7, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF):
            return self.string_op(op, rep, seg_override)
        if op in (0x86, 0x87):                          # XCHG r/m, reg
            bits = 8 if op == 0x86 else 16
            _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            if bits == 8:
                a = self.get_reg8(reg); b = operand.read(); self.set_reg8(reg, b); operand.write(a); return T and f"xchg {operand.text},{REG8[reg]}"
            a = self.get_reg16(reg); b = operand.read(); self.set_reg16(reg, b); operand.write(a); return T and f"xchg {operand.text},{REG16[reg]}"
        if op in (0x88, 0x89, 0x8A, 0x8B):              # MOV r/m <-> reg
            bits = 8 if op in (0x88, 0x8A) else 16
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            if op in (0x88, 0x89):
                val = self.get_reg8(reg) if bits == 8 else self.get_reg16(reg)
                operand.write(val)
                return T and f"mov {operand.text},{REG8[reg] if bits == 8 else REG16[reg]}"
            val = operand.read()
            if bits == 8: self.set_reg8(reg, val)
            else: self.set_reg16(reg, val)
            return T and f"mov {REG8[reg] if bits == 8 else REG16[reg]},{operand.text}"
        if 0x40 <= op <= 0x47:                          # INC r16
            reg = op - 0x40; old = self.get_reg16(reg); res = (old + 1) & 0xFFFF; self.set_incdec_flags(old, res, 16, dec=False); self.set_reg16(reg,res); return T and f"inc {REG16[reg]}"
        if 0x48 <= op <= 0x4F:                          # DEC r16
            reg = op - 0x48; old = self.get_reg16(reg); res = (old - 1) & 0xFFFF; self.set_incdec_flags(old, res, 16, dec=True); self.set_reg16(reg,res); return T and f"dec {REG16[reg]}"
        # MOV immediate to register
        if 0xB0 <= op <= 0xB7:
            reg = op - 0xB0; imm = self.fetch8(); self.set_reg8(reg, imm); return T and f"mov {REG8[reg]},{imm:02X}h"
        if 0xB8 <= op <= 0xBF:
            reg = op - 0xB8; imm = self.fetch16(); self.set_reg16(reg, imm); return T and f"mov {REG16[reg]},{imm:04X}h"

        # PUSH/POP registers and segment registers
        if 0x50 <= op <= 0x57:
            reg = op - 0x50; self.push(self.get_reg16(reg)); return T and f"push {REG16[reg]}"
        if 0x58 <= op <= 0x5F:
            reg = op - 0x58; self.set_reg16(reg, self.pop()); return T and f"pop {REG16[reg]}"
        if op == 0x60:              # PUSHA (80186+): AX,CX,DX,BX,orig SP,BP,SI,DI
            sp0 = s.sp
            for v in (s.ax, s.cx, s.dx, s.bx, sp0, s.bp, s.si, s.di):
                self.push(v)
            return T and "pusha"
        if op == 0x61:              # POPA (80186+): DI,SI,BP,(skip SP),BX,DX,CX,AX
            s.di = self.pop(); s.si = self.pop(); s.bp = self.pop()
            self.pop()                                  # discard the saved SP
            s.bx = self.pop(); s.dx = self.pop(); s.cx = self.pop(); s.ax = self.pop()
            return T and "popa"
        if op in (0x06, 0x0E, 0x16, 0x1E):
            idx = {0x06: 0, 0x0E: 1, 0x16: 2, 0x1E: 3}[op]; self.push(self.get_sreg(idx)); return T and f"push {SREG[idx]}"
        if op in (0x07, 0x17, 0x1F):
            idx = {0x07: 0, 0x17: 2, 0x1F: 3}[op]; self.set_sreg(idx, self.pop()); return T and f"pop {SREG[idx]}"
        if op == 0x68:
            imm = self.fetch16()
            self.push(imm)
            return T and f"push {imm:04X}h"
        if op == 0x6A:
            imm8 = self.fetch8()
            imm = imm8 | 0xFF00 if imm8 & 0x80 else imm8
            self.push(imm)
            return T and f"push {imm:04X}h"
        if op in (0x69, 0x6B):  # IMUL r16, r/m16, imm (80186+; Win16 code)
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, 16, seg_override)
            src = self.sign16(operand.read())
            imm = self.sign8(self.fetch8()) if op == 0x6B else self.sign16(self.fetch16())
            result = src * imm
            self.set_reg16(reg, result & 0xFFFF)
            carry = not (-32768 <= result <= 32767)
            self.set_flag(CF, carry); self.set_flag(OF, carry)
            return T and f"imul {REG16[reg]},{operand.text},{imm}"
        if op == 0x9C:
            self.push(s.flags); return T and "pushf"
        if op == 0x9D:
            s.flags = self.pop() | 0x0002; return T and "popf"
        if op == 0x9B:  # WAIT/FWAIT: no coprocessor exceptions are modelled,
            # so this is a no-op (first exercised by Win16 x87-emulator code).
            return T and "wait"
        if op == 0x9F:  # LAHF: AH = low FLAGS byte (SF ZF 0 AF 0 PF 1 CF).
            # First decoded in a Win16 CRT's raw-INT-21h write path.
            s.ax = (s.ax & 0x00FF) | (((s.flags & 0xD5) | 0x02) << 8)
            return T and "lahf"
        if op == 0x9E:  # SAHF: SF ZF AF PF CF from AH; other flags preserved.
            s.flags = (s.flags & ~0xD5) | ((s.ax >> 8) & 0xD5) | 0x0002
            return T and "sahf"
        if op == 0x98:
            al = s.ax & 0x00FF
            s.ax = al | (0xFF00 if al & 0x80 else 0x0000)
            return T and "cbw"
        if op == 0x99:  # CWD: DX:AX = sign-extended AX (first hit by Win16 code)
            s.dx = 0xFFFF if s.ax & 0x8000 else 0x0000
            return T and "cwd"

        # MOV between r/m and reg / segment
        if op == 0x8C:
            _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod, rm, 16, seg_override); operand.write(self.get_sreg(reg & 3)); return T and f"mov {operand.text},{SREG[reg & 3]}"
        if op == 0x8E:
            _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod, rm, 16, seg_override); val = operand.read(); self.set_sreg(reg & 3, val); return T and f"mov {SREG[reg & 3]},{operand.text}"
        if op in (0xA0, 0xA1, 0xA2, 0xA3):
            off = self.fetch16(); seg = getattr(s, seg_override or "ds")
            if op == 0xA0:
                s.ax = (s.ax & 0xFF00) | self.mem.rb(seg, off); return T and f"mov al,[{off:04X}]"
            if op == 0xA1:
                s.ax = self.mem.rw(seg, off); return T and f"mov ax,[{off:04X}]"
            if op == 0xA2:
                self.mem.wb(seg, off, s.ax); return T and f"mov [{off:04X}],al"
            self.mem.ww(seg, off, s.ax); return T and f"mov [{off:04X}],ax"
        if op in (0xC6, 0xC7):
            bits = 8 if op == 0xC6 else 16
            _, mod, reg, rm = self.peek_modrm()
            if reg != 0: raise UnsupportedInstruction(f"group mov /{reg} at {s.cs:04X}:{(s.ip-2)&0xffff:04X}")
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            imm = self.fetch8() if bits == 8 else self.fetch16()
            operand.write(imm)
            return T and f"mov {operand.text},{imm:0{bits//4}X}h"

        # Arithmetic accumulator immediates
        if op in (0x04,0x05,0x0C,0x0D,0x14,0x15,0x1C,0x1D,0x24,0x25,0x2C,0x2D,0x34,0x35,0x3C,0x3D):
            bits = 8 if op % 2 == 0 else 16
            imm = self.fetch8() if bits == 8 else self.fetch16()
            a = self.get_reg8(0) if bits == 8 else s.ax
            group = (op >> 3) & 7
            res, name = self.alu(group, a, imm, bits, write=False)
            if group != 7:
                if bits == 8: self.set_reg8(0, res)
                else: s.ax = res & 0xFFFF
            return T and f"{name} {'al' if bits == 8 else 'ax'},{imm:0{bits//4}X}h"

        # (Arithmetic r/m-with-reg is hoisted into the fast path above.)

        # Group 1 immediate to r/m
        if op in (0x80, 0x81, 0x82, 0x83):
            bits = 8 if op in (0x80, 0x82) else 16
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            dstv = operand.read()
            if op == 0x83:
                imm = self.sign8(self.fetch8()) & 0xFFFF
            else:
                imm = self.fetch8() if bits == 8 else self.fetch16()
            res, name = self.alu(reg, dstv, imm, bits, write=False)
            if reg != 7:
                operand.write(res)
            return T and f"{name} {operand.text},{imm:0{bits//4}X}h"

        # (INC/DEC r16 and the FE/FF group are hoisted into the fast path above.)

        # TEST/XCHG/LEA
        if op in (0x84, 0x85):
            bits = 8 if op == 0x84 else 16; _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod, rm, bits, seg_override); a = operand.read(); b = self.get_reg8(reg) if bits == 8 else self.get_reg16(reg); self.set_logic_flags(a & b,bits); return T and f"test {operand.text},{REG8[reg] if bits==8 else REG16[reg]}"
        if op in (0xA8, 0xA9):
            bits = 8 if op == 0xA8 else 16; imm = self.fetch8() if bits==8 else self.fetch16(); a = self.get_reg8(0) if bits==8 else s.ax; self.set_logic_flags(a & imm,bits); return T and f"test {'al' if bits==8 else 'ax'},{imm:X}h"
        if op == 0x8D:
            _, mod, reg, rm = self.peek_modrm()
            if mod == 3: raise UnsupportedInstruction("lea with register source")
            ea = self.decode_ea(mod, rm, seg_override); self.set_reg16(reg, ea.offset); return T and f"lea {REG16[reg]},{ea.text}"
        if op in (0xC4, 0xC5):
            _, mod, reg, rm = self.peek_modrm()
            if mod == 3:
                raise UnsupportedInstruction("les/lds requires memory source")
            operand = self.decode_rm_operand(mod, rm, 16, seg_override)
            off, seg = operand.read_far()
            self.set_reg16(reg, off)
            if op == 0xC4:
                s.es = seg
                return T and f"les {REG16[reg]},{operand.text} -> {seg:04X}:{off:04X}"
            s.ds = seg
            return T and f"lds {REG16[reg]},{operand.text} -> {seg:04X}:{off:04X}"
        if op == 0x8F:  # POP r/m16.  The 8086 ignores the reg field (it is not a
            # real opcode group); some code emits non-zero reg bits and still pops.
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, 16, seg_override)
            operand.write(self.pop())
            return T and f"pop {operand.text}"
        if 0x90 <= op <= 0x97:
            reg = op - 0x90
            if reg:
                ax = s.ax; s.ax = self.get_reg16(reg); self.set_reg16(reg, ax)
                return T and f"xchg ax,{REG16[reg]}"
            return T and "nop"

        # Control flow (CALL near / RET near / LOOPcc / JMP short are hoisted above)
        if op == 0x9A:
            off = self.fetch16(); seg = self.fetch16(); ret_cs, ret_ip = s.cs, s.ip
            self.push(ret_cs); self.push(ret_ip); s.cs = seg; s.ip = off
            self.call_depth += 1
            return T and f"call far -> {seg:04X}:{off:04X} ret={ret_cs:04X}:{ret_ip:04X}"
        if op == 0xE9:
            rel = self.sign16(self.fetch16()); target = (s.ip + rel) & 0xFFFF; s.ip = target; return T and f"jmp near -> {s.cs:04X}:{target:04X}"
        if op == 0xEA:
            off = self.fetch16(); seg = self.fetch16(); s.cs = seg; s.ip = off; return T and f"jmp far -> {seg:04X}:{off:04X}"
        if op == 0xC2:
            n = self.fetch16(); target = self.pop(); s.ip = target; s.sp = (s.sp + n) & 0xFFFF; self.call_depth = max(0, self.call_depth - 1); return T and f"ret near {n} -> {s.cs:04X}:{target:04X}"
        if op == 0xCB:
            target_ip = self.pop(); target_cs = self.pop(); s.ip = target_ip; s.cs = target_cs; self.call_depth = max(0, self.call_depth - 1); return T and f"ret far -> {target_cs:04X}:{target_ip:04X}"
        if op == 0xCA:
            n = self.fetch16(); target_ip = self.pop(); target_cs = self.pop(); s.ip = target_ip; s.cs = target_cs; s.sp = (s.sp + n) & 0xFFFF; self.call_depth = max(0, self.call_depth - 1); return T and f"ret far {n} -> {target_cs:04X}:{target_ip:04X}"
        if op == 0xC8:  # ENTER alloc,nesting (80186+): make a stack frame.
            alloc = self.fetch16()
            nesting = self.fetch8() & 0x1F
            self.push(s.bp & 0xFFFF)
            frame = s.sp & 0xFFFF
            for _ in range(1, nesting):         # copy enclosing frame pointers
                s.bp = (s.bp - 2) & 0xFFFF
                self.push(self.mem.rw(s.ss, s.bp))
            if nesting > 0:
                self.push(frame)
            s.bp = frame
            s.sp = (s.sp - alloc) & 0xFFFF
            return T and f"enter {alloc:#06x},{nesting}"
        if op == 0xC9:  # LEAVE (80186+): SP=BP, pop BP.  Win16 (MSC) epilogues
            # use it; first exercised by an NE executable's WinMain path.
            s.sp = s.bp
            s.bp = self.pop()
            return T and "leave"
        if op == 0xCF:  # IRET: pop IP, CS, FLAGS
            target_ip = self.pop(); target_cs = self.pop(); s.flags = self.pop() | 0x0002
            s.ip = target_ip; s.cs = target_cs; self.call_depth = max(0, self.call_depth - 1)
            return T and f"iret -> {target_cs:04X}:{target_ip:04X}"
        if op == 0xE3:
            rel = self.sign8(self.fetch8()); take = s.cx == 0
            target = (s.ip + rel) & 0xFFFF
            if take: s.ip = target
            return T and f"jcxz -> {s.cs:04X}:{target:04X} {'taken' if take else 'not'}"

        # (String operations are hoisted into the fast path above.)

        if op == 0xD7:  # XLAT
            seg = getattr(s, seg_override or "ds")
            off = (s.bx + (s.ax & 0xFF)) & 0xFFFF
            self.set_reg8(0, self.mem.rb(seg, off))
            return T and f"xlat {seg_override or 'ds'}:[bx+al]"

        if op == 0x27:  # DAA - decimal adjust AL after BCD addition
            old_al = self.get_reg8(0)
            old_cf = self.get_flag(CF)
            al = old_al
            adjust_low = (al & 0x0F) > 9 or self.get_flag(AF)
            if adjust_low:
                al = (al + 0x06) & 0xFF
            adjust_high = old_al > 0x99 or old_cf
            if adjust_high:
                al = (al + 0x60) & 0xFF
            self.set_reg8(0, al)
            self.set_flag(AF, adjust_low)
            self.set_flag(CF, adjust_high)
            # 8086 defines SF/ZF/PF from adjusted AL. OF is undefined; leave it
            # unchanged so code that does not rely on undefined OF remains stable.
            self.set_flag(ZF, al == 0)
            self.set_flag(SF, bool(al & 0x80))
            self.set_flag(PF, self.parity(al))
            return T and "daa"

        # (Shift/rotate group 2 is hoisted into the fast path above.)

        # Flag and misc
        if op == 0xF5: self.set_flag(CF, not self.get_flag(CF)); return T and "cmc"
        if op == 0xF8: self.set_flag(CF, False); return T and "clc"
        if op == 0xF9: self.set_flag(CF, True); return T and "stc"
        if op == 0xFC: self.set_flag(DF, False); return T and "cld"
        if op == 0xFD: self.set_flag(DF, True); return T and "std"
        if op == 0xFA: self.set_flag(IF, False); return T and "cli"
        if op == 0xFB: self.set_flag(IF, True); return T and "sti"
        if op == 0xF4: self.halted = True; return T and "hlt"
        if op in (0xE4, 0xE5, 0xEC, 0xED):
            if op in (0xE4, 0xE5):
                port = self.fetch8()
            else:
                port = s.dx
            bits = 8 if op in (0xE4, 0xEC) else 16
            value = self.port_reader(self, port & 0xFFFF, bits) if self.port_reader else 0
            if bits == 8:
                self.set_reg8(0, value)
                return T and f"in al,{port:04X}h -> {value & 0xFF:02X}h"
            s.ax = value & 0xFFFF
            return T and f"in ax,{port:04X}h -> {value & 0xFFFF:04X}h"
        if op in (0xE6, 0xE7, 0xEE, 0xEF):
            if op in (0xE6, 0xE7):
                port = self.fetch8()
            else:
                port = s.dx
            bits = 8 if op in (0xE6, 0xEE) else 16
            value = self.get_reg8(0) if bits == 8 else s.ax
            if self.port_writer:
                self.port_writer(self, port & 0xFFFF, value, bits)
            return T and f"out {port:04X}h,{'al' if bits == 8 else 'ax'} ({value:0{bits//4}X}h)"
        if op == 0xCD:
            num = self.fetch8()
            before_ax = s.ax
            if self.interrupt_handler:
                self.interrupt_handler(self, num)
            else:
                raise UnsupportedInstruction(f"INT {num:02X}h not hooked")
            cf = 1 if self.get_flag(CF) else 0
            return T and f"int {num:02X}h ah={(before_ax >> 8) & 0xFF:02X}h ax:{before_ax:04X}->{s.ax:04X} cf={cf}"
        if op == 0xCC:
            if self.interrupt_handler: self.interrupt_handler(self, 3)
            return T and "int3"

        # Group 3 unary
        if op in (0xF6,0xF7):
            bits = 8 if op == 0xF6 else 16
            _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod,rm,bits,seg_override); val = operand.read()
            if reg == 0:
                imm = self.fetch8() if bits == 8 else self.fetch16(); self.set_logic_flags(val & imm,bits); return T and f"test {operand.text},{imm:X}h"
            if reg == 2:
                operand.write((~val)&((1<<bits)-1)); return T and f"not {operand.text}"
            if reg == 3:
                res = (-val) & ((1<<bits)-1); self.set_sub_flags(0,val,-val,bits); operand.write(res); return T and f"neg {operand.text}"
            if reg == 4:  # MUL unsigned
                if bits == 8:
                    result = (s.ax & 0x00FF) * (val & 0xFF)
                    s.ax = result & 0xFFFF
                    carry = (result >> 8) != 0
                else:
                    result = (s.ax & 0xFFFF) * (val & 0xFFFF)
                    s.ax = result & 0xFFFF
                    s.dx = (result >> 16) & 0xFFFF
                    carry = s.dx != 0
                self.set_flag(CF, carry); self.set_flag(OF, carry)
                return T and f"mul {operand.text}"
            if reg == 5:  # IMUL signed
                if bits == 8:
                    a = self.sign8(s.ax & 0xFF); b = self.sign8(val & 0xFF); result = a * b
                    s.ax = result & 0xFFFF
                    carry = not (-128 <= result <= 127)
                else:
                    a = self.sign16(s.ax); b = self.sign16(val); result = a * b
                    s.ax = result & 0xFFFF
                    s.dx = (result >> 16) & 0xFFFF
                    carry = not (-32768 <= result <= 32767)
                self.set_flag(CF, carry); self.set_flag(OF, carry)
                return T and f"imul {operand.text}"
            if reg == 6:  # DIV unsigned
                if val == 0:
                    raise ZeroDivisionError(f"div by zero at {s.cs:04X}:{s.ip:04X}")
                if bits == 8:
                    dividend = s.ax & 0xFFFF
                    q, r = divmod(dividend, val & 0xFF)
                    if q > 0xFF: raise OverflowError("8-bit div quotient overflow")
                    s.ax = ((r & 0xFF) << 8) | (q & 0xFF)
                else:
                    dividend = ((s.dx & 0xFFFF) << 16) | (s.ax & 0xFFFF)
                    q, r = divmod(dividend, val & 0xFFFF)
                    if q > 0xFFFF: raise OverflowError("16-bit div quotient overflow")
                    s.ax = q & 0xFFFF; s.dx = r & 0xFFFF
                return T and f"div {operand.text}"
            if reg == 7:  # IDIV signed
                if val == 0:
                    raise ZeroDivisionError(f"idiv by zero at {s.cs:04X}:{s.ip:04X}")
                if bits == 8:
                    dividend = self.sign16(s.ax)
                    divisor = self.sign8(val & 0xFF)
                    q = int(dividend / divisor); r = dividend - q * divisor
                    if q < -128 or q > 127: raise OverflowError("8-bit idiv quotient overflow")
                    s.ax = ((r & 0xFF) << 8) | (q & 0xFF)
                else:
                    dividend = ((s.dx & 0xFFFF) << 16) | (s.ax & 0xFFFF)
                    if dividend & 0x80000000: dividend -= 0x100000000
                    divisor = self.sign16(val & 0xFFFF)
                    q = int(dividend / divisor); r = dividend - q * divisor
                    if q < -32768 or q > 32767: raise OverflowError("16-bit idiv quotient overflow")
                    s.ax = q & 0xFFFF; s.dx = r & 0xFFFF
                return T and f"idiv {operand.text}"
            raise UnsupportedInstruction(f"group F6/F7 /{reg}")

        raise UnsupportedInstruction(f"Unsupported opcode {op:02X} at {s.cs:04X}:{(s.ip-1)&0xFFFF:04X}")

    def alu(self, group: int, a: int, b: int, bits: int, write: bool) -> tuple[int, str]:
        mask = (1 << bits) - 1
        names = _ALU_NAMES
        if group == 0:
            res = a + b; self.set_add_flags(a,b,res,bits)
        elif group == 1:
            res = a | b; self.set_logic_flags(res,bits)
        elif group == 2:
            carry = 1 if self.get_flag(CF) else 0; res = a + b + carry; self.set_add_flags(a,b,res,bits,carry)
        elif group == 3:
            carry = 1 if self.get_flag(CF) else 0; res = a - b - carry; self.set_sub_flags(a,b,res,bits,carry)
        elif group == 4:
            res = a & b; self.set_logic_flags(res,bits)
        elif group == 5:
            res = a - b; self.set_sub_flags(a,b,res,bits)
        elif group == 6:
            res = a ^ b; self.set_logic_flags(res,bits)
        elif group == 7:
            res = a - b; self.set_sub_flags(a,b,res,bits)
        else:
            raise AssertionError(group)
        return res & mask, names[group]

    def condition(self, cond: int) -> bool:
        # Hot path: direct flag-bit tests, evaluating only the asked condition
        # (previously a 16-element list built every condition, five get_flag
        # calls, on every conditional branch).
        f = self.s.flags
        if cond == 0x0: return bool(f & OF)
        if cond == 0x1: return not (f & OF)
        if cond == 0x2: return bool(f & CF)
        if cond == 0x3: return not (f & CF)
        if cond == 0x4: return bool(f & ZF)
        if cond == 0x5: return not (f & ZF)
        if cond == 0x6: return bool(f & (CF | ZF))
        if cond == 0x7: return not (f & (CF | ZF))
        if cond == 0x8: return bool(f & SF)
        if cond == 0x9: return not (f & SF)
        if cond == 0xA: return bool(f & PF)
        if cond == 0xB: return not (f & PF)
        sf, of = bool(f & SF), bool(f & OF)
        if cond == 0xC: return sf != of
        if cond == 0xD: return sf == of
        if cond == 0xE: return bool(f & ZF) or (sf != of)
        return not (f & ZF) and (sf == of)

    def string_op(self, op: int, rep: int | None, seg_override: str | None = None) -> str:
        s = self.s
        width = 2 if op in (0x6D,0x6F,0xA5,0xA7,0xAB,0xAD,0xAF) else 1
        delta = -width if self.get_flag(DF) else width
        count = s.cx if rep is not None else 1
        if count > self.max_rep_count:
            raise RuntimeError(f"REP count too large: {count}")

        # --- bulk fast path for REP MOVS / REP STOS -------------------------
        # A 32KB frame blit is one Python slice operation instead of 16k
        # element iterations.  Guarded so it is BYTE-EXACT equal to the loop:
        # real mode only, DF=0, no write watchers, no 16-bit offset wrap, no
        # 1MB linear wrap, neither range touching the EGA aperture (latch /
        # plane-mask semantics) or ROM (stores there are ignored), and for
        # MOVS no src<dst overlap (the 8086's element-order copy then reads
        # bytes it already wrote — the classic repeating-fill pattern — which
        # a snapshot slice copy would get wrong).  MOVS/STOS never touch
        # flags, so the end state is just CX=0 and advanced SI/DI.
        # (F2 is intentionally accepted: REPNE only means "check ZF" for
        # CMPS/SCAS; for MOVS/STOS this loop treats it identically to F3.)
        if rep is not None and count > 8 and op in (0xA4, 0xA5, 0xAA, 0xAB):
            mem = self.mem
            if mem.sel_base is None and delta > 0 and not mem.write_watchers:
                n = count * width
                di = s.di & 0xFFFF
                dst = (((s.es & 0xFFFF) << 4) + di) & 0xFFFFF
                dst_ok = (di + n <= 0x10000 and dst + n <= 0x100000
                          and dst + n <= BIOS_ROM_BASE
                          and (not mem.ega_planar
                               or dst + n <= EGA_CPU_APERTURE or dst >= _EGA_WINDOW_END))
                if dst_ok and op in (0xAA, 0xAB):        # rep stos
                    if width == 1:
                        mem.data[dst:dst + n] = bytes((s.ax & 0xFF,)) * count
                    else:
                        mem.data[dst:dst + n] = bytes((s.ax & 0xFF, (s.ax >> 8) & 0xFF)) * count
                    s.di = (di + n) & 0xFFFF
                    s.cx = 0
                    return ("rep " if rep else "") + ("stosb" if width == 1 else "stosw") + f" ; {count}"
                if dst_ok and op in (0xA4, 0xA5):        # rep movs
                    si = s.si & 0xFFFF
                    src_seg = getattr(s, seg_override or "ds") & 0xFFFF
                    src = ((src_seg << 4) + si) & 0xFFFFF
                    src_ok = (si + n <= 0x10000 and src + n <= 0x100000
                              and (not mem.ega_planar
                                   or src + n <= EGA_CPU_APERTURE or src >= _EGA_WINDOW_END))
                    overlap_repeats = src < dst < src + n
                    if src_ok and not overlap_repeats:
                        mem.data[dst:dst + n] = mem.data[src:src + n]
                        s.si = (si + n) & 0xFFFF
                        s.di = (di + n) & 0xFFFF
                        s.cx = 0
                        return ("rep " if rep else "") + ("movsb" if width == 1 else "movsw") + f" ; {count}"

        done = 0
        while count > 0:
            done += 1
            if op in (0x6C,0x6D):
                value = self.port_reader(self, s.dx & 0xFFFF, 8 if width == 1 else 16) if self.port_reader else 0
                if width == 1: self.mem.wb(s.es, s.di, value)
                else: self.mem.ww(s.es, s.di, value)
                s.di = (s.di + delta) & 0xFFFF
            elif op in (0x6E,0x6F):
                src_seg = getattr(s, seg_override or "ds")
                value = self.mem.rb(src_seg, s.si) if width == 1 else self.mem.rw(src_seg, s.si)
                if self.port_writer:
                    self.port_writer(self, s.dx & 0xFFFF, value, 8 if width == 1 else 16)
                s.si = (s.si + delta) & 0xFFFF
            elif op in (0xA4,0xA5):
                src_seg = getattr(s, seg_override or "ds")
                val = self.mem.rb(src_seg, s.si) if width == 1 else self.mem.rw(src_seg, s.si)
                if width == 1: self.mem.wb(s.es, s.di, val)
                else: self.mem.ww(s.es, s.di, val)
                s.si = (s.si + delta) & 0xFFFF; s.di = (s.di + delta) & 0xFFFF
            elif op in (0xA6,0xA7):
                src_seg = getattr(s, seg_override or "ds")
                left = self.mem.rb(src_seg, s.si) if width == 1 else self.mem.rw(src_seg, s.si)
                right = self.mem.rb(s.es, s.di) if width == 1 else self.mem.rw(s.es, s.di)
                self.set_sub_flags(left, right, left - right, 8 if width == 1 else 16)
                s.si = (s.si + delta) & 0xFFFF
                s.di = (s.di + delta) & 0xFFFF
                if rep is not None:
                    s.cx = (s.cx - 1) & 0xFFFF
                    count -= 1
                    if rep == 0xF3 and not self.get_flag(ZF):
                        break
                    if rep == 0xF2 and self.get_flag(ZF):
                        break
                    continue
            elif op in (0xAA,0xAB):
                if width == 1: self.mem.wb(s.es, s.di, s.ax)
                else: self.mem.ww(s.es, s.di, s.ax)
                s.di = (s.di + delta) & 0xFFFF
            elif op in (0xAC,0xAD):
                src_seg = getattr(s, seg_override or "ds")
                if width == 1: self.set_reg8(0, self.mem.rb(src_seg, s.si))
                else: s.ax = self.mem.rw(src_seg, s.si)
                s.si = (s.si + delta) & 0xFFFF
            elif op in (0xAE,0xAF):
                memv = self.mem.rb(s.es, s.di) if width == 1 else self.mem.rw(s.es, s.di)
                acc = self.get_reg8(0) if width == 1 else s.ax
                self.set_sub_flags(acc, memv, acc - memv, 8 if width == 1 else 16)
                s.di = (s.di + delta) & 0xFFFF
                if rep is not None:
                    s.cx = (s.cx - 1) & 0xFFFF
                    count -= 1
                    if rep == 0xF3 and not self.get_flag(ZF):
                        break
                    if rep == 0xF2 and self.get_flag(ZF):
                        break
                    continue
            if rep is not None:
                s.cx = (s.cx - 1) & 0xFFFF
            count -= 1
            if rep is None:
                break
        names = {
            0x6C:"insb",0x6D:"insw",0x6E:"outsb",0x6F:"outsw",
            0xA4:"movsb",0xA5:"movsw",0xA6:"cmpsb",0xA7:"cmpsw",0xAA:"stosb",0xAB:"stosw",
            0xAC:"lodsb",0xAD:"lodsw",0xAE:"scasb",0xAF:"scasw",
        }
        return ("rep " if rep else "") + names[op] + (f" ; {done}" if rep else "")

    def shift(self, group: int, val: int, count: int, bits: int) -> int:
        # Closed-form shifts/rotates (this used to be a per-bit loop with a
        # set_flag call per iteration — SHL AX,8 cost 8 iterations; codec-heavy
        # code like the SQZ/LZS decoders spends a large share of its time here).
        # Each formula reproduces exactly what the bit-loop left in the result
        # and in CF; the regression gate is a byte-exact digest of multi-million
        # instruction runs of real games plus the loop-vs-closed-form fuzz test.
        mask = (1 << bits) - 1
        count &= 0x1F
        res = val & mask
        if count == 0:
            return res
        orig = res
        f = self.s.flags
        if group == 4:      # shl/sal — CF = last bit shifted out = bit(bits-count)
            if count <= bits:
                cf = (orig >> (bits - count)) & 1
                res = (orig << count) & mask
            else:
                cf = 0
                res = 0
        elif group == 5:    # shr — CF = last bit shifted out = bit(count-1)
            if count <= bits:
                cf = (orig >> (count - 1)) & 1
                res = orig >> count
            else:
                cf = 0
                res = 0
        elif group == 7:    # sar — shifts in copies of the sign bit
            sign = (orig >> (bits - 1)) & 1
            if count >= bits:
                res = mask if sign else 0
                cf = sign
            else:
                cf = (orig >> (count - 1)) & 1
                sval = orig - (1 << bits) if sign else orig
                res = (sval >> count) & mask
        elif group == 0:    # rol — CF = bit rotated into the lsb on the last step
            n = count % bits
            if n:
                res = ((orig << n) | (orig >> (bits - n))) & mask
            cf = res & 1
        elif group == 1:    # ror — CF = bit rotated into the msb on the last step
            n = count % bits
            if n:
                res = ((orig >> n) | (orig << (bits - n))) & mask
            cf = (res >> (bits - 1)) & 1
        elif group == 2:    # rcl — rotate through CF: a (bits+1)-wide rotation
            width = bits + 1
            n = count % width
            combined = ((f & CF) << bits) | orig
            if n:
                combined = ((combined << n) | (combined >> (width - n))) & ((1 << width) - 1)
            res = combined & mask
            cf = (combined >> bits) & 1
        elif group == 3:    # rcr — the right rotation of the same (bits+1) value
            width = bits + 1
            n = count % width
            combined = ((f & CF) << bits) | orig
            if n:
                combined = ((combined >> n) | (combined << (width - n))) & ((1 << width) - 1)
            res = combined & mask
            cf = (combined >> bits) & 1
        else:
            raise UnsupportedInstruction(f"unsupported shift group /{group}")

        f = (f & ~CF) | cf
        # Rotates only define CF and, for count=1, OF. They do not update ZF/SF/PF.
        # The PRE2 SQZ decoder relies on CF flowing through SHR/RCL chains, so
        # touching the normal arithmetic flags here corrupts compressed streams.
        if group in (4, 5, 7):
            f &= ~(ZF | SF | PF)
            if res == 0:
                f |= ZF
            if res & (1 << (bits - 1)):
                f |= SF
            if _PARITY[res & 0xFF]:
                f |= PF
        # OF is only architecturally defined for a 1-bit shift/rotate (undefined for
        # other counts, so we leave it untouched there).  Drives JO/JG/JL/JGE/JLE.
        if count == 1:
            msb = (res >> (bits - 1)) & 1
            if group == 4:      # shl/sal: OF = CF(orig msb) ^ new msb
                of = ((orig >> (bits - 1)) & 1) ^ msb
            elif group == 5:    # shr: OF = original msb
                of = (orig >> (bits - 1)) & 1
            elif group == 7:    # sar: OF always 0
                of = 0
            elif group == 0:    # rol: OF = msb ^ new lsb (= new CF)
                of = msb ^ (res & 1)
            elif group == 2:    # rcl: OF = msb ^ new CF
                of = msb ^ cf
            else:               # ror / rcr: OF = top two bits of result differ
                of = msb ^ ((res >> (bits - 2)) & 1)
            if of:
                f |= OF
            else:
                f &= ~OF
        self.s.flags = (f | 0x0002) & 0x0FFF
        return res & mask

    def last_ea_offset(self, mod: int, rm: int) -> int:
        # Not reliable after decoding, placeholder for far indirect implementation work.
        raise UnsupportedInstruction("far indirect call/jmp EA reread not implemented")
