"""dos_re.lift x87 support: differential proof that lifted FP == interpreted FP.

The ESC opcodes (D8-DF) scan as ordinary modrm-shaped SEQ instructions and the
emitter delegates their semantics to the interpreter's own helpers
(``cpu.fpu_reg_op`` / ``cpu.fpu_mem_op`` — the factored halves of
``execute_fpu``), so FP behaviour has ONE source of truth.  These tests prove
the whole seam: static scan (lengths, incl. seg-override forms), native EA
computation at the emitted site, helper dispatch, virtual time, and the
fail-loud contract (FP-stack over/underflow and unimplemented forms raise
UnsupportedInstruction identically on both paths).

House pattern (test_lift_emit.py): hand-assembled synthetic code, randomized
start states — including the FP stack, status and control words — interpreted
vs lifted, full-state diff.  FP registers are compared bit-exact via their
IEEE-754 encodings (NaN-safe).  Synthetic code only (game-free tests rule).
"""
from __future__ import annotations

import random
import struct

import pytest

from dos_re.cpu import CPU8086, CPUState, UnsupportedInstruction
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit import emit_function
from dos_re.memory import Memory

CS = 0x1000
ENTRY = 0x0100
RET_IP = 0xBEEF
CS_DATA = 0x0400          # offset of FP constants planted in the CODE segment


def _lift(code: bytes, **kw):
    fetch = lambda off: code[(off - ENTRY) & 0xFFFF] if 0 <= (off - ENTRY) < len(code) else 0x90
    scan = scan_function(fetch, ENTRY)
    assert scan.liftable, [(f"{r.ip:04X}", r.reason, r.detail) for r in scan.refusals]
    src = emit_function(scan, CS, "lifted", signature=code[:8], **kw)
    ns: dict = {}
    exec(compile(src, "<lifted>", "exec"), ns)   # noqa: S102 — that's the point
    return ns["lifted"], scan, src


def _make_cpu(code: bytes, state: CPUState, data: bytes = b"",
              cs_data: bytes = b"") -> CPU8086:
    mem = Memory()
    mem.load(CS, ENTRY, code)
    if data:
        mem.load(0x4000, 0x0000, data)
    if cs_data:
        mem.load(CS, CS_DATA, cs_data)
    cpu = CPU8086(mem, state)
    cpu.trace_enabled = False
    cpu.push(RET_IP)
    return cpu


def _rand_state(rng: random.Random) -> CPUState:
    return CPUState(
        ax=rng.randrange(0x10000), bx=0, cx=rng.randrange(1, 8),
        dx=rng.randrange(0x10000),
        si=rng.randrange(0x100), di=rng.randrange(0x100), bp=rng.randrange(0x100),
        sp=0x2000, cs=CS, ip=ENTRY, ds=0x4000, es=0x4000, ss=0x3000,
        flags=(rng.getrandbits(16) & 0x0CD5) | 0x0202,
        fst=[_rand_double(rng) for _ in range(rng.randrange(3))],
        fsw=rng.getrandbits(16) & 0x47FF,
        fcw=0x037F | (rng.randrange(4) << 10),      # randomized rounding control
    )


def _rand_double(rng: random.Random) -> float:
    if rng.random() < 0.3:
        return float(rng.randrange(-0x8000, 0x8000))     # integral (FIST-friendly)
    return rng.uniform(-1e6, 1e6)


def _rand_fp_data(rng: random.Random, n_doubles: int = 8) -> bytes:
    """A data page whose first quadwords are well-formed finite doubles (so
    FLD m64 loads real values), followed by random filler for the int forms."""
    doubles = b"".join(struct.pack("<d", _rand_double(rng) or 1.0)
                       for _ in range(n_doubles))
    return doubles + bytes(rng.randrange(256) for _ in range(0x100 - len(doubles)))


def _clone(st: CPUState) -> CPUState:
    kw = {k: getattr(st, k) for k in st.__slots__}
    kw["fst"] = list(st.fst)                             # never share the stack list
    return CPUState(**kw)


def _fst_bits(fst) -> list[bytes]:
    return [struct.pack("<d", v) for v in fst]           # bit-exact, NaN-safe


def _run_interpreted(cpu: CPU8086, limit: int = 20000) -> None:
    for _ in range(limit):
        if (cpu.s.cs, cpu.s.ip) == (CS, RET_IP):
            return
        cpu.step()
    raise AssertionError("interpreted run did not reach the return address")


def _assert_fpu_equivalent(code: bytes, *, cases: int = 40, seed: int = 0xF80,
                           fst_min: int = 0, cs_doubles: int = 0, **kw) -> str:
    kw.setdefault("count_instructions", True)
    lifted, _scan, src = _lift(code, **kw)
    rng = random.Random(seed)
    for case in range(cases):
        state = _rand_state(rng)
        while len(state.fst) < fst_min:                  # ops that need depth
            state.fst.append(_rand_double(rng))
        data = _rand_fp_data(rng)
        cs_data = b"".join(CPU8086._double_to_f80(_rand_double(rng))
                           for _ in range(cs_doubles))

        asm = _make_cpu(code, _clone(state), data, cs_data)
        hook = _make_cpu(code, _clone(state), data, cs_data)
        _run_interpreted(asm)
        lifted(hook)

        assert (hook.s.cs, hook.s.ip) == (CS, RET_IP), f"case {case}: lifted did not return"
        assert asm.s.snapshot() == hook.s.snapshot(), f"case {case} registers/flags\n{src}"
        assert _fst_bits(asm.s.fst) == _fst_bits(hook.s.fst), f"case {case} FP stack\n{src}"
        assert (asm.s.fsw, asm.s.fcw) == (hook.s.fsw, hook.s.fcw), \
            f"case {case} FP status/control\n{src}"
        assert asm.mem.data == hook.mem.data, f"case {case} memory\n{src}"
        assert asm.instruction_count == hook.instruction_count, \
            f"case {case} virtual time\n{src}"
    return src


# --- the scan-level gate: x87 no longer refuses ----------------------------------

def test_x87_function_scans_liftable_and_emits_native():
    # The observed MSC floating-point runtime-code shape
    # mixing mem and register forms.  Scans clean, emits with NO interpreter
    # fallback — every x87 line is a native call into the shared FPU helpers.
    code = bytes.fromhex(
        "BB0000"      # mov bx, 0
        "DD07"        # fld qword [bx]
        "DD4608"      # fld qword [bp+8]     (ss-default r/m form)
        "D9C9"        # fxch st(1)
        "DEC1"        # faddp st(1), st
        "D9E0"        # fchs
        "DD5F10"      # fstp qword [bx+0x10]
        "DDD8"        # fstp st(0)
        "C3")
    _lifted, scan, src = _lift(code)
    assert scan.liftable
    assert "# (interpreter fallback)" not in src
    assert "cpu.fpu_mem_op(0xDD, 0, s.ds, _o)" in src
    assert "cpu.fpu_reg_op(0xDE, 0, 1)" in src           # faddp st(1),st


# --- differential proofs, one family per test -------------------------------------

def test_fld_fst_fstp_m64_and_fstp_st0():
    # dd 07/dd 46 xx (FLD m64), dd 16/dd 57 (FST m64), dd 5f (FSTP m64),
    # dd d8 (FSTP ST(0)) — the observed MSC load/store family.
    code = bytes.fromhex(
        "BB0000"      # mov bx, 0
        "DD07"        # fld qword [bx]
        "DD4708"      # fld qword [bx+8]
        "DD5710"      # fst qword [bx+0x10]
        "DD5F18"      # fstp qword [bx+0x18]
        "DDD8"        # fstp st(0)
        "C3")
    src = _assert_fpu_equivalent(code)
    assert "# (interpreter fallback)" not in src


def test_register_arithmetic_and_constants():
    # d9 e8/ee (FLD1/FLDZ), d9 c9 (FXCH), d9 e0 (FCHS), d9 e1 (FABS),
    # d8 c1 (FADD ST,ST(1)), dc ca (FMUL ST(2),ST), de e9 (FSUBP).
    code = bytes.fromhex(
        "D9E8"        # fld1
        "D9EE"        # fldz
        "D9C9"        # fxch st(1)
        "D9E0"        # fchs
        "D9E1"        # fabs
        "D8C1"        # fadd st, st(1)
        "DCCA"        # fmul st(2), st       (needs depth 3)
        "DEE9"        # fsubp st(1), st
        "DD1E4000"    # fstp qword [0x0040]
        "DD1E4800"    # fstp qword [0x0048]
        "C3")
    _assert_fpu_equivalent(code, fst_min=1)


def test_mem_arithmetic_m32_m64_and_integer_operands():
    # d8 (m32 float), dc (m64 double), da (m32int), de (m16int) arithmetic.
    code = bytes.fromhex(
        "BB0000"      # mov bx, 0
        "D9E8"        # fld1
        "DC07"        # fadd qword [bx]
        "DC6708"      # fsub qword [bx+8]
        "D84720"      # fadd dword [bx+0x20]
        "D84F24"      # fmul dword [bx+0x24]
        "DA4740"      # fiadd dword [bx+0x40]
        "DE4750"      # fiadd word [bx+0x50]
        "DE4F52"      # fimul word [bx+0x52]
        "DD5F30"      # fstp qword [bx+0x30]
        "C3")
    _assert_fpu_equivalent(code)


def test_fild_fist_fistp_rounding_control():
    # db 46 xx (FILD m32), df 46 (FILD m16), fist/fistp at both widths —
    # exercised under all four rounding-control settings (fcw randomized).
    code = bytes.fromhex(
        "BB0000"      # mov bx, 0
        "DF4740"      # fild word [bx+0x40]
        "DB4744"      # fild dword [bx+0x44]
        "DEC1"        # faddp
        "DF5748"      # fist word [bx+0x48]
        "DB574C"      # fist dword [bx+0x4C]
        "DB5F50"      # fistp dword [bx+0x50]
        "C3")
    _assert_fpu_equivalent(code)


def test_f80_load_store_conversions():
    # db 6e xx (FLD m80) / db 7e xx (FSTP m80): the _f80_to_double /
    # _double_to_f80 conversions, round-tripped through memory.
    code = bytes.fromhex(
        "BB0000"      # mov bx, 0
        "D9E8"        # fld1
        "DC0F"        # fmul qword [bx]
        "DB7F60"      # fstp tbyte [bx+0x60]
        "DB6F60"      # fld tbyte [bx+0x60]
        "DD5F70"      # fstp qword [bx+0x70]
        "C3")
    _assert_fpu_equivalent(code)


def test_control_and_status_words():
    # d9 7e xx (FNSTCW m16, ss-based [bp+disp]), d9 3e xx (FNSTCW direct),
    # d9 6e xx (FLDCW), db e2 (FNCLEX), df e0 (FNSTSW AX), dd 7f (FNSTSW m16).
    code = bytes.fromhex(
        "D97E10"      # fnstcw [bp+0x10]
        "D93E8000"    # fnstcw [0x0080]
        "D96E10"      # fldcw [bp+0x10]
        "DBE2"        # fnclex
        "DFE0"        # fnstsw ax
        "DD7F20"      # fnstsw [bx+0x20]
        "C3")
    src = _assert_fpu_equivalent(code)
    assert "s.ss" in src                                 # bp-based EA kept its segment


def test_wait_prefix_instruction_is_native():
    # 9b d9 3e xx xx — FSTCW is WAIT + FNSTCW; both instructions emit native.
    code = bytes.fromhex("9B" "D93E8000" "C3")
    src = _assert_fpu_equivalent(code)
    assert "# (interpreter fallback)" not in src
    assert "pass  # wait" in src


def test_segment_override_forms():
    # 26 dd 07 (es: FLD m64) and 2e db 2f (cs: FLD m80) — the observed
    # override forms; the cs: constant lives in the code segment.
    code = bytes.fromhex(
        "BB0004"      # mov bx, CS_DATA (0x0400)
        "2EDB2F"      # cs: fld tbyte [bx]
        "BB0000"      # mov bx, 0
        "26DD07"      # es: fld qword [bx]
        "DEC1"        # faddp
        "DD5F78"      # fstp qword [bx+0x78]
        "C3")
    src = _assert_fpu_equivalent(code, cs_doubles=1)
    assert "cpu.fpu_mem_op(0xDB, 5, s.cs, _o)" in src
    assert "cpu.fpu_mem_op(0xDD, 0, s.es, _o)" in src


def test_compare_status_word_drives_a_branch():
    # The MSC compare shape: FCOM/FCOMP set C0/C2/C3, FNSTSW AX + SAHF turn
    # them into CPU flags, a conditional branch consumes them.
    code = bytes.fromhex(
        "BB0000"      # mov bx, 0
        "DD07"        # fld qword [bx]
        "DC5708"      # fcom qword [bx+8]
        "DFE0"        # fnstsw ax
        "9E"          # sahf
        "7603"        # jbe +3
        "40"          # inc ax
        "DDD8"        # fstp st(0)   (0x010C: joined path... keep both popping)
        "C3")
    # note: jbe target 0x010C is the fstp — both paths pop the loaded value.
    _assert_fpu_equivalent(code)


def test_fsqrt_frndint_on_abs_values():
    code = bytes.fromhex(
        "BB0000"      # mov bx, 0
        "DD07"        # fld qword [bx]
        "D9E1"        # fabs
        "D9FA"        # fsqrt
        "D9FC"        # frndint     (honours the randomized rounding control)
        "DD5F08"      # fstp qword [bx+8]
        "C3")
    _assert_fpu_equivalent(code)


# --- the fail-loud contract --------------------------------------------------------

def _raise_both_paths(code: bytes, fst, match: str) -> None:
    lifted, _scan, _src = _lift(code)
    st = _rand_state(random.Random(0xDEAD))
    st.fst = list(fst)
    asm = _make_cpu(code, _clone(st))
    hook = _make_cpu(code, _clone(st))
    with pytest.raises(UnsupportedInstruction, match=match):
        _run_interpreted(asm)
    with pytest.raises(UnsupportedInstruction, match=match):
        lifted(hook)


def test_fp_stack_underflow_raises_on_both_paths():
    # FSTP m80 pops before it reads (the _fpop underflow guard); an empty FP
    # stack raises the interpreter's own UnsupportedInstruction on both paths.
    _raise_both_paths(bytes.fromhex("DB7F00" "C3"), [], "x87 stack underflow")


def test_fp_stack_overflow_raises_on_both_paths():
    _raise_both_paths(bytes.fromhex("D9E8" * 9 + "C3"),
                      [], "x87 stack overflow")


def test_unimplemented_x87_form_stays_a_loud_refusal():
    # FFREE ST(0) (dd c0): execute_fpu does not implement it — the SAME
    # UnsupportedInstruction fires interpreted and lifted (never guess).
    _raise_both_paths(bytes.fromhex("DDC0" "C3"), [1.0], "x87 opcode DD")


def test_fst_underflow_raises_unsupported_not_indexerror():
    """Reading/writing ST(i) on an empty (or too-shallow) x87 stack must raise
    the model's standard UnsupportedInstruction -- the fail-loud signal the
    runner's gap-snapshot machinery understands -- NOT a bare IndexError.

    Regression: _fst / _fst_set indexed self.s.fst[-1-i] with no bounds check,
    so a memory-form FPU op reading ST(0) on an empty stack (reachable on an
    upstream execution divergence, e.g. VGA Lemmings' interactive cold-boot
    load path 2026-07-17) died with an opaque IndexError mid-step."""
    st = CPUState(cs=CS, ip=ENTRY, ss=0x9000, sp=0x2000)
    cpu = _make_cpu(b"\x90", st)
    assert cpu.s.fst == []                      # empty x87 stack
    with pytest.raises(UnsupportedInstruction):
        cpu._fst(0)
    with pytest.raises(UnsupportedInstruction):
        cpu._fst_set(0, 1.0)
    cpu._fpush(3.5)                             # one value -> ST(0) ok, ST(1) not
    assert cpu._fst(0) == 3.5
    with pytest.raises(UnsupportedInstruction):
        cpu._fst(1)
