"""A frame pointer carried BIASED by a compile-time constant promotes.

A compiler does not always keep the frame pointer pointing exactly at the frame
base.  Two shapes appear in real object code, and both used to refuse:

* **a tagged saved frame pointer** -- ``inc bp ; push bp ; mov bp,sp`` ... ``mov
  sp,bp ; pop bp ; dec bp``.  bp is biased BEFORE it is saved so the stacked
  word carries a tag a stack walker can test, and the epilogue removes the tag
  symmetrically.  The old analysis credited a ``push bp`` only when bp ALREADY
  held the frame base, so this prologue's save -- which happens one instruction
  before ``mov bp,sp`` makes bp the base -- was never counted, and the matching
  ``pop bp`` refused ``frame-pointer-pop-without-save``.
* **a biased teardown** -- ``dec bp ; dec bp ; mov sp,bp ; pop ds ; pop bp``.
  The frame pointer is biased to NAME A SLOT inside the frame (here the saved
  ``ds``), so the single ``mov sp,bp`` lands sp on that register rather than on
  the base.  The old analysis saw a write to bp and called it
  ``frame-pointer-clobbered``.

Both are the same mechanism: ``bp == framebase + k`` for a statically known
``k``.  Modelling that number (``_bp_const_bias``) keeps bp frame-derived across
the arithmetic, so the teardown depth stays exactly computable
(``frame_base - bias``) instead of collapsing to "clobbered".  Nothing here
recognises a byte sequence -- ``inc``/``dec``/``add``/``sub`` on bp all fall out
of the one rule, so any convention built from constant bp arithmetic is covered.

These are DIFFERENTIAL regressions: the composed CPUless body is exec'd and its
whole register file + stack memory diffed against stepping the identical bytes
through the interpreter (``CPU8086``).  They FAIL on the old emitter (the
function refuses, so there is no body to compare).  The negative guards prove
the relaxation stays sound -- a genuine non-constant clobber at a teardown, and
a BIASED fused ``leave`` (whose pop would read a slot that is not the saved
base), both still refuse.

The biased-teardown case also pins the DS != SS contract: it runs with
``ds != ss`` and reads through BOTH, so a body that confused the two would
diff against the interpreter here.
"""
from __future__ import annotations

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit_cpuless import (Refusal, _bp_const_bias,
                                      _check_frame_pointer, check_promotable,
                                      emit_recovered)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")

# inc bp; push bp; mov bp,sp; sub sp,4; mov [bp-4],ax; add ax,cx;
# mov [bp-2],ax; mov dx,[bp-4]; mov sp,bp; pop bp; dec bp; ret
#   -- the tagged-frame-pointer prologue/epilogue around a real frame with two
#      locals.  bp is biased +1 across the push and un-biased after the pop.
_TAGGED = bytes.fromhex(
    "45"        # inc bp             <- tag the frame pointer
    "55"        # push bp            <- the SAVE the old model never counted
    "8bec"      # mov bp, sp
    "83ec04"    # sub sp, 4
    "8946fc"    # mov [bp-4], ax
    "03c1"      # add ax, cx
    "8946fe"    # mov [bp-2], ax
    "8b56fc"    # mov dx, [bp-4]
    "8be5"      # mov sp, bp         <- teardown at bias 0
    "5d"        # pop bp
    "4d"        # dec bp             <- remove the tag
    "c3")       # ret

# inc bp; push bp; mov bp,sp; push ds; mov ds,ax; mov cx,[0x1000];
# mov [bp-4],cx  (via SS); dec bp; dec bp; mov sp,bp; pop ds; pop bp; dec bp; ret
#   -- the biased teardown: `mov sp,bp` lands on the saved ds, two bytes BELOW
#      the frame base.  Reads [0x1000] through DS and writes [bp-4] through SS.
_BIASED_TEARDOWN = bytes.fromhex(
    "45"        # inc bp
    "55"        # push bp
    "8bec"      # mov bp, sp
    "1e"        # push ds
    "8ed8"      # mov ds, ax          <- __loadds: DS is reloaded, DS != SS
    "83ec04"    # sub sp, 4
    "8b0e0010"  # mov cx, [0x1000]    <- a DS-relative read
    "894efc"    # mov [bp-4], cx      <- a BP-relative (SS) write
    "4d"        # dec bp
    "4d"        # dec bp              <- bias -2: name the saved-ds slot
    "8be5"      # mov sp, bp
    "1f"        # pop ds
    "5d"        # pop bp
    "4d"        # dec bp
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


def test_tagged_frame_pointer_promotes_and_matches_interpreter() -> None:
    """``inc bp; push bp; mov bp,sp`` ... ``pop bp; dec bp`` -- the save that
    happens BEFORE the establish is now counted, so the epilogue's pop no longer
    refuses ``frame-pointer-pop-without-save``."""
    out = _assert_matches(_TAGGED,
                          {"ax": 0x1234, "cx": 0x1111, "bp": 0x7777,
                           "sp": 0x0100},
                          ss=0x3000)
    assert out["ax"] & 0xFFFF == (0x1234 + 0x1111) & 0xFFFF
    assert out["dx"] & 0xFFFF == 0x1234          # the local round-tripped


def test_biased_teardown_promotes_and_matches_interpreter() -> None:
    """``dec bp; dec bp; mov sp,bp`` lands sp on the saved ds -- two bytes below
    the frame base -- and the depth walk follows it exactly.

    Also pins DS != SS: ``ds`` is reloaded from ax, the body reads ``[0x1000]``
    through DS and writes ``[bp-4]`` through SS, and both must land where the
    interpreter puts them."""
    ss, ds = 0x3000, 0x5000
    seed = {(ds << 4) + 0x1000: 0xCD, (ds << 4) + 0x1001: 0xAB,
            (ss << 4) + 0x1000: 0x11, (ss << 4) + 0x1001: 0x22}
    out = _assert_matches(_BIASED_TEARDOWN,
                          {"ax": ds, "bp": 0x7777, "sp": 0x0100, "ds": ss},
                          ss=ss, seed=seed)
    # the DS-relative read took the DS bytes, NOT the same offset in SS.
    assert out["cx"] & 0xFFFF == 0xABCD


def test_old_model_would_have_refused_both() -> None:
    """Pin the exact behaviour the fix changes: both shapes must now pass the
    frame check (the old model raised ``frame-pointer-pop-without-save`` on the
    tagged prologue and ``frame-pointer-clobbered`` on the biased teardown)."""
    assert _check_frame_pointer(_fn_scan(_TAGGED), {}, {}) == {0x12: 0}
    # the biased teardown reports bp two bytes below the base at its `mov sp,bp`
    assert _check_frame_pointer(_fn_scan(_BIASED_TEARDOWN), {}, {}) == {0x13: -2}


def test_non_constant_bp_clobber_at_teardown_still_refuses() -> None:
    """bp loaded from DATA is not frame-derived by any bias -- a teardown that
    reads it still refuses.  This is the guard that keeps the relaxation from
    laundering a genuine clobber."""
    # push bp; mov bp,sp; mov bp,[0x1000]; mov sp,bp; pop bp; ret
    code = bytes.fromhex("55" "8bec" "8b2e0010" "8be5" "5d" "c3")
    with pytest.raises(Refusal, match="frame-pointer-clobbered"):
        _check_frame_pointer(_fn_scan(code), {}, {})


def test_biased_fused_leave_still_refuses() -> None:
    """A fused ``leave`` POPS from the address bp names, so a biased bp would
    read a slot that is not the saved base.  Only the split form (`mov sp,bp`
    plus an explicit pop) is exact at a nonzero bias."""
    # push bp; mov bp,sp; dec bp; dec bp; leave; ret
    code = bytes.fromhex("55" "8bec" "4d" "4d" "c9" "c3")
    with pytest.raises(Refusal, match="frame-pointer-biased-leave"):
        _check_frame_pointer(_fn_scan(code), {}, {})


@pytest.mark.parametrize("hexs,expect", [
    ("45", 1),                 # inc bp
    ("4d", -1),                # dec bp
    ("83c506", 6),             # add bp, 6       (imm8)
    ("83ed06", -6),            # sub bp, 6       (imm8)
    ("83c5fe", -2),            # add bp, -2      (imm8 sign-extended)
    ("81c53412", 0x1234),      # add bp, 0x1234  (imm16)
    ("81ed3412", -0x1234),     # sub bp, 0x1234  (imm16)
    ("8be5", None),            # mov sp, bp      -- not a bp adjustment
    ("8b2e0010", None),        # mov bp, [0x1000] -- not a CONSTANT
])
def test_bp_const_bias_recognises_the_arithmetic_family(hexs, expect) -> None:
    """The bias is read off the ARITHMETIC, not off a fixed byte sequence: inc,
    dec and both immediate widths of add/sub all resolve, and anything that is
    not a constant adjustment resolves to None."""
    scan = _fn_scan(bytes.fromhex(hexs) + b"\xc3")
    assert _bp_const_bias(scan.insts[0]) == expect
