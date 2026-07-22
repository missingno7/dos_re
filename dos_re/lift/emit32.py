"""Emit a literal Python hook for one lifted 32-bit (CPU386) function.

The flat protected-mode counterpart of :mod:`.emit`, same priority order:

1. **Faithful.** Registers/flags/memory update at every instruction boundary
   exactly as ``dos_re.cpu386`` would: flags via the interpreter's own
   ``_flags_add``/``_flags_sub``/``_flags_logic``; shifts/strings via the
   interpreter's ``_shift``/``_string``; memory via ``mem.r*/w*`` (so the
   VGA-aperture routing holds by construction).
2. **Total.** Any non-transfer instruction without a native form emits as a
   one-instruction interpreter call (``interp_one32``) — the to-do list, not
   a refusal.  x87 lines always fall back (the doubles caveat lives in ONE
   place: the interpreter).
3. **Refactorable.** One line per instruction with address/bytes/mnemonic
   comments; control flow is an explicit basic-block dispatch loop.

v1 native coverage: ALU, MOV (r/m, moffs, imm), PUSH/POP, INC/DEC, TEST,
XCHG, LEA, MOVZX/MOVSX, SETcc, CWDE/CDQ, shifts, string ops, all direct
control flow.  16-bit *address-size* memory operands (0x67) fall back — flat
Watcom code doesn't emit them.
"""
from __future__ import annotations

from .cfg32 import FunctionScan32
from .decode import CALL, CALL_IND, INT, IRET, JCC, JMP, JMP_IND, RET, SEQ
from .decode32 import Inst32

_REG32 = ("r[0]", "r[1]", "r[2]", "r[3]", "r[4]", "r[5]", "r[6]", "r[7]")
_LOOP_MNEMS = {"loopnz", "loopz", "loop", "jecxz"}


class EmitUnsupported(Exception):
    pass


def _sx(value: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    return (value & (sign - 1)) - (value & sign)


def _reg_read(idx: int, size: int) -> str:
    if size == 4:
        return _REG32[idx]
    if size == 2:
        return f"{_REG32[idx]} & 0xFFFF"
    return f"cpu.reg({idx}, 1)"


def _reg_write(idx: int, size: int, value: str) -> str:
    if size == 4:
        return f"{_REG32[idx]} = ({value}) & 0xFFFFFFFF"
    if size == 2:
        return f"{_REG32[idx]} = ({_REG32[idx]} & 0xFFFF0000) | (({value}) & 0xFFFF)"
    return f"cpu.set_reg({idx}, 1, {value})"


class _Operand:
    """An emitted r/m operand (register or flat memory address in ``_o``)."""

    def __init__(self, is_reg: bool, size: int, reg_idx: int = 0):
        self.is_reg = is_reg
        self.size = size
        self.reg_idx = reg_idx

    def read(self) -> str:
        if self.is_reg:
            return _reg_read(self.reg_idx, self.size)
        return f"mem.r{self.size * 8}(_o)"

    def write(self, value: str) -> list[str]:
        if self.is_reg:
            return [_reg_write(self.reg_idx, self.size, value)]
        return [f"mem.w{self.size * 8}(_o, {value})"]


def _offset_expr(inst: Inst32) -> tuple[str, str]:
    """(plain 32-bit offset expression, default segment) for a memory ModRM.

    Mirrors ``CPU386._addr32`` exactly, including the SS default for
    EBP/ESP-based forms and the mod0/base5 and mod0/rm5 disp32 cases."""
    mod, rm = inst.mod, inst.rm
    parts: list[str] = []
    default_seg = "ds"
    if rm == 4:
        sib = inst.sib
        scale, index, base = sib >> 6, (sib >> 3) & 7, sib & 7
        if index != 4:
            parts.append(_REG32[index] if scale == 0 else f"({_REG32[index]} << {scale})")
        if not (base == 5 and mod == 0):
            parts.append(_REG32[base])
            if base in (4, 5):
                default_seg = "ss"
    elif not (rm == 5 and mod == 0):
        parts.append(_REG32[rm])
        if rm == 5:
            default_seg = "ss"
    disp = inst.disp
    if disp:
        parts.append(f"0x{disp:X}" if disp > 0 else f"-0x{-disp:X}")
    if not parts:
        parts.append("0")
    return " + ".join(parts), default_seg


def _addr_expr(inst: Inst32) -> str:
    """The flat linear address expression (segment base + offset)."""
    offset, default_seg = _offset_expr(inst)
    seg = inst.seg_override or default_seg
    return f'(sb["{seg}"] + {offset}) & 0xFFFFFFFF'


def _rm_operand(inst: Inst32, size: int, out: list[str]) -> _Operand:
    if inst.mod == 3:
        return _Operand(True, size, reg_idx=inst.rm)
    if inst.adsize != 4:
        raise _Fallback()
    out.append(f"_o = {_addr_expr(inst)}")
    return _Operand(False, size)


class _Fallback(Exception):
    """Signal: no native form — emit the interpreter fallback line."""


def _alu_lines(group: int, bits: int, a: str, b: str, dst: _Operand | None,
               out: list[str]) -> None:
    mask = (1 << bits) - 1
    out.append(f"_a = {a}")
    out.append(f"_b = {b}")
    if group == 0:
        out.append("_r = _a + _b")
        out.append(f"cpu._flags_add(_a, _b, _r, {bits})")
    elif group == 2:
        out.append("_c = 1 if cpu.eflags & CF else 0")
        out.append("_r = _a + _b + _c")
        out.append(f"cpu._flags_add(_a, _b, _r, {bits}, _c)")
    elif group == 3:
        out.append("_c = 1 if cpu.eflags & CF else 0")
        out.append("_r = _a - _b - _c")
        out.append(f"cpu._flags_sub(_a, _b, _r, {bits}, _c)")
    elif group in (5, 7):
        out.append("_r = _a - _b")
        out.append(f"cpu._flags_sub(_a, _b, _r, {bits})")
    else:
        opsym = {1: "|", 4: "&", 6: "^"}[group]
        out.append(f"_r = _a {opsym} _b")
        out.append(f"cpu._flags_logic(_r, {bits})")
    if group != 7 and dst is not None:
        out.extend(dst.write(f"_r & 0x{mask:X}"))


def _emit_instruction(inst: Inst32, out: list[str]) -> bool:
    """Append native Python for ``inst``; False requests the fallback."""
    op = inst.op
    osz = inst.opsize
    bits = osz * 8

    try:
        # --- ALU r/m,reg / reg,r/m / acc,imm --------------------------------
        if op < 0x40 and (op & 7) in (0, 1, 2, 3):
            group = (op >> 3) & 7
            sz = 1 if (op & 1) == 0 else osz
            to_reg = bool(op & 2)
            rm = _rm_operand(inst, sz, out)
            reg = _Operand(True, sz, reg_idx=inst.reg)
            if to_reg:
                _alu_lines(group, sz * 8, reg.read(), rm.read(), reg, out)
            else:
                _alu_lines(group, sz * 8, rm.read(), reg.read(), rm, out)
            return True
        if op < 0x40 and (op & 7) in (4, 5):
            group = (op >> 3) & 7
            sz = 1 if (op & 7) == 4 else osz
            acc = _Operand(True, sz, reg_idx=0)
            _alu_lines(group, sz * 8, acc.read(), f"0x{inst.imm:X}", acc, out)
            return True
        if op in (0x80, 0x81, 0x83):
            sz = 1 if op == 0x80 else osz
            group = inst.reg
            rm = _rm_operand(inst, sz, out)
            imm = inst.imm
            if op == 0x83:
                imm = _sx(imm, 8) & ((1 << (sz * 8)) - 1)
            _alu_lines(group, sz * 8, rm.read(), f"0x{imm:X}", rm, out)
            return True

        # --- MOV -------------------------------------------------------------
        if op in (0x88, 0x89, 0x8A, 0x8B):
            sz = 1 if op in (0x88, 0x8A) else osz
            rm = _rm_operand(inst, sz, out)
            reg = _Operand(True, sz, reg_idx=inst.reg)
            out.extend(reg.write(rm.read()) if op & 2 else rm.write(reg.read()))
            return True
        if op in (0xA0, 0xA1, 0xA2, 0xA3):
            if inst.adsize != 4:
                return False
            sz = 1 if op in (0xA0, 0xA2) else osz
            seg = inst.seg_override or "ds"
            out.append(f'_o = (sb["{seg}"] + 0x{inst.imm:X}) & 0xFFFFFFFF')
            acc = _Operand(True, sz, reg_idx=0)
            memop = _Operand(False, sz)
            out.extend(acc.write(memop.read()) if op in (0xA0, 0xA1)
                       else memop.write(acc.read()))
            return True
        if 0xB0 <= op <= 0xB7:
            out.append(_reg_write(op - 0xB0, 1, f"0x{inst.imm:02X}"))
            return True
        if 0xB8 <= op <= 0xBF:
            out.append(_reg_write(op - 0xB8, osz, f"0x{inst.imm:X}"))
            return True
        if op in (0xC6, 0xC7):
            sz = 1 if op == 0xC6 else osz
            rm = _rm_operand(inst, sz, out)
            out.extend(rm.write(f"0x{inst.imm:X}"))
            return True

        # --- TEST / XCHG / LEA ------------------------------------------------
        if op in (0x84, 0x85):
            sz = 1 if op == 0x84 else osz
            rm = _rm_operand(inst, sz, out)
            out.append(f"cpu._flags_logic({rm.read()} & ({_reg_read(inst.reg, sz)}), {sz * 8})")
            return True
        if op in (0xA8, 0xA9):
            sz = 1 if op == 0xA8 else osz
            out.append(f"cpu._flags_logic(({_reg_read(0, sz)}) & 0x{inst.imm:X}, {sz * 8})")
            return True
        if op in (0x86, 0x87):
            sz = 1 if op == 0x86 else osz
            rm = _rm_operand(inst, sz, out)
            reg = _Operand(True, sz, reg_idx=inst.reg)
            out.append(f"_a = {reg.read()}")
            out.append(f"_b = {rm.read()}")
            out.extend(reg.write("_b"))
            out.extend(rm.write("_a"))
            return True
        if 0x91 <= op <= 0x97:
            i = op - 0x90
            if osz == 4:
                out.append(f"r[0], r[{i}] = r[{i}], r[0]")
            else:
                out.append(f"_a = {_reg_read(0, osz)}")
                out.append(f"_b = {_reg_read(i, osz)}")
                out.append(_reg_write(0, osz, "_b"))
                out.append(_reg_write(i, osz, "_a"))
            return True
        if op == 0x90:
            out.append("pass  # nop")
            return True
        if op == 0x8D:
            if inst.mod == 3 or inst.adsize != 4:
                raise EmitUnsupported("lea with register source / 16-bit address")
            # LEA takes the plain OFFSET — no segment base.
            offset, _ = _offset_expr(inst)
            out.append(_reg_write(inst.reg, osz, f"({offset})"))
            return True

        # --- INC/DEC (preserve CF) --------------------------------------------
        if 0x40 <= op <= 0x4F:
            dec = op >= 0x48
            i = op & 7
            out.append(f"_old = {_reg_read(i, osz)}")
            out.append("_cf = cpu.eflags & CF")
            out.append(f"_r = _old {'-' if dec else '+'} 1")
            fl = "cpu._flags_sub" if dec else "cpu._flags_add"
            out.append(f"{fl}(_old, 1, _r, {bits})")
            out.append("cpu.eflags = (cpu.eflags & ~CF) | _cf")
            out.append(_reg_write(i, osz, "_r"))
            return True
        if op in (0xFE, 0xFF) and inst.reg in (0, 1):
            sz = 1 if op == 0xFE else osz
            dec = inst.reg == 1
            rm = _rm_operand(inst, sz, out)
            out.append(f"_old = {rm.read()}")
            out.append("_cf = cpu.eflags & CF")
            out.append(f"_r = _old {'-' if dec else '+'} 1")
            fl = "cpu._flags_sub" if dec else "cpu._flags_add"
            out.append(f"{fl}(_old, 1, _r, {sz * 8})")
            out.append("cpu.eflags = (cpu.eflags & ~CF) | _cf")
            out.extend(rm.write("_r"))
            return True

        # --- PUSH / POP -------------------------------------------------------
        if 0x50 <= op <= 0x57:
            out.append(f"cpu.push({_reg_read(op - 0x50, osz)}, {osz})")
            return True
        if 0x58 <= op <= 0x5F:
            out.append(_reg_write(op - 0x58, osz, f"cpu.pop({osz})"))
            return True
        if op == 0x68:
            out.append(f"cpu.push(0x{inst.imm:X}, {osz})")
            return True
        if op == 0x6A:
            imm = _sx(inst.imm, 8) & ((1 << bits) - 1)
            out.append(f"cpu.push(0x{imm:X}, {osz})")
            return True
        if op == 0x8F:
            rm = _rm_operand(inst, osz, out)
            out.append(f"_v = cpu.pop({osz})")
            out.extend(rm.write("_v"))
            return True
        if op == 0xFF and inst.reg == 6:
            rm = _rm_operand(inst, osz, out)
            out.append(f"cpu.push({rm.read()}, {osz})")
            return True

        # --- leave / pushad / popad (mirror CPU386 exactly) --------------------
        if op == 0xC9:      # leave: esp = ebp ; ebp = pop
            out.append("r[4] = r[5]")
            out.extend(_Operand(True, osz, reg_idx=5).write(f"cpu.pop({osz})"))
            return True
        if op == 0x60:      # pushad
            out.append(f"cpu._pusha({osz})")
            return True
        if op == 0x61:      # popad
            out.append(f"cpu._popa({osz})")
            return True

        # --- flag ops ----------------------------------------------------------
        if op == 0xF5:      # cmc
            out.append("cpu.eflags ^= CF")
            return True
        if 0xF8 <= op <= 0xFD:      # clc/stc/cli/sti/cld/std
            _flag, _val = {0xF8: ("CF", False), 0xF9: ("CF", True),
                           0xFA: ("IF", False), 0xFB: ("IF", True),
                           0xFC: ("DF", False), 0xFD: ("DF", True)}[op]
            out.append(f"cpu.set_flag({_flag}, {_val})")
            return True

        # --- port I/O (in/out) -------------------------------------------------
        if op in (0xE4, 0xE5, 0xEC, 0xED):      # in
            sz = 1 if op in (0xE4, 0xEC) else osz
            port = f"0x{inst.imm:X}" if op in (0xE4, 0xE5) else "(r[2] & 0xFFFF)"
            out.append(f"_v = cpu.port_reader(cpu, {port}, {sz * 8}) "
                       f"if cpu.port_reader else 0")
            out.extend(_Operand(True, sz, reg_idx=0).write("_v"))
            return True
        if op in (0xE6, 0xE7, 0xEE, 0xEF):      # out
            sz = 1 if op in (0xE6, 0xEE) else osz
            port = f"0x{inst.imm:X}" if op in (0xE6, 0xE7) else "(r[2] & 0xFFFF)"
            out.append(f"cpu.port_writer(cpu, {port}, {_reg_read(0, sz)}, "
                       f"{sz * 8}) if cpu.port_writer else None")
            return True

        # --- grp3 (test/not/neg/mul/imul/div/idiv) -> CPU386 primitive ---------
        if op in (0xF6, 0xF7):
            sz = 1 if op == 0xF6 else osz
            imm = (inst.imm or 0) if inst.reg in (0, 1) else 0
            if inst.mod == 3:
                out.append(f"cpu._grp3_op({inst.reg}, True, {inst.rm}, {sz}, 0x{imm:X})")
            else:
                if inst.adsize != 4:
                    return False
                out.append(f"_o = {_addr_expr(inst)}")
                out.append(f"cpu._grp3_op({inst.reg}, False, _o, {sz}, 0x{imm:X})")
            return True

        # --- imul (two/three-operand) -> CPU386 _imul_store --------------------
        if op in (0x69, 0x6B, 0x0FAF):
            _hi = 1 << bits
            _sbit = 1 << (bits - 1)

            def _signed(expr: str) -> str:
                return f"(({expr}) - 0x{_hi:X} if ({expr}) & 0x{_sbit:X} else ({expr}))"

            if op == 0x0FAF:                     # imul r, r/m
                rm = _rm_operand(inst, osz, out)
                out.append(f"_a = {_signed(_reg_read(inst.reg, osz))}")
                out.append(f"_b = {_signed(rm.read())}")
                out.append(f"cpu._imul_store({inst.reg}, {osz}, _a, _b)")
            else:                                # imul r, r/m, imm
                rm = _rm_operand(inst, osz, out)
                imm = _sx(inst.imm, 8 if op == 0x6B else bits)
                out.append(f"_a = {_signed(rm.read())}")
                out.append(f"cpu._imul_store({inst.reg}, {osz}, _a, {imm})")
            return True

        # --- shifts: delegate to the interpreter's own _shift ------------------
        if op in (0xC0, 0xC1, 0xD0, 0xD1, 0xD2, 0xD3):
            sz = 1 if op in (0xC0, 0xD0, 0xD2) else osz
            if op in (0xC0, 0xC1):
                count = f"0x{inst.imm:02X}"
            elif op in (0xD0, 0xD1):
                count = "1"
            else:
                count = "r[1] & 0xFF"
            if inst.mod == 3:
                out.append(f"cpu._shift({inst.reg}, True, {inst.rm}, {sz}, {count})")
            else:
                if inst.adsize != 4:
                    return False
                out.append(f"_o = {_addr_expr(inst)}")
                out.append(f"cpu._shift({inst.reg}, False, _o, {sz}, {count})")
            return True

        # --- widen / sign ------------------------------------------------------
        if op == 0x98:      # cwde (or cbw with 0x66)
            out.append(f"_v = {_reg_read(0, osz // 2)}")
            out.append(_reg_write(0, osz, f"_v - 0x{1 << (bits // 2):X} "
                                          f"if _v & 0x{1 << (bits // 2 - 1):X} else _v"))
            return True
        if op == 0x99:      # cdq / cwd
            out.append(f"_v = {_reg_read(0, osz)}")
            out.append(_reg_write(2, osz, f"0x{(1 << bits) - 1:X} "
                                          f"if _v & 0x{1 << (bits - 1):X} else 0"))
            return True
        if op == 0x9C:
            out.append(f"cpu.push(cpu.eflags, {osz})")
            return True
        if op == 0x9D:
            out.append(f"cpu.eflags = (cpu.pop({osz}) & 0x0FD5) | 0x0002")
            return True

        # --- string ops through the interpreter's engine -----------------------
        if op in (0xA4, 0xA5, 0xA6, 0xA7, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF):
            rep = f"0x{inst.rep:02X}" if inst.rep is not None else "0"
            ovr = f"{inst.seg_override!r}" if inst.seg_override else "None"
            out.append(f"cpu._opsize = {osz}; cpu._adsize = {inst.adsize}; "
                       f"cpu._segovr = {ovr}")
            out.append(f"cpu._string(0x{op:02X}, {rep})")
            return True

        # --- two-byte natives ---------------------------------------------------
        if op in (0x0FB6, 0x0FB7, 0x0FBE, 0x0FBF):     # movzx/movsx
            srcsz = 1 if op in (0x0FB6, 0x0FBE) else 2
            rm = _rm_operand(inst, srcsz, out)
            out.append(f"_v = {rm.read()}")
            if op in (0x0FBE, 0x0FBF):
                sbit = 1 << (srcsz * 8 - 1)
                out.append(f"_v = (_v - 0x{sbit * 2:X}) if _v & 0x{sbit:X} else _v")
            out.append(_reg_write(inst.reg, osz, "_v"))
            return True
        if 0x0F90 <= op <= 0x0F9F:                     # setcc
            rm = _rm_operand(inst, 1, out)
            out.extend(rm.write(f"1 if cpu._cond(0x{op & 0xF:X}) else 0"))
            return True

        return False
    except _Fallback:
        return False


def _terminator_lines(inst: Inst32, bb_of: dict[int, int], out: list[str],
                      indent: str) -> None:
    kind = inst.kind
    if kind == JCC:
        t, f = bb_of[inst.target], bb_of[inst.next_ip]
        if inst.mnemonic in _LOOP_MNEMS:
            reg = "r[1]" if inst.adsize == 4 else "(r[1] & 0xFFFF)"
            if inst.mnemonic != "jecxz":
                if inst.adsize == 4:
                    out.append(f"{indent}r[1] = (r[1] - 1) & 0xFFFFFFFF")
                else:
                    out.append(f"{indent}r[1] = (r[1] & 0xFFFF0000) | ((r[1] - 1) & 0xFFFF)")
            cond = {
                "loopnz": f"{reg} != 0 and not (cpu.eflags & ZF)",
                "loopz": f"{reg} != 0 and (cpu.eflags & ZF)",
                "loop": f"{reg} != 0",
                "jecxz": f"{reg} == 0",
            }[inst.mnemonic]
            out.append(f"{indent}bb = {t} if ({cond}) else {f}")
        else:
            out.append(f"{indent}bb = {t} if cpu._cond(0x{inst.op & 0xF:X}) else {f}")
    elif kind == JMP:
        out.append(f"{indent}bb = {bb_of[inst.target]}")
    elif kind == RET:
        out.append(f"{indent}cpu.eip = cpu.pop(4)")
        if inst.imm:
            out.append(f"{indent}r[4] = (r[4] + 0x{inst.imm:X}) & 0xFFFFFFFF")
        out.append(f"{indent}return")
    elif kind == IRET:
        out.append(f"{indent}cpu.eip = cpu.pop(4)")
        out.append(f'{indent}cpu.set_seg("cs", cpu.pop(4))')
        out.append(f"{indent}cpu.eflags = (cpu.pop(4) & 0x0FD5) | 0x0002")
        out.append(f"{indent}return")
    elif kind == JMP_IND:
        # Tail jump: compute the target and hand control back to the VM (the
        # lifted function ends here; a hook installed at the target re-enters).
        if inst.reg == 5:
            raise EmitUnsupported(f"far indirect jump at 0x{inst.ip:X}")
        if inst.mod == 3:
            out.append(f"{indent}cpu.eip = {_reg_read(inst.rm, 4)}")
        elif inst.adsize == 4:
            out.append(f"{indent}_o = {_addr_expr(inst)}")
            out.append(f"{indent}cpu.eip = mem.r32(_o)")
        else:
            raise EmitUnsupported(f"16-bit-address indirect jump at 0x{inst.ip:X}")
        out.append(f"{indent}return")
    else:
        raise EmitUnsupported(f"terminator {kind} at 0x{inst.ip:X}")


def emit_function32(scan: FunctionScan32, name: str, *, signature: bytes,
                    count_instructions: bool = False,
                    min_iterations: int | None = None,
                    link_map: dict[int, str] | None = None,
                    boundary_heads: frozenset = frozenset(),
                    detect_spin: bool = False,
                    link_imports: tuple[str, ...] = ()) -> str:
    """Return the source of a module defining the lifted hook ``name``.

    ``link_map`` is the generated-call linker seam (the 32-bit mirror of
    ``emit.py``): a ``{near_call_target_eip: python_callable_expr}`` map.  A
    direct near CALL to a mapped target emits ``call_linked32(cpu, target,
    <callee>, ret_ip)`` instead of ``emulate_call32`` — original CALL/RET
    stack semantics, no interpreter in the path, and the child remains a
    verifier-visible boundary in the hybrid.  Only all-near-RET-exit callees
    are safe to link (the link pass enforces this).  ``link_imports`` lines
    are appended to the module header verbatim (e.g. a module-level
    ``LINKS = {"0xEIP": None}`` table that the graph activator's
    ``resolve_links32`` fills at install time; emitted modules load flat via
    ``spec_from_file_location``, so relative imports are unavailable).

    ``boundary_heads`` is the flat mirror of ``emit.py``'s boundary observers:
    each declared spin/wait head EIP gets an emitted
    ``cpu.boundary_hook(cpu, head_eip, resume_eip)`` call after its instruction,
    and both the head and its successor become RESUME ENTRIES — block leaders
    exported in ``RESUME_ENTRIES`` so the graph activator can register a
    re-entry hook there.  A host that parks (the hook re-points EIP and raises)
    then resumes INSIDE the lifted body via ``fn(cpu, bb)`` — an environment
    wait that only an IRQ can exit runs interpreted for its spin and lifted for
    the rest, instead of being excluded whole.  The function grows an optional
    ``bb`` parameter (the starting block) for that re-entry.
    """
    link_map = link_map or {}
    # BOUNDARY OBSERVERS: a declared head becomes a block leader and gets an
    # observer after its instruction; the head AND its successor are exported as
    # resume entries (a park leaves EIP on the successor, but a snapshot / an
    # IRET can land the machine back ON the head mid-spin -- see emit.py and
    # test_lift_boundary_resume).  The head must be a non-transfer instruction
    # with an in-region successor, or a park there could not resume.
    heads = frozenset(h for h in boundary_heads if h in scan.insts)
    resume_points: set[int] = set()
    forced = set(scan.block_leaders())
    for h in sorted(heads):
        hi = scan.insts[h]
        if hi.kind not in (SEQ, CALL, CALL_IND, INT):
            raise EmitUnsupported(
                f"boundary head 0x{h:X} is a control transfer ({hi.kind}); "
                f"register the head at a sequential instruction of the wait loop")
        if hi.next_ip not in scan.insts:
            raise EmitUnsupported(
                f"boundary head 0x{h:X} has no in-region successor -- a park "
                f"there could not resume in host code")
        forced.update((h, hi.next_ip))
        resume_points.update((h, hi.next_ip))
    leaders = sorted(forced)
    bb_of = {ip: i for i, ip in enumerate(leaders)}
    leader_set = set(leaders)

    blocks: list[list[Inst32]] = []
    for leader in leaders:
        body: list[Inst32] = []
        ip = leader
        while True:
            inst = scan.insts[ip]
            body.append(inst)
            if inst.kind not in (SEQ, CALL, CALL_IND, INT):
                break
            nxt = inst.next_ip
            if nxt in leader_set or nxt not in scan.insts:
                break
            ip = nxt
        blocks.append(body)

    L: list[str] = []
    A = L.append
    A('"""AUTOGENERATED by dos_re.lift (32-bit). DO NOT hand-edit in place.')
    A("")
    A(f"Function 0x{scan.entry:X}  "
      f"({len(scan.insts)} instructions, {len(leaders)} basic blocks)")
    A("")
    A("Refactor freely: the oracle tests are the contract, not this text. Lines")
    A('marked "(interpreter fallback)" are instructions the emitter has no native')
    A("form for yet -- they are exact, but they are also the to-do list.")
    A('"""')
    A("from __future__ import annotations")
    A("")
    A("from dos_re.cpu import CF, DF, IF, ZF")
    A("from dos_re.lift.runtime32 import (LiftRuntimeError, call_linked32,")
    A("                                   check_signature, emulate_call32,")
    A("                                   emulate_int32, interp_one32)")
    if detect_spin:
        A("from dos_re.lift.runtime32 import stuck_error")
    A("")
    A(f"ENTRY = 0x{scan.entry:X}")
    A(f"SIGNATURE = bytes.fromhex({signature.hex()!r})")
    A(f"MAX_ITERATIONS = {max(min_iterations or 10_000, len(scan.insts) * 5_000)}")
    for extra in link_imports:
        A(extra)
    # BLOCK_ADDRS / RESUME_ENTRIES are emitted only under boundary observation,
    # so a head-less module stays byte-for-byte what the pre-boundary emitter
    # produced (the committed graphs' reproducibility --check depends on it).
    if resume_points or detect_spin:
        A("#: dispatch block -> its leader address (re-entry + diagnosis)")
        A("BLOCK_ADDRS = {"
          + ", ".join(f"{bi}: 0x{body[0].ip:X}" for bi, body in enumerate(blocks))
          + "}")
    if resume_points:
        entries = ", ".join(f'"0x{rp:X}": {bb_of[rp]}'
                            for rp in sorted(resume_points))
        A("#: re-entry points (boundary head + its successor); the graph")
        A("#: activator registers a re-entry hook at each. address -> block.")
        A(f"RESUME_ENTRIES = {{{entries}}}")
    if detect_spin:
        A("PROGRESS_SAMPLE = 0xFFFF  # sample state every 64K dispatches")
    A("")
    A("")
    entry_bb = bb_of[scan.entry]
    # Entry block, not index 0: leaders are address-sorted and a region can
    # contain branch targets below the entry (see emit.py — same fix).  With
    # boundary observation the entry block is the default of a ``bb`` re-entry
    # parameter (a park resumes by calling fn(cpu, resume_bb)); without it the
    # signature is unchanged and bb is a plain local.
    if resume_points:
        A(f"def {name}(cpu, bb={entry_bb}):")
    else:
        A(f"def {name}(cpu):")
    A(f'    """Lifted replacement for flat 0x{scan.entry:X}."""')
    A(f"    check_signature(cpu, ENTRY, SIGNATURE, {name!r})")
    A("    r, mem, sb = cpu.r, cpu.mem, cpu.sbase")
    if count_instructions:
        A("    cpu.instruction_count -= 1  # step() counts the hook as 1")
    if not resume_points:
        A(f"    bb = {entry_bb}")
    if detect_spin:
        # No-progress detector (emit.py mirror): a lifted body runs
        # synchronously, so a loop that returns to the SAME block with IDENTICAL
        # registers is provably waiting on something that cannot change inside
        # it -- an environment wait.  Name the head instead of guessing.
        A("    _last_snap = None")
    A("    for _guard in range(MAX_ITERATIONS):")
    if detect_spin:
        A("        if not _guard & PROGRESS_SAMPLE:")
        A("            _snap = (bb, r[0], r[1], r[2], r[3], r[4], r[5], r[6],")
        A("                     r[7], cpu.eflags)")
        A("            if _snap == _last_snap:")
        A("                raise stuck_error(__name__, cpu, bb, BLOCK_ADDRS,")
        A("                                  iterations=_guard,")
        A("                                  no_progress=PROGRESS_SAMPLE + 1)")
        A("            _last_snap = _snap")

    ind = " " * 12
    for bi, body in enumerate(blocks):
        A(f"        {'if' if bi == 0 else 'elif'} bb == {bi}:")
        lines: list[str] = []
        native = 0
        term: Inst32 | None = None
        for inst in body:
            lines.append(f"# 0x{inst.ip:X}  {inst.raw.hex():<14} {inst.mnemonic}")
            if inst.kind == SEQ:
                body_lines: list[str] = []
                try:
                    ok = _emit_instruction(inst, body_lines)
                except EmitUnsupported as exc:
                    raise EmitUnsupported(f"0x{inst.ip:X}: {exc}") from None
                if ok:
                    lines.extend(body_lines)
                    native += 1
                else:
                    lines.append(f"interp_one32(cpu, 0x{inst.ip:X})"
                                 f"  # (interpreter fallback)")
            elif inst.kind == CALL:
                if inst.target in link_map:
                    lines.append(f"call_linked32(cpu, 0x{inst.target:X}, "
                                 f"{link_map[inst.target]}, 0x{inst.next_ip:X})")
                else:
                    lines.append(f"emulate_call32(cpu, 0x{inst.target:X}, 0x{inst.next_ip:X})")
                native += 1
            elif inst.kind == CALL_IND:
                if inst.mod == 3:
                    tgt = _reg_read(inst.rm, 4)
                elif inst.adsize == 4:
                    lines.append(f"_o = {_addr_expr(inst)}")
                    tgt = "mem.r32(_o)"
                else:
                    raise EmitUnsupported(f"0x{inst.ip:X}: 16-bit-address indirect call")
                lines.append(f"emulate_call32(cpu, {tgt}, 0x{inst.next_ip:X})")
                native += 1
            elif inst.kind == INT:
                lines.append(f"emulate_int32(cpu, 0x{inst.int_no:02X}, 0x{inst.next_ip:X})")
                native += 1
            else:
                term = inst
                native += 1
                break
            if inst.ip in heads:
                # Boundary observer, mirroring emit.py: flush the native count
                # BEFORE the event so a park (the hook raises) never loses the
                # instructions already executed, then offer the boundary.  The
                # host's hook re-points EIP at the successor and raises to park;
                # None (the default) just continues the lifted body.
                if count_instructions and native:
                    lines.append(f"cpu.instruction_count += {native}")
                    native = 0
                lines.append("if cpu.boundary_hook is not None:")
                lines.append(f"    cpu.boundary_hook(cpu, 0x{inst.ip:X}, "
                             f"0x{inst.next_ip:X})")

        if count_instructions and native:
            lines.append(f"cpu.instruction_count += {native}")
        for ln in lines:
            A(ind + ln)

        if term is not None:
            _terminator_lines(term, bb_of, L, ind)
        else:
            fall = body[-1].next_ip
            if fall not in bb_of:
                raise EmitUnsupported(
                    f"block at 0x{body[0].ip:X} falls out of the region")
            A(f"{ind}bb = {bb_of[fall]}")
    if detect_spin:
        A("    raise stuck_error(__name__, cpu, bb, BLOCK_ADDRS,")
        A("                      iterations=MAX_ITERATIONS, no_progress=0)")
    else:
        A("    raise LiftRuntimeError(")
        A(f"        {name!r} + ' exceeded MAX_ITERATIONS (unbounded internal loop -- "
          f"likely an environment wait; hook it by hand)')")
    A("")
    return "\n".join(L)
