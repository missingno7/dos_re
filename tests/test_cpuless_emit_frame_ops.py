"""The emitted CPUless form of idiv / pusha / popa must match the interpreter.

The ABI side of these (test_cpuless_frame_ops) says a function CONTAINING them
can promote; this is the other half -- that the pure-Python the emitter writes
COMPUTES the same thing the VM does. The risk lives in the details the ABI layer
never sees: idiv truncates toward zero (not Python's floor //), and pusha
snapshots sp BEFORE its first push. So each emitted instruction is exec'd and
diffed against a single interpreter step over identical state.
"""
from __future__ import annotations

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.decode import decode_one
from dos_re.lift.emit_cpuless import _translate
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")


def _emit(code: bytes) -> list[str]:
    inst = decode_one(lambda o: code[o] if o < len(code) else 0x90, 0)
    lines: list[str] = []
    _translate(inst, lines, set())
    return lines


def _run_emitted(code: bytes, regs: dict, mem: Memory, ss: int):
    ns = dict(regs)
    ns["ss"] = ss
    ns["ds"] = regs.get("ds", ss)
    ns["mem"] = mem
    ns["_PARITY"] = [0] * 256
    exec("\n".join(_emit(code)), {}, ns)
    return ns


def _run_interp(code: bytes, regs: dict, mem: Memory, ss: int):
    st = CPUState(cs=0x2000, ip=0, ss=ss, ds=regs.get("ds", ss),
                  **{r: regs.get(r, 0) for r in W16 if r not in ("sp",)})
    st.sp = regs.get("sp", 0x100)
    cpu = CPU8086(mem, st)
    for k, b in enumerate(code):
        mem.data[(0x2000 << 4) + k] = b
    cpu.step()
    return cpu.s


def _check(code: bytes, regs: dict, compare_regs, *, ss=0x3000):
    m1, m2 = Memory(), Memory()
    ns = _run_emitted(code, regs, m1, ss)
    s = _run_interp(code, regs, m2, ss)
    for r in compare_regs:
        assert ns[r] & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{code.hex()}: {r} emitted={ns[r] & 0xFFFF:04X} "
            f"interp={getattr(s, r) & 0xFFFF:04X}")
    # stack region must agree too (pusha/popa write memory)
    base = ss << 4
    assert bytes(m1.data[base:base + 0x200]) == bytes(m2.data[base:base + 0x200])


def test_idiv16_positive_and_negative_truncate_toward_zero() -> None:
    # F7 /7 idiv bx.  -7 / 2 = -3 rem -1 (truncation), NOT floor's -4 rem 1.
    for dx, ax, bx in [(0, 100, 7), (0xFFFF, 0xFFF9, 2),      # -7 / 2
                       (0xFFFF, 0xFFF9, 0xFFFE),               # -7 / -2
                       (0, 0x0007, 0xFFFE)]:                   # 7 / -2
        _check(bytes.fromhex("f7fb"), {"dx": dx, "ax": ax, "bx": bx},
               ("ax", "dx"))


def test_idiv8_matches_interp() -> None:
    # F6 /7 idiv bl (8-bit): AX / BL -> AL quotient, AH remainder.
    for ax, bx in [(100, 7), (0xFFF9, 2), (0xFFF9, 0xFFFE)]:
        _check(bytes.fromhex("f6fb"), {"ax": ax, "bx": bx}, ("ax",))


def test_pusha_snapshots_sp_before_pushing() -> None:
    # 0x60 pusha: the SP written to the stack is the value BEFORE any push.
    _check(bytes.fromhex("60"),
           {"ax": 0x1111, "cx": 0x2222, "dx": 0x3333, "bx": 0x4444,
            "sp": 0x100, "bp": 0x5555, "si": 0x6666, "di": 0x7777},
           ("sp",))


def test_add_sub_sp_imm_match_interp() -> None:
    # 83 c4 08  add sp, 8   (cdecl caller cleanup)
    # 83 ec 04  sub sp, 4   (frameless local alloc)
    # 81 c4 00 01  add sp, 0x100  (imm16 form)
    # 83 c4 fc  add sp, -4  (imm8 sign-extends)
    for code in ("83c408", "83ec04", "81c40001", "83c4fc"):
        _check(bytes.fromhex(code), {"sp": 0x0120}, ("sp",))


def test_mov_reg_sp_captures_the_stack_pointer() -> None:
    # 8b dc  mov bx, sp     (frameless arg base)
    # 8b ec  mov bp, sp     (hand-rolled prologue's frame set)
    # 89 e3  mov bx, sp     (the 89 encoding: sp is the reg source)
    # 89 e5  mov bp, sp
    for code, dst in [("8bdc", "bx"), ("8bec", "bp"),
                      ("89e3", "bx"), ("89e5", "bp")]:
        _check(bytes.fromhex(code), {"sp": 0x0abc}, (dst,))


def _emit_seq(code: bytes) -> list[str]:
    """Translate a byte string of back-to-back instructions (not just one)."""
    lines: list[str] = []
    off = 0
    while off < len(code):
        inst = decode_one(lambda o: code[o] if o < len(code) else 0x90, off)
        _translate(inst, lines, set())
        off += inst.length
    return lines


def test_mov_bx_sp_then_read_stack_arg_matches_interp() -> None:
    # The whole frameless idiom end to end: `mov bx,sp; mov ax,[bx+2]` reads the
    # word 2 bytes above sp (the caller's first pushed arg in the small model,
    # where the read defaults to DS == SS).  Seed that slot and require the
    # emitted pair to read it exactly as the VM does.
    ss = 0x3000
    code = bytes.fromhex("8bdc8b4702")          # mov bx,sp ; mov ax,[bx+2]
    m1, m2 = Memory(), Memory()
    for m in (m1, m2):
        m.data[(ss << 4) + 0x0102] = 0x34       # [sp+2] low
        m.data[(ss << 4) + 0x0103] = 0x12       # [sp+2] high
    ns = {"ss": ss, "ds": ss, "sp": 0x0100, "mem": m1, "_PARITY": [0] * 256}
    exec("\n".join(_emit_seq(code)), {}, ns)
    s = CPUState(cs=0x2000, ip=0, ss=ss, ds=ss)
    s.sp = 0x0100
    cpu = CPU8086(m2, s)
    for k, b in enumerate(code):
        m2.data[(0x2000 << 4) + k] = b
    cpu.step(); cpu.step()
    assert ns["ax"] & 0xFFFF == cpu.s.ax & 0xFFFF == 0x1234


def test_popa_discards_the_saved_sp() -> None:
    # 0x61 popa restores all but SP (the stacked sp word is skipped). Round-trip
    # a pusha frame: push then pop must return every register unchanged.
    m = Memory()
    ss = 0x3000
    regs = {"ax": 0x1111, "cx": 0x2222, "dx": 0x3333, "bx": 0x4444,
            "sp": 0x100, "bp": 0x5555, "si": 0x6666, "di": 0x7777}
    ns = _run_emitted(bytes.fromhex("60"), regs, m, ss)      # pusha
    after = _run_emitted(bytes.fromhex("61"),
                         {r: ns[r] for r in list(W16) + []}, m, ss)  # popa
    for r in ("ax", "cx", "dx", "bx", "bp", "si", "di", "sp"):
        assert after[r] & 0xFFFF == regs[r], f"{r} not restored by popa/pusha"
