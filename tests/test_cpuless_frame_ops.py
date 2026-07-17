"""The 80186 frame ops, without which a Borland-compiled program cannot promote.

`enter N,0` / `leave` are how nearly every function of a Borland/Turbo DOS
program opens and closes. Unmodelled, the ABI analysis refuses each one
(`unanalyzed-opcode-C8`/`-C9`) and the refusal blocks the containing function --
so it is not 85 stray instructions, it is 85 FUNCTIONS, which was skyroads' single
largest M3 blocker. The Lemmings pilot never needed them.

This is the ABI/stack analysis only. The VMless emitter has always had native
forms for all of these -- the corpus contains zero interp_one.
"""
from __future__ import annotations

import pytest

from dos_re.lift.cfg import scan_function
from dos_re.lift.cpuless import register_effects
from dos_re.lift.emit_cpuless import (Refusal, _check_frame_pointer,
                                      _is_sp_capture, _is_stack_family,
                                      check_promotable, emit_recovered)


def _inst(code: bytes):
    scan = scan_function(lambda off: code[off] if off < len(code) else 0x90, 0)
    return scan.insts[0]


def _eff(code: bytes):
    return register_effects(_inst(code))


def test_enter_level0_is_push_bp_mov_bp_sp_sub_sp() -> None:
    #: c8 16 00 00 -- `enter 0016,00`, the exact opening of skyroads' 1010:4331
    e = _eff(bytes.fromhex("c8160000"))
    assert e.refusal is None
    assert e.stack_delta == -(2 + 0x16)      # the pushed bp AND the frame
    assert {"bp", "sp"} <= e.writes
    assert {"bp", "sp", "ss"} <= e.reads
    assert e.mem_write


def test_enter_frame_size_is_read_from_the_immediate() -> None:
    assert _eff(bytes.fromhex("c8000000")).stack_delta == -2      # enter 0,0
    assert _eff(bytes.fromhex("c8ff0000")).stack_delta == -(2 + 0xFF)
    assert _eff(bytes.fromhex("c800010000"[:8])).stack_delta == -(2 + 0x0100)


def test_enter_with_nesting_refuses_loudly() -> None:
    """Level > 0 copies display words from the caller's frame. Real, rare, and
    not what a C compiler emits -- so refuse rather than model it from a guess.
    An uncertain contract must fail, not approximate."""
    e = _eff(bytes.fromhex("c8160001"))
    assert e.refusal == "enter-nesting-level-1"


def test_leave_is_data_dependent_not_a_refusal() -> None:
    """`leave` is `mov sp,bp; pop bp`: sp comes from a REGISTER, so its delta is
    honestly unknown. That is None, not a refusal -- None only makes
    max_stack_use unknown (a supported report state), while a refusal would
    block the function. The frame it tears down was measured at its `enter`."""
    e = _eff(bytes.fromhex("c9"))
    assert e.refusal is None
    assert e.stack_delta is None
    assert {"bp", "sp"} <= e.writes
    assert "bp" in e.reads
    assert e.mem_read


def test_pusha_popa_move_sixteen_bytes() -> None:
    push = _eff(bytes.fromhex("60"))
    assert push.refusal is None and push.stack_delta == -16
    assert {"ax", "bx", "cx", "dx", "si", "di", "bp"} <= push.reads

    pop = _eff(bytes.fromhex("61"))
    assert pop.refusal is None and pop.stack_delta == +16
    assert {"ax", "bx", "cx", "dx", "si", "di", "bp"} <= pop.writes


def test_imul_three_operand_invents_no_ax_dx() -> None:
    """The 80186 three-operand form touches NO implicit ax/dx -- unlike its F7
    /5 cousin. Modelling it off that one would put registers in the contract
    that the instruction never reads or writes."""
    e = _eff(bytes.fromhex("6bc60a"))        # imul ax, si, 10
    assert e.refusal is None
    assert "ax" in e.writes
    assert "si" in e.reads
    assert "dx" not in e.writes              # the F7 cousin writes dx; this does not
    assert e.stack_delta == 0


def test_imul_three_operand_emits_a_native_form() -> None:
    """The ABI models `imul r16, r/m16, imm8` (above); the CPUless emitter must
    also have a native form, or the containing function refuses at emit time
    (`emitter-unsupported-op-6B-grp0`) -- the last runtime frontier blocker.
    dst := r/m16 * sign-extended-imm, low word; CF=OF on 16-bit overflow; no
    implicit ax/dx (unlike the F7/5 cousin)."""
    # imul ax, si, 10 ; ret  -- 6b c6 0a is the exact form skyroads 4591 uses.
    scan = scan_function(
        lambda off: bytes.fromhex("6bc60ac3")[off]
        if off < 4 else 0x90, 0)
    spec = check_promotable(scan)
    src = emit_recovered(scan, spec.abi, "1010:4591",
                         recovered_import_base="x", needs_plat=spec.needs_plat,
                         df_livein=spec.df_livein, sp_output=spec.sp_output,
                         flags_livein=spec.flags_livein)
    assert "_b = si" in src
    assert "_sb = _b - 0x10000 if _b & 0x8000 else _b" in src
    assert "_t = _sb * (10)" in src
    assert "ax = _t & 0xFFFF" in src
    assert "cf = of = not (-32768 <= _t <= 32767)" in src
    assert "dx =" not in src                 # the three-operand form leaves dx alone
    # exec the body: ax := si * 10 (positive, no overflow).
    ns = {}
    exec(compile(src, "<imul>", "exec"), ns)
    fn = next(v for k, v in ns.items() if k.startswith("func_"))
    out, _compat = fn(mem=None, si=7)
    assert out["ax"] == 70


def test_imul_three_operand_negative_immediate_sign_extends() -> None:
    # imul ax, si, -2  (6b c6 fe): the imm8 0xFE sign-extends to -2, not +254.
    scan = scan_function(
        lambda off: bytes.fromhex("6bc6fec3")[off]
        if off < 4 else 0x90, 0)
    spec = check_promotable(scan)
    src = emit_recovered(scan, spec.abi, "1010:1FD9",
                         recovered_import_base="x", needs_plat=spec.needs_plat,
                         df_livein=spec.df_livein, sp_output=spec.sp_output,
                         flags_livein=spec.flags_livein)
    assert "_t = _sb * (-2)" in src


# --- the Borland stack-arg idiom (sp as maintained-exact quantity) -----------
# The frameless cdecl idioms: `add sp,N` cleans the caller's pushed args,
# `mov bx,sp`/`mov bp,sp` capture a frame base to read them.  sp stays
# STATICALLY EXACT, so these are stack discipline, not sp-as-data.

def test_add_sp_imm_is_a_positive_depth_delta() -> None:
    # `add sp,N` raises sp -- it POPS N bytes, matching a pop's positive delta.
    assert _eff(bytes.fromhex("83c408")).stack_delta == +8      # add sp,8
    assert _eff(bytes.fromhex("83c410")).stack_delta == +16     # add sp,16
    assert _eff(bytes.fromhex("81c40001")).stack_delta == +0x100  # add sp,0x100
    # sub sp,N lowers sp -- it PUSHES (allocates), a negative delta.
    assert _eff(bytes.fromhex("83ec04")).stack_delta == -4      # sub sp,4
    # imm8 sign-extends: add sp,-4 lowers sp like a sub.
    assert _eff(bytes.fromhex("83c4fc")).stack_delta == -4
    for code in ("83c408", "83ec04", "81c40001", "83c4fc"):
        e = _eff(bytes.fromhex(code))
        assert e.refusal is None
        assert e.reads == frozenset({"sp"}) and e.writes == frozenset({"sp"})


def test_grp1_sp_only_add_and_sub_get_a_delta() -> None:
    # and/or/xor/cmp sp,imm are NOT depth arithmetic: sp stays general data
    # there (the generic grp1 path, stack_delta 0 -- gated as sp-as-data).
    assert _eff(bytes.fromhex("81e4f0ff")).stack_delta == 0     # and sp,0xFFF0
    assert _eff(bytes.fromhex("83fc08")).stack_delta == 0       # cmp sp,8


def test_sp_capture_recognises_both_encodings() -> None:
    # mov r16, sp -- sp read into a GP register (frame base).  Both the 8B form
    # (sp is the r/m) and the 89 form (sp is the reg).
    assert _is_sp_capture(_inst(bytes.fromhex("8bdc")))         # mov bx, sp (8B)
    assert _is_sp_capture(_inst(bytes.fromhex("8bec")))         # mov bp, sp (8B)
    assert _is_sp_capture(_inst(bytes.fromhex("89e3")))         # mov bx, sp (89)
    # a WRITE to sp is a frame restore, NOT a capture -- must stay refused.
    assert not _is_sp_capture(_inst(bytes.fromhex("8be5")))     # mov sp, bp (8B)
    assert not _is_sp_capture(_inst(bytes.fromhex("89ec")))     # mov sp, bp (89)
    # an ordinary reg-reg mov not touching sp is not a capture.
    assert not _is_sp_capture(_inst(bytes.fromhex("8bd8")))     # mov bx, ax


def test_hand_rolled_frame_establish_and_restore_effects() -> None:
    # mov bp, sp  (8b ec / 89 e5) -- ESTABLISH: bp := sp, depth unchanged.
    for code in ("8bec", "89e5"):
        e = _eff(bytes.fromhex(code))
        assert e.refusal is None and e.frame_establish and not e.frame_restore_to_base
        assert e.reads == frozenset({"sp"}) and e.writes == frozenset({"bp"})
        assert e.stack_delta == 0
    # mov sp, bp  (8b e5 / 89 ec) -- RESTORE to frame base: sp := bp.
    for code in ("8be5", "89ec"):
        e = _eff(bytes.fromhex(code))
        assert e.refusal is None and e.frame_restore_to_base and not e.frame_establish
        assert e.reads == frozenset({"bp"}) and e.writes == frozenset({"sp"})
        assert e.stack_delta is None            # a reset, not a constant delta
    # a plain reg move (mov bp, ax) is NEITHER.
    n = _eff(bytes.fromhex("8be8"))             # mov bp, ax
    assert not n.frame_establish and not n.frame_restore_to_base


def test_stack_family_admits_the_sp_discipline_forms() -> None:
    # add/sub sp,imm ; mov r16,sp captures ; and the hand-rolled frame restore
    # `mov sp,bp` (8be5 / 89ec) -- all sp-as-discipline, not sp-as-data.
    for code in ("83c408", "83ec04", "8bdc", "8bec", "89e3", "8be5", "89ec"):
        assert _is_stack_family(_inst(bytes.fromhex(code))), code
    # a genuine sp-as-data write (`mov sp, ax`) is still refused.
    assert not _is_stack_family(_inst(bytes.fromhex("8be0")))    # mov sp, ax


def _fn_scan(code: bytes):
    return scan_function(lambda off: code[off] if off < len(code) else 0x90, 0)


def test_bp_used_as_scratch_but_saved_and_restored_is_a_valid_frame() -> None:
    # enter 0; push bp; mov bp,0x1000; add bp,ax; pop bp; leave; ret
    # bp is repurposed as a data pointer, but saved (push bp) and restored
    # (pop bp) so it holds the frame base at `leave`. The frame proof accepts it.
    code = bytes.fromhex("c8000000" "55" "bd0010" "03e8" "5d" "c9" "c3")
    _check_frame_pointer(_fn_scan(code), {}, {})     # must not raise


def test_bp_clobbered_without_restore_still_refuses() -> None:
    # enter 0; mov bp,0x1000; leave; ret -- bp clobbered and NOT restored, so
    # `leave` would read a garbage frame base. Must refuse.
    code = bytes.fromhex("c8000000" "bd0010" "c9" "c3")
    with pytest.raises(Refusal):
        _check_frame_pointer(_fn_scan(code), {}, {})
