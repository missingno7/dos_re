"""``sp`` as a SOURCE, and the frame restore that lands at an OFFSET from bp.

Two shapes that a C compiler's runtime emits routinely, both of which used to
collapse into the single refusal ``sp-as-data``:

* **sp read as an ordinary operand** -- ``cmp ax,sp`` / ``sub ax,sp``.  A routine
  that REPORTS the free stack does exactly this: compare the current stack
  pointer against the limit word the runtime keeps, and return the difference.
  Reading sp is not sp-as-data.  The CPUless ABI keeps ``sp`` EXACT -- that is
  the premise the whole depth walk rests on, and the emitted body carries it as
  an ordinary integer local -- so an instruction that takes sp as a source and
  does not write it consumes a value the body already has and changes nothing
  about the stack.  The refusal is about sp as a DESTINATION.

  This subsumes the two narrow recognisers that preceded it, each of which had
  admitted one shape at a time: ``mov r16, sp`` (a frameless routine capturing
  its frame base to index stack args) and ``mov m16, sp`` (a setjmp snapshot).

* **``lea sp, [bp+disp]``** -- the un-fused teardown naming a slot at a FIXED
  offset from the frame base, so the single instruction lands sp on a register
  saved inside the frame rather than on the base itself.  It is precisely what
  ``mov sp,bp`` is with a zero offset; modelling the offset as a NUMBER
  (``_frame_restore_disp``) keeps the teardown depth exactly computable
  (``frame_base - bias - disp``).  That is the same move ``_bp_const_bias``
  makes for bp, on the other side of the assignment -- and the two compose: a
  frame pointer carried at a bias AND torn down at an offset resolves to the
  sum, with no case for either.

These are DIFFERENTIAL regressions: the composed CPUless body is exec'd and its
whole register file + stack memory diffed against stepping the identical bytes
through the interpreter (``CPU8086``).  They FAIL on the old emitter -- each
positive body refuses ``sp-as-data``, so there is no body to compare.

The negative guards are what keep the relaxation sound.  An sp WRITE from a
register (``mov sp,ax``, ``sub sp,dx`` -- a runtime-sized stack allocation)
still refuses ``sp-as-data``; ``lea sp,[bp+si]`` carries a RUNTIME index rather
than a constant and so is not a frame restore at all and still refuses; and a
``lea sp,[bp+disp]`` with no matching establish still refuses, so the restore
stays gated on a real frame.
"""
from __future__ import annotations

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.cpuless import register_effects
from dos_re.lift.emit_cpuless import (Refusal, _frame_restore_disp,
                                      check_promotable, emit_recovered)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")

# --- positive bodies -------------------------------------------------------

# cmp ax,sp; sbb dx,dx; sub ax,sp; neg ax; ret
#   -- sp as a plain ALU SOURCE, twice, with the flags it sets consumed in
#      between.  The "how much stack is left" routine, reduced to its essence.
_SP_AS_SOURCE = bytes.fromhex(
    "3bc4"      # cmp ax, sp          <- sp as a source (refused before)
    "1bd2"      # sbb dx, dx          <- consume the borrow, so the flags matter
    "2bc4"      # sub ax, sp          <- sp as a source again
    "f7d8"      # neg ax
    "c3")       # ret

# push bp; mov bp,sp; push ds; sub sp,4; mov [bp-4],cx; lea sp,[bp-2];
# pop ds; pop bp; ret
#   -- the teardown lands two bytes BELOW the frame base, exactly on the saved
#      ds, in one instruction.  bp is never biased, so the whole correction
#      comes from the lea's displacement.
_LEA_RESTORE = bytes.fromhex(
    "55"        # push bp
    "8bec"      # mov bp, sp
    "1e"        # push ds
    "83ec04"    # sub sp, 4
    "894efc"    # mov [bp-4], cx
    "8d66fe"    # lea sp, [bp-2]      <- the restore-at-an-offset
    "1f"        # pop ds
    "5d"        # pop bp
    "c3")       # ret

# mov ax,ds; inc bp; push bp; mov bp,sp; push ds; mov ds,ax; xor ax,ax;
# lea sp,[bp-2]; pop ds; pop bp; dec bp; ret
#   -- the BIASED frame pointer (the tagged-far-frame prologue) together with
#      the offset teardown.  bp's bias is 0 at the lea and the lea contributes
#      -2, and the two corrections have to compose for the depth to land on the
#      saved ds.  Runs with ds != ss.
_BIASED_PLUS_LEA_RESTORE = bytes.fromhex(
    "8cd8"      # mov ax, ds
    "45"        # inc bp              <- tag the frame pointer
    "55"        # push bp
    "8bec"      # mov bp, sp
    "1e"        # push ds
    "8ed8"      # mov ds, ax          <- __loadds
    "33c0"      # xor ax, ax
    "8d66fe"    # lea sp, [bp-2]
    "1f"        # pop ds
    "5d"        # pop bp
    "4d"        # dec bp              <- remove the tag
    "c3")       # ret


def _fn_scan(code: bytes):
    return scan_function(lambda off: code[off] if off < len(code) else 0x90, 0)


def _interp(code: bytes, regs: dict, mem: Memory, ss: int):
    """Step the interpreter through the whole body EXCEPT the trailing ``ret``
    (the ret pop is the adapter's job, not the body's register effect)."""
    st = CPUState(cs=0x2000, ip=0, ss=ss, ds=regs.get("ds", ss),
                  **{r: regs.get(r, 0) for r in W16 if r != "sp"})
    st.sp = regs.get("sp", 0x0100)
    cpu = CPU8086(mem, st)
    for k, b in enumerate(code):
        mem.data[(0x2000 << 4) + k] = b
    while cpu.s.ip < len(code) - 1:            # stop at the final ret
        cpu.step()
    return cpu.s


def _run_body(code: bytes, inputs: dict, mem: Memory, ss: int):
    scan = _fn_scan(code)
    spec = check_promotable(scan)              # <- refuses on the old emitter
    src = emit_recovered(scan, spec.abi, "2000:0000",
                         recovered_import_base="x", needs_plat=spec.needs_plat,
                         df_livein=spec.df_livein, sp_output=spec.sp_output,
                         flags_livein=spec.flags_livein)
    ns: dict = {"_PARITY": [0] * 256}
    exec(compile(src, "<rec>", "exec"), ns)
    fn = next(v for k, v in ns.items() if k.startswith("func_"))
    out, _compat = fn(mem=mem, ss=ss, **inputs)
    return out


def _assert_matches(code: bytes, inputs: dict, ss: int, seed=None):
    m_body, m_interp = Memory(), Memory()
    for m in (m_body, m_interp):
        for addr, val in (seed or {}).items():
            m.data[addr] = val
    out = _run_body(code, dict(inputs), m_body, ss)
    s = _interp(code, {**inputs, "sp": inputs["sp"]}, m_interp, ss)
    for r in ("ax", "cx", "dx", "bx", "bp", "si", "di"):
        expected = out[r] if r in out else inputs.get(r, 0)
        assert expected & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{r}: body={expected & 0xFFFF:04X} interp={getattr(s, r):04X}")
    base = ss << 4
    assert bytes(m_body.data[base:base + 0x200]) == \
        bytes(m_interp.data[base:base + 0x200]), "stack memory diverged"
    return out


# --- positives -------------------------------------------------------------

def test_sp_as_alu_source_promotes_and_matches_interpreter() -> None:
    """``cmp ax,sp`` / ``sub ax,sp``: sp is consumed as a value, never written.

    The body must reproduce the interpreter's registers exactly -- which it can
    only do if its ``sp`` local really holds the caller's stack pointer, so this
    also pins that sp became a contract INPUT."""
    sp_in = 0x0100
    out = _assert_matches(_SP_AS_SOURCE,
                          {"ax": 0x0180, "dx": 0x0000, "sp": sp_in},
                          ss=0x3000)
    assert out["ax"] & 0xFFFF == (-(0x0180 - sp_in)) & 0xFFFF
    assert out["dx"] & 0xFFFF == 0            # 0x180 >= 0x100: no borrow


def test_sp_as_source_below_the_stack_pointer_matches() -> None:
    """The same body at a different caller sp, taking the OTHER flag path
    (``cmp`` borrows, so ``sbb dx,dx`` yields 0xFFFF)."""
    out = _assert_matches(_SP_AS_SOURCE,
                          {"ax": 0x0040, "dx": 0x1234, "sp": 0x0100},
                          ss=0x3000)
    assert out["dx"] & 0xFFFF == 0xFFFF


def test_lea_frame_restore_promotes_and_matches_interpreter() -> None:
    """``lea sp,[bp-2]`` tears the frame down onto the saved ds in one
    instruction.  The depth walk must land on exactly that slot, or the
    following ``pop ds ; pop bp`` reads the wrong words and the register file
    diverges from the interpreter."""
    ss = 0x3000
    out = _assert_matches(_LEA_RESTORE,
                          {"cx": 0xBEEF, "bp": 0x7777, "sp": 0x0100,
                           "ds": ss},
                          ss=ss)
    assert out["bp"] & 0xFFFF == 0x7777       # the saved bp came back


def test_biased_bp_and_lea_restore_compose() -> None:
    """A frame pointer carried at a constant BIAS and torn down at a constant
    OFFSET are two corrections to the same distance, and they compose.

    Runs with ``ds != ss`` (``mov ds,ax`` reloads it), so a body that confused
    the two segments would diff against the interpreter here."""
    ss, ds = 0x3000, 0x5000
    out = _assert_matches(_BIASED_PLUS_LEA_RESTORE,
                          {"ax": 0, "bp": 0x7777, "sp": 0x0100, "ds": ds},
                          ss=ss)
    assert out["ax"] & 0xFFFF == 0
    assert out["bp"] & 0xFFFF == 0x7777       # tagged, saved, restored, untagged


# --- what the recogniser reads off the encoding ----------------------------

@pytest.mark.parametrize("hexs,expect", [
    ("8be5", 0),                # mov sp, bp        -- the zero-offset form
    ("89ec", 0),                # mov sp, bp        -- the other encoding
    ("8d66fe", -2),             # lea sp, [bp-2]    (disp8, sign-extended)
    ("8d6604", 4),              # lea sp, [bp+4]    (disp8)
    ("8da600ff", -0x100),       # lea sp, [bp-0x100] (disp16)
    ("8d22", None),             # lea sp, [bp+si]   -- a RUNTIME index
    ("8d23", None),             # lea sp, [bp+di]   -- a RUNTIME index
    ("8d260010", None),         # lea sp, [0x1000]  -- no bp at all
    ("8d5efe", None),           # lea bx, [bp-2]    -- not a write to sp
])
def test_frame_restore_disp_reads_the_encoding(hexs, expect) -> None:
    """The offset comes off the ADDRESSING FORM, not off a byte signature: both
    ``mov sp,bp`` encodings resolve to 0, both immediate widths of
    ``lea sp,[bp+disp]`` resolve to their displacement, and anything carrying a
    runtime index -- or not naming sp/bp at all -- resolves to None."""
    scan = _fn_scan(bytes.fromhex(hexs) + b"\xc3")
    assert _frame_restore_disp(scan.insts[0]) == expect


# --- negative guards: the relaxation must not launder an sp WRITE ----------

@pytest.mark.parametrize("hexs,what", [
    ("8be0", "mov sp, ax"),     # sp from a register
    ("2be2", "sub sp, dx"),     # a RUNTIME-SIZED stack allocation
    ("03e1", "add sp, cx"),
    ("8d22", "lea sp, [bp+si]"),   # a frame restore at a RUNTIME offset
])
def test_computed_sp_write_still_refuses_sp_as_data(hexs, what) -> None:
    """Writing sp with a value the analysis cannot pin makes the stack depth
    unknowable and the frame unrecoverable.  That is what ``sp-as-data`` is
    for, and relaxing the READ side must not touch it -- including for a
    ``lea sp,[bp+reg]`` whose offset is a runtime index rather than a
    constant."""
    with pytest.raises(Refusal, match="sp-as-data"):
        check_promotable(_fn_scan(bytes.fromhex(hexs) + b"\xc3"))
    assert "sp" in register_effects(_fn_scan(
        bytes.fromhex(hexs) + b"\xc3").insts[0]).writes, what


def test_lea_restore_without_establish_still_refuses() -> None:
    """``lea sp,[bp-2]`` restores to a frame base -- with no establish there is
    no base to restore to, exactly as for ``mov sp,bp``."""
    # push bp; lea sp,[bp-2]; pop bp; ret   -- no `mov bp,sp`
    code = bytes.fromhex("55" "8d66fe" "5d" "c3")
    with pytest.raises(Refusal, match="frame-restore-without-establish"):
        check_promotable(_fn_scan(code))
