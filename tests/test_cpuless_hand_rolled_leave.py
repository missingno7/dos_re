"""A fused ``leave`` established by a HAND-ROLLED ``push bp; mov bp,sp`` promotes.

``leave`` is the one-byte encoding of ``mov sp,bp; pop bp``.  A compiler freely
pairs a hand-rolled ``push bp; mov bp,sp`` prologue with a ``leave`` epilogue
(instead of the atomic ``enter``), and every such function used to refuse
``leave-without-enter`` -- SimAnt's Borland corpus alone had 86 of them, each
then transitively blocking its callers.  The gate now recognises the hand-rolled
frame establish for a fused leave, exactly as ``_prove_bp_framebase_at_teardowns``
already does for the clobber case.

This is a DIFFERENTIAL regression: the composed CPUless body is exec'd and its
whole register file + stack memory diffed against stepping the identical bytes
through the interpreter (``CPU8086``).  It FAILS on the old gate (the function
never promotes, so there is no body to compare).  The negative guards prove the
relaxation stays sound: a bare ``mov bp,sp`` with no push, and an alt-entry
epilogue fragment (``leave; ret`` with no establish at all), both still refuse.
"""
from __future__ import annotations

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit_cpuless import (Refusal, _check_frame_pointer,
                                      check_promotable, emit_recovered)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")

# push bp; mov bp,sp; sub sp,4; mov [bp-2],ax; mov ax,cx; add ax,[bp-2];
# mov [bp-4],ax; mov dx,[bp-4]; leave; ret
#   -- a real hand-rolled frame: a local ([bp-2]) is written and read back, a
#      second local ([bp-4]) holds the result, and the epilogue is a fused leave.
_HAND_ROLLED = bytes.fromhex(
    "55"        # push bp
    "8bec"      # mov bp, sp          <- hand-rolled establish (NOT `enter`)
    "83ec04"    # sub sp, 4
    "8946fe"    # mov [bp-2], ax
    "8bc1"      # mov ax, cx
    "0346fe"    # add ax, [bp-2]
    "8946fc"    # mov [bp-4], ax
    "8b56fc"    # mov dx, [bp-4]
    "c9"        # leave              <- fused mov sp,bp; pop bp
    "c3")       # ret


def _fn_scan(code: bytes):
    return scan_function(lambda off: code[off] if off < len(code) else 0x90, 0)


def _interp_after_leave(code: bytes, regs: dict, mem: Memory, ss: int):
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


def test_hand_rolled_leave_promotes_and_matches_interpreter() -> None:
    scan = _fn_scan(_HAND_ROLLED)
    # 1. the gate accepts it (this is what FAILS on the old code: Refusal).
    spec = check_promotable(scan)
    src = emit_recovered(scan, spec.abi, "2000:0000",
                         recovered_import_base="x", needs_plat=spec.needs_plat,
                         df_livein=spec.df_livein, sp_output=spec.sp_output,
                         flags_livein=spec.flags_livein)

    # 2. differential: run the composed body and the interpreter on identical
    #    state, then diff the whole register file AND the stack memory.
    ss = 0x3000
    inputs = {"ax": 0x1234, "cx": 0x1111, "bp": 0x7777}
    m_body, m_interp = Memory(), Memory()

    ns: dict = {"_PARITY": [0] * 256}
    exec(compile(src, "<rec>", "exec"), ns)
    fn = next(v for k, v in ns.items() if k.startswith("func_"))
    out, _compat = fn(mem=m_body, ss=ss, sp=0x0100,
                      ax=inputs["ax"], cx=inputs["cx"], bp=inputs["bp"])

    s = _interp_after_leave(_HAND_ROLLED, {**inputs, "sp": 0x0100}, m_interp, ss)

    # a register not in `out` was not written -> it kept its input value.
    for r in ("ax", "cx", "dx", "bx", "bp", "si", "di"):
        expected = out[r] if r in out else inputs.get(r, 0)
        assert expected & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{r}: body={expected & 0xFFFF:04X} interp={getattr(s, r) & 0xFFFF:04X}")
    # the computed value is exactly cx + ax (the frame local round-tripped).
    assert out["ax"] & 0xFFFF == (0x1234 + 0x1111) & 0xFFFF
    assert out["dx"] & 0xFFFF == (0x1234 + 0x1111) & 0xFFFF
    # the stack region the frame touched is byte-identical.
    base = ss << 4
    assert bytes(m_body.data[base:base + 0x200]) == \
        bytes(m_interp.data[base:base + 0x200])


def test_old_gate_would_have_refused() -> None:
    """Pin the exact behaviour the fix changes: a fused leave whose only frame
    establish is the hand-rolled ``mov bp,sp`` must now pass the frame check
    (the old gate raised ``leave-without-enter`` here)."""
    _check_frame_pointer(_fn_scan(_HAND_ROLLED), {}, {})   # must NOT raise


def test_bare_mov_bp_sp_without_push_still_refuses() -> None:
    """A ``mov bp,sp; leave`` with NO ``push bp`` is not a balanced frame: the
    fused leave's own pop would consume a word that was never saved.  The
    hand-rolled establish is accepted only WITH its matching push."""
    # mov bp,sp; leave; ret  -- establish present, but no push bp.
    code = bytes.fromhex("8bec" "c9" "c3")
    with pytest.raises(Refusal, match="leave-without-enter"):
        _check_frame_pointer(_fn_scan(code), {}, {})


def test_alt_entry_epilogue_fragment_still_refuses() -> None:
    """An alt-entry epilogue fragment (``leave; ret`` with neither an establish
    nor a push in its own span) still refuses -- its frame base lives in the
    container function, which composes it as a whole, not standalone."""
    code = bytes.fromhex("c9" "c3")            # leave; ret  (SimAnt case_0237)
    with pytest.raises(Refusal, match="leave-without-enter"):
        _check_frame_pointer(_fn_scan(code), {}, {})


def test_enter_established_leave_still_promotes() -> None:
    """The atomic ``enter`` path is unchanged: an ``enter``-established fused
    leave still passes without needing a separate push bp."""
    # enter 4,0; mov [bp-2],ax; leave; ret
    code = bytes.fromhex("c8040000" "8946fe" "c9" "c3")
    _check_frame_pointer(_fn_scan(code), {}, {})   # must NOT raise
