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
                     RET, RETF, SEQ, Inst)

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
               out: list[str]) -> None:
    """Mirror ``CPU8086.alu`` for a compile-time-constant group."""
    mask = 0xFFFF if bits == 16 else 0xFF
    out.append(f"_a = {a}")
    out.append(f"_b = {b}")
    if group in (0, 2):                      # add / adc
        if group == 2:
            out.append("_c = 1 if s.flags & CF else 0")
            out.append("_r = _a + _b + _c")
            out.append(f"cpu.set_add_flags(_a, _b, _r, {bits}, _c)")
        else:
            out.append("_r = _a + _b")
            out.append(f"cpu.set_add_flags(_a, _b, _r, {bits})")
    elif group in (3, 5, 7):                 # sbb / sub / cmp
        if group == 3:
            out.append("_c = 1 if s.flags & CF else 0")
            out.append("_r = _a - _b - _c")
            out.append(f"cpu.set_sub_flags(_a, _b, _r, {bits}, _c)")
        else:
            out.append("_r = _a - _b")
            out.append(f"cpu.set_sub_flags(_a, _b, _r, {bits})")
    else:                                    # or / and / xor
        opsym = {1: "|", 4: "&", 6: "^"}[group]
        out.append(f"_r = _a {opsym} _b")
        out.append(f"cpu.set_logic_flags(_r, {bits})")
    if group != 7 and dst is not None:       # cmp writes nothing
        out.extend(dst.write(f"_r & 0x{mask:X}"))


def _emit_instruction(inst: Inst, cs: int, out: list[str]) -> bool:
    """Append the native Python for ``inst``. Return False to request the
    interpreter fallback (never for a control transfer)."""
    op = inst.op
    tmp = "_o"

    # --- ALU r/m,reg and reg,r/m -------------------------------------------
    if op < 0x40 and (op & 0x04) == 0 and (op & 0x07) in (0, 1, 2, 3):
        group = (op >> 3) & 7
        bits = 8 if (op & 1) == 0 else 16
        to_reg = bool(op & 2)
        rm = _rm_operand(inst, bits, out, tmp)
        regv = f"s.{REG16[inst.reg]}" if bits == 16 else f"cpu.get_reg8({inst.reg})"
        reg_dst = Operand(True, bits, "", reg_idx=inst.reg)
        if to_reg:
            _alu_lines(group, bits, regv, rm.read(), reg_dst, out)
        else:
            _alu_lines(group, bits, rm.read(), regv, rm, out)
        return True

    # --- ALU acc,imm --------------------------------------------------------
    if op < 0x40 and (op & 0x07) in (4, 5):
        group = (op >> 3) & 7
        bits = 8 if (op & 1) == 0 else 16
        acc = Operand(True, bits, "", reg_idx=0)
        _alu_lines(group, bits, acc.read(), f"0x{inst.imm:X}", acc, out)
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
        _alu_lines(group, bits, rm.read(), f"0x{imm:X}", rm, out)
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
        out.append(f"cpu.set_reg8({op - 0xB0}, 0x{inst.imm:02X})")
        return True
    if 0xB8 <= op <= 0xBF:
        out.append(f"s.{REG16[op - 0xB8]} = 0x{inst.imm:04X}")
        return True
    if op in (0xC6, 0xC7):
        bits = 8 if op == 0xC6 else 16
        rm = _rm_operand(inst, bits, out, tmp)
        out.extend(rm.write(f"0x{inst.imm:X}"))
        return True

    # --- TEST --------------------------------------------------------------
    if op in (0x84, 0x85):
        bits = 8 if op == 0x84 else 16
        rm = _rm_operand(inst, bits, out, tmp)
        regv = f"s.{REG16[inst.reg]}" if bits == 16 else f"cpu.get_reg8({inst.reg})"
        out.append(f"cpu.set_logic_flags({rm.read()} & {regv}, {bits})")
        return True
    if op in (0xA8, 0xA9):
        bits = 8 if op == 0xA8 else 16
        acc = Operand(True, bits, "", reg_idx=0)
        out.append(f"cpu.set_logic_flags({acc.read()} & 0x{inst.imm:X}, {bits})")
        return True

    # --- INC/DEC -----------------------------------------------------------
    if 0x40 <= op <= 0x4F:
        dec = op >= 0x48
        r = REG16[op & 7]
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
        out.append(f"cpu.push(0x{inst.imm:04X})")
        return True
    if op == 0x6A:
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

    # --- misc simple -------------------------------------------------------
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
    if op in (0xF5, 0xF8, 0xF9, 0xFA, 0xFB, 0xFC, 0xFD):
        return False                          # flag ops: rare; fallback keeps IF/DF exact
    if op == 0xD7:                            # xlat
        seg = f"s.{inst.seg_override or 'ds'}"
        out.append(f"cpu.set_reg8(0, mem.rb({seg}, (s.bx + (s.ax & 0xFF)) & 0xFFFF))")
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
        seg, off = inst.far_target
        out.append(f"{indent}s.cs, s.ip = 0x{seg:04X}, 0x{off:04X}")
        out.append(f"{indent}return")
    else:
        raise EmitUnsupported(f"terminator {kind} at {inst.ip:04X}")


def emit_function(scan: FunctionScan, cs: int, name: str, *,
                  signature: bytes, count_instructions: bool = False,
                  coverage: bool = False) -> str:
    """Return the source of a module defining the lifted hook ``name``.

    ``coverage`` adds a module-level ``BLOCKS_SEEN`` set that records which
    basic blocks actually executed, plus ``BLOCK_COUNT`` and ``coverage()`` —
    so a verify run can report *which paths* were exercised, not just that the
    hook passed (docs/lifting_design.md §7). It is inert otherwise.
    """
    leaders = scan.block_leaders()
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
    A('"""AUTOGENERATED by dos_re.lift — literal lift. DO NOT hand-edit in place.')
    A("")
    A(f"Function {cs:04X}:{scan.entry:04X}  "
      f"({len(scan.insts)} instructions, {len(leaders)} basic blocks)")
    A("")
    A("Refactor freely: the oracle tests are the contract, not this text. Lines")
    A('marked "(interpreter fallback)" are instructions the emitter has no native')
    A("form for yet — they are exact, but they are also the to-do list.")
    A('"""')
    A("from __future__ import annotations")
    A("")
    A("from dos_re.cpu import AF, CF, DF, IF, OF, PF, SF, ZF")
    A("from dos_re.hooks import self_disable_if_patched")
    A("from dos_re.lift.runtime import (emulate_call, emulate_far_call, emulate_int,")
    A("                                 interp_one)")
    A("")
    A(f"ENTRY = (0x{cs:04X}, 0x{scan.entry:04X})")
    A(f"SIGNATURE = bytes.fromhex({signature.hex()!r})")
    if coverage:
        A("")
        A(f"BLOCK_COUNT = {len(leaders)}")
        A("BLOCKS_SEEN = set()  # basic blocks that actually executed")
        A("")
        A("")
        A("def coverage():")
        A('    """(len(seen), total) — how much of the function a verify run exercised."""')
        A("    return len(BLOCKS_SEEN), BLOCK_COUNT")
    A("")
    A("")
    A(f"def {name}(cpu):")
    A(f'    """Lifted replacement for {cs:04X}:{scan.entry:04X}."""')
    A(f"    # Fail loud if the entry bytes no longer match what was lifted")
    A(f"    # (self-modifying code / overlay swap): raises rather than running a")
    A(f"    # replacement for code that is no longer there.")
    A(f"    self_disable_if_patched(cpu, 0x{scan.entry:04X}, SIGNATURE, {name!r})")
    A("    s, mem = cpu.s, cpu.mem")
    if count_instructions:
        A("    cpu.instruction_count -= 1  # step() counts the hook as 1; count real instructions")
    A("    bb = 0")
    A("    while True:")

    ind = " " * 12
    for bi, body in enumerate(blocks):
        A(f"        {'if' if bi == 0 else 'elif'} bb == {bi}:")
        lines: list[str] = []
        if coverage:
            lines.append(f"BLOCKS_SEEN.add({bi})")
        # Instructions executed natively (i.e. NOT re-entered through step(),
        # which does its own accounting). Calls/INTs count 1 for the transfer
        # instruction itself; their callees are counted by the VM.
        native = 0
        term: Inst | None = None
        for inst in body:
            lines.append(f"# {cs:04X}:{inst.ip:04X}  {inst.raw.hex():<12} {inst.mnemonic}")
            if inst.kind == SEQ:
                body_lines: list[str] = []
                try:
                    ok = _emit_instruction(inst, cs, body_lines)
                except EmitUnsupported as exc:
                    raise EmitUnsupported(f"{cs:04X}:{inst.ip:04X}: {exc}") from None
                if ok:
                    lines.extend(body_lines)
                    native += 1
                else:
                    lines.append(f"interp_one(cpu, 0x{cs:04X}, 0x{inst.ip:04X})"
                                 f"  # (interpreter fallback)")
            elif inst.kind == CALL:
                lines.append(f"emulate_call(cpu, 0x{cs:04X}, 0x{inst.target:04X}, "
                             f"0x{inst.next_ip:04X})")
                native += 1
            elif inst.kind == CALL_FAR:
                seg, off = inst.far_target
                lines.append(f"emulate_far_call(cpu, 0x{seg:04X}, 0x{off:04X}, "
                             f"0x{cs:04X}, 0x{inst.next_ip:04X})")
                native += 1
            elif inst.kind == CALL_IND:
                rm = _rm_operand(inst, 16, lines, "_o")
                if inst.reg == 3:             # call far [mem]
                    lines.append(f"_off = mem.rw({rm.seg_expr}, _o)")
                    lines.append(f"_seg = mem.rw({rm.seg_expr}, (_o + 2) & 0xFFFF)")
                    lines.append(f"emulate_far_call(cpu, _seg, _off, 0x{cs:04X}, "
                                 f"0x{inst.next_ip:04X})")
                else:                          # call near r/m16
                    lines.append(f"emulate_call(cpu, 0x{cs:04X}, {rm.read()}, "
                                 f"0x{inst.next_ip:04X})")
                native += 1
            elif inst.kind == INT:
                lines.append(f"emulate_int(cpu, 0x{inst.int_no:02X}, 0x{cs:04X}, "
                             f"0x{inst.next_ip:04X})")
                native += 1
            else:
                term = inst
                native += 1
                break

        if count_instructions and native:
            lines.append(f"cpu.instruction_count += {native}")
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
    A("")
    return "\n".join(L) + "\n"
