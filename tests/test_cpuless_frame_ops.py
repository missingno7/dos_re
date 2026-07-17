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

from dos_re.lift.cfg import scan_function
from dos_re.lift.cpuless import register_effects
from dos_re.lift.emit_cpuless import _is_sp_capture, _is_stack_family


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


def test_stack_family_admits_the_sp_discipline_forms() -> None:
    for code in ("83c408", "83ec04", "8bdc", "8bec", "89e3"):
        assert _is_stack_family(_inst(bytes.fromhex(code))), code
    # the frame RESTORE `mov sp,bp` is deliberately still refused (sp write).
    assert not _is_stack_family(_inst(bytes.fromhex("8be5")))
