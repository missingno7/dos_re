"""Emit a literal Python hook for one lifted function (docs/lifting_design.md §5).

Design constraints, in priority order:

1. **Faithful.** Architectural state (registers, flags, memory) is updated at
   every instruction boundary exactly as ``dos_re.cpu`` would. Emitted code
   never re-derives flag semantics: it calls the interpreter's own
   ``set_add_flags`` / ``set_sub_flags`` / ``set_logic_flags`` /
   ``set_incdec_flags`` / ``shift`` / ``condition``. Memory always goes
   through ``mem.rb/rw/wb/ww`` so EGA-aperture, ROM and watcher semantics are
   identical by construction.
2. **Total.** Any non-transfer instruction the native emitter does not know is
   emitted as a one-instruction interpreter call (``interp_one``), so a
   function is lifted in full even while the native opcode set is small. Such
   lines are marked ``# (interpreter fallback)`` — they are the to-do list for
   both the emitter and the refactoring AI.
3. **Refactorable.** One line of Python per instruction, each preceded by its
   address, raw bytes and disassembly. Control flow becomes an explicit
   basic-block dispatch loop (Python has no goto).

Calls, far calls and INTs delegate to the VM (``lift.runtime``), so callees
never need lifting and hooks compose automatically.
"""
from __future__ import annotations

from dataclasses import dataclass

from .cfg import FunctionScan
from .decode import (CALL, CALL_FAR, CALL_IND, INT, IRET, JCC, JMP, JMP_FAR,
                     JMP_IND, RET, RETF, SEQ, Inst)

REG16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")
REG8 = ("al", "cl", "dl", "bl", "ah", "ch", "dh", "bh")
SREG = ("es", "cs", "ss", "ds")
_ALU = ("add", "or", "adc", "sbb", "and", "sub", "xor", "cmp")
_JCC_COND = {0xE0: "loopnz", 0xE1: "loopz", 0xE2: "loop", 0xE3: "jcxz"}

#: rm -> (offset expression parts, default segment)
_RM_BASE = {
    0: (("s.bx", "s.si"), "ds"), 1: (("s.bx", "s.di"), "ds"),
    2: (("s.bp", "s.si"), "ss"), 3: (("s.bp", "s.di"), "ss"),
    4: (("s.si",), "ds"), 5: (("s.di",), "ds"),
    6: (("s.bp",), "ss"), 7: (("s.bx",), "ds"),
}


class EmitUnsupported(Exception):
    """This instruction has no native form and no legal fallback."""


@dataclass
class Operand:
    """An emitted r/m operand: how to read it, and how to write it back."""
    is_reg: bool
    bits: int
    text: str
    reg_idx: int = 0
    seg_expr: str = ""
    off_var: str = ""          # name of the local holding the offset (memory only)
    off_expr: str = ""

    def read(self) -> str:
        if self.is_reg:
            return f"s.{REG16[self.reg_idx]}" if self.bits == 16 else f"cpu.get_reg8({self.reg_idx})"
        rd = "rw" if self.bits == 16 else "rb"
        return f"mem.{rd}({self.seg_expr}, {self.off_var})"

    def write(self, value: str) -> list[str]:
        if self.is_reg:
            if self.bits == 16:
                return [f"s.{REG16[self.reg_idx]} = ({value}) & 0xFFFF"]
            return [f"cpu.set_reg8({self.reg_idx}, {value})"]
        wr = "ww" if self.bits == 16 else "wb"
        return [f"mem.{wr}({self.seg_expr}, {self.off_var}, {value})"]


def _rm_operand(inst: Inst, bits: int, out: list[str], tmp: str) -> Operand:
    """Emit the address computation (if any) and describe the r/m operand.

    Mirrors ``CPU8086.decode_ea``: base registers sum unmasked, displacement
    added, then a single 16-bit mask. ``mod==0, rm==6`` is the direct-address
    form (unsigned disp16, default DS).
    """
    mod, rm = inst.mod, inst.rm
    if mod == 3:
        return Operand(True, bits, REG16[rm] if bits == 16 else REG8[rm], reg_idx=rm)

    if mod == 0 and rm == 6:
        off_expr = f"0x{inst.disp & 0xFFFF:04X}"
        default_seg, text = "ds", f"[{inst.disp & 0xFFFF:04X}]"
    else:
        parts, default_seg = _RM_BASE[rm]
        text = "[" + "+".join(p[2:] for p in parts) + "]"
        expr = " + ".join(parts)
        disp = inst.disp or 0
        if disp > 0:
            expr += f" + 0x{disp:X}"
        elif disp < 0:
            expr += f" - 0x{-disp:X}"
        off_expr = f"({expr}) & 0xFFFF"

    seg = inst.seg_override or default_seg
    out.append(f"{tmp} = {off_expr}")
    return Operand(False, bits, text, seg_expr=f"s.{seg}", off_var=tmp, off_expr=off_expr)


def _alu_lines(group: int, bits: int, a: str, b: str, dst: Operand | None,
               out: list[str], *, emit_flags: bool = True) -> None:
    """Mirror ``CPU8086.alu`` for a compile-time-constant group.

    ``emit_flags=False`` is the de-carrier's dead-flag elision
    (analyze.dead_flag_sites proved nothing can observe this site's flags
    before a guaranteed overwrite): the result computation is kept, the flag
    call is dropped.  A CMP with dead flags is a complete no-op."""
    mask = 0xFFFF if bits == 16 else 0xFF
    if group == 7 and not emit_flags:        # cmp, flags dead: no-op
        out.append("pass  # cmp (flags dead)")
        return
    out.append(f"_a = {a}")
    out.append(f"_b = {b}")
    if group in (0, 2):                      # add / adc
        if group == 2:
            out.append("_c = 1 if s.flags & CF else 0")
            out.append("_r = _a + _b + _c")
            if emit_flags:
                out.append(f"cpu.set_add_flags(_a, _b, _r, {bits}, _c)")
        else:
            out.append("_r = _a + _b")
            if emit_flags:
                out.append(f"cpu.set_add_flags(_a, _b, _r, {bits})")
    elif group in (3, 5, 7):                 # sbb / sub / cmp
        if group == 3:
            out.append("_c = 1 if s.flags & CF else 0")
            out.append("_r = _a - _b - _c")
            if emit_flags:
                out.append(f"cpu.set_sub_flags(_a, _b, _r, {bits}, _c)")
        else:
            out.append("_r = _a - _b")
            if emit_flags:
                out.append(f"cpu.set_sub_flags(_a, _b, _r, {bits})")
    else:                                    # or / and / xor
        opsym = {1: "|", 4: "&", 6: "^"}[group]
        out.append(f"_r = _a {opsym} _b")
        if emit_flags:
            out.append(f"cpu.set_logic_flags(_r, {bits})")
    if group != 7 and dst is not None:       # cmp writes nothing
        out.extend(dst.write(f"_r & 0x{mask:X}"))


def _patched_read(inst: Inst) -> str | None:
    """The live-memory read expression for a de-SMC'd operand, or ``None``.

    ``inst.patched_slot = (kind, field_addr, field_size)`` is attached by the
    de-SMC emit path (``liftemit --desmc``, from the IR's ``smc`` verdicts --
    dos_re.lift.smc): the operand field's bytes are RUNTIME-PATCHED code, so
    the emitted line reads them from memory -- exactly what the real CPU
    decodes at that moment -- instead of freezing one snapshot's constant."""
    slot = getattr(inst, "patched_slot", None)
    if slot is None or slot[0] != "imm":
        return None
    _, addr, size = slot
    return (f"mem.rb(s.cs, 0x{addr:04X})" if size == 1
            else f"mem.rw(s.cs, 0x{addr:04X})")


def _imm_txt(inst: Inst) -> str:
    """The immediate operand as emitted text: the constant, or the de-SMC
    live-memory read when this instruction's imm field is runtime-patched."""
    return _patched_read(inst) or f"0x{inst.imm:X}"


def _emit_instruction(inst: Inst, cs: int, out: list[str], *,
                      drop_flags: bool = False) -> bool:
    """Append the native Python for ``inst``. Return False to request the
    interpreter fallback (never for a control transfer).  ``drop_flags``
    (from analyze.dead_flag_sites) elides the flag-write line at sites whose
    flags are provably unobservable — only for the instruction families that
    emit flags as a separable line (ALU, TEST, INC/DEC)."""
    op = inst.op
    tmp = "_o"
    ef = not drop_flags

    # --- ALU r/m,reg and reg,r/m -------------------------------------------
    if op < 0x40 and (op & 0x04) == 0 and (op & 0x07) in (0, 1, 2, 3):
        group = (op >> 3) & 7
        bits = 8 if (op & 1) == 0 else 16
        to_reg = bool(op & 2)
        rm = _rm_operand(inst, bits, out, tmp)
        regv = f"s.{REG16[inst.reg]}" if bits == 16 else f"cpu.get_reg8({inst.reg})"
        reg_dst = Operand(True, bits, "", reg_idx=inst.reg)
        if to_reg:
            _alu_lines(group, bits, regv, rm.read(), reg_dst, out,
                       emit_flags=ef)
        else:
            _alu_lines(group, bits, rm.read(), regv, rm, out, emit_flags=ef)
        return True

    # --- ALU acc,imm --------------------------------------------------------
    if op < 0x40 and (op & 0x07) in (4, 5):
        group = (op >> 3) & 7
        bits = 8 if (op & 1) == 0 else 16
        acc = Operand(True, bits, "", reg_idx=0)
        _alu_lines(group, bits, acc.read(), _imm_txt(inst), acc, out,
                   emit_flags=ef)
        return True

    # --- ALU r/m,imm (80/81/83) --------------------------------------------
    if op in (0x80, 0x81, 0x82, 0x83):
        bits = 8 if op in (0x80, 0x82) else 16
        group = inst.reg
        rm = _rm_operand(inst, bits, out, tmp)
        if op == 0x83:                        # imm8 sign-extended to 16
            imm = inst.imm - 0x100 if inst.imm & 0x80 else inst.imm
            imm &= 0xFFFF
        else:
            imm = inst.imm
        _alu_lines(group, bits, rm.read(), f"0x{imm:X}", rm, out,
                   emit_flags=ef)
        return True

    # --- MOV ---------------------------------------------------------------
    if op in (0x88, 0x89, 0x8A, 0x8B):
        bits = 8 if op in (0x88, 0x8A) else 16
        to_reg = bool(op & 2)
        rm = _rm_operand(inst, bits, out, tmp)
        reg = Operand(True, bits, "", reg_idx=inst.reg)
        out.extend(reg.write(rm.read()) if to_reg else rm.write(reg.read()))
        return True
    if op == 0x8C:                            # mov r/m16, sreg
        rm = _rm_operand(inst, 16, out, tmp)
        out.extend(rm.write(f"s.{SREG[inst.reg & 3]}"))
        return True
    if op == 0x8E:                            # mov sreg, r/m16
        rm = _rm_operand(inst, 16, out, tmp)
        out.append(f"cpu.set_sreg({inst.reg & 3}, {rm.read()})")
        return True
    if op in (0xA0, 0xA1, 0xA2, 0xA3):        # mov acc <-> moffs
        seg = f"s.{inst.seg_override or 'ds'}"
        off = f"0x{inst.imm:04X}"
        if op == 0xA0:
            out.append(f"s.ax = (s.ax & 0xFF00) | mem.rb({seg}, {off})")
        elif op == 0xA1:
            out.append(f"s.ax = mem.rw({seg}, {off})")
        elif op == 0xA2:
            out.append(f"mem.wb({seg}, {off}, s.ax & 0xFF)")
        else:
            out.append(f"mem.ww({seg}, {off}, s.ax)")
        return True
    if 0xB0 <= op <= 0xB7:
        out.append(f"cpu.set_reg8({op - 0xB0}, {_imm_txt(inst)})")
        return True
    if 0xB8 <= op <= 0xBF:
        out.append(f"s.{REG16[op - 0xB8]} = {_imm_txt(inst)}")
        return True
    if op in (0xC6, 0xC7):
        bits = 8 if op == 0xC6 else 16
        rm = _rm_operand(inst, bits, out, tmp)
        out.extend(rm.write(f"0x{inst.imm:X}"))
        return True

    # --- TEST --------------------------------------------------------------
    if op in (0x84, 0x85):
        if drop_flags:
            out.append("pass  # test (flags dead)")
            return True
        bits = 8 if op == 0x84 else 16
        rm = _rm_operand(inst, bits, out, tmp)
        regv = f"s.{REG16[inst.reg]}" if bits == 16 else f"cpu.get_reg8({inst.reg})"
        out.append(f"cpu.set_logic_flags({rm.read()} & {regv}, {bits})")
        return True
    if op in (0xA8, 0xA9):
        if drop_flags:
            out.append("pass  # test (flags dead)")
            return True
        bits = 8 if op == 0xA8 else 16
        acc = Operand(True, bits, "", reg_idx=0)
        out.append(f"cpu.set_logic_flags({acc.read()} & 0x{inst.imm:X}, {bits})")
        return True

    # --- INC/DEC -----------------------------------------------------------
    if 0x40 <= op <= 0x4F:
        dec = op >= 0x48
        r = REG16[op & 7]
        if drop_flags:
            out.append(f"s.{r} = (s.{r} {'-' if dec else '+'} 1) & 0xFFFF")
            return True
        out.append(f"_old = s.{r}")
        out.append(f"_r = (_old {'-' if dec else '+'} 1) & 0xFFFF")
        out.append(f"cpu.set_incdec_flags(_old, _r, 16, dec={dec})")
        out.append(f"s.{r} = _r")
        return True
    if op in (0xFE, 0xFF) and inst.reg in (0, 1):
        bits = 8 if op == 0xFE else 16
        dec = inst.reg == 1
        mask = 0xFFFF if bits == 16 else 0xFF
        rm = _rm_operand(inst, bits, out, tmp)
        out.append(f"_old = {rm.read()}")
        out.append(f"_r = (_old {'-' if dec else '+'} 1) & 0x{mask:X}")
        if not drop_flags:
            out.append(f"cpu.set_incdec_flags(_old, _r, {bits}, dec={dec})")
        out.extend(rm.write("_r"))
        return True

    # --- PUSH/POP ----------------------------------------------------------
    if 0x50 <= op <= 0x57:
        out.append(f"cpu.push(s.{REG16[op - 0x50]})")
        return True
    if 0x58 <= op <= 0x5F:
        out.append(f"s.{REG16[op - 0x58]} = cpu.pop()")
        return True
    if op in (0x06, 0x0E, 0x16, 0x1E):
        out.append(f"cpu.push(s.{SREG[(op >> 3) & 3]})")
        return True
    if op in (0x07, 0x17, 0x1F):
        out.append(f"cpu.set_sreg({(op >> 3) & 3}, cpu.pop())")
        return True
    if op == 0x68:
        out.append(f"cpu.push({_imm_txt(inst)})")
        return True
    if op == 0x6A:
        pr = _patched_read(inst)
        if pr is not None:
            # push imm8 sign-extends to 16 -- at RUNTIME for a patched operand.
            out.append(f"_pi = {pr}")
            out.append("cpu.push((_pi | 0xFF00) if _pi & 0x80 else _pi)")
            return True
        imm = inst.imm - 0x100 if inst.imm & 0x80 else inst.imm
        out.append(f"cpu.push(0x{imm & 0xFFFF:04X})")
        return True
    if op == 0x8F:
        rm = _rm_operand(inst, 16, out, tmp)
        out.extend(rm.write("cpu.pop()"))
        return True
    if op == 0xFF and inst.reg == 6:
        rm = _rm_operand(inst, 16, out, tmp)
        out.append(f"cpu.push({rm.read()})")
        return True

    # --- XCHG / LEA --------------------------------------------------------
    if op in (0x86, 0x87):
        bits = 8 if op == 0x86 else 16
        rm = _rm_operand(inst, bits, out, tmp)
        reg = Operand(True, bits, "", reg_idx=inst.reg)
        out.append(f"_a = {reg.read()}")
        out.append(f"_b = {rm.read()}")
        out.extend(reg.write("_b"))
        out.extend(rm.write("_a"))
        return True
    if 0x91 <= op <= 0x97:                    # xchg ax, r16  (0x90 is nop)
        r = REG16[op - 0x90]
        out.append(f"s.ax, s.{r} = s.{r}, s.ax")
        return True
    if op == 0x90:
        out.append("pass  # nop")
        return True
    if op == 0x8D:                            # lea
        if inst.mod == 3:
            raise EmitUnsupported("lea with register source")
        rm = _rm_operand(inst, 16, out, tmp)
        out.append(f"s.{REG16[inst.reg]} = {rm.off_var}")
        return True
    if op in (0xC4, 0xC5):                    # les / lds — load far pointer
        # Mirror CPU8086 op 0xC4/0xC5: read the 16:16 pointer at the r/m
        # address, put the offset in the reg and the segment in ES (les) / DS
        # (lds).  Reg-source form is illegal (no memory operand to point at).
        if inst.mod == 3:
            raise EmitUnsupported("les/lds requires memory source")
        rm = _rm_operand(inst, 16, out, tmp)
        out.append(f"_off = mem.rw({rm.seg_expr}, {rm.off_var})")
        out.append(f"_seg = mem.rw({rm.seg_expr}, ({rm.off_var} + 2) & 0xFFFF)")
        out.append(f"s.{REG16[inst.reg]} = _off")
        out.append(f"s.{'es' if op == 0xC4 else 'ds'} = _seg")
        return True

    # --- shifts / rotates: delegate the intricate part to cpu.shift ---------
    if op in (0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3):
        bits = 8 if op in (0xC0, 0xD0, 0xD2) else 16
        rm = _rm_operand(inst, bits, out, tmp)
        if op in (0xD0, 0xD1):
            count = "1"
        elif op in (0xD2, 0xD3):
            count = "s.cx & 0xFF"
        else:
            count = f"0x{inst.imm:02X}"
        out.append(f"_r = cpu.shift({inst.reg}, {rm.read()}, {count}, {bits})")
        out.extend(rm.write("_r"))
        return True

    # --- PUSHA / POPA (80186+) ----------------------------------------------
    if op == 0x60:                            # pusha: AX CX DX BX origSP BP SI DI
        out.append("_sp0 = s.sp")
        for r in ("s.ax", "s.cx", "s.dx", "s.bx", "_sp0", "s.bp", "s.si", "s.di"):
            out.append(f"cpu.push({r})")
        return True
    if op == 0x61:                            # popa: DI SI BP (skip SP) BX DX CX AX
        out.append("s.di = cpu.pop()")
        out.append("s.si = cpu.pop()")
        out.append("s.bp = cpu.pop()")
        out.append("cpu.pop()  # discard the saved SP")
        out.append("s.bx = cpu.pop()")
        out.append("s.dx = cpu.pop()")
        out.append("s.cx = cpu.pop()")
        out.append("s.ax = cpu.pop()")
        return True

    # --- IMUL r16, r/m16, imm (80186+) --------------------------------------
    if op in (0x69, 0x6B):
        # Mirror CPU8086 op 0x69/0x6B: signed 16x16 multiply of r/m16 by the
        # sign-extended immediate; low word to the reg, CF=OF=overflow-out-of-16.
        rm = _rm_operand(inst, 16, out, tmp)
        if op == 0x6B:
            imm = inst.imm - 0x100 if inst.imm & 0x80 else inst.imm
        else:
            imm = inst.imm - 0x10000 if inst.imm & 0x8000 else inst.imm
        out.append(f"_a = {rm.read()}")
        out.append("_a = _a - 0x10000 if _a & 0x8000 else _a")
        out.append(f"_r = _a * {imm}")
        out.append(f"s.{REG16[inst.reg]} = _r & 0xFFFF")
        out.append("_c = not (-32768 <= _r <= 32767)")
        out.append("cpu.set_flag(CF, _c)")
        out.append("cpu.set_flag(OF, _c)")
        return True

    # --- ENTER / LEAVE (80186+; every MSC Win16 prologue/epilogue) -----------
    if op == 0xC8:
        # Mirror CPU8086 op 0xC8.  imm is 3 bytes (alloc16 + nesting8), which
        # the decoder leaves in raw; the flat nesting==0 form is the only one
        # compilers emit — the nested form falls back to the interpreter.
        alloc = inst.raw[-3] | (inst.raw[-2] << 8)
        nesting = inst.raw[-1] & 0x1F
        if nesting:
            return False                      # rare nested frame: interp_one
        out.append("cpu.push(s.bp & 0xFFFF)")
        out.append("s.bp = s.sp & 0xFFFF")
        if alloc:
            out.append(f"s.sp = (s.sp - 0x{alloc:X}) & 0xFFFF")
        return True
    if op == 0xC9:                            # leave: SP=BP, pop BP
        out.append("s.sp = s.bp")
        out.append("s.bp = cpu.pop()")
        return True

    # --- misc simple -------------------------------------------------------
    if op == 0x27:                            # daa — decimal adjust after BCD add
        out.append("cpu.daa()")               # single source of truth: CPU8086.daa
        return True
    if op == 0x9F:                            # lahf — mirror CPU8086 op 0x9F
        out.append("s.ax = (s.ax & 0x00FF) | (((s.flags & 0xD5) | 0x02) << 8)")
        return True
    if op == 0x9E:                            # sahf — mirror CPU8086 op 0x9E
        out.append("s.flags = (s.flags & ~0xD5) | ((s.ax >> 8) & 0xD5) | 0x0002")
        return True
    if op == 0x98:
        out.append("_al = s.ax & 0x00FF")
        out.append("s.ax = _al | (0xFF00 if _al & 0x80 else 0x0000)")
        return True
    if op == 0x99:
        out.append("s.dx = 0xFFFF if s.ax & 0x8000 else 0x0000")
        return True
    if op == 0x9C:
        out.append("cpu.push(s.flags)")
        return True
    if op == 0x9D:
        out.append("s.flags = cpu.pop() | 0x0002")
        return True
    # --- flag ops: mirror cpu.set_flag exactly (sets bit1, masks to 0x0FFF) --
    if op in (0xF5, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD):
        if op == 0xF5:                        # cmc
            out.append("cpu.set_flag(CF, not cpu.get_flag(CF))")
        else:
            flag, val = {
                0xF8: ("CF", "False"), 0xF9: ("CF", "True"),   # clc / stc
                0xFA: ("IF", "False"), 0xFB: ("IF", "True"),   # cli / sti
                0xFC: ("DF", "False"), 0xFD: ("DF", "True"),   # cld / std
            }[op]
            out.append(f"cpu.set_flag({flag}, {val})")
        return True

    # --- IN/OUT: route through cpu.port_reader/port_writer, same as execute_opcode
    if op in (0xE4, 0xE5, 0xEC, 0xED):        # in al/ax, imm8 / dx
        port = f"0x{inst.imm:02X}" if op in (0xE4, 0xE5) else "s.dx"
        bits = 8 if op in (0xE4, 0xEC) else 16
        out.append(f"_val = cpu.port_reader(cpu, ({port}) & 0xFFFF, {bits}) "
                   "if cpu.port_reader else 0")
        if bits == 8:
            out.append("cpu.set_reg8(0, _val)")
        else:
            out.append("s.ax = _val & 0xFFFF")
        return True
    if op in (0xE6, 0xE7, 0xEE, 0xEF):        # out imm8 / dx, al/ax
        port = f"0x{inst.imm:02X}" if op in (0xE6, 0xE7) else "s.dx"
        bits = 8 if op in (0xE6, 0xEE) else 16
        out.append("_val = cpu.get_reg8(0)" if bits == 8 else "_val = s.ax")
        out.append("if cpu.port_writer:")
        out.append(f"    cpu.port_writer(cpu, ({port}) & 0xFFFF, _val, {bits})")
        return True

    # --- Group 3 unary (F6/F7): test/not/neg/mul/imul/div/idiv ---------------
    # Mirror cpu.py's Group-3 block exactly; call cpu's own helpers/flag setters
    # and reproduce its divide-by-zero and quotient-overflow raises.
    if op in (0xF6, 0xF7) and inst.reg != 1:  # reg==1 is undefined -> fallback
        bits = 8 if op == 0xF6 else 16
        mask = 0xFFFF if bits == 16 else 0xFF
        reg = inst.reg
        rm = _rm_operand(inst, bits, out, tmp)
        out.append(f"_v = {rm.read()}")
        if reg == 0:                          # test
            out.append(f"cpu.set_logic_flags(_v & 0x{inst.imm:X}, {bits})")
        elif reg == 2:                        # not
            out.extend(rm.write(f"(~_v) & 0x{mask:X}"))
        elif reg == 3:                        # neg
            out.append(f"cpu.set_sub_flags(0, _v, -_v, {bits})")
            out.extend(rm.write(f"(-_v) & 0x{mask:X}"))
        elif reg == 4:                        # mul (unsigned)
            if bits == 8:
                out.append("_result = (s.ax & 0x00FF) * (_v & 0xFF)")
                out.append("s.ax = _result & 0xFFFF")
                out.append("_carry = (_result >> 8) != 0")
            else:
                out.append("_result = (s.ax & 0xFFFF) * (_v & 0xFFFF)")
                out.append("s.ax = _result & 0xFFFF")
                out.append("s.dx = (_result >> 16) & 0xFFFF")
                out.append("_carry = s.dx != 0")
            out.append("cpu.set_flag(CF, _carry)")
            out.append("cpu.set_flag(OF, _carry)")
        elif reg == 5:                        # imul (signed)
            if bits == 8:
                out.append("_result = cpu.sign8(s.ax & 0xFF) * cpu.sign8(_v & 0xFF)")
                out.append("s.ax = _result & 0xFFFF")
                out.append("_carry = not (-128 <= _result <= 127)")
            else:
                out.append("_result = cpu.sign16(s.ax) * cpu.sign16(_v)")
                out.append("s.ax = _result & 0xFFFF")
                out.append("s.dx = (_result >> 16) & 0xFFFF")
                out.append("_carry = not (-32768 <= _result <= 32767)")
            out.append("cpu.set_flag(CF, _carry)")
            out.append("cpu.set_flag(OF, _carry)")
        elif reg == 6:                        # div (unsigned)
            out.append("if _v == 0:")
            out.append("    raise ZeroDivisionError('div by zero')")
            if bits == 8:
                out.append("_q, _r = divmod(s.ax & 0xFFFF, _v & 0xFF)")
                out.append("if _q > 0xFF:")
                out.append("    raise OverflowError('8-bit div quotient overflow')")
                out.append("s.ax = ((_r & 0xFF) << 8) | (_q & 0xFF)")
            else:
                out.append("_q, _r = divmod(((s.dx & 0xFFFF) << 16) | (s.ax & 0xFFFF), _v & 0xFFFF)")
                out.append("if _q > 0xFFFF:")
                out.append("    raise OverflowError('16-bit div quotient overflow')")
                out.append("s.ax = _q & 0xFFFF")
                out.append("s.dx = _r & 0xFFFF")
        else:                                 # reg == 7: idiv (signed)
            out.append("if _v == 0:")
            out.append("    raise ZeroDivisionError('idiv by zero')")
            if bits == 8:
                out.append("_dividend = cpu.sign16(s.ax)")
                out.append("_divisor = cpu.sign8(_v & 0xFF)")
                out.append("_q = int(_dividend / _divisor)")
                out.append("_r = _dividend - _q * _divisor")
                out.append("if _q < -128 or _q > 127:")
                out.append("    raise OverflowError('8-bit idiv quotient overflow')")
                out.append("s.ax = ((_r & 0xFF) << 8) | (_q & 0xFF)")
            else:
                out.append("_dividend = ((s.dx & 0xFFFF) << 16) | (s.ax & 0xFFFF)")
                out.append("if _dividend & 0x80000000:")
                out.append("    _dividend -= 0x100000000")
                out.append("_divisor = cpu.sign16(_v & 0xFFFF)")
                out.append("_q = int(_dividend / _divisor)")
                out.append("_r = _dividend - _q * _divisor")
                out.append("if _q < -32768 or _q > 32767:")
                out.append("    raise OverflowError('16-bit idiv quotient overflow')")
                out.append("s.ax = _q & 0xFFFF")
                out.append("s.dx = _r & 0xFFFF")
        return True
    if op == 0xD7:                            # xlat
        seg = f"s.{inst.seg_override or 'ds'}"
        out.append(f"cpu.set_reg8(0, mem.rb({seg}, (s.bx + (s.ax & 0xFF)) & 0xFFFF))")
        return True

    # --- x87 ESC (D8-DF): shared-semantics delegation ------------------------
    # FP semantics live in ONE place — the interpreter's execute_fpu, factored
    # into cpu.fpu_reg_op (mod==3) and cpu.fpu_mem_op (memory forms with a
    # pre-computed EA).  The emitted line computes the effective address
    # natively (identical to decode_ea, seg overrides included) and calls the
    # SAME helper the interpreter dispatches to: zero drift, including the
    # doubles-for-80-bit precision caveat, the _f80_to_double/_double_to_f80
    # conversions, and the UnsupportedInstruction refusals (FP-stack over/
    # underflow and any form execute_fpu does not implement fail loud on both
    # paths, never guess).
    if 0xD8 <= op <= 0xDF:
        if inst.mod == 3:
            out.append(f"cpu.fpu_reg_op(0x{op:02X}, {inst.reg}, {inst.rm})")
        else:
            rm = _rm_operand(inst, 16, out, tmp)
            out.append(f"cpu.fpu_mem_op(0x{op:02X}, {inst.reg}, "
                       f"{rm.seg_expr}, {rm.off_var})")
        return True
    if op == 0x9B:                            # wait/fwait — mirror cpu op 0x9B:
        out.append("pass  # wait (no coprocessor exceptions modelled)")
        return True

    # --- string ops: reuse the interpreter's own (IP-independent) primitive -
    # cpu.string_op reads nothing from the instruction stream — it takes the
    # already-decoded opcode, the F2/F3 prefix and the segment override — so a
    # direct call reproduces it byte-exactly (incl. DF direction, REP count and
    # the bulk MOVS/STOS fast path) while staying a single refactorable line.
    if op in (0xA4, 0xA5, 0xA6, 0xA7, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF,
              0x6C, 0x6D, 0x6E, 0x6F):
        rep = f"0x{inst.rep:02X}" if inst.rep is not None else "None"
        seg = f"{inst.seg_override!r}" if inst.seg_override else "None"
        out.append(f"cpu.string_op(0x{op:02X}, {rep}, {seg})")
        return True

    return False                              # -> interpreter fallback


def _terminator_lines(inst: Inst, cs: int, bb_of: dict[int, int], out: list[str],
                      indent: str) -> None:
    """Emit control flow for a block's last instruction."""
    kind = inst.kind
    nxt = inst.next_ip
    if kind == JCC:
        t, f = bb_of[inst.target], bb_of[nxt]
        op = inst.op
        if op in _JCC_COND:                   # LOOP/LOOPZ/LOOPNZ/JCXZ
            if op != 0xE3:
                out.append(f"{indent}s.cx = (s.cx - 1) & 0xFFFF")
            cond = {
                0xE0: "s.cx != 0 and not (s.flags & ZF)",
                0xE1: "s.cx != 0 and (s.flags & ZF)",
                0xE2: "s.cx != 0",
                0xE3: "s.cx == 0",
            }[op]
            out.append(f"{indent}bb = {t} if ({cond}) else {f}")
        else:
            out.append(f"{indent}bb = {t} if cpu.condition(0x{inst.op & 0xF:X}) else {f}")
    elif kind == JMP:
        out.append(f"{indent}bb = {bb_of[inst.target]}")
    elif kind == RET:
        out.append(f"{indent}s.ip = cpu.pop()")
        if inst.imm:
            out.append(f"{indent}s.sp = (s.sp + 0x{inst.imm:X}) & 0xFFFF")
        out.append(f"{indent}return")
    elif kind == RETF:
        out.append(f"{indent}s.ip = cpu.pop()")
        out.append(f"{indent}s.cs = cpu.pop()")
        if inst.imm:
            out.append(f"{indent}s.sp = (s.sp + 0x{inst.imm:X}) & 0xFFFF")
        out.append(f"{indent}return")
    elif kind == IRET:
        out.append(f"{indent}s.ip = cpu.pop()")
        out.append(f"{indent}s.cs = cpu.pop()")
        out.append(f"{indent}s.flags = cpu.pop() | 0x0002")
        out.append(f"{indent}return")
    elif kind == JMP_FAR:
        slot = getattr(inst, "patched_slot", None)
        if slot is not None and slot[0] == "far-target":
            # The ptr16:16 is RUNTIME-PATCHED code (de-SMC, dos_re.lift.smc):
            # read the live target words -- through the OLD cs, before any
            # assignment -- exactly as the CPU would decode them.
            _, addr, _size = slot
            out.append(f"{indent}_ti = mem.rw(s.cs, 0x{addr:04X})")
            out.append(f"{indent}_ts = mem.rw(s.cs, 0x{(addr + 2) & 0xFFFF:04X})")
            out.append(f"{indent}s.cs, s.ip = _ts, _ti")
            out.append(f"{indent}return")
        else:
            seg, off = inst.far_target
            out.append(f"{indent}s.cs, s.ip = 0x{seg:04X}, 0x{off:04X}")
            out.append(f"{indent}return")
    elif kind == JMP_IND:
        # Tail exit (the 32-bit pipeline's treatment): compute the runtime
        # target, set CS:IP, hand control back to the VM.  A dispatcher lifts
        # as prologue + tail transfer; the cases stay interpreted and any hook
        # installed at them dispatches normally.
        setup: list[str] = []
        rm = _rm_operand(inst, 16, setup, "_o")
        for ln in setup:
            out.append(indent + ln)
        if inst.reg == 5:                     # jmp far [mem]: offset then segment
            out.append(f"{indent}s.ip = mem.rw({rm.seg_expr}, _o)")
            out.append(f"{indent}s.cs = mem.rw({rm.seg_expr}, (_o + 2) & 0xFFFF)")
        else:                                  # jmp near r/m16
            out.append(f"{indent}s.ip = {rm.read()} & 0xFFFF")
        out.append(f"{indent}return")
    else:
        raise EmitUnsupported(f"terminator {kind} at {inst.ip:04X}")


def emit_function(scan: FunctionScan, cs: int, name: str, *,
                  signature: bytes, count_instructions: bool = False,
                  coverage: bool = False, min_iterations: int | None = None,
                  link_map: dict[int, str] | None = None,
                  far_link_map: dict[tuple[int, int], str] | None = None,
                  dead_flag_ips: frozenset = frozenset(),
                  boundary_heads: frozenset = frozenset(),
                  dispatch_entries: frozenset = frozenset(),
                  resume_calls: bool = False,
                  link_imports: tuple[str, ...] = ()) -> str:
    """Return the source of a module defining the lifted hook ``name``.

    ``coverage`` adds a module-level ``BLOCKS_SEEN`` set that records which
    basic blocks actually executed, plus ``BLOCK_COUNT`` and ``coverage()`` —
    so a verify run can report *which paths* were exercised, not just that the
    hook passed (docs/lifting_design.md §7). It is inert otherwise.

    ``link_map`` is THE LINKER SEAM (the recovery pipeline's de-VM step): a
    ``{near_call_target_ip: python_callable_expr}`` map. A direct near CALL to
    a mapped target emits ``call_installed_hook_like_near_call(cpu, key,
    <callee>, ret_ip)`` instead of ``emulate_call`` — original CALL/RET stack
    semantics, no interpreter in the path, and the child remains a
    verifier-visible boundary in the hybrid (pitfall #5). Only near-RET-exit
    callees are safe to link — plus all-``retf`` callees whose every call
    site is the ``push cs; call near`` idiom, where the caller's own emitted
    ``push cs`` supplies the segment word the retf pops (the LINK TOOL
    enforces both preconditions; tail exits / iret / mixed-exit callees must
    stay ``emulate_call``).  ``far_link_map``
    is the FAR mirror: ``{(seg, off): python_callable_expr}`` — a direct far
    CALL to a mapped target emits ``call_installed_hook_like_far_call`` (far
    return frame; only all-``retf``-exit callees qualify, again enforced by
    the link tool).

    ``count_instructions`` is VIRTUAL-TIME PRESERVATION: each block advances
    ``cpu.instruction_count`` by exactly the original instructions it
    replaces (transfer instructions count 1; callees/ISRs/fallbacks count
    themselves through the interpreter), and the module marks its function
    ``owns_time = True`` so ``step()`` skips its own +1 for the dispatch.
    With it, instruction count — and everything the machine models derive
    from it (PIT reads, any time-keyed observable) — is IDENTICAL between
    the interpreted oracle and the lifted graph.  It composes with
    ``link_map`` when the whole corpus is emitted counting (a linked callee
    accounts for itself); a MIXED corpus (counting callers linking
    non-counting callees) would under-count, which is why the assembly
    pipeline turns it on for every module or none.
    ``link_imports`` lines are appended to the module header verbatim — the
    link tool supplies its cross-module binding there (e.g. a module-level
    ``LINKS = {"CS:IP": None}`` table that ``dos_re.lift.install.resolve_links``
    fills at install time; emitted modules are loaded flat via
    ``spec_from_file_location``, so relative imports are not available).

    ``min_iterations`` raises the runaway guard's floor above the default
    10,000 (the guard is still at least ``len(scan.insts) * 5_000`` either
    way). A data-driven loop over a large real dataset can legitimately need
    more block-transitions than a small function's instruction count alone
    would predict — e.g. a per-boot buffer-init loop iterating hundreds of
    records, each a handful of blocks. Raise this rather than treat a hit
    guard as proof of a decode bug; the static census (``scan_function``)
    already cross-checks every instruction length against live execution.
    """
    leaders = scan.block_leaders()
    # BOUNDARY OBSERVERS (docs/history/dos_re_2.0.md §1a — the VMless wall): each
    # ``boundary_heads`` ip gets an emitted event call after its instruction,
    # and the instruction AFTER the head becomes a RESUME ENTRY — a block
    # leader exported in RESUME_ENTRIES so the installer can register a
    # re-entry hook there.  A boundary park raised by the runtime hook then
    # resumes INSIDE the lifted body: boundary observation without a single
    # interpreted instruction (boundary-shadowing dissolved).
    heads = frozenset(h for h in boundary_heads if h in scan.insts)
    dispatch = frozenset(d for d in dispatch_entries if d in scan.insts)
    resume_points: set[int] = set()
    forced = set(leaders)
    # THE BLOCKING-INT RE-ENTRY RULE.  Unconditional, and deliberately not
    # gated on boundary observation: an INT is a SYNCHRONOUS UNWIND POINT in
    # its own right.  The host-service convention (dos.py: a console read with
    # nothing queued does ``cpu.s.ip -= 2; raise ConsoleInputWouldBlock``) puts
    # IP back ON the INT and expects the RETRY to re-execute it — exactly what
    # the interpreter would do.  So the address the retry lands on is the INT
    # itself, never its successor, and the ``next_ip`` continuation the unwind
    # rule below records is the one address that CANNOT serve it.
    #
    # Without this a lifted body is a one-way door: the INT unwinds out of the
    # whole Python call chain and the resume finds no hook at the INT, so it
    # continues INTERPRETED — a VMless wall violation, and a silent divergence
    # anywhere the wall is not armed.  Found by skyroads' menu loop, whose
    # ``mov ah,07 / int 21h`` (1010:5FEB) blocks on every keypress wait.
    #
    # Forcing the INT as a leader is what makes re-entry FAITHFUL rather than
    # merely possible: the preceding ``mov ah,07`` stays in the previous block,
    # so resuming at the INT re-executes the INT alone, with AH as the guest
    # left it.  Cost when nothing ever blocks: one extra block boundary and one
    # unused hook.
    for inst in scan.insts.values():
        if inst.kind == INT:
            forced.add(inst.ip)
            resume_points.add(inst.ip)
    if heads or dispatch or resume_calls:
        for d in sorted(dispatch):
            # DYNAMIC DISPATCH ENTRY (recovery-IR concept, distinct from a
            # boundary/call/iret resume): an address reached from OUTSIDE via
            # indirect control flow.  Force it as a block leader and export it
            # so the installer registers a hook that re-enters THIS function's
            # dispatcher at that block — sharing the recovered blocks, not
            # cloning them into a new module.
            forced.add(d)
            resume_points.add(d)
        for h in sorted(heads):
            hi = scan.insts[h]
            if hi.kind not in (SEQ, CALL, CALL_FAR, CALL_IND, INT):
                raise EmitUnsupported(
                    f"boundary head {cs:04X}:{h:04X} is a control transfer "
                    f"({hi.kind}); register the head at a sequential "
                    f"instruction of the wait loop instead")
            nip = hi.next_ip
            if nip not in scan.insts:
                raise EmitUnsupported(
                    f"boundary head {cs:04X}:{h:04X} has no in-region "
                    f"successor -- a park there could not resume in host code")
            # The head is a re-entry point TOO, not just its successor. A park
            # leaves IP on the successor, so that is where a resumed park comes
            # back -- but it is not the only way to be here. The machine can sit
            # ON the head: a snapshot captured while the game spun in the wait
            # (skyroads' gameplay replays all start at 1010:22F8, mid-spin), or an
            # IRET returning to it after an IRQ landed on that instruction. Both
            # are ordinary; neither could dispatch, because the head was forced
            # as a block leader but never exported, so the wall fired at the one
            # address the game is most often found at.
            forced.add(h)
            resume_points.add(h)
            forced.add(nip)
            resume_points.add(nip)
        # THE UNWIND RE-ENTRY RULE: a boundary park unwinds the WHOLE lifted
        # Python call chain but resumes only the innermost function; every
        # OUTER frame later resumes through its guest return address — the
        # instruction after its call site.  Those continuations must
        # therefore be RESUME entries in EVERY function whose frame can be
        # abandoned (``resume_calls`` — set pipeline-wide whenever boundary
        # observation is on), or the abandoned callers would continue
        # INTERPRETED (a wall violation and a pass-count asymmetry).
        if resume_calls or heads:
            for inst in scan.insts.values():
                if (inst.kind in (CALL, CALL_FAR, CALL_IND, INT)
                        and inst.next_ip in scan.insts):
                    forced.add(inst.next_ip)
                    resume_points.add(inst.next_ip)
    leaders = sorted(forced)
    bb_of = {ip: i for i, ip in enumerate(leaders)}
    leader_set = set(leaders)

    # Slice the reachable set into basic blocks.
    blocks: list[list[Inst]] = []
    for leader in leaders:
        body: list[Inst] = []
        ip = leader
        while True:
            inst = scan.insts[ip]
            body.append(inst)
            if inst.kind != SEQ and inst.kind not in (CALL, CALL_FAR, CALL_IND, INT):
                break
            nxt = inst.next_ip
            if nxt in leader_set or nxt not in scan.insts:
                break
            ip = nxt
        blocks.append(body)

    L: list[str] = []
    A = L.append
    A('"""AUTOGENERATED by dos_re.lift -- literal lift. DO NOT hand-edit in place.')
    A("")
    A(f"Function {cs:04X}:{scan.entry:04X}  "
      f"({len(scan.insts)} instructions, {len(leaders)} basic blocks)")
    A("")
    A("Refactor freely: the oracle tests are the contract, not this text. Lines")
    A('marked "(interpreter fallback)" are instructions the emitter has no native')
    A("form for yet -- they are exact, but they are also the to-do list.")
    A('"""')
    A("from __future__ import annotations")
    A("")
    A("from dos_re.cpu import AF, CF, DF, IF, OF, PF, SF, ZF")
    A("from dos_re.hooks import self_disable_if_patched")
    if link_map:
        A("from dos_re.hooks import call_installed_hook_like_near_call")
    if far_link_map:
        A("from dos_re.hooks import call_installed_hook_like_far_call")
    A("from dos_re.lift.runtime import (LiftRuntimeError, emulate_call, emulate_far_call,")
    A("                                 stuck_error,")
    A("                                 emulate_int, interp_one)")
    if link_map or far_link_map:
        for ln in link_imports:
            A(ln)
    A("")
    A(f"ENTRY = (0x{cs:04X}, 0x{scan.entry:04X})")
    A(f"SIGNATURE = bytes.fromhex({signature.hex()!r})")
    # A BACKSTOP, not the guard. The no-progress detector below catches a real
    # spin in ~64K dispatches by PROVING it (same block, identical registers),
    # so this only has to stop the pathological rest: a loop that keeps changing
    # state yet never terminates. It must therefore be far above anything honest
    # code does.
    #
    # It used to be `len(insts) * 5_000`, which is not a bound on anything: a
    # loop's trip count has no relation to its instruction count. A 27-
    # instruction LZS decompressor got 135,000 -- and legitimately needs
    # millions -- so every port hit it and "fixed" it by raising a magic number
    # until the number stopped mattering. That is how a guard trains people to
    # ignore it.
    A(f"MAX_ITERATIONS = {min_iterations or 100_000_000}  "
      "# backstop only -- the no-progress detector below is the real guard")
    #: bb -> leader address, so a stuck report can name WHERE it is spinning
    #: instead of just "unbounded internal loop".
    A("#: dispatch block -> its leader address (for the stuck diagnosis)")
    A("BLOCK_ADDRS = {"
      + ", ".join(f"{bi}: 0x{body[0].ip:04X}" for bi, body in enumerate(blocks))
      + "}")
    #: How often the no-progress detector samples. A spin returns to the same
    #: (block, registers) every iteration, so it is caught at the 2nd sample;
    #: a decompressor's registers advance every iteration, so it never is.
    A("PROGRESS_SAMPLE = 0xFFFF  # sample the state every 64K dispatches")
    if resume_points:
        entries_txt = ", ".join(
            f'"{cs:04X}:{rp:04X}": {bb_of[rp]}'
            for rp in sorted(resume_points))
        A("#: re-entry points into this body: every INT (a blocking host service")
        A("#: rewinds IP onto it and retries), plus -- when boundary observation")
        A("#: is on -- head successors and call-site continuations (the unwind")
        A("#: re-entry rule). address -> dispatch block index; the installer")
        A("#: registers a re-entry hook at each.")
        A(f"RESUME_ENTRIES = {{{entries_txt}}}")
    if coverage:
        A("")
        A(f"BLOCK_COUNT = {len(leaders)}")
        A("BLOCKS_SEEN = set()  # basic blocks that actually executed")
        A("")
        A("")
        A("def coverage():")
        A('    """(len(seen), total) -- how much of the function a verify run exercised."""')
        A("    return len(BLOCKS_SEEN), BLOCK_COUNT")
    A("")
    A("")
    entry_bb = bb_of[scan.entry]
    A(f"def {name}(cpu, bb={entry_bb}):")
    A(f'    """Lifted replacement for {cs:04X}:{scan.entry:04X}."""')
    A(f"    # Fail loud if the entry bytes no longer match what was lifted")
    A(f"    # (self-modifying code / overlay swap): raises rather than running a")
    A(f"    # replacement for code that is no longer there.")
    A(f"    self_disable_if_patched(cpu, 0x{scan.entry:04X}, SIGNATURE, {name!r})")
    A("    s, mem = cpu.s, cpu.mem")
    # count_instructions needs no entry compensation: the function is marked
    # owns_time, so step() skips its dispatch +1 (and a LINKED call never had
    # one) — the per-block adds are the complete, exact account.
    # Start at the ENTRY block, which is not necessarily block index 0:
    # block_leaders() sorts by address, and a region can contain branch
    # targets BELOW the entry (a backward jump above the function head).
    # Hardcoding 0 executed the lowest-address block first — wrong code, at
    # the wrong time, for that entry class (found by the Lemmings pilot's
    # whole-program census; caught in-situ as guaranteed DIVERGED lifts).
    A("    # A lifted function runs SYNCHRONOUSLY to completion: the interpreter")
    A("    # interleaves the outside world between instructions, but nothing")
    A("    # external can happen inside a lifted body -- so a loop WAITING for the")
    A("    # world to change waits forever. Detect that precisely rather than")
    A("    # guessing from an iteration count: a spin returns to the same block")
    A("    # with IDENTICAL registers, which is provably no progress; a long but")
    A("    # honest loop (a decompressor) advances its registers and is never")
    A("    # reported. MAX_ITERATIONS remains only as a backstop.")
    A("    _last_snap = None")
    A("    for _guard in range(MAX_ITERATIONS):")
    A("        if not _guard & PROGRESS_SAMPLE:")
    A("            _snap = (bb, s.ax, s.bx, s.cx, s.dx, s.si, s.di,")
    A("                     s.bp, s.sp, s.ds, s.es, s.flags)")
    A("            if _snap == _last_snap:")
    A("                raise stuck_error(__name__, cpu, bb, BLOCK_ADDRS,")
    A("                                  iterations=_guard,")
    A("                                  no_progress=PROGRESS_SAMPLE + 1)")
    A("            _last_snap = _snap")

    ind = " " * 12
    for bi, body in enumerate(blocks):
        A(f"        {'if' if bi == 0 else 'elif'} bb == {bi}:")
        lines: list[str] = []
        if coverage:
            lines.append(f"BLOCKS_SEEN.add({bi})")
        # Virtual time: instructions executed natively (i.e. NOT re-entered
        # through step(), which does its own accounting).  Calls/INTs count 1
        # for the transfer instruction itself; their callees are counted by
        # the VM or by their own counted module.  The pending count is FLUSHED
        # BEFORE every call-family line: a callee (or an emulated interpreter
        # stretch under it) may unwind out of this function at a boundary park
        # (tick_clock._BoundaryReached), and everything already executed —
        # including the transfer instruction whose stack effect the helper
        # performs up front — must be on the clock by then, exactly as the
        # interpreted oracle would have counted it.
        pending = [0]

        def _flush() -> None:
            if count_instructions and pending[0]:
                lines.append(f"cpu.instruction_count += {pending[0]}")
                pending[0] = 0

        term: Inst | None = None
        for inst in body:
            lines.append(f"# {cs:04X}:{inst.ip:04X}  {inst.raw.hex():<12} {inst.mnemonic}")
            if inst.kind == SEQ:
                body_lines: list[str] = []
                try:
                    ok = _emit_instruction(
                        inst, cs, body_lines,
                        drop_flags=inst.ip in dead_flag_ips)
                except EmitUnsupported as exc:
                    raise EmitUnsupported(f"{cs:04X}:{inst.ip:04X}: {exc}") from None
                if ok:
                    lines.extend(body_lines)
                    pending[0] += 1
                else:
                    lines.append(f"interp_one(cpu, 0x{cs:04X}, 0x{inst.ip:04X})"
                                 f"  # (interpreter fallback)")
            elif inst.kind == CALL:
                pending[0] += 1
                _flush()
                if link_map and inst.target in link_map:
                    # LINKED: direct native call — CALL/RET stack semantics
                    # preserved, verifier-visible child boundary, no VM.
                    lines.append(f"call_installed_hook_like_near_call(cpu, "
                                 f"(0x{cs:04X}, 0x{inst.target:04X}), "
                                 f"{link_map[inst.target]}, 0x{inst.next_ip:04X})")
                else:
                    lines.append(f"emulate_call(cpu, 0x{cs:04X}, 0x{inst.target:04X}, "
                                 f"0x{inst.next_ip:04X})")
            elif inst.kind == CALL_FAR:
                seg, off = inst.far_target
                pending[0] += 1
                _flush()
                if far_link_map and (seg, off) in far_link_map:
                    # LINKED far call — far return frame, direct native callee.
                    lines.append(f"call_installed_hook_like_far_call(cpu, "
                                 f"(0x{seg:04X}, 0x{off:04X}), "
                                 f"{far_link_map[(seg, off)]}, "
                                 f"0x{cs:04X}, 0x{inst.next_ip:04X})")
                else:
                    lines.append(f"emulate_far_call(cpu, 0x{seg:04X}, 0x{off:04X}, "
                                 f"0x{cs:04X}, 0x{inst.next_ip:04X})")
            elif inst.kind == CALL_IND:
                rm = _rm_operand(inst, 16, lines, "_o")
                pending[0] += 1
                _flush()
                if inst.reg == 3:             # call far [mem]
                    lines.append(f"_off = mem.rw({rm.seg_expr}, _o)")
                    lines.append(f"_seg = mem.rw({rm.seg_expr}, (_o + 2) & 0xFFFF)")
                    lines.append(f"emulate_far_call(cpu, _seg, _off, 0x{cs:04X}, "
                                 f"0x{inst.next_ip:04X})")
                else:                          # call near r/m16
                    lines.append(f"emulate_call(cpu, 0x{cs:04X}, {rm.read()}, "
                                 f"0x{inst.next_ip:04X})")
            elif inst.kind == INT:
                pending[0] += 1
                _flush()
                lines.append(f"emulate_int(cpu, 0x{inst.int_no:02X}, 0x{cs:04X}, "
                             f"0x{inst.next_ip:04X})")
            else:
                term = inst
                pending[0] += 1
                break
            if inst.ip in heads:
                # Boundary observer: count is flushed BEFORE the event so a
                # park (the hook raises) never loses executed instructions;
                # the hook re-points CS:IP at the resume entry when it parks.
                # ABI: (cpu, head_cs, head_ip, resume_ip) — the head identity
                # lets the clock apply per-head park costs (frame gates vs
                # pacing spins, input_waits.HEAD_KINDS).
                _flush()
                lines.append("if cpu.boundary_hook is not None:")
                lines.append(f"    cpu.boundary_hook(cpu, 0x{cs:04X}, "
                             f"0x{inst.ip:04X}, 0x{inst.next_ip:04X})")

        _flush()
        for ln in lines:
            A(ind + ln)

        if term is not None:
            _terminator_lines(term, cs, bb_of, L, ind)
        else:
            # Fell through into the next leader (a branch target).
            fall = body[-1].next_ip
            if fall not in bb_of:
                raise EmitUnsupported(
                    f"block at {cs:04X}:{body[0].ip:04X} falls out of the region")
            A(f"{ind}bb = {bb_of[fall]}")
    # The dispatch loop only exits via a block's `return`. Reaching here means
    # MAX_ITERATIONS blocks executed without returning — an unbounded internal
    # wait/spin. Fail loud instead of hanging.
    A("    raise stuck_error(__name__, cpu, bb, BLOCK_ADDRS,")
    A("                      iterations=MAX_ITERATIONS, no_progress=0)")
    if count_instructions:
        A("")
        A("")
        A("# Virtual-time preservation: this function accounts its own")
        A("# instruction_count per block, so step() must not add its dispatch +1.")
        A(f"{name}.owns_time = True")
    A("")
    return "\n".join(L) + "\n"
