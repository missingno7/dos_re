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

from .decode import JCC, JMP, RET, SEQ
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
_FLAG_BITS = {"cf": FCF, "pf": FPF, "af": FAF, "zf": FZF, "sf": FSF, "of": FOF}


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
        if alu in ("adc", "sbb"):
            raise Refusal("flag-carry-chain (adc/sbb)")
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
        if alu in ("adc", "sbb"):
            raise Refusal("flag-carry-chain (adc/sbb)")
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
    if op == 0x98:                                        # cbw
        lines.append("ax = (ax & 0xFF) | (0xFF00 if ax & 0x80 else 0)")
        return
    if op == 0x99:                                        # cwd
        lines.append("dx = 0xFFFF if ax & 0x8000 else 0")
        return
    # stack ops (tier 2) -----------------------------------------------------
    # LITERAL SS:SP memory traffic: the pushed bytes are observable state (the
    # boundary differential hashes full memory, and stack residue below SP
    # persists after return), so push/pop write/read the real guest stack.
    # ``sp`` is a plain integer local -- an address base, never data (the gate
    # refuses sp-as-data); balance is verified statically, so sp never appears
    # in the outputs and the adapter's RET ABI is unchanged.
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
    raise Refusal(f"emitter-unsupported-op-{op:02X}"
                  + (f"-grp{inst.reg}" if inst.modrm is not None else ""))


def _emit_alu(lines, flags, alu, wide, a, b, inst, form, dst_rm, dst_acc):
    mask = 0xFFFF if wide else 0xFF
    lines.append(f"_a = {a}")
    lines.append(f"_b = {b}")
    if alu in ("add",):
        lines.append("_t = _a + _b")
        _flags_arith(lines, "add", wide, "_a", "_b", "_t")
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

def check_promotable(scan, *, excluded_addrs=frozenset()) -> "tuple":
    """The strict first-subset gate.  Returns (abi, block_map) or raises
    :class:`Refusal` with the census reason."""
    abi = abi_scan(scan)
    for cap in abi.refusals:
        raise Refusal({"call-abi-composition": "contains-call",
                       "int-platform-effect": "contains-interrupt",
                       "indirect-or-far-transfer": "indirect-control-flow",
                       "port-io-platform-effect": "port-io",
                       }.get(cap, cap))
    for i in scan.insts.values():
        e = register_effects(i)
        if i.kind == RET:
            if i.imm or 0:
                raise Refusal("ret-n-stack-args (needs stack-arg ABI)")
            continue                       # the RET ABI is the adapter's job
        if i.kind in ("retf", "iret"):
            raise Refusal("far-or-interrupt-return (needs adapter variant)")
        if i.op in (0x9C, 0x9D):
            raise Refusal("flags-as-stack-data (pushf/popf)")
        if e.stack_delta is None:
            raise Refusal("unresolved-stack-effect")
        if e.writes & frozenset({"cs", "ss"}):
            raise Refusal("cs-or-ss-mutation")
        if "sp" in (e.reads | e.writes) and not _is_stack_family(i):
            raise Refusal("sp-as-data")
    _check_stack_depths(scan)
    if any(i.ip in excluded_addrs for i in scan.insts.values()):
        raise Refusal("boundary-or-dispatch-address")
    # flag live-in at entry: a jcc may not read a flag no in-function
    # instruction has definitely written on every path -- refuse (the
    # contract would need caller flags).
    _check_flag_liveins(scan)
    return abi


def _is_stack_family(i) -> bool:
    """Instructions whose sp use IS the stack discipline (allowed), as opposed
    to sp used as general data (refused)."""
    op = i.op
    return (0x50 <= op <= 0x5F
            or op in (0x06, 0x0E, 0x16, 0x1E)      # push seg
            or op in (0x07, 0x17, 0x1F)            # pop seg (pop ss refuses
                                                    # earlier via ss-mutation)
            or op in (0x68, 0x6A)
            or (op == 0xFF and i.reg == 6) or (op == 0x8F and i.reg == 0)
            or i.kind == RET)


def _check_stack_depths(scan) -> None:
    """Static stack-discipline verification (tier 2): every address has ONE
    net push depth (bytes) from entry, consistent at every join; RET happens
    at depth 0 (balanced -- so sp never appears in the outputs and the
    adapter's RET ABI is unchanged); the depth never goes negative (a pop
    below the entry depth would read the caller's frame -- the return
    address -- which is machine-ABI data, not game state)."""
    depth: dict[int, int] = {scan.entry: 0}
    work = [scan.entry]
    while work:
        ip = work.pop()
        i = scan.insts[ip]
        e = register_effects(i)
        d = depth[ip]
        if i.kind == RET:
            if d != 0:
                raise Refusal("unbalanced-stack-at-ret")
            continue
        after = d - (e.stack_delta or 0)
        if after < 0:
            raise Refusal("stack-underflow (reads caller frame)")
        succs = []
        if i.kind in (SEQ,):
            succs = [i.next_ip]
        elif i.kind == JCC:
            succs = [i.next_ip, i.target]
        elif i.kind == JMP:
            succs = [i.target]
        for s in succs:
            if s is None or s not in scan.insts:
                continue
            if s in depth:
                if depth[s] != after:
                    raise Refusal("inconsistent-stack-depth-at-join")
            else:
                depth[s] = after
                work.append(s)


def _check_flag_liveins(scan) -> None:
    writes_all = {"cf", "pf", "af", "zf", "sf", "of"}
    defined: dict[int, frozenset] = {scan.entry: frozenset()}
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
                    raise Refusal("flag-live-in (caller flags observed)")
            new = defined[ip] | _flags_defined_by(i)
            succs = []
            if i.kind in (SEQ,):
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


def _flags_defined_by(i) -> frozenset:
    op = i.op
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
    has real stack traffic (then it is an address base for the literal SS:SP
    memory ops; balance keeps it out of the outputs).  Otherwise the RET-ABI
    sp read stays the adapter's business."""
    needs_sp = any(_is_stack_family(i) and i.kind != RET
                   for i in scan.insts.values())
    inputs = sorted(abi.inputs - {"sp"})
    if needs_sp:
        inputs = sorted(set(inputs) | {"sp"})
    return inputs


def emit_recovered(scan, abi, key: str) -> str:
    """Generate the recovered module source for one promotable function."""
    cs = int(key.split(":")[0], 16)
    name = f"func_{key.replace(':', '_').lower()}"
    leaders = scan.block_leaders()
    bb_of = {ip: n for n, ip in enumerate(leaders)}
    inputs = _contract_inputs(scan, abi)
    outputs = sorted((abi.outputs - {"sp"}) & (frozenset(W16) | frozenset({"ds", "es"})))

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
    A("")
    A("_PARITY = tuple((1 - bin(v).count('1') % 2) == 1 for v in range(256))")
    A("")
    A("")
    args = ", ".join(f"{r}=0" for r in inputs)
    A(f"def {name}(mem, *, {args}):" if inputs else f"def {name}(mem):")
    body: list[str] = []
    B = body.append
    B("_cost = 0")
    B("cf = pf = af = zf = sf = of = False")
    B("_fmask = 0")
    B(f"bb = {bb_of[scan.entry]}")
    B("while True:")
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
            if i.kind == RET:
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
            _translate(i, blk, flag_written)
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
                          for f in ("cf", "pf", "af", "zf", "sf", "of")))
    B("_flags = (" + fl_expr + ") & _fmask")
    B(f"return {{{out_dict}}}, {{'flags': _flags, 'fmask': _fmask, 'cost': _cost}}")
    L.extend("    " + ln for ln in body)
    A("")
    return "\n".join(L) + "\n"


def emit_adapter(scan, abi, key: str, *, signature: bytes,
                 recovered_import_base: str) -> str:
    """Generate the CPU-ABI adapter that occupies the lifted slot."""
    cs = int(key.split(":")[0], 16)
    entry = scan.entry
    name = f"lifted_{key.replace(':', '_').lower()}"
    rec = f"func_{key.replace(':', '_').lower()}"
    inputs = _contract_inputs(scan, abi)
    outputs = sorted((abi.outputs - {"sp"}) & (frozenset(W16) | frozenset({"ds", "es"})))

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
    A("")
    A(f"ENTRY = (0x{cs:04X}, 0x{entry:04X})")
    A(f"SIGNATURE = bytes.fromhex({signature.hex()!r})")
    A("")
    A("")
    A(f"def {name}(cpu, bb=0):")
    A(f'    """Generated CPU-ABI adapter for {key} (recovered body is the')
    A('    single implementation)."""')
    A("    s = cpu.s")
    kw = ", ".join(f"{r}=s.{r}" for r in inputs)
    A(f"    _out, _compat = {rec}(cpu.mem{', ' + kw if kw else ''})")
    for r in outputs:
        A(f"    s.{r} = _out['{r}']")
    A("    # exit flags: touched bits from the compat channel, rest preserved")
    A("    s.flags = (s.flags & ~_compat['fmask']) | _compat['flags'] | 0x0002")
    A("    # historical RET ABI + exact virtual time (owns_time contract)")
    A("    s.ip = cpu.pop()")
    A("    cpu.instruction_count += _compat['cost']")
    A("")
    A("")
    A(f"#: this adapter accounts its own instruction_count per executed path.")
    A(f"{name}.owns_time = True")
    return "\n".join(L) + "\n"
