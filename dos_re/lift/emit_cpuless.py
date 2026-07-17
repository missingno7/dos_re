"""The CPUless emitter (M3, stage 2) -- recovered function + CPU-ABI adapter.

Emits, for one promotable function, TWO generated artifacts from the shared
recovery IR (never from generated Python):

* the RECOVERED module -- pure Python computing the game behavior over
  ``(mem, <inferred live-in registers>)``.  It imports NOTHING (generated
  local helpers only), receives no CPU object, and its PUBLIC contract is the
  semantic result alone: a dict of written-register outputs.  Exact timing and
  exit-flag reproduction are generated COMPATIBILITY METADATA returned through
  a separate hidden channel (``_compat``) -- owned by the adapter, never part
  of the semantic API (owner correction 1: instruction counts are not game
  logic; owner correction 2: dead registers/flags are not semantic outputs).

* the ADAPTER module -- drops into the function's lifted slot: reads ONLY the
  inferred inputs from the CPU carrier, calls the recovered function, writes
  the inferred outputs back, reproduces the exit FLAGS word from the compat
  channel, applies the historical RET ABI and the exact per-path virtual-time
  cost, and preserves the lifted module interface (ENTRY / SIGNATURE /
  ``owns_time``) so the existing VMless graph, linker and installer are
  untouched.  The recovered body is authoritative; the lifted entry becomes
  its generated adapter -- no duplicate implementation survives.

FIRST PROMOTION SUBSET (strict, refusal-first): straight-line/branchy leaf
functions with no calls, no interrupts, no boundary/dispatch addresses, no
indirect transfers, no segment writes, no stack traffic (zero stack delta
everywhere; the final RET is the adapter's job), no flag live-ins, and only
opcodes this emitter has exact pure-Python semantics for.  Anything else
REFUSES with a named reason -- conservative over-promotion, never a silent
full-CPU fallback.
"""
from __future__ import annotations

from dataclasses import dataclass

from .decode import (CALL, CALL_FAR, CALL_IND, INT, IRET, JCC, JMP, JMP_IND,
                     RET, RETF, SEQ)
from .cpuless import abi_scan, register_effects, W16, SEGS

# x86 FLAGS bits (mirrors dos_re.cpu constants; literal here because the
# recovered module may not import the CPU carrier).
FCF, FPF, FAF, FZF, FSF, FOF = 0x0001, 0x0004, 0x0010, 0x0040, 0x0080, 0x0800

_JCC_EXPR = {
    0x70: "of", 0x71: "not of", 0x72: "cf", 0x73: "not cf",
    0x74: "zf", 0x75: "not zf", 0x76: "cf or zf", 0x77: "not (cf or zf)",
    0x78: "sf", 0x79: "not sf", 0x7A: "pf", 0x7B: "not pf",
    0x7C: "sf != of", 0x7D: "sf == of",
    0x7E: "zf or (sf != of)", 0x7F: "not (zf or (sf != of))",
}
#: flags each jcc condition READS (for the flag-live-in refusal check).
_JCC_READS = {
    0xE0: {"zf"}, 0xE1: {"zf"}, 0xE2: set(), 0xE3: set(),
    0x70: {"of"}, 0x71: {"of"}, 0x72: {"cf"}, 0x73: {"cf"},
    0x74: {"zf"}, 0x75: {"zf"}, 0x76: {"cf", "zf"}, 0x77: {"cf", "zf"},
    0x78: {"sf"}, 0x79: {"sf"}, 0x7A: {"pf"}, 0x7B: {"pf"},
    0x7C: {"sf", "of"}, 0x7D: {"sf", "of"},
    0x7E: {"zf", "sf", "of"}, 0x7F: {"zf", "sf", "of"},
}
_ALU = ("add", "or", "adc", "sbb", "and", "sub", "xor", "cmp")
_INT_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "ds", "es")
#: the full bundle a runtime-dispatched callee may read/write (tier 9).
_DYN_REGS = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di", "ds", "es", "ss")
#: safety net for the recovered block-dispatch loop -- a spin-wait on a wrong
#: port or an interrupt-only flag would otherwise hang forever.  Set far above
#: any legitimate in-function loop (a retrace/vblank wait exits in a handful of
#: block transitions; a rep is ONE dispatch step) so it only catches genuine
#: unbounded spins, and reports the function + block instead of freezing.
_DISPATCH_ITER_CAP = 20_000_000
FDF, FIF = 0x0400, 0x0200
_FLAG_BITS = {"cf": FCF, "pf": FPF, "af": FAF, "zf": FZF, "sf": FSF,
              "of": FOF, "df": FDF, "intf": FIF}


class Refusal(Exception):
    """Promotion refused; ``str(exc)`` names the missing capability."""


# --------------------------------------------------------------------------
# operand expression helpers (over locals; 8-bit halves live inside the word)

def _reg16(n): return W16[n]


def _r8_read(n):
    r = W16[n & 3]
    return f"({r} & 0xFF)" if n < 4 else f"(({r} >> 8) & 0xFF)"


def _r8_write(n, val):
    r = W16[n & 3]
    if n < 4:
        return f"{r} = ({r} & 0xFF00) | (({val}) & 0xFF)"
    return f"{r} = ({r} & 0x00FF) | ((({val}) & 0xFF) << 8)"


_EA_EXPR = ("bx + si", "bx + di", "bp + si", "bp + di", "si", "di", None, "bx")


def _ea(inst):
    """(offset_expr, segment_local) for a memory ModRM operand."""
    seg = None
    for p in inst.prefixes:
        if p in (0x26, 0x2E, 0x36, 0x3E):
            seg = SEGS[(p >> 3) & 3]
    if inst.mod == 0 and inst.rm == 6:
        return f"0x{(inst.disp or 0) & 0xFFFF:X}", seg or "ds"
    base = _EA_EXPR[inst.rm]
    default = "ss" if inst.rm in (2, 3, 6) else "ds"
    off = f"({base})" if inst.mod == 0 else f"({base} + {inst.disp or 0})"
    return f"({off} & 0xFFFF)", seg or default


def _mem_read(inst, wide):
    off, seg = _ea(inst)
    return f"mem.{'rw' if wide else 'rb'}({seg}, {off})"


def _mem_write(inst, wide, val):
    off, seg = _ea(inst)
    return f"mem.{'ww' if wide else 'wb'}({seg}, {off}, ({val}) & 0x{'FFFF' if wide else 'FF'})"


def _rm_read(inst, wide):
    if inst.mod == 3:
        return _reg16(inst.rm) if wide else _r8_read(inst.rm)
    return _mem_read(inst, wide)


def _rm_write_lines(inst, wide, val):
    if inst.mod == 3:
        if wide:
            return [f"{_reg16(inst.rm)} = ({val}) & 0xFFFF"]
        return [_r8_write(inst.rm, val)]
    return [_mem_write(inst, wide, val)]


def _flags_arith(lines, kind, wide, a, b, r):
    """Exact flag updates for add/sub/cmp family (pure expressions).

    ``a``/``b`` are the operand expressions, ``r`` the raw (unmasked) result
    expression; caller assigns ``_t = {r}`` first and passes '_t'."""
    msb = 0x8000 if wide else 0x80
    mask = 0xFFFF if wide else 0xFF
    lines.append(f"zf = ({r} & 0x{mask:X}) == 0")
    lines.append(f"sf = ({r} & 0x{msb:X}) != 0")
    lines.append(f"pf = _PARITY[{r} & 0xFF]")
    if kind == "logic":
        # CF=OF=0; AF is PRESERVED (mirrors the interpreter's set_logic_flags
        # -- "leave AF, like the original"), so af is neither set nor masked.
        lines.append("cf = of = False")
        return
    lines.append(f"af = (({a}) ^ ({b}) ^ {r}) & 0x10 != 0")
    if kind == "add":
        lines.append(f"cf = {r} > 0x{mask:X}")
        lines.append(f"of = (~(({a}) ^ ({b})) & (({a}) ^ {r}) & 0x{msb:X}) != 0")
    else:  # sub / cmp
        lines.append(f"cf = {r} < 0")
        lines.append(f"of = ((({a}) ^ ({b})) & (({a}) ^ {r}) & 0x{msb:X}) != 0")
    return


def _flags_incdec(lines, wide, a, r, inc):
    msb = 0x8000 if wide else 0x80
    mask = 0xFFFF if wide else 0xFF
    lines.append(f"zf = ({r} & 0x{mask:X}) == 0")
    lines.append(f"sf = ({r} & 0x{msb:X}) != 0")
    lines.append(f"pf = _PARITY[{r} & 0xFF]")
    lines.append(f"af = (({a}) ^ 1 ^ {r}) & 0x10 != 0")
    if inc:
        lines.append(f"of = ({r} & 0x{mask:X}) == 0x{msb:X}")
    else:
        lines.append(f"of = (({a}) & 0x{mask:X}) == 0x{msb:X}")


# --------------------------------------------------------------------------

def _translate(inst, lines, flag_written):
    """Append pure-Python statements for one instruction; raise Refusal for
    anything outside the exact-semantics subset."""
    op = inst.op
    wide = (op & 1) == 1

    def flags(*names):
        flag_written.update(names)

    # mov -------------------------------------------------------------------
    if 0xB8 <= op <= 0xBF:
        lines.append(f"{_reg16(op & 7)} = 0x{(inst.imm or 0) & 0xFFFF:X}")
        return
    if 0xB0 <= op <= 0xB7:
        lines.append(_r8_write(op & 7, f"0x{(inst.imm or 0) & 0xFF:X}"))
        return
    if op in (0x88, 0x89):
        src = _reg16(inst.reg) if wide else _r8_read(inst.reg)
        lines.extend(_rm_write_lines(inst, wide, src))
        return
    if op in (0x8A, 0x8B):
        val = _rm_read(inst, wide)
        if wide:
            lines.append(f"{_reg16(inst.reg)} = {val}")
        else:
            lines.append(_r8_write(inst.reg, val))
        return
    if op in (0xC6, 0xC7) and inst.reg == 0:
        lines.extend(_rm_write_lines(inst, wide, f"0x{(inst.imm or 0):X}"))
        return
    if op in (0xA0, 0xA1):
        seg = "ds"
        for p in inst.prefixes:
            if p in (0x26, 0x2E, 0x36, 0x3E):
                seg = SEGS[(p >> 3) & 3]
        rd = f"mem.{'rw' if wide else 'rb'}({seg}, 0x{(inst.imm or 0) & 0xFFFF:X})"
        if wide:
            lines.append(f"ax = {rd}")
        else:
            lines.append(_r8_write(0, rd))
        return
    if op in (0xA2, 0xA3):
        seg = "ds"
        for p in inst.prefixes:
            if p in (0x26, 0x2E, 0x36, 0x3E):
                seg = SEGS[(p >> 3) & 3]
        val = "ax" if wide else "(ax & 0xFF)"
        lines.append(f"mem.{'ww' if wide else 'wb'}({seg}, "
                     f"0x{(inst.imm or 0) & 0xFFFF:X}, {val} & "
                     f"0x{'FFFF' if wide else 'FF'})")
        return
    if op == 0x8D:                                        # lea
        off, _seg = _ea(inst)
        lines.append(f"{_reg16(inst.reg)} = {off} & 0xFFFF")
        return

    # alu / cmp / test ---------------------------------------------------------
    if op <= 0x3D and (op & 7) <= 5 and (op & 0xC7) not in (0x06, 0x07, 0xC6, 0xC7):
        alu = _ALU[(op >> 3) & 7]
        form = op & 7
        if form in (4, 5):                                # acc, imm
            a = "ax" if wide else "(ax & 0xFF)"
            b = f"0x{(inst.imm or 0):X}"
            dst_rm = None; dst_acc = True
        elif form in (0, 1):                              # r/m, r
            a = _rm_read(inst, wide)
            b = _reg16(inst.reg) if wide else _r8_read(inst.reg)
            dst_rm = inst; dst_acc = False
        else:                                             # r, r/m
            a = _reg16(inst.reg) if wide else _r8_read(inst.reg)
            b = _rm_read(inst, wide)
            dst_rm = None; dst_acc = False
        _emit_alu(lines, flags, alu, wide, a, b, inst, form, dst_rm, dst_acc)
        return
    if op in (0x80, 0x81, 0x83):
        alu = _ALU[inst.reg]
        a = _rm_read(inst, wide)
        imm = (inst.imm or 0) & (0xFFFF if wide else 0xFF)
        if op == 0x83:                    # imm8 sign-extended to 16 bits
            imm = (inst.imm or 0) & 0xFF
            if imm & 0x80:
                imm |= 0xFF00
        _emit_alu(lines, flags, alu, wide, a, f"0x{imm:X}", inst, 0, inst, False)
        return
    if op in (0x84, 0x85):                                # test r/m, r
        a = _rm_read(inst, wide)
        b = _reg16(inst.reg) if wide else _r8_read(inst.reg)
        lines.append(f"_t = ({a}) & ({b})")
        _flags_arith(lines, "logic", wide, a, b, "_t")
        flags("cf", "pf", "zf", "sf", "of")
        return
    if op in (0xA8, 0xA9):                                # test acc, imm
        a = "ax" if wide else "(ax & 0xFF)"
        lines.append(f"_t = ({a}) & 0x{(inst.imm or 0):X}")
        _flags_arith(lines, "logic", wide, a, "0", "_t")
        flags("cf", "pf", "zf", "sf", "of")
        return
    if 0x40 <= op <= 0x4F:                                # inc/dec r16
        r = _reg16(op & 7)
        inc = op < 0x48
        lines.append(f"_a = {r}")
        lines.append(f"_t = _a {'+' if inc else '-'} 1")
        _flags_incdec(lines, True, "_a", "_t", inc)
        lines.append(f"{r} = _t & 0xFFFF")
        flags("pf", "af", "zf", "sf", "of")
        return
    if op in (0xFE, 0xFF) and inst.reg in (0, 1):         # inc/dec r/m
        inc = inst.reg == 0
        lines.append(f"_a = {_rm_read(inst, wide)}")
        lines.append(f"_t = _a {'+' if inc else '-'} 1")
        _flags_incdec(lines, wide, "_a", "_t", inc)
        lines.extend(_rm_write_lines(inst, wide, "_t"))
        flags("pf", "af", "zf", "sf", "of")
        return
    if op in (0xF6, 0xF7) and inst.reg == 0:              # test r/m, imm
        a = _rm_read(inst, wide)
        lines.append(f"_t = ({a}) & 0x{(inst.imm or 0):X}")
        _flags_arith(lines, "logic", wide, a, "0", "_t")
        flags("cf", "pf", "zf", "sf", "of")
        return
    if op in (0xF6, 0xF7) and inst.reg == 3:              # neg r/m
        lines.append(f"_a = {_rm_read(inst, wide)}")
        lines.append("_t = -_a")
        _flags_arith(lines, "sub", wide, "0", "_a", "_t")
        lines.extend(_rm_write_lines(inst, wide, "_t"))
        flags("cf", "pf", "af", "zf", "sf", "of")
        return
    if op in (0xF6, 0xF7) and inst.reg == 2:              # not r/m (no flags)
        lines.append(f"_t = ~({_rm_read(inst, wide)})")
        lines.extend(_rm_write_lines(inst, wide, "_t"))
        return
    if op == 0x90:                                        # nop (xchg ax,ax)
        return
    if op in (0xF6, 0xF7) and inst.reg == 4:              # mul (unsigned)
        lines.append(f"_b = {_rm_read(inst, wide)}")
        if wide:
            lines.append("_t = ax * _b")
            lines.append("ax = _t & 0xFFFF")
            lines.append("dx = (_t >> 16) & 0xFFFF")
            lines.append("cf = of = dx != 0")
        else:
            lines.append("_t = (ax & 0xFF) * _b")
            lines.append("ax = _t & 0xFFFF")
            lines.append("cf = of = _t > 0xFF")
        flags("cf", "of")     # ZF/SF/PF/AF untouched (mirrors the interpreter)
        return
    if op in (0xF6, 0xF7) and inst.reg == 5:              # imul (signed)
        lines.append(f"_b = {_rm_read(inst, wide)}")
        if wide:
            lines.append("_sa = ax - 0x10000 if ax & 0x8000 else ax")
            lines.append("_sb = _b - 0x10000 if _b & 0x8000 else _b")
            lines.append("_t = _sa * _sb")
            lines.append("ax = _t & 0xFFFF")
            lines.append("dx = (_t >> 16) & 0xFFFF")
            lines.append("cf = of = not (-32768 <= _t <= 32767)")
        else:
            lines.append("_sa = (ax & 0xFF) - 0x100 if ax & 0x80 else (ax & 0xFF)")
            lines.append("_sb = _b - 0x100 if _b & 0x80 else _b")
            lines.append("_t = _sa * _sb")
            lines.append("ax = _t & 0xFFFF")
            lines.append("cf = of = not (-128 <= _t <= 127)")
        flags("cf", "of")
        return
    if op in (0xF6, 0xF7) and inst.reg == 6:              # div (unsigned)
        # fail-loud on zero/overflow exactly like the interpreter (a crash is
        # a crash on both sides; no flags are written by div)
        lines.append(f"_b = {_rm_read(inst, wide)}")
        lines.append("if _b == 0:")
        lines.append("    raise ZeroDivisionError('div by zero (recovered)')")
        if wide:
            lines.append("_q, _r = divmod((dx << 16) | ax, _b)")
            lines.append("if _q > 0xFFFF:")
            lines.append("    raise OverflowError('16-bit div quotient overflow')")
            lines.append("ax = _q & 0xFFFF")
            lines.append("dx = _r & 0xFFFF")
        else:
            lines.append("_q, _r = divmod(ax & 0xFFFF, _b)")
            lines.append("if _q > 0xFF:")
            lines.append("    raise OverflowError('8-bit div quotient overflow')")
            lines.append("ax = ((_r & 0xFF) << 8) | (_q & 0xFF)")
        return
    if op in (0xF6, 0xF7) and inst.reg == 7:             # idiv (signed)
        # Mirrors the interpreter (cpu.py IDIV) BYTE for byte: truncate toward
        # zero via int(a/b) -- NOT Python's floor // -- so the remainder takes
        # the dividend's sign, exactly as the 8086 does. Same fail-loud on
        # zero/overflow (a crash is a crash on both sides).
        lines.append(f"_b = {_rm_read(inst, wide)}")
        lines.append("if _b == 0:")
        lines.append("    raise ZeroDivisionError('idiv by zero (recovered)')")
        if wide:
            lines.append("_d = (dx << 16) | ax")
            lines.append("if _d & 0x80000000: _d -= 0x100000000")
            lines.append("_v = _b - 0x10000 if _b & 0x8000 else _b")
            lines.append("_q = int(_d / _v); _r = _d - _q * _v")
            lines.append("if _q < -32768 or _q > 32767:")
            lines.append("    raise OverflowError('16-bit idiv quotient overflow')")
            lines.append("ax = _q & 0xFFFF")
            lines.append("dx = _r & 0xFFFF")
        else:
            lines.append("_d = ax - 0x10000 if ax & 0x8000 else ax")
            lines.append("_v = _b - 0x100 if _b & 0x80 else _b")
            lines.append("_q = int(_d / _v); _r = _d - _q * _v")
            lines.append("if _q < -128 or _q > 127:")
            lines.append("    raise OverflowError('8-bit idiv quotient overflow')")
            lines.append("ax = ((_r & 0xFF) << 8) | (_q & 0xFF)")
        return
    if op == 0x98:                                        # cbw
        lines.append("ax = (ax & 0xFF) | (0xFF00 if ax & 0x80 else 0)")
        return
    if op == 0x99:                                        # cwd
        lines.append("dx = 0xFFFF if ax & 0x8000 else 0")
        return
    # flag-control ops --------------------------------------------------------
    if op == 0xF8:                                        # clc
        lines.append("cf = False")
        flags("cf")
        return
    if op == 0xF9:                                        # stc
        lines.append("cf = True")
        flags("cf")
        return
    if op == 0xFC:                                        # cld
        lines.append("df = False")
        flags("df")
        return
    if op == 0xFD:                                        # std
        lines.append("df = True")
        flags("df")
        return
    if op == 0xFA:                                        # cli
        lines.append("intf = False")
        flags("intf")
        return
    if op == 0xFB:                                        # sti
        lines.append("intf = True")
        flags("intf")
        return

    # shifts / rotates (mirrors the interpreter's closed-form cpu.shift():
    # count &= 0x1F; count==0 touches NO flags; shl/shr/sar set CF+ZF/SF/PF
    # (AF untouched); rotates set CF only; OF only defined for count==1) ----
    if op in (0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3) and inst.reg in (0, 1, 2, 3, 4, 5, 7):
        grp = inst.reg
        bits = 16 if wide else 8
        mask = (1 << bits) - 1
        msb = 1 << (bits - 1)
        if op in (0xD0, 0xD1):
            lines.append("_n = 1")
        elif op in (0xD2, 0xD3):
            lines.append("_n = cx & 0x1F")
        else:
            lines.append(f"_n = 0x{(inst.imm or 0) & 0x1F:X}")
        lines.append(f"_a = {_rm_read(inst, wide)}")
        lines.append("if _n:")
        s = lines.append
        if grp == 4:                                      # shl/sal
            s(f"    if _n <= {bits}:")
            s(f"        cf = ((_a >> ({bits} - _n)) & 1) != 0")
            s(f"        _t = (_a << _n) & 0x{mask:X}")
            s("    else:")
            s("        cf = False; _t = 0")
        elif grp == 5:                                    # shr
            s(f"    if _n <= {bits}:")
            s("        cf = ((_a >> (_n - 1)) & 1) != 0")
            s("        _t = _a >> _n")
            s("    else:")
            s("        cf = False; _t = 0")
        elif grp == 7:                                    # sar
            s(f"    _sgn = (_a >> {bits - 1}) & 1")
            s(f"    if _n >= {bits}:")
            s(f"        _t = 0x{mask:X} if _sgn else 0")
            s("        cf = _sgn != 0")
            s("    else:")
            s("        cf = ((_a >> (_n - 1)) & 1) != 0")
            s(f"        _sv = _a - 0x{mask + 1:X} if _sgn else _a")
            s(f"        _t = (_sv >> _n) & 0x{mask:X}")
        elif grp == 0:                                    # rol
            s(f"    _r = _n % {bits}")
            s(f"    _t = ((_a << _r) | (_a >> ({bits} - _r))) & 0x{mask:X} if _r else _a")
            s("    cf = (_t & 1) != 0")
        elif grp == 1:                                    # ror
            s(f"    _r = _n % {bits}")
            s(f"    _t = ((_a >> _r) | (_a << ({bits} - _r))) & 0x{mask:X} if _r else _a")
            s(f"    cf = (_t & 0x{msb:X}) != 0")
        elif grp == 2:                                    # rcl: (bits+1)-wide
            width = bits + 1
            s(f"    _c = ((1 if cf else 0) << {bits}) | _a")
            s(f"    _r = _n % {width}")
            s("    if _r:")
            s(f"        _c = ((_c << _r) | (_c >> ({width} - _r))) & 0x{(1 << width) - 1:X}")
            s(f"    _t = _c & 0x{mask:X}")
            s(f"    cf = ((_c >> {bits}) & 1) != 0")
        else:                                             # rcr (grp 3)
            width = bits + 1
            s(f"    _c = ((1 if cf else 0) << {bits}) | _a")
            s(f"    _r = _n % {width}")
            s("    if _r:")
            s(f"        _c = ((_c >> _r) | (_c << ({width} - _r))) & 0x{(1 << width) - 1:X}")
            s(f"    _t = _c & 0x{mask:X}")
            s(f"    cf = ((_c >> {bits}) & 1) != 0")
        if grp in (4, 5, 7):
            s(f"    zf = _t == 0")
            s(f"    sf = (_t & 0x{msb:X}) != 0")
            s("    pf = _PARITY[_t & 0xFF]")
            s(f"    _fmask |= 0x{FCF | FZF | FSF | FPF:X}")
        else:
            s(f"    _fmask |= 0x{FCF:X}")
        s("    if _n == 1:")
        if grp == 4:
            s(f"        of = (((_a >> {bits - 1}) & 1) ^ ((_t >> {bits - 1}) & 1)) != 0")
        elif grp == 5:
            s(f"        of = ((_a >> {bits - 1}) & 1) != 0")
        elif grp == 7:
            s("        of = False")
        elif grp == 0:
            s(f"        of = (((_t >> {bits - 1}) & 1) ^ (_t & 1)) != 0")
        elif grp == 2:                                    # rcl: msb ^ new CF
            s(f"        of = (((_t >> {bits - 1}) & 1) ^ (1 if cf else 0)) != 0")
        else:                                             # ror / rcr
            s(f"        of = (((_t >> {bits - 1}) ^ (_t >> {bits - 2})) & 1) != 0")
        s(f"        _fmask |= 0x{FOF:X}")
        for w in _rm_write_lines(inst, wide, "_t"):
            s("    " + w)
        # NOTE: flags/fmask updates are emitted INSIDE the if-_n block above
        # (count 0 touches nothing), so flag_written stays empty here.
        return

    # string ops (lods/stos/movs, +- rep; direction from the df local -- the
    # gate refuses when DF is not defined in-body before use).  A REP string
    # instruction counts as ONE instruction in the oracle's virtual time, so
    # the static per-block cost of 1 is exact regardless of CX. -------------
    if op in (0xAC, 0xAD, 0xAA, 0xAB, 0xA4, 0xA5):
        w = 2 if wide else 1
        rep = any(pfx in (0xF2, 0xF3) for pfx in inst.prefixes)
        src_seg = "ds"
        for pfx in inst.prefixes:
            if pfx in (0x26, 0x2E, 0x36, 0x3E):
                src_seg = SEGS[(pfx >> 3) & 3]
        lines.append(f"_d = -{w} if df else {w}")
        body = []
        if op in (0xAC, 0xAD):                            # lods
            rd = f"mem.{'rw' if wide else 'rb'}({src_seg}, si)"
            if wide:
                body.append(f"ax = {rd}")
            else:
                body.append(_r8_write(0, rd))
            body.append("si = (si + _d) & 0xFFFF")
        elif op in (0xAA, 0xAB):                          # stos
            val = "ax" if wide else "(ax & 0xFF)"
            body.append(f"mem.{'ww' if wide else 'wb'}(es, di, {val})")
            body.append("di = (di + _d) & 0xFFFF")
        else:                                             # movs
            rd = f"mem.{'rw' if wide else 'rb'}({src_seg}, si)"
            body.append(f"mem.{'ww' if wide else 'wb'}(es, di, {rd})")
            body.append("si = (si + _d) & 0xFFFF")
            body.append("di = (di + _d) & 0xFFFF")
        if rep:
            lines.append("while cx:")
            for ln in body:
                lines.append("    " + ln)
            lines.append("    cx = (cx - 1) & 0xFFFF")
        else:
            lines.extend(body)
        return

    # stack ops (tier 2) -----------------------------------------------------
    # LITERAL SS:SP memory traffic: the pushed bytes are observable state (the
    # boundary differential hashes full memory, and stack residue below SP
    # persists after return), so push/pop write/read the real guest stack.
    # ``sp`` is a plain integer local -- an address base, never data (the gate
    # refuses sp-as-data); balance is verified statically, so sp never appears
    # in the outputs and the adapter's RET ABI is unchanged.
    if op == 0xC8:                                        # enter imm16, 0
        # level 0 IS `push bp; mov bp,sp; sub sp,size` -- emitted as those three,
        # because that is what it is (nesting level > 0 refuses in the ABI pass).
        size = inst.raw[-3] | (inst.raw[-2] << 8)
        lines.append("sp = (sp - 2) & 0xFFFF")
        lines.append("mem.ww(ss, sp, bp)")
        lines.append("bp = sp")
        if size:
            lines.append(f"sp = (sp - {size}) & 0xFFFF")
        return
    if op == 0xC9:                                        # leave
        lines.append("sp = bp")
        lines.append("bp = mem.rw(ss, sp)")
        lines.append("sp = (sp + 2) & 0xFFFF")
        return
    if op == 0x60:                                        # pusha
        # push ax,cx,dx,bx, the ORIGINAL sp, bp,si,di -- sp is captured before
        # any push (8086 semantics), so snapshot it first.
        lines.append("_sp0 = sp")
        for r in ("ax", "cx", "dx", "bx"):
            lines.append("sp = (sp - 2) & 0xFFFF")
            lines.append(f"mem.ww(ss, sp, {r})")
        lines.append("sp = (sp - 2) & 0xFFFF")
        lines.append("mem.ww(ss, sp, _sp0)")
        for r in ("bp", "si", "di"):
            lines.append("sp = (sp - 2) & 0xFFFF")
            lines.append(f"mem.ww(ss, sp, {r})")
        return
    if op == 0x61:                                        # popa
        # pop di,si,bp, DISCARD the stacked sp, then bx,dx,cx,ax.
        for r in ("di", "si", "bp"):
            lines.append(f"{r} = mem.rw(ss, sp)")
            lines.append("sp = (sp + 2) & 0xFFFF")
        lines.append("sp = (sp + 2) & 0xFFFF")            # skip the saved sp
        for r in ("bx", "dx", "cx", "ax"):
            lines.append(f"{r} = mem.rw(ss, sp)")
            lines.append("sp = (sp + 2) & 0xFFFF")
        return
    if 0x50 <= op <= 0x57:                                # push r16
        lines.append("sp = (sp - 2) & 0xFFFF")
        lines.append(f"mem.ww(ss, sp, {_reg16(op & 7)})")
        return
    if 0x58 <= op <= 0x5F:                                # pop r16
        lines.append(f"{_reg16(op & 7)} = mem.rw(ss, sp)")
        lines.append("sp = (sp + 2) & 0xFFFF")
        return
    if op in (0x06, 0x0E, 0x16, 0x1E):                    # push seg
        lines.append("sp = (sp - 2) & 0xFFFF")
        lines.append(f"mem.ww(ss, sp, {SEGS[(op >> 3) & 3]})")
        return
    if op in (0x07, 0x1F):                                # pop es / pop ds
        lines.append(f"{SEGS[(op >> 3) & 3]} = mem.rw(ss, sp)")
        lines.append("sp = (sp + 2) & 0xFFFF")
        return
    if op == 0x8E and (inst.reg & 3) in (0, 3):           # mov es/ds, r/m
        lines.append(f"{SEGS[inst.reg & 3]} = {_rm_read(inst, True)}")
        return
    if op == 0x8C:                                        # mov r/m, sreg
        lines.extend(_rm_write_lines(inst, True, SEGS[inst.reg & 3]))
        return
    if op in (0x68, 0x6A):                                # push imm (186)
        imm = (inst.imm or 0) & 0xFFFF
        if op == 0x6A and imm & 0x80:                     # imm8 sign-extends
            imm |= 0xFF00
        lines.append("sp = (sp - 2) & 0xFFFF")
        lines.append(f"mem.ww(ss, sp, 0x{imm & 0xFFFF:X})")
        return
    if op == 0xFF and inst.reg == 6:                      # push r/m16
        lines.append(f"_t = {_rm_read(inst, True)}")
        lines.append("sp = (sp - 2) & 0xFFFF")
        lines.append("mem.ww(ss, sp, _t)")
        return
    if op == 0x8F and inst.reg == 0:                      # pop r/m16
        lines.append("_t = mem.rw(ss, sp)")
        lines.append("sp = (sp + 2) & 0xFFFF")
        lines.extend(_rm_write_lines(inst, True, "_t"))
        return
    # pushf / popf (tier 12: the FLAGS word as literal stack data) ------------
    if op == 0x9C:                                        # pushf
        fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                        for f in ("cf", "pf", "af", "zf", "sf", "of",
                                  "df", "intf"))
        lines.append(f"_pfw = (_flags_in & ~_fmask) | (({fw}) & _fmask)")
        lines.append("sp = (sp - 2) & 0xFFFF")
        lines.append("mem.ww(ss, sp, _pfw)")
        return
    if op == 0x9D:                                        # popf
        lines.append("_pfw = mem.rw(ss, sp) | 0x0002")
        lines.append("sp = (sp + 2) & 0xFFFF")
        for fname, fbit in _FLAG_BITS.items():
            lines.append(f"{fname} = (_pfw & 0x{fbit:X}) != 0")
        flags("cf", "pf", "af", "zf", "sf", "of", "df", "intf")
        return
    # xchg ---------------------------------------------------------------------
    if 0x91 <= op <= 0x97:                                # xchg ax, r16
        r = _reg16(op & 7)
        lines.append(f"_t = ax; ax = {r}; {r} = _t")
        return
    if op in (0x86, 0x87):                                # xchg r, r/m
        if wide:
            src = _reg16(inst.reg)
            lines.append(f"_t = {_rm_read(inst, True)}")
            lines.extend(_rm_write_lines(inst, True, src))
            lines.append(f"{src} = _t & 0xFFFF")
        else:
            # read both halves BEFORE either write; the half-register writes
            # then preserve the CURRENT sibling half (aliased xchg al,ah is
            # exact this way).
            lines.append(f"_t = {_rm_read(inst, False)}")
            lines.append(f"_u = {_r8_read(inst.reg)}")
            lines.extend(_rm_write_lines(inst, False, "_u"))
            lines.append(_r8_write(inst.reg, "_t"))
        return
    # les / lds (load far pointer from memory) ----------------------------------
    if op in (0xC4, 0xC5) and inst.mod != 3:
        off, seg = _ea(inst)
        lines.append(f"_o = {off}")
        lines.append(f"{_reg16(inst.reg)} = mem.rw({seg}, _o)")
        lines.append(f"{'es' if op == 0xC4 else 'ds'} = mem.rw({seg}, (_o + 2) & 0xFFFF)")
        return
    # xlat -----------------------------------------------------------------------
    if op == 0xD7:
        seg = "ds"
        for p in inst.prefixes:
            if p in (0x26, 0x2E, 0x36, 0x3E):
                seg = SEGS[(p >> 3) & 3]
        lines.append(_r8_write(0, f"mem.rb({seg}, (bx + (ax & 0xFF)) & 0xFFFF)"))
        return
    # cmps / scas (compare string ops; repe/repne terminate on ZF; a REP
    # instruction is ONE instruction of virtual time -- tier-5 cost rule).
    # Flags ride a runtime _fmask update inside the body: a rep with cx=0
    # executes zero iterations and touches NOTHING. ---------------------------
    if op in (0xA6, 0xA7, 0xAE, 0xAF):
        w = 2 if wide else 1
        rep = None
        for pfx in inst.prefixes:
            if pfx in (0xF2, 0xF3):
                rep = pfx
        src_seg = "ds"
        for pfx in inst.prefixes:
            if pfx in (0x26, 0x2E, 0x36, 0x3E):
                src_seg = SEGS[(pfx >> 3) & 3]
        lines.append(f"_d = -{w} if df else {w}")
        body: list[str] = []
        if op in (0xA6, 0xA7):                            # cmps: [si] - es:[di]
            body.append(f"_a = mem.{'rw' if wide else 'rb'}({src_seg}, si)")
            body.append(f"_b = mem.{'rw' if wide else 'rb'}(es, di)")
        else:                                             # scas: acc - es:[di]
            body.append(f"_a = {'ax' if wide else '(ax & 0xFF)'}")
            body.append(f"_b = mem.{'rw' if wide else 'rb'}(es, di)")
        body.append("_t = _a - _b")
        _flags_arith(body, "sub", wide, "_a", "_b", "_t")
        body.append(f"_fmask |= 0x{FCF | FPF | FAF | FZF | FSF | FOF:X}")
        if op in (0xA6, 0xA7):
            body.append("si = (si + _d) & 0xFFFF")
        body.append("di = (di + _d) & 0xFFFF")
        if rep:
            lines.append("while cx:")
            for ln in body:
                lines.append("    " + ln)
            lines.append("    cx = (cx - 1) & 0xFFFF")
            if rep == 0xF3:                               # repe: stop when NZ
                lines.append("    if not zf:")
            else:                                         # repne: stop when Z
                lines.append("    if zf:")
            lines.append("        break")
        else:
            lines.extend(body)
        return
    raise Refusal(f"emitter-unsupported-op-{op:02X}"
                  + (f"-grp{inst.reg}" if inst.modrm is not None else ""))


def _emit_alu(lines, flags, alu, wide, a, b, inst, form, dst_rm, dst_acc):
    mask = 0xFFFF if wide else 0xFF
    lines.append(f"_a = {a}")
    lines.append(f"_b = {b}")
    if alu in ("add",):
        lines.append("_t = _a + _b")
        _flags_arith(lines, "add", wide, "_a", "_b", "_t")
    elif alu == "adc":
        # carry-inclusive result: the existing add formulas (CF > mask,
        # xor-trick AF, sign-overlap OF) stay interpreter-exact with the
        # incoming carry folded into _t (set_add_flags keeps a/b original).
        lines.append("_t = _a + _b + (1 if cf else 0)")
        _flags_arith(lines, "add", wide, "_a", "_b", "_t")
    elif alu == "sbb":
        lines.append("_t = _a - _b - (1 if cf else 0)")
        _flags_arith(lines, "sub", wide, "_a", "_b", "_t")
    elif alu in ("sub", "cmp"):
        lines.append("_t = _a - _b")
        _flags_arith(lines, "sub", wide, "_a", "_b", "_t")
    else:                                                 # and / or / xor
        pyop = {"and": "&", "or": "|", "xor": "^"}[alu]
        lines.append(f"_t = _a {pyop} _b")
        _flags_arith(lines, "logic", wide, "_a", "_b", "_t")
    if alu in ("and", "or", "xor"):
        flags("cf", "pf", "zf", "sf", "of")   # AF preserved on logic ops
    else:
        flags("cf", "pf", "af", "zf", "sf", "of")
    if alu == "cmp":
        return
    if dst_rm is not None:
        lines.extend(_rm_write_lines(dst_rm, wide, "_t"))
    elif dst_acc:
        if wide:
            lines.append("ax = _t & 0xFFFF")
        else:
            lines.append(_r8_write(0, "_t"))
    else:
        if wide:
            lines.append(f"{_reg16(inst.reg)} = _t & 0xFFFF")
        else:
            lines.append(_r8_write(inst.reg, "_t"))


# --------------------------------------------------------------------------

@dataclass
class CalleeContract:
    """The composed contract of an already-promoted callee, as the emitter
    needs it at a caller's call site."""
    name: str                       # recovered function name (func_1010_xxxx)
    inputs: tuple                   # keyword inputs (incl. sp/ss when needed)
    outputs: tuple                  # register outputs in the result dict
    exit_flags: frozenset           # flags DEFINITELY defined on every exit
    needs_plat: bool = False        # the callee takes the platform interface
    ret_kind: str = "near"          # "near" (ret) | "far" (retf) exit ABI
    df_livein: bool = False         # takes the caller's DF (_df compat input)
    sp_delta: int | None = 0        # net caller-depth effect (stack args /
                                    # extra pops; None = varies); excludes
                                    # the ret-addr pair
    ret_pop: int = 0                # ret N immediate (uniform per function)
    sp_output: bool = False         # body is unbalanced: sp is a real output
    sp_deltas: tuple = (0,)         # ALL possible exit-depth effects -- the
                                    # caller's depth-set tracker forks on
                                    # each (runtime sp flows via outputs)
    flags_livein: bool = False      # takes the caller's full FLAGS word
                                    # (_flags_in compat input, tier 12)
    parks: bool = False             # contains a boundary head (or composes a
                                    # callee that does): STANDALONE-ONLY --
                                    # its adapter must not enter the VMless
                                    # demo graph (a park unwind would lose
                                    # composed caller locals)


@dataclass
class PromotionSpec:
    """Everything check_promotable proves about one promotable function."""
    abi: object
    exit_flags: frozenset
    needs_plat: bool
    ret_kind: str
    df_livein: bool
    sp_delta: int | None = 0
    ret_pop: int = 0
    sp_output: bool = False
    sp_deltas: tuple = (0,)
    flags_livein: bool = False
    parks: bool = False


def check_promotable(scan, *, excluded_addrs=frozenset(), callees=None,
                     far_callees=None, dispatch_addrs=frozenset(),
                     boundary_addrs=frozenset()):
    """The strict promotion gate.  Returns a :class:`PromotionSpec` or
    raises :class:`Refusal` with the census reason.

    ``callees`` (call-ABI composition, tier 4): maps a direct near-call
    target ip to its :class:`CalleeContract`.  ``far_callees`` (tier 9) is
    the same map for direct FAR calls, keyed by the static (seg, off)
    target.  A CALL/CALL FAR whose target is present composes; any other
    call still refuses.

    ``dispatch_addrs`` (tier 9): recorded dynamic-arrival addresses in this
    segment.  One that falls inside this scan becomes an ALTERNATE ENTRY of
    the recovered function (a generated ``_entry_ip`` compatibility channel;
    the dispatcher/installer enters the shared blocks there) -- the contract
    widens to the full register bundle, flags/stack analyses seed the entry
    conditions of a dynamic arrival (no flags defined, depth 0)."""
    callees = callees or {}
    far_callees = far_callees or {}
    # a dispatch entry inside the scan is FORCED as a block leader by the
    # emitter (same rule as the VMless emitter) -- it need only be a decoded
    # instruction start, which membership in scan.insts guarantees.
    alt_entries = frozenset(dispatch_addrs) & frozenset(scan.insts) \
        - frozenset({scan.entry})
    callee_effects = {ip: (frozenset(c.inputs) - frozenset({"sp", "ss"}),
                           frozenset(c.outputs))
                      for ip, c in callees.items()}
    far_effects = {tgt: (frozenset(c.inputs) - frozenset({"sp", "ss"}),
                         frozenset(c.outputs))
                   for tgt, c in far_callees.items()}
    abi = abi_scan(scan, callee_effects=callee_effects,
                   far_callee_effects=far_effects)
    heads = frozenset(boundary_addrs) & frozenset(scan.insts)
    for h in heads:
        if scan.insts[h].kind != SEQ:
            raise Refusal("boundary-head-on-transfer")
    if alt_entries or heads:
        # dynamic arrival / boundary observer: liveness is unknown (the
        # observer passes the FULL live bundle) -- the honest conservative
        # contract takes the whole register file.
        abi.inputs = abi.inputs | frozenset(W16) | frozenset({"ds", "es", "ss"})
    for cap in abi.refusals:
        if cap == "call-abi-composition":
            missing = [i.ip for i in scan.insts.values()
                       if (i.kind == CALL and (i.target is None or
                                               i.target not in callees))
                       or (i.kind == CALL_FAR and
                           (i.far_target is None or
                            i.far_target not in far_callees))]
            if not missing:
                continue            # every call target composes
            raise Refusal("contains-call")
        raise Refusal({"int-platform-effect": "contains-interrupt",
                       "indirect-or-far-transfer": "indirect-control-flow",
                       "port-io-platform-effect": "port-io",
                       }.get(cap, cap))
    # return-kind discipline: one uniform exit ABI per function (the adapter
    # emits exactly one RET variant); a near call must compose a near callee
    # and a far call a far callee -- the machine frame sizes differ.  An
    # IRET exit (tier 12) makes the function an interrupt handler: invoked
    # only through the vector-dispatch path, never near/far-composed.
    _KIND = {RET: "near", RETF: "far", IRET: "iret"}
    ret_kinds = {_KIND[i.kind] for i in scan.insts.values()
                 if i.kind in (RET, RETF, IRET)}
    # an ISR-chain tail exits through the chained handler's iret: the
    # function IS an interrupt handler (tier 13).
    if any(_is_isr_chain(i) for i in scan.insts.values()):
        ret_kinds.add("iret")
    if len(ret_kinds) > 1:
        raise Refusal("mixed-return-kinds")
    ret_kind = next(iter(ret_kinds), "near")
    for i in scan.insts.values():
        if i.kind == CALL and i.target in callees \
                and callees[i.target].ret_kind not in ("near", "far"):
            raise Refusal("ret-kind-mismatch (near call to iret callee)")
        # a near CALL into a retf callee is the MSC push-cs idiom: the
        # caller pushed CS explicitly, the callee's retf pops both words
        # (composition pops 4 and the depth tracker sees the extra word).
        if i.kind == CALL_FAR and i.far_target in far_callees \
                and far_callees[i.far_target].ret_kind != "far":
            raise Refusal("ret-kind-mismatch (far call to near callee)")
    for i in scan.insts.values():
        e = register_effects(i)
        if i.kind in (RET, RETF, IRET):
            continue    # the RET ABI (incl. a uniform ret N / the iret
                        # frame) is the adapter's job; the depth checker
                        # gates the rest
        if e.stack_delta is None and not e.frame_restore:
            raise Refusal("unresolved-stack-effect")
        if e.writes & frozenset({"cs", "ss"}):
            raise Refusal("cs-or-ss-mutation")
        if "sp" in (e.reads | e.writes) and not _is_stack_family(i):
            raise Refusal("sp-as-data")
    _check_frame_pointer(scan)
    sp_delta, ret_pop, sp_output, sp_deltas = _check_stack_depths(
        scan, alt_entries, callees, far_callees)
    if sp_output and any(_is_dyn(i) and i.kind == JMP_IND
                         for i in scan.insts.values()):
        # a tail dispatch assumes the caller frame is next on the stack;
        # an unbalanced body would hand the callee a shifted frame.
        raise Refusal("tail-dispatch-with-unbalanced-stack")
    if any(i.ip in excluded_addrs for i in scan.insts.values()):
        raise Refusal("boundary-or-dispatch-address")
    # flag live-ins: DF alone rides the _df compat input (tier 9); ANY other
    # flag read while undefined (jcc, cmc, rcl/rcr, adc/sbb) makes the whole
    # FLAGS word the _flags_in compat input, and every flag local
    # initializes from it -- machine-correct caller values (tier 13).
    exit_flags, df_livein, fl_needed = _check_flag_liveins(
        scan, callees, far_callees, alt_entries)
    needs_plat = _func_needs_plat(scan, callees, far_callees)
    # the full FLAGS word is ALSO a compat input when a game-INT frame or
    # pushf writes it here (untracked bits ride the caller word) or a
    # composed callee needs it -- transitive, like _df (tier 12).
    # A near-DYNAMIC site needs it for the same reason a vectored one does
    # (tier 14): the target is unknown at emit time, so the site must be able
    # to hand the callee a full FLAGS word -- reconstructing it needs the
    # untracked bits, which only _flags_in carries.  Without this a
    # flags-livein function could not be a dispatch target at all.
    flags_livein = fl_needed or bool(heads) \
        or any(_is_dyn(i) or _is_game_int(i) or _is_isr_chain(i)
               or i.op == 0x9C
               for i in scan.insts.values()) \
        or any(i.kind == CALL and i.target in callees
               and callees[i.target].flags_livein
               for i in scan.insts.values()) \
        or any(i.kind == CALL_FAR and i.far_target in far_callees
               and far_callees[i.far_target].flags_livein
               for i in scan.insts.values())
    # a parking function (or one composing a parking callee) is
    # STANDALONE-ONLY: the demo graph keeps its original lifted module.
    parks = bool(heads) \
        or any(i.kind == CALL and i.target in callees
               and callees[i.target].parks
               for i in scan.insts.values()) \
        or any(i.kind == CALL_FAR and i.far_target in far_callees
               and far_callees[i.far_target].parks
               for i in scan.insts.values())
    return PromotionSpec(abi=abi, exit_flags=exit_flags,
                         needs_plat=needs_plat or bool(heads),
                         ret_kind=ret_kind,
                         df_livein=df_livein, sp_delta=sp_delta,
                         ret_pop=ret_pop, sp_output=sp_output,
                         sp_deltas=sp_deltas, flags_livein=flags_livein,
                         parks=parks)


def _func_needs_plat(scan, callees, far_callees=None) -> bool:
    """A function needs the platform interface if it does port I/O or
    interrupts directly, or composes a call to a callee that needs it."""
    from .cpuless import register_effects
    for i in scan.insts.values():
        e = register_effects(i)
        if e.port_io or e.int_effect is not None:
            return True
        if _is_dyn(i) or _is_game_int(i) or _is_isr_chain(i):
            return True     # a dynamic/vectored callee may need the platform
        if (i.kind == CALL and i.target in (callees or {})
                and callees[i.target].needs_plat):
            return True
        if (i.kind == CALL_FAR and i.far_target in (far_callees or {})
                and far_callees[i.far_target].needs_plat):
            return True
    return False


def _check_frame_pointer(scan) -> None:
    """``leave`` restores sp FROM bp, so its depth effect is exact only while bp
    still holds the frame base its ``enter`` put there.

    That is the ONE assumption behind treating leave as a depth reset, so it is
    checked rather than assumed: a leave with no enter has no frame base to
    return to, and anything else writing bp means the value leave reads is not
    the one enter wrote. Both refuse -- the alternative is a stack depth that is
    confidently wrong, which is worse than a refusal by exactly the margin that
    makes refusals worth having.

    A balanced ``push bp`` / ``pop bp`` is fine and NOT a clobber: the stack
    depth tracker already proves the pair balances, and a ``pop bp`` that
    restores the frame base is exactly the value ``leave`` needs. Only a
    NON-stack write to bp -- ``mov bp,sp`` re-pointing the frame, ``mov
    bp,<data>``, ``les bp,...`` -- breaks the leave-as-reset invariant, and that
    is what this catches. (A push/pop pair with bp used as scratch BETWEEN them
    would need dominance analysis to prove the restore precedes the leave; that
    still refuses, via the mov-bp write in the middle.)
    """
    leaves = [i for i in scan.insts.values() if i.op == 0xC9]
    if not leaves:
        return
    if not any(i.op == 0xC8 for i in scan.insts.values()):
        raise Refusal("leave-without-enter")
    for i in scan.insts.values():
        # Only a SEQUENTIAL instruction can clobber bp as data. A transfer's
        # register writes are a CONSERVATIVE dataflow bundle, not a literal
        # assignment: register_effects models a DOS/BIOS INT and a CALL as
        # writing the whole bundle (bp included) for input/output inference, but
        # neither destroys the frame pointer -- the callee restores it by ABI,
        # the INT preserves it, and each is gated on its own terms elsewhere.
        # Counting those as clobbers mislabels every INT/call function as
        # "frame-pointer-clobbered" instead of its true reason.
        if i.kind != SEQ or i.op in (0xC8, 0xC9) or _is_stack_family(i):
            continue                       # transfers + frame ops + push/pop bp
        if "bp" in register_effects(i).writes:
            raise Refusal("frame-pointer-clobbered")


_OUTPUT_KEEP = frozenset(W16) | frozenset({"ds", "es"})


def _output_set(abi, sp_output: bool) -> list[str]:
    """The registers the adapter writes back to the CPU carrier.

    DEAD-REGISTER-OUTPUT PRUNING (§ dead_register_outputs.md), applied
    IDENTICALLY here for the recovered function's return dict and its adapter's
    writeback -- the two MUST agree, so they share this one definition rather
    than repeat the expression and risk drift.

    Keep a register only if it is live at some clean return exit (``abi.exit_live``).
    ``exit_live is None`` means a tail transfer governs the live-out, so retain
    everything. ``sp`` is never subject to the liveness prune -- it is governed
    by ``sp_output`` (an unbalanced/varying stack makes it a real output).

    Under the current conservative exit seed this removes NOTHING: abi_scan
    seeds every may-written register live at exit so the whole-register-file
    boundary differential matches, hence ``exit_live == outputs`` for every
    clean-return function. The prune is here as a sound, regeneratable mechanism
    that a future inter-procedural exit liveness can make bite; today it proves
    the emitted output set is already minimal.
    """
    outs = abi.outputs & _OUTPUT_KEEP
    if abi.exit_live is not None:
        # sp survives the liveness filter (exit_live never carries it); the
        # sp_output rule below is its sole gate.
        outs = outs & (abi.exit_live | frozenset({"sp"}))
    return sorted(outs - (frozenset() if sp_output else frozenset({"sp"})))


def output_prune_removed(abi, sp_output: bool) -> list[str]:
    """Registers the liveness prune dropped from this function's output set
    (for the aggregate report). Empty under the current conservative seed."""
    full = sorted((abi.outputs & _OUTPUT_KEEP)
                  - (frozenset() if sp_output else frozenset({"sp"})))
    return sorted(set(full) - set(_output_set(abi, sp_output)))


def _is_stack_family(i) -> bool:
    """Instructions whose sp use IS the stack discipline (allowed), as opposed
    to sp used as general data (refused)."""
    op = i.op
    return (0x50 <= op <= 0x5F
            or op in (0xC8, 0xC9)                  # enter/leave: the frame ops
            or op in (0x60, 0x61)                  # pusha/popa
            or op in (0x06, 0x0E, 0x16, 0x1E)      # push seg
            or op in (0x9C, 0x9D)                  # pushf/popf (tier 12)
            or op in (0x07, 0x17, 0x1F)            # pop seg (pop ss refuses
                                                    # earlier via ss-mutation)
            or op in (0x68, 0x6A)
            or _is_sp_capture(i)                   # mov r16, sp: frame base into
                                                    # a GP reg (bp/bx frame ptr)
            or (op in (0x81, 0x83) and i.mod == 3 and i.rm == 4
                and i.reg in (0, 5))               # add/sub sp, imm: cdecl
                                                    # cleanup / frameless alloc
            or (op == 0xFF and i.reg == 6) or (op == 0x8F and i.reg == 0)
            or i.kind in (CALL, CALL_FAR)  # composed call: ret-addr push/pop
            or _is_dyn(i)                  # recovered dispatch: balanced
            or _is_game_int(i)             # vector dispatch: frame symmetric
            or _is_isr_chain(i)            # chain tail: frame is the callee's
            or i.kind in (RET, RETF, IRET))


def _is_sp_capture(i) -> bool:
    """`mov r16, sp` -- the current stack pointer READ into a general register.

    The frameless Borland/Turbo idiom for a routine that reads its stack
    arguments: `mov bx,sp` (then `[bx+k]`), or the hand-rolled prologue's
    `mov bp,sp` (paired with a preceding `push bp`).  sp is a SOURCE only, never
    written, so the depth is untouched and this is stack discipline, not
    sp-as-data.  The dependent `[reg+k]` reads are ordinary memory operands the
    emitter already handles; only the sp READ needed unblocking.

    Both encodings: 8B (mov r16, r/m16 -- sp is the r/m) and 89 (mov r/m16, r16
    -- sp is the reg).  A write to sp (`mov sp, r16`) is deliberately NOT here:
    that is a frame RESTORE, handled only when its establishing `mov bp,sp` is
    proven un-clobbered -- a separate, heavier analysis.
    """
    return (i.mod == 3
            and ((i.op == 0x8B and i.rm == 4)       # mov r16, sp
                 or (i.op == 0x89 and i.reg == 4)))  # mov r/m16, sp


def _is_dyn(i) -> bool:
    """A NEAR indirect call/jmp -- emitted as runtime-resolved recovered
    dispatch (tier 9).  Far variants stay refusals (isr-chain tier)."""
    return i.kind in (CALL_IND, JMP_IND) and i.modrm is not None \
        and ((i.modrm >> 3) & 7) in (2, 4)


def _is_game_int(i) -> bool:
    """A game-vectored INT (tier 12): dispatched through the runtime IVT to
    a recovered IRET-contract handler -- a call into game code, never a
    platform effect.  int3 (a debug trap in dead paths) rides the same
    mechanism: its runtime vector is the promoted BIOS dummy-IRET stub."""
    return i.kind == INT and i.int_no in (3, 0x60, 0x61)


def _is_isr_chain(i) -> bool:
    """A far indirect jmp through a memory vector (tier 13): the ISR chain
    tail -- dispatched through HANDLERS; the chained handler's iret ends
    THIS function's interrupt (the function's exit kind is iret)."""
    return i.kind == JMP_IND and i.modrm is not None \
        and ((i.modrm >> 3) & 7) == 5


def _check_stack_depths(scan, alt_entries=frozenset(), callees=None,
                        far_callees=None) -> tuple:
    """Static stack-discipline verification (tiers 2 + 11): every address has
    ONE net push depth (bytes) from entry, consistent at every join.

    The depth MAY go negative and a RET may happen at ANY depth (tier 11,
    stack-args ABI): pops below the entry depth read the caller's frame --
    literal, byte-exact memory the recovered body computes over -- and an
    unbalanced exit simply makes ``sp`` a real contract OUTPUT (the adapter
    pops the return address at the runtime sp).  ``ret N`` is legal when the
    immediate is UNIFORM across exits (the adapter adds it after the pop).

    A dynamic tail dispatch (near jmp_ind) must still run at depth 0 -- the
    dispatched callee's return uses the caller's own frame.  ``alt_entries``
    (dynamic arrivals) seed additional depth-0 roots.  A composed call to a
    callee with a static sp_delta shifts the depth by it; a callee whose
    exit depth VARIES refuses (callee-sp-escape).

    Depth is tracked as a SET per address: conditional pops before a join
    (path-dependent depth) are legal -- the recovered body's runtime ``sp``
    local is correct on whichever path executes -- they simply make ``sp``
    an output and the function non-composable (sp_delta None).  A depth set
    that would grow past the cap (correlated caller/callee branches a
    per-address set cannot see, e.g. varying-delta callees in a loop)
    widens to UNKNOWN (None) instead of refusing: every downstream depth is
    unknown, sp is an output, and the exit-delta contract is None -- the
    runtime sp stays exact throughout.

    Returns (sp_delta, ret_pop, sp_output, sp_deltas):
      sp_delta  -- uniform exit depth minus ret_pop (None when exits
                   disagree or are unknown);
      ret_pop   -- the uniform ret N immediate (0 if plain ret);
      sp_output -- exits unbalanced/varying/unknown (sp joins the outputs);
      sp_deltas -- every possible exit delta, or None when unknown."""
    callees = callees or {}
    far_callees = far_callees or {}
    depths: dict[int, set[int]] = {scan.entry: {0}}
    work = [(scan.entry, 0)]
    for a in alt_entries:
        depths.setdefault(a, set()).add(0)
        work.append((a, 0))
    exit_depths: set[int] = set()
    ret_pops: set[int] = set()
    while work:
        ip, d = work.pop()
        i = scan.insts[ip]
        e = register_effects(i)
        if i.kind in (RET, RETF, IRET):
            # ANY exit depth is legal: an unbalanced/varying exit makes sp a
            # runtime output, and both the adapter's frame pops and a
            # composed INT site's frame read use the RETURNED sp -- exact
            # regardless of the static picture (alt-entry seeds make the
            # static depth an artifact for mid-ISR fragments).
            if i.kind == RETF and (i.imm or 0):
                raise Refusal("ret-n-stack-args (retf N needs far variant)")
            exit_depths.add(d)
            ret_pops.add((i.imm or 0) if i.kind == RET else 0)
            continue
        if i.kind == JMP_IND:
            if _is_isr_chain(i):
                # chain tail: the chained handler returns balanced and the
                # invoking site pops the frame at the merged runtime sp --
                # no static depth requirement.
                exit_depths.add(d if d is not None else 0)
                ret_pops.add(0)
                continue
            if d not in (0, None):  # unknown cannot prove the tail rule
                raise Refusal("tail-dispatch-at-nonzero-depth")
            exit_depths.add(0)
            ret_pops.add(0)
            continue
        # a composed callee may shift the caller's depth by ANY of its exit
        # deltas: the tracker FORKS (the runtime sp flows back through the
        # callee's sp output, so every fork is a real executable path).
        # UNKNOWN (None) absorbs: unknown in -> unknown out.
        if e.frame_restore:
            # `leave`: sp := bp, and bp is the frame base its `enter` set (that
            # is checked, see _check_frame_pointer), so the depth returns to the
            # function's entry depth -- 0. A RESET, not arithmetic, and exact
            # even when d is UNKNOWN: whatever the body did to the depth, leave
            # discards it. That is what makes an enter/leave function composable
            # instead of an sp-output that blocks every caller.
            afters = [0]
        elif d is None:
            afters = [None]
        else:
            afters = [d - (e.stack_delta or 0)]
            if i.kind == CALL and i.target in callees:
                c = callees[i.target]
                # push-cs idiom: a retf callee behind a NEAR call also pops
                # the caller's explicitly pushed CS word
                extra = -2 if c.ret_kind == "far" else 0
                afters = [None] if c.sp_deltas is None \
                    else [d + x + extra for x in c.sp_deltas]
            if i.kind == CALL_FAR and i.far_target in far_callees:
                ds = far_callees[i.far_target].sp_deltas
                afters = [None] if ds is None else [d + x for x in ds]
        succs = []
        if i.kind in (SEQ, CALL, CALL_FAR, CALL_IND):
            succs = [i.next_ip]
        elif i.kind == JCC:
            succs = [i.next_ip, i.target]
        elif i.kind == JMP:
            succs = [i.target]
        for s in succs:
            if s is None or s not in scan.insts:
                continue
            seen = depths.setdefault(s, set())
            for after in afters:
                if len(seen) >= 8:
                    # correlated-branch fork explosion: widen to UNKNOWN
                    after = None
                if after not in seen:
                    seen.add(after)
                    work.append((s, after))
    if len(ret_pops) > 1:
        raise Refusal("mixed-ret-pop (differing ret N immediates)")
    ret_pop = next(iter(ret_pops), 0)
    unknown = None in exit_depths
    sp_output = unknown or len(exit_depths) > 1 \
        or any(d != 0 for d in exit_depths)
    if unknown:
        sp_delta, sp_deltas = None, None
    else:
        sp_delta = (next(iter(exit_depths)) - ret_pop
                    if len(exit_depths) == 1 else None)
        sp_deltas = tuple(sorted(d - ret_pop for d in exit_depths)) or (0,)
        if not exit_depths:
            sp_delta = 0
    return sp_delta, ret_pop, sp_output, sp_deltas


def _check_flag_liveins(scan, callees=None, far_callees=None,
                        alt_entries=frozenset()) -> tuple:
    """Must-defined flag analysis over the CFG (meet = intersection).

    Refuses when a jcc reads a flag not DEFINITELY defined on every path from
    entry (a flag live-in would need caller flags in the contract).  A
    composed CALL defines its callee's must-defined exit flags -- what makes
    the ubiquitous ``call G; jnz ...`` idiom promotable.

    DF is special-cased (tier 9): a string op -- or a composed callee that
    itself needs the caller's DF, or a dynamic dispatch -- reached with DF
    not yet defined does not refuse; it makes DF a hidden compat INPUT of
    this function (``_df``; the adapter passes the machine DF, composed
    callers pass their live df local, so the value is machine-correct along
    every df-live-in chain).  Any OTHER flag live-in still refuses.

    Returns (exit_flags, df_livein)."""
    exit_flags, df_c, fl_c = _flag_pass(scan, callees or {}, far_callees or {},
                                        alt_entries, seed="none")
    if fl_c:
        # some flag is a live-in beyond DF: the whole FLAGS word becomes the
        # _flags_in compat input and EVERY flag local initializes from it
        # (machine-correct caller values), so nothing is ever undefined.
        exit_flags, _, _ = _flag_pass(scan, callees or {}, far_callees or {},
                                      alt_entries, seed="all")
        return exit_flags, df_c, True
    if not df_c:
        return exit_flags, False, False
    # DF alone is a live-in: rerun with DF defined at every entry (the _df
    # input supplies it), which settles the downstream sets exactly.
    exit_flags, _, _ = _flag_pass(scan, callees or {}, far_callees or {},
                                  alt_entries, seed="df")
    return exit_flags, True, False


def _flag_pass(scan, callees, far_callees, alt_entries, *, seed):
    """One must-defined fixpoint.  Returns (exit_flags, df_consumed,
    flags_consumed) -- whether DF (resp. any other flag) was read while
    undefined (making it a live-in via _df / the full _flags_in word)."""
    seed0 = (_ALL_FLAGS if seed == "all"
             else frozenset({"df"}) if seed == "df" else frozenset())
    defined: dict[int, frozenset] = {scan.entry: seed0}
    for a in alt_entries:       # dynamic arrival: same seeding as the entry
        defined[a] = seed0
    df_consumed = False
    fl_consumed = False
    exit_sets: dict[int, frozenset] = {}
    order = sorted(scan.insts)
    changed = True
    while changed:
        changed = False
        for ip in order:
            if ip not in defined:
                continue
            i = scan.insts[ip]
            if i.kind == JCC:
                need = _JCC_READS.get(i.op)
                if need is None:
                    raise Refusal(f"emitter-unsupported-op-{i.op:02X}")
                if not need <= set(defined[ip]):
                    fl_consumed = True
            if "df" not in defined[ip]:
                if i.op in _STRING_OPS or _is_dyn(i):
                    df_consumed = True
                if (i.kind == CALL and i.target in callees
                        and callees[i.target].df_livein):
                    df_consumed = True
                if (i.kind == CALL_FAR and i.far_target in far_callees
                        and far_callees[i.far_target].df_livein):
                    df_consumed = True
            if "cf" not in defined[ip]:
                if i.op == 0xF5:                          # cmc
                    fl_consumed = True
                if (i.op in (0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3)
                        and i.reg in (2, 3)):             # rcl/rcr
                    fl_consumed = True
                if (i.op <= 0x1D and ((i.op >> 3) & 7) in (2, 3)
                        and (i.op & 7) <= 5) \
                        or (i.op in (0x80, 0x81, 0x83)
                            and i.reg in (2, 3)):         # adc/sbb
                    fl_consumed = True
            new = defined[ip] | _flags_defined_by(i)
            if (i.kind == CALL and i.target is not None
                    and i.target in callees):
                new = defined[ip] | callees[i.target].exit_flags
            if (i.kind == CALL_FAR and i.far_target is not None
                    and i.far_target in far_callees):
                new = defined[ip] | far_callees[i.far_target].exit_flags
            if i.kind in (RET, RETF, IRET):
                exit_sets[ip] = defined[ip]
            if i.kind == JMP_IND:
                # dynamic tail: the exit flags are the runtime callee's --
                # statically unknown, so this exit guarantees nothing.
                exit_sets[ip] = frozenset()
            succs = []
            if i.kind in (SEQ, CALL, CALL_FAR, CALL_IND):
                succs = [i.next_ip]
            elif i.kind == JCC:
                succs = [i.next_ip, i.target]
            elif i.kind == JMP:
                succs = [i.target]
            for s in succs:
                if s is None or s not in scan.insts:
                    continue
                cur = defined.get(s)
                nxt = new if cur is None else (cur & new)   # must-defined meet
                if cur is None or nxt != cur:
                    defined[s] = nxt
                    changed = True
    if not exit_sets:
        return frozenset(), df_consumed, fl_consumed
    out = None
    for s in exit_sets.values():
        out = s if out is None else (out & s)
    return out, df_consumed, fl_consumed


#: string ops read the direction flag; DF undefined at one of these makes
#: the function df-live-in (the hidden _df compat input).
_STRING_OPS = (0xA4, 0xA5, 0xA6, 0xA7, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF)


_ALL_FLAGS = frozenset({"cf", "pf", "af", "zf", "sf", "of", "df", "intf"})


def _flags_defined_by(i) -> frozenset:
    op = i.op
    if i.kind == INT:
        return _ALL_FLAGS
    if op == 0x9D:              # popf: the whole word from stack data
        return _ALL_FLAGS
    if op == 0xF8 or op == 0xF9:
        return frozenset({"cf"})
    if op in (0xFC, 0xFD):
        return frozenset({"df"})
    if op in (0xFA, 0xFB):
        return frozenset({"intf"})
    if op in (0xC0, 0xC1) and i.reg in (0, 1, 2, 3, 4, 5, 7):
        # shift by a NONZERO immediate always writes CF (+ZF/SF/PF for
        # shl/shr/sar); OF only when the count is exactly 1.
        n = (i.imm or 0) & 0x1F
        if n == 0:
            return frozenset()
        base = {"cf"} | ({"zf", "sf", "pf"} if i.reg in (4, 5, 7) else set())
        if n == 1:
            base |= {"of"}
        return frozenset(base)
    if op in (0xD0, 0xD1) and i.reg in (0, 1, 2, 3, 4, 5, 7):  # count == 1
        base = {"cf", "of"} | ({"zf", "sf", "pf"} if i.reg in (4, 5, 7) else set())
        return frozenset(base)
    if op in (0xA6, 0xA7, 0xAE, 0xAF) \
            and not any(p in (0xF2, 0xF3) for p in i.prefixes):
        # non-rep cmps/scas: exactly one comparison, full subtraction flags.
        # (With repe/repne, cx may be 0 -- nothing is defined statically.)
        return frozenset({"cf", "pf", "af", "zf", "sf", "of"})
    if op in (0xF6, 0xF7) and i.reg in (4, 5):            # mul/imul: CF+OF only
        return frozenset({"cf", "of"})
    # D2/D3 (count from CL) define nothing statically (count may be 0).
    if (op <= 0x3D and (op & 7) <= 5 and (op & 0xC7) not in (0x06, 0x07, 0xC6, 0xC7)) \
            or op in (0x80, 0x81, 0x83, 0x84, 0x85, 0xA8, 0xA9) \
            or (op in (0xF6, 0xF7) and i.reg == 3):
        return frozenset({"cf", "pf", "af", "zf", "sf", "of"})
    if 0x40 <= op <= 0x4F or (op in (0xFE, 0xFF) and i.reg in (0, 1)):
        return frozenset({"pf", "af", "zf", "sf", "of"})
    return frozenset()


# --------------------------------------------------------------------------

def _contract_inputs(scan, abi) -> list[str]:
    """The recovered function's input list.  ``sp`` joins only when the body
    has real stack traffic OR composed calls (both write the guest stack
    literally: pushed bytes and return-address bytes are observable state);
    balance keeps it out of the outputs.  Otherwise the RET-ABI sp read stays
    the adapter's business."""
    needs_sp = any((_is_stack_family(i) and i.kind not in (RET, RETF))
                   or i.kind in (CALL, CALL_FAR)
                   for i in scan.insts.values())
    inputs = sorted(abi.inputs - {"sp"})
    if needs_sp:
        inputs = sorted(set(inputs) | {"sp", "ss"})
    return inputs


def _emit_boundary_observer(blk, cs, i, count):
    """Emit the boundary-head observer AFTER the head instruction: pass the
    full live bundle + composed flags word + the absolute virtual time to
    plat.boundary; merge back the (possibly parked-and-resumed) bundle,
    flags, and the extra time the delivered ISRs executed (tier 13)."""
    fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                    for f in ("cf", "pf", "af", "zf", "sf", "of",
                              "df", "intf"))
    blk.append(f"_bw = (_flags_in & ~_fmask) | (({fw}) & _fmask)")
    bundle = ", ".join(f"'{r}': {r}" for r in _DYN_REGS) \
        + f", 'cs': 0x{cs:04X}, '_df': (1 if df else 0), '_flags_in': _bw"
    blk.append(f"_bo = plat.boundary(0x{cs:04X}, 0x{i.ip:04X}, "
               f"0x{i.next_ip:04X}, {{{bundle}}}, _base + _cost + {count})")
    for r in _DYN_REGS:
        blk.append(f"{r} = _bo[0]['{r}']")
    blk.append("_bf = _bo[1]")
    for fname, fbit in (("cf", 0x01), ("pf", 0x04), ("af", 0x10),
                        ("zf", 0x40), ("sf", 0x80), ("of", 0x800),
                        ("intf", 0x200), ("df", 0x400)):
        blk.append(f"{fname} = (_bf & 0x{fbit:X}) != 0")
    blk.append("_fmask |= 0xED5")
    blk.append("_cost += _bo[2]")


def emit_recovered(scan, abi, key: str, *, callees=None, far_callees=None,
                   recovered_import_base: str = "", needs_plat=False,
                   dispatch_addrs=frozenset(), df_livein=False,
                   sp_output=False, flags_livein=False,
                   boundary_addrs=frozenset()) -> str:
    """Generate the recovered module source for one promotable function.

    ``callees`` (tier 4): CalleeContract per direct near-call target -- the
    recovered body calls the recovered callee DIRECTLY (composition at the
    recovered level); the machine call's return-address bytes are written
    literally (observable stack residue), the callee's exit flags merge
    through the compat mask, and its virtual-time cost accumulates.
    ``far_callees`` (tier 9): the same per static far-call (seg, off) target;
    the 4-byte far frame (static CS + return offset) is written literally."""
    callees = callees or {}
    far_callees = far_callees or {}
    cs = int(key.split(":")[0], 16)
    name = f"func_{key.replace(':', '_').lower()}"
    alt_entries = sorted(frozenset(dispatch_addrs) & frozenset(scan.insts)
                         - frozenset({scan.entry}))
    heads = frozenset(boundary_addrs) & frozenset(scan.insts)
    # dispatch entries are FORCED block leaders (dynamic arrivals enter the
    # shared blocks there) -- the same rule the VMless emitter applies.
    leaders = sorted(set(scan.block_leaders()) | set(alt_entries))
    bb_of = {ip: n for n, ip in enumerate(leaders)}
    has_dyn = any(_is_dyn(i) for i in scan.insts.values())
    inputs = _contract_inputs(scan, abi)
    outputs = _output_set(abi, sp_output)

    L: list[str] = []
    A = L.append
    A('"""AUTOGENERATED by dos_re.lift.emit_cpuless -- CPUless recovered')
    A(f"function for {key} (DOS_RE 2.0 stage 2).  DO NOT hand-edit; regenerate.")
    A("")
    A("Public contract (semantic): the returned dict of live outputs.")
    A("The second return value (_compat) is generated compatibility metadata")
    A("(exit-flag reproduction + virtual-time cost) consumed ONLY by the")
    A("generated CPU-ABI adapter -- it is not part of the recovered API.")
    A('"""')
    used_names = sorted(
        ({callees[i.target].name for i in scan.insts.values()
          if i.kind == CALL and i.target in callees}
         | {far_callees[i.far_target].name for i in scan.insts.values()
            if i.kind == CALL_FAR and i.far_target in far_callees})
        - {name})    # direct self-recursion: the module-level name suffices
    if used_names:
        A("")
        for cname in used_names:
            A(f"from {recovered_import_base}.{cname} import {cname}")
    has_ivec = any(_is_game_int(i) or _is_isr_chain(i)
                   for i in scan.insts.values())
    if has_dyn:
        A("")
        A(f"from {recovered_import_base}._dyncall import dyn_exec as _dyn")
    if has_ivec:
        A("")
        A(f"from {recovered_import_base}._dyncall import ivec_exec as _ivec")
    A("")
    A("_PARITY = tuple((1 - bin(v).count('1') % 2) == 1 for v in range(256))")
    if alt_entries:
        A("")
        A("#: dynamic-arrival ALTERNATE ENTRIES (recovery-fact dispatch")
        A("#: entries inside this function): arrival ip -> dispatch block.")
        A("_ENTRIES = {" + ", ".join(f"0x{a:04X}: {bb_of[a]}"
                                     for a in alt_entries) + "}")
    if has_dyn:
        A("")
        A("#: intra-function landing map for near jump-table dispatch:")
        A("#: block-leader ip -> dispatch block index.")
        A("_LOCAL = {" + ", ".join(f"0x{ip:04X}: {n}"
                                   for ip, n in sorted(bb_of.items())) + "}")
    A("")
    A("")
    argl = (["_base=0"] if needs_plat else []) \
        + (["_entry_ip=None"] if alt_entries else []) \
        + (["_df=0"] if df_livein else []) \
        + (["_flags_in=2"] if flags_livein else []) \
        + [f"{r}=0" for r in inputs]
    args = ", ".join(argl)
    _p = "mem, plat" if needs_plat else "mem"
    A(f"def {name}({_p}, *, {args}):" if args else f"def {name}({_p}):")
    body: list[str] = []
    B = body.append
    B("_cost = 0")
    B("cf = pf = af = zf = sf = of = df = intf = False")
    if flags_livein:
        # every flag local starts MACHINE-CORRECT from the caller word --
        # nothing is ever "undefined" in a flags-livein body (tier 13).
        for fname, fbit in sorted(_FLAG_BITS.items()):
            B(f"{fname} = (_flags_in & 0x{fbit:X}) != 0")
    if df_livein:
        B("df = _df != 0    # caller DF (hidden compat input, tier 9)")
    B("_fmask = 0")
    if alt_entries:
        B(f"bb = {bb_of[scan.entry]} if _entry_ip is None else _ENTRIES[_entry_ip]")
    else:
        B(f"bb = {bb_of[scan.entry]}")
    # Unbounded-spin guard (mirrors the VMless emitter): the block-dispatch
    # loop only exits via a block's break/return.  A recovered spin-wait on a
    # condition an interrupt must change -- or on a wrong port after a state
    # divergence -- would loop forever; fail LOUD with the function + block so
    # a hang is a debuggable error, never a silent freeze.
    B("_iters = 0")
    B("while True:")
    B(f"    _iters += 1")
    B(f"    if _iters > {_DISPATCH_ITER_CAP}:")
    B(f"        raise RuntimeError('CPUless dispatch spin in {key} "
      f"(block %d, cost %d): loop exceeded {_DISPATCH_ITER_CAP} iterations "
      f"-- an unbounded wait (interrupt-updated flag, or a wrong port after "
      f"a state divergence)' % (bb, _cost))")
    # blocks
    for n, leader in enumerate(leaders):
        blk: list[str] = []
        flag_written: set[str] = set()
        ip = leader
        count = 0
        terminated = False
        while ip in scan.insts:
            i = scan.insts[ip]
            count += 1
            if i.kind in (RET, RETF, IRET):
                blk.append(f"_cost += {count}")
                if flag_written:
                    bits = " | ".join(f"0x{_FLAG_BITS[f]:X}" for f in sorted(flag_written))
                    blk.append(f"_fmask |= {bits}")
                blk.append("break")
                terminated = True
                break
            if i.kind == JMP:
                blk.append(f"_cost += {count}")
                if flag_written:
                    bits = " | ".join(f"0x{_FLAG_BITS[f]:X}" for f in sorted(flag_written))
                    blk.append(f"_fmask |= {bits}")
                blk.append(f"bb = {bb_of[i.target]}")
                blk.append("continue")
                terminated = True
                break
            if i.kind == JCC:
                blk.append(f"_cost += {count}")
                if flag_written:
                    bits = " | ".join(f"0x{_FLAG_BITS[f]:X}" for f in sorted(flag_written))
                    blk.append(f"_fmask |= {bits}")
                if i.op in (0xE0, 0xE1, 0xE2):    # loopnz/loopz/loop: dec cx, NO flags
                    blk.append("cx = (cx - 1) & 0xFFFF")
                    cond = {0xE0: "cx != 0 and not zf",
                            0xE1: "cx != 0 and zf",
                            0xE2: "cx != 0"}[i.op]
                elif i.op == 0xE3:                # jcxz
                    cond = "cx == 0"
                else:
                    cond = _JCC_EXPR[i.op]
                blk.append(f"if {cond}:")
                blk.append(f"    bb = {bb_of[i.target]}")
                blk.append("    continue")
                blk.append(f"bb = {bb_of[i.next_ip]}")
                blk.append("continue")
                terminated = True
                break
            if _is_isr_chain(i):
                # tier 13: the ISR CHAIN tail -- read the saved far vector
                # from memory, dispatch its recovered handler (HANDLERS
                # registry) on OUR interrupt frame; its iret ends this
                # interrupt, so this is the function's exit.  An unknown
                # vector raises the witness -- never a fallback.
                off = count - 1
                cost = "_base + _cost" if off == 0 else f"_base + _cost + {off}"
                eoff, eseg = _ea(i)
                blk.append(f"_co = {eoff}")
                blk.append(f"_vo = mem.rw({eseg}, _co)")
                blk.append(f"_vs = mem.rw({eseg}, (_co + 2) & 0xFFFF)")
                fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                                for f in ("cf", "pf", "af", "zf", "sf", "of",
                                          "df", "intf"))
                bundle = ", ".join(f"'{r}': {r}" for r in _DYN_REGS) \
                    + (", 'cs': _vs, '_df': (1 if df else 0), "
                       f"'_flags_in': ((_flags_in & ~_fmask) | (({fw}) & _fmask))")
                blk.append(f"_do, _dc = _ivec(\"%04X:%04X\" % (_vs, _vo), "
                           f"mem, plat, {cost}, {{{bundle}}})")
                for r in _DYN_REGS:
                    blk.append(f"{r} = _do['{r}']")
                blk.append("_gm = _dc['fmask']")
                blk.append("if _gm:")
                blk.append("    _gf = _dc['flags']")
                for fname, fbit in (("cf", 0x01), ("pf", 0x04), ("af", 0x10),
                                    ("zf", 0x40), ("sf", 0x80), ("of", 0x800),
                                    ("intf", 0x200), ("df", 0x400)):
                    blk.append(f"    if _gm & 0x{fbit:X}: "
                               f"{fname} = (_gf & 0x{fbit:X}) != 0")
                blk.append("    _fmask |= _gm")
                blk.append("_cost += _dc['cost']")
                blk.append(f"_cost += {count}")
                if flag_written:
                    bits = " | ".join(f"0x{_FLAG_BITS[f]:X}"
                                      for f in sorted(flag_written))
                    blk.append(f"_fmask |= {bits}")
                blk.append("break")
                terminated = True
                break
            if _is_dyn(i):
                # tier 9: runtime-resolved recovered dispatch (near indirect
                # call/jmp).  Target computed from regs/memory, resolved
                # through the generated registry; an intra-function landing
                # (jump table) becomes a direct block transfer; an unknown
                # selector raises UnknownDispatchTarget -- never a fallback.
                is_call = i.kind == CALL_IND
                off = count - 1
                cost = "_base + _cost" if off == 0 else f"_base + _cost + {off}"
                blk.append(f"_dt = ({_rm_read(i, True)}) & 0xFFFF")
                if not is_call:
                    blk.append("if _dt in _LOCAL:")
                    blk.append(f"    _cost += {count}")
                    if flag_written:
                        bits = " | ".join(f"0x{_FLAG_BITS[f]:X}"
                                          for f in sorted(flag_written))
                        blk.append(f"    _fmask |= {bits}")
                    blk.append("    bb = _LOCAL[_dt]")
                    blk.append("    continue")
                if is_call:
                    blk.append("sp = (sp - 2) & 0xFFFF")
                    blk.append(f"mem.ww(ss, sp, 0x{i.next_ip:04X})")
                # near dispatch stays in this segment, so the callee's CS is
                # this function's static CS (cs-as-data contracts need it).
                # The bundle carries the full FLAGS word (tier 14, same
                # reconstruction as the vectored site above): bits this
                # function never wrote ride _flags_in, the rest are packed
                # from the live flag vars.  _exec forwards it only to a
                # target whose contract declares flags_livein.
                fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                                for f in ("cf", "pf", "af", "zf", "sf", "of",
                                          "df", "intf"))
                bundle = ", ".join(f"'{r}': {r}" for r in _DYN_REGS) \
                    + (f", 'cs': 0x{cs:04X}, '_df': (1 if df else 0), "
                       f"'_flags_in': ((_flags_in & ~_fmask) "
                       f"| (({fw}) & _fmask))")
                blk.append(f"_do, _dc = _dyn(\"{cs:04X}:%04X\" % _dt, "
                           f"mem, plat, {cost}, {{{bundle}}})")
                for r in _DYN_REGS:
                    blk.append(f"{r} = _do['{r}']")
                blk.append("_gm = _dc['fmask']")
                blk.append("if _gm:")
                blk.append("    _gf = _dc['flags']")
                for fname, fbit in (("cf", 0x01), ("pf", 0x04), ("af", 0x10),
                                    ("zf", 0x40), ("sf", 0x80), ("of", 0x800),
                                    ("intf", 0x200), ("df", 0x400)):
                    blk.append(f"    if _gm & 0x{fbit:X}: "
                               f"{fname} = (_gf & 0x{fbit:X}) != 0")
                blk.append("    _fmask |= _gm")
                blk.append("_cost += _dc['cost']")
                if is_call:
                    blk.append("sp = (sp + 2) & 0xFFFF")
                    ip = i.next_ip
                    if ip in bb_of:
                        blk.append(f"_cost += {count}")
                        if flag_written:
                            bits = " | ".join(f"0x{_FLAG_BITS[f]:X}"
                                              for f in sorted(flag_written))
                            blk.append(f"_fmask |= {bits}")
                        blk.append(f"bb = {bb_of[ip]}")
                        blk.append("continue")
                        terminated = True
                        break
                    continue
                # dynamic TAIL: the dispatched callee's return IS this
                # function's exit (its ret pops our caller's frame).
                blk.append(f"_cost += {count}")
                if flag_written:
                    bits = " | ".join(f"0x{_FLAG_BITS[f]:X}"
                                      for f in sorted(flag_written))
                    blk.append(f"_fmask |= {bits}")
                blk.append("break")
                terminated = True
                break
            if i.kind in (CALL, CALL_FAR):
                far = i.kind == CALL_FAR
                c = far_callees[i.far_target] if far else callees[i.target]
                # the machine call: return-address bytes are observable
                if far:
                    # far frame: static CS, then the return offset
                    blk.append("sp = (sp - 2) & 0xFFFF")
                    blk.append(f"mem.ww(ss, sp, 0x{cs:04X})")
                blk.append("sp = (sp - 2) & 0xFFFF")
                blk.append(f"mem.ww(ss, sp, 0x{i.next_ip:04X})")
                kw = ", ".join(f"{r}={r}" for r in c.inputs)
                _pass = "mem, plat" if c.needs_plat else "mem"
                if c.flags_livein:
                    # the callee needs the caller's FULL flags word: runtime
                    # bits we defined, entry word for the rest
                    fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                                    for f in ("cf", "pf", "af", "zf", "sf",
                                              "of", "df", "intf"))
                    kw = (f"_flags_in=((_flags_in & ~_fmask) | (({fw}) & _fmask))"
                          + (", " + kw if kw else ""))
                if c.df_livein:
                    kw = ("_df=(1 if df else 0)" + (", " + kw if kw else ""))
                if c.needs_plat:
                    _boff = count - 1
                    _b = "_base + _cost" if _boff == 0 else f"_base + _cost + {_boff}"
                    kw = (f"_base={_b}" + (", " + kw if kw else ""))
                blk.append(f"_o, _c = {c.name}({_pass}{', ' + kw if kw else ''})")
                for r in c.outputs:
                    blk.append(f"{r} = _o['{r}']")
                blk.append("_gm = _c['fmask']")
                blk.append("if _gm:")
                blk.append("    _gf = _c['flags']")
                for fname, fbit in (("cf", 0x01), ("pf", 0x04), ("af", 0x10),
                                    ("zf", 0x40), ("sf", 0x80), ("of", 0x800),
                                    ("intf", 0x200), ("df", 0x400)):
                    blk.append(f"    if _gm & 0x{fbit:X}: "
                               f"{fname} = (_gf & 0x{fbit:X}) != 0")
                blk.append("    _fmask |= _gm")
                blk.append("_cost += _c['cost']")
                # sp after the composed call: an sp-output callee already
                # merged its runtime sp through the outputs loop above; the
                # ret-addr pair (+ a uniform ret N) pops here.  A retf
                # callee behind a NEAR call (push-cs idiom) pops 4: our
                # return offset plus the caller's explicitly pushed CS.
                pop_n = 4 if (far or c.ret_kind == "far") else 2 + c.ret_pop
                blk.append(f"sp = (sp + {pop_n}) & 0xFFFF")
                ip = i.next_ip
                if ip in bb_of:
                    blk.append(f"_cost += {count}")
                    if flag_written:
                        bits = " | ".join(f"0x{_FLAG_BITS[f]:X}"
                                          for f in sorted(flag_written))
                        blk.append(f"_fmask |= {bits}")
                    blk.append(f"bb = {bb_of[ip]}")
                    blk.append("continue")
                    terminated = True
                    break
                continue
            if _is_game_int(i):
                # tier 12: a GAME-VECTORED interrupt is a call into game
                # code.  Push the literal interrupt frame (full flags word
                # composed from runtime-defined bits + the caller word),
                # read the runtime IVT vector, dispatch to the recovered
                # IRET-contract handler, then pop the frame -- flags reload
                # from the (possibly handler-modified) stacked word.
                off = count - 1
                cost = "_base + _cost" if off == 0 else f"_base + _cost + {off}"
                fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                                for f in ("cf", "pf", "af", "zf", "sf", "of",
                                          "df", "intf"))
                blk.append(f"_fw = (_flags_in & ~_fmask) | (({fw}) & _fmask)")
                blk.append("sp = (sp - 2) & 0xFFFF")
                blk.append("mem.ww(ss, sp, _fw)")
                blk.append("sp = (sp - 2) & 0xFFFF")
                blk.append(f"mem.ww(ss, sp, 0x{cs:04X})")
                blk.append("sp = (sp - 2) & 0xFFFF")
                blk.append(f"mem.ww(ss, sp, 0x{i.next_ip:04X})")
                blk.append(f"_vo = mem.rw(0, 0x{i.int_no * 4:X})")
                blk.append(f"_vs = mem.rw(0, 0x{i.int_no * 4 + 2:X})")
                bundle = ", ".join(f"'{r}': {r}" for r in _DYN_REGS) \
                    + ", 'cs': _vs, '_df': (1 if df else 0), '_flags_in': _fw"
                blk.append(f"_do, _dc = _ivec(\"%04X:%04X\" % (_vs, _vo), "
                           f"mem, plat, {cost}, {{{bundle}}})")
                for r in _DYN_REGS:
                    blk.append(f"{r} = _do['{r}']")
                blk.append("_cost += _dc['cost']")
                # iret: flags come from the stacked word (handler may have
                # edited it in place); ip/cs slots are ours by construction.
                blk.append("_rw = mem.rw(ss, (sp + 4) & 0xFFFF)")
                blk.append("sp = (sp + 6) & 0xFFFF")
                for fname, fbit in (("cf", 0x01), ("pf", 0x04), ("af", 0x10),
                                    ("zf", 0x40), ("sf", 0x80), ("of", 0x800),
                                    ("intf", 0x200), ("df", 0x400)):
                    blk.append(f"{fname} = (_rw & 0x{fbit:X}) != 0")
                blk.append("_fmask |= 0xED5")
                nxt = i.next_ip
                if nxt in bb_of and nxt != ip:
                    blk.append(f"_cost += {count}")
                    blk.append(f"bb = {bb_of[nxt]}")
                    blk.append("continue")
                    terminated = True
                    break
                ip = nxt
                continue
            if i.kind == INT:
                off = count - 1
                cost = "_base + _cost" if off == 0 else f"_base + _cost + {off}"
                fw = " | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                                for f in ("cf", "pf", "af", "zf", "sf", "of",
                                          "df", "intf"))
                _regd = ", ".join("'%s': %s" % (r, r) for r in _INT_REGS)
                blk.append("_ib = {%s, '_flags': (%s)}" % (_regd, fw))
                blk.append(f"_ir = plat.intr(0x{i.int_no:02X}, _ib, {cost})")
                for r in _INT_REGS:
                    blk.append(f"{r} = _ir['{r}']")
                blk.append("_if = _ir['flags']")
                for fname, fbit in (("cf", 0x01), ("pf", 0x04), ("af", 0x10),
                                    ("zf", 0x40), ("sf", 0x80), ("of", 0x800),
                                    ("intf", 0x200), ("df", 0x400)):
                    blk.append(f"{fname} = (_if & 0x{fbit:X}) != 0")
                blk.append(f"_fmask |= 0x{0x0001|0x0004|0x0010|0x0040|0x0080|0x0800|0x0200|0x0400:X}")
                nxt = i.next_ip
                if nxt in bb_of and nxt != ip:
                    blk.append(f"_cost += {count}")
                    blk.append(f"bb = {bb_of[nxt]}")
                    blk.append("continue")
                    terminated = True
                    break
                ip = nxt
                continue
            if i.op in (0xE4, 0xE5, 0xE6, 0xE7, 0xEC, 0xED, 0xEE, 0xEF):
                width = 2 if (i.op & 1) else 1
                # instruction_count the interpreter would hold at this port
                # access: entry + prior-in-block instructions (count-1).
                off = count - 1
                cost = "_base + _cost" if off == 0 else f"_base + _cost + {off}"
                if i.op in (0xE4, 0xE5):          # in acc, imm8
                    port = f"0x{(i.imm or 0) & 0xFF:X}"
                    read = True
                elif i.op in (0xEC, 0xED):        # in acc, dx
                    port = "dx"
                    read = True
                elif i.op in (0xE6, 0xE7):        # out imm8, acc
                    port = f"0x{(i.imm or 0) & 0xFF:X}"
                    read = False
                else:                             # out dx, acc
                    port = "dx"
                    read = False
                if read:
                    blk.append(f"_pv = plat.inp({port}, {width}, {cost})")
                    if width == 2:
                        blk.append("ax = _pv")
                    else:
                        blk.append(_r8_write(0, "_pv"))
                else:
                    val = "ax" if width == 2 else "(ax & 0xFF)"
                    blk.append(f"plat.outp({port}, {val}, {width}, {cost})")
                if i.ip in heads:
                    _emit_boundary_observer(blk, cs, i, count)
                nxt = i.next_ip
                if nxt in bb_of and nxt != ip:
                    blk.append(f"_cost += {count}")
                    if flag_written:
                        bits = " | ".join(f"0x{_FLAG_BITS[f]:X}"
                                          for f in sorted(flag_written))
                        blk.append(f"_fmask |= {bits}")
                    blk.append(f"bb = {bb_of[nxt]}")
                    blk.append("continue")
                    terminated = True
                    break
                ip = nxt
                continue
            _translate(i, blk, flag_written)
            if i.ip in heads:
                # the observer's composed flags word must see this
                # instruction's own flag writes: flush them into _fmask now
                if flag_written:
                    bits = " | ".join(f"0x{_FLAG_BITS[f]:X}"
                                      for f in sorted(flag_written))
                    blk.append(f"_fmask |= {bits}")
                    flag_written.clear()
                _emit_boundary_observer(blk, cs, i, count)
            nxt = i.next_ip
            if nxt in bb_of and nxt != ip:     # falls into the next block
                blk.append(f"_cost += {count}")
                if flag_written:
                    bits = " | ".join(f"0x{_FLAG_BITS[f]:X}" for f in sorted(flag_written))
                    blk.append(f"_fmask |= {bits}")
                blk.append(f"bb = {bb_of[nxt]}")
                blk.append("continue")
                terminated = True
                break
            ip = nxt
        if not terminated:
            raise Refusal("block-falls-off-region")
        B(f"    if bb == {n}:  # {cs:04X}:{leader:04X}")
        for ln in blk:
            B(f"        {ln}")
    B("    raise AssertionError('unreachable dispatch')")
    # returns
    out_dict = ", ".join(f"'{r}': {r} & 0xFFFF" for r in outputs)
    fl_expr = (" | ".join(f"(0x{_FLAG_BITS[f]:X} if {f} else 0)"
                          for f in ("cf", "pf", "af", "zf", "sf", "of",
                                    "df", "intf")))
    B("_flags = (" + fl_expr + ") & _fmask")
    B(f"return {{{out_dict}}}, {{'flags': _flags, 'fmask': _fmask, 'cost': _cost}}")
    L.extend("    " + ln for ln in body)
    A("")
    return "\n".join(L) + "\n"


#: generated runtime-dispatch support module (written into the recovered
#: package by the promote driver).  Owns the ONLY dynamic-transfer path of
#: the recovered program: resolve through the generated DISPATCH table to a
#: direct recovered call, or raise a structured witness.  No dos_re import,
#: no CPU object, no interpreter -- ever.
DYNCALL_SUPPORT_SRC = '''\
"""AUTOGENERATED by dos_re.lift.emit_cpuless -- runtime dispatch support for
recovered dynamic transfers (DOS_RE 2.0 stage 2, tier 9).

A recovered indirect call/jmp resolves its runtime CS:IP selector through the
generated DISPATCH table (.dispatch) to a DIRECT recovered call -- possibly at
a dynamic-arrival alternate entry.  An unknown selector raises
:class:`UnknownDispatchTarget` (a structured frontier witness).  There is NO
fallback path: not to a CPU, not to a lifted graph, not to an interpreter.
DO NOT hand-edit; regenerate."""
import importlib

from .dispatch import DISPATCH, HANDLERS


class UnknownDispatchTarget(RuntimeError):
    """A dynamic transfer selected a target with no recovered implementation.
    Carries the witness; the caller of the recovered program decides what to
    do -- the recovered code itself never falls back."""

    def __init__(self, key, regs, base):
        super().__init__(
            "dynamic dispatch to %s: no recovered implementation (frontier "
            "witness; promote the target or record why it cannot be)" % key)
        self.key = key
        self.regs = dict(regs)
        self.base = base


_cache = {}


def _exec(table, kind, key, mem, plat, base, regs):
    ent = table.get(key)
    if ent is None:
        raise UnknownDispatchTarget(kind + " " + key, regs, base)
    fn = _cache.get((kind, key))
    if fn is None:
        modname, fname, entry_ip, inputs, needs_plat, df_livein, fl_livein = ent
        f = getattr(importlib.import_module(modname), fname)

        def fn(mem, plat, base, regs, _f=f, _e=entry_ip, _ins=inputs,
               _np=needs_plat, _dfl=df_livein, _fl=fl_livein):
            kw = {r: regs[r] for r in _ins}
            if _e is not None:
                kw["_entry_ip"] = _e
            if _dfl:
                kw["_df"] = regs.get("_df", 0)
            if _fl:
                kw["_flags_in"] = regs.get("_flags_in", 2)
            if _np:
                out, c = _f(mem, plat, _base=base, **kw)
            else:
                out, c = _f(mem, **kw)
            merged = dict(regs)
            merged.update(out)
            return merged, c

        _cache[(kind, key)] = fn
    return fn(mem, plat, base, regs)


def dyn_exec(key, mem, plat, base, regs):
    """Dispatch one dynamic near transfer: full register bundle in, merged
    bundle out (unwritten registers pass through), plus the compat channel."""
    return _exec(DISPATCH, "dyn", key, mem, plat, base, regs)


def ivec_exec(key, mem, plat, base, regs):
    """Dispatch one game-vectored interrupt (tier 12) to its recovered
    IRET-contract handler.  The CALLER owns the interrupt frame (already
    pushed; popped after); the handler is an ordinary recovered function
    whose iret exits are plain returns."""
    return _exec(HANDLERS, "ivec", key, mem, plat, base, regs)
'''


def emit_dispatch_table(entries: dict, handlers: dict | None = None) -> str:
    """Generate the dispatch registry module source.  ``entries`` maps a
    "CS:IP" selector to (module_name, func_name, entry_ip_or_None,
    input_names, needs_plat, df_livein, flags_livein); ``handlers`` is the
    same map for game-vectored interrupt handlers (IRET contract, keyed by
    the runtime vector value)."""
    def block(name, table):
        out = [f"{name} = {{"]
        for key in sorted(table or {}):
            mod, fn, eip, inputs, np, dfl, fl = table[key]
            eip_s = "None" if eip is None else f"0x{eip:04X}"
            ins = "(" + ", ".join(f"{r!r}" for r in inputs) \
                + ("," if len(inputs) == 1 else "") + ")"
            out.append(f"    {key!r}: ({mod!r}, {fn!r}, {eip_s}, {ins}, "
                       f"{np}, {dfl}, {fl}),")
        out.append("}")
        return out

    L = ['"""AUTOGENERATED by dos_re.lift.emit_cpuless -- the recovered',
         "dynamic-dispatch registry (tiers 9/12): runtime CS:IP selector ->",
         "(module, function, alternate-entry ip, contract inputs, needs_plat,",
         "df_livein, flags_livein).  DISPATCH serves near indirect transfers",
         "(balanced near-return functions only); HANDLERS serves game-vectored",
         "interrupts (IRET-contract functions, keyed by the runtime vector).",
         'Regenerated by the promote driver every apply.  DO NOT hand-edit."""',
         ""]
    L += block("DISPATCH", entries)
    L.append("")
    L += block("HANDLERS", handlers)
    return "\n".join(L) + "\n"


def emit_adapter(scan, abi, key: str, *, signature: bytes,
                 recovered_import_base: str, needs_plat=False,
                 ret_kind: str = "near", dispatch_addrs=frozenset(),
                 df_livein=False, sp_output=False, ret_pop=0,
                 flags_livein=False) -> str:
    """Generate the CPU-ABI adapter that occupies the lifted slot."""
    cs = int(key.split(":")[0], 16)
    entry = scan.entry
    name = f"lifted_{key.replace(':', '_').lower()}"
    rec = f"func_{key.replace(':', '_').lower()}"
    inputs = _contract_inputs(scan, abi)
    outputs = _output_set(abi, sp_output)
    alt_entries = sorted(frozenset(dispatch_addrs) & frozenset(scan.insts)
                         - frozenset({scan.entry}))

    L: list[str] = []
    A = L.append
    A('"""AUTOGENERATED by dos_re.lift.emit_cpuless -- CPU-ABI adapter (stage 2).')
    A("")
    A(f"The recovered implementation ({recovered_import_base}.{rec}) is")
    A("authoritative; this generated adapter restores the historical machine")
    A("ABI around it: reads only the inferred live inputs from the CPU")
    A("carrier, writes the inferred outputs back, reproduces the exit FLAGS")
    A("word and the exact per-path virtual-time cost from the generated")
    A("compatibility metadata, and applies the RET ABI.  No interpreted")
    A('fallback exists on any path.  DO NOT hand-edit; regenerate."""')
    A("from __future__ import annotations")
    A("")
    A(f"from {recovered_import_base}.{rec} import {rec}")
    if needs_plat:
        A("from dos_re.lift.platform import make_cpu_platform")
    A("")
    A(f"ENTRY = (0x{cs:04X}, 0x{entry:04X})")
    A(f"SIGNATURE = bytes.fromhex({signature.hex()!r})")
    if alt_entries:
        A("")
        A("#: dynamic-arrival re-entry hooks (dispatch entries sharing this")
        A("#: function's recovered blocks): the installer registers the hook")
        A("#: at each address; bb carries the arrival ip into the recovered")
        A("#: body's generated _entry_ip channel.")
        A("RESUME_ENTRIES = {"
          + ", ".join(f'"{cs:04X}:{a:04X}": 0x{a:04X}' for a in alt_entries)
          + "}")
    A("")
    A("")
    A(f"def {name}(cpu, bb=0):")
    A(f'    """Generated CPU-ABI adapter for {key} (recovered body is the')
    A('    single implementation)."""')
    A("    s = cpu.s")
    if needs_plat:
        A("    _entry = cpu.instruction_count")
        A("    _plat = make_cpu_platform(cpu)")
    kw = ", ".join(f"{r}=s.{r}" for r in inputs)
    if flags_livein:
        kw = ("_flags_in=s.flags" + (", " + kw if kw else ""))
    if df_livein:
        kw = ("_df=(s.flags >> 10) & 1" + (", " + kw if kw else ""))
    if alt_entries:
        kw = ("_entry_ip=(bb or None)" + (", " + kw if kw else ""))
    _mem = "cpu.mem, _plat" if needs_plat else "cpu.mem"
    A(f"    _out, _compat = {rec}({_mem}{', ' + kw if kw else ''})")
    for r in outputs:
        A(f"    s.{r} = _out['{r}']")
    A("    # exit flags: touched bits from the compat channel, rest preserved")
    A("    s.flags = (s.flags & ~_compat['fmask']) | _compat['flags'] | 0x0002")
    A("    # historical RET ABI + exact virtual time (owns_time contract)")
    A("    # (an unbalanced body already wrote its runtime sp back above,")
    A("    # so the pop reads the return address exactly where it now is)")
    if ret_kind == "iret":
        A("    s.ip = cpu.pop()")
        A("    s.cs = cpu.pop()")
        A("    s.flags = cpu.pop() | 0x0002")
    elif ret_kind == "far":
        A("    s.ip = cpu.pop()")
        A("    s.cs = cpu.pop()")
    else:
        A("    s.ip = cpu.pop()")
    if ret_pop:
        A(f"    s.sp = (s.sp + {ret_pop}) & 0xFFFF   # ret {ret_pop}")
    if needs_plat:
        # plat effects already moved instruction_count to _entry + <mid cost>;
        # settle it at the absolute total (an increment would double-count).
        A("    cpu.instruction_count = _entry + _compat['cost']")
    else:
        A("    cpu.instruction_count += _compat['cost']")
    A("")
    A("")
    A(f"#: this adapter accounts its own instruction_count per executed path.")
    A(f"{name}.owns_time = True")
    return "\n".join(L) + "\n"
