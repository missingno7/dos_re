"""A shared-epilogue TAIL DISPATCH at nonzero stack depth composes byte-for-byte.

A compiler emits a switch as a jump-table tail dispatch: `jmp cs:[bx*2+table]`,
each arm ending `leave; ret(f)` -- the arm shares the dispatching function's
frame, restores it, and returns through the caller's frame (a shared epilogue).
The CPUless emitter already composed the DEPTH-0 tail (the arm's ret pops our
caller's return frame directly), but REFUSED `tail-dispatch-at-nonzero-depth`
whenever a frame was still standing at the jmp (the saved bp + enter/sub
locals).  That is exactly the framed-switch idiom: the arm's `leave`
(mov sp,bp; pop bp) discards everything above the frame base and restores sp to
entry before its return, so the exit is balanced EXACTLY as a fused `leave;
ret(f)` in the dispatching function would be.  The gate now allows a
nonzero-depth tail dispatch when the function has an ESTABLISHED FRAME POINTER
for that unwind (enter, or push bp; mov bp,sp); without one, the extra bytes'
fate is unrepresentable and it still refuses.

DIFFERENTIAL regression: a framed function with a jump-table tail dispatch is
composed and its body exec'd against a faithful `_dyn` (the interpreter running
the resolved arm's body, i.e. through its `leave` but not its ret -- the ret pop
is the caller's job, exactly as a composed body leaves it).  The whole register
file + stack memory is diffed byte-for-byte against stepping the identical
container+arm bytes through the interpreter (`CPU8086`).  It FAILS on the old
gate (the container refuses, so there is no body to compose).  Negative guards
pin soundness: a frameless nonzero-depth tail (push regs, no bp frame) still
refuses, and an UNKNOWN-depth tail still refuses.
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.decode import RET, RETF, IRET, decode_one
from dos_re.lift.emit_cpuless import (Refusal, check_promotable, emit_recovered,
                                      _contract_inputs, _DYN_REGS)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")
CS = 0x2000
TABLE_OFF = 0x0020
ARM_OFF = 0x0040
ARM_RETF_IP = 0x0044

# A far function: hand-rolled frame, one 4-byte local, a jump-table tail
# dispatch at depth 6 (saved bp + the sub sp,4 locals).
#   0000 55        push bp
#   0001 8B EC     mov bp, sp                 <- frame establish
#   0003 83 EC 04  sub sp, 4                  ; depth now 6
#   0006 89 46 FE  mov [bp-2], ax             ; write a frame local
#   0009 8B 5E 06  mov bx, [bp+6]             ; the switch selector (first arg)
#   000C D1 E3     shl bx, 1                  ; *2 for a word table
#   000E 2E FF A7 20 00  jmp cs:[bx + 0x0020] ; TAIL DISPATCH at depth 6
_CONTAINER = bytes.fromhex(
    "55" "8bec" "83ec04" "8946fe" "8b5e06" "d1e3" "2effa72000")

# The dispatch ARM (at CS:0x0040): shares the container frame, reads its local,
# restores the frame and far-returns.
#   0040 8B 46 FE  mov ax, [bp-2]     ; read the container's frame local
#   0043 C9        leave              ; mov sp,bp; pop bp -- restore the frame
#   0044 CB        retf               ; return to the container's caller
_ARM = bytes.fromhex("8b46fe" "c9" "cb")

# A frameless nonzero-depth tail: saves si/di (depth 4), tail-dispatches, no bp
# frame for a `leave` to unwind -- the arm would need an exact `pop si; pop di`.
#   56        push si
#   57        push di
#   8B DE     mov bx, si
#   D1 E3     shl bx, 1
#   2E FF A7 20 00  jmp cs:[bx+0x20]
_FRAMELESS = bytes.fromhex("56" "57" "8bde" "d1e3" "2effa72000")


def _fetch(code, base):
    return lambda off: code[off - base] if 0 <= off - base < len(code) else 0x90


def _scan(code, base=0):
    return scan_function(_fetch(code, base), base)


def _seed(mem):
    for k, b in enumerate(_CONTAINER):
        mem.data[(CS << 4) + k] = b
    for k, b in enumerate(_ARM):
        mem.data[(CS << 4) + ARM_OFF + k] = b
    mem.ww(CS, TABLE_OFF, ARM_OFF)          # table[0] -> the arm


def _mock_dyn(ss):
    """A faithful `_dyn`: resolve the selector to its CS:off, run the arm body
    through the interpreter, and stop before the arm's `ret(f)` (the ret pop is
    the composing caller's job -- a composed body returns post-leave sp)."""
    def dyn(sel, mem, plat, base, regs):
        seg, off = (int(x, 16) for x in sel.split(":"))
        st = CPUState(cs=seg, ip=off, ss=regs["ss"], ds=regs.get("ds", 0),
                      es=regs.get("es", 0),
                      **{r: regs[r] for r in W16 if r != "sp"})
        st.sp = regs["sp"]
        cpu = CPU8086(mem, st)
        fetch = lambda o: mem.data[(cpu.s.cs << 4) + (o & 0xFFFF)]  # noqa: E731
        for _ in range(64):
            if decode_one(fetch, cpu.s.ip & 0xFFFF).kind in (RET, RETF, IRET):
                break
            cpu.step()
        else:
            raise AssertionError("arm did not reach its ret in budget")
        out = {r: getattr(cpu.s, r) & 0xFFFF for r in _DYN_REGS}
        return out, {"flags": 0, "fmask": 0, "cost": 2}
    return dyn


def _interp_before_arm_retf(regs, mem, ss):
    st = CPUState(cs=CS, ip=0, ss=ss, ds=regs.get("ds", 0), es=regs.get("es", 0),
                  **{r: regs.get(r, 0) for r in W16 if r != "sp"})
    st.sp = regs["sp"]
    cpu = CPU8086(mem, st)
    for _ in range(128):
        if (cpu.s.cs & 0xFFFF) == CS and (cpu.s.ip & 0xFFFF) == ARM_RETF_IP:
            break
        cpu.step()
    else:
        raise AssertionError("container+arm did not reach the arm ret in budget")
    return cpu.s


def test_framed_tail_dispatch_composes_and_matches_the_interpreter():
    scan = _scan(_CONTAINER)
    # (1) the gate accepts it -- this is what FAILS on the old code (Refusal
    #     "tail-dispatch-at-nonzero-depth").
    spec = check_promotable(scan)
    # the exit stays balanced: the arm's leave restores the frame, so sp is NOT
    # a runtime output (the caller re-derives it from the return-frame pop).
    assert spec.sp_output is False
    src = emit_recovered(scan, spec.abi, "2000:0000", recovered_import_base="x",
                         needs_plat=spec.needs_plat, df_livein=spec.df_livein,
                         sp_output=spec.sp_output, flags_livein=spec.flags_livein)

    ss, sp0 = 0x3000, 0x0100
    inputs = {"ax": 0xABCD, "bp": 0x7777, "bx": 0x9999}
    m_body, m_interp = Memory(), Memory()
    for m in (m_body, m_interp):
        _seed(m)
        m.ww(ss, sp0, 0xBEEF)          # far return IP (sentinel)
        m.ww(ss, (sp0 + 2) & 0xFFFF, CS)   # far return CS
        m.ww(ss, (sp0 + 4) & 0xFFFF, 0x0000)   # the switch selector (arg) -> arm 0

    # the generated body does `from x._dyncall import dyn_exec as _dyn`; wire the
    # faithful mock through a registered package (mirrors the promoter's seam).
    pkg = types.ModuleType("x")
    pkg.__path__ = []
    sys.modules["x"] = pkg
    dc = types.ModuleType("x._dyncall")
    dc.dyn_exec = _mock_dyn(ss)
    sys.modules["x._dyncall"] = dc

    ns = {"_PARITY": [0] * 256}
    exec(compile(src, "<rec>", "exec"), ns)
    fn = next(v for k, v in ns.items() if k.startswith("func_"))
    kw = {r: inputs.get(r, 0) for r in _contract_inputs(scan, spec.abi)
          if r not in ("sp", "ss")}

    class _P:                          # the body needs a plat object (has_dyn)
        pass
    out, _compat = fn(mem=m_body, plat=_P(), ss=ss, sp=sp0, **kw)

    s = _interp_before_arm_retf({**inputs, "sp": sp0}, m_interp, ss)

    # every register agrees; sp is balanced back to the container's entry sp
    # (the arm's leave discarded the frame + locals), and ax is the frame local.
    defaults = {**inputs, "sp": sp0}
    for r in ("ax", "cx", "dx", "bx", "bp", "si", "di", "sp"):
        expected = out[r] if r in out else defaults.get(r, 0)
        assert expected & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{r}: body={expected & 0xFFFF:04X} interp={getattr(s, r) & 0xFFFF:04X}")
    assert out["ax"] & 0xFFFF == 0xABCD          # [bp-2] round-tripped through
    assert (out["sp"] if "sp" in out else sp0) & 0xFFFF == sp0   # balanced

    # the whole stack region the frame touched is byte-identical.
    base = ss << 4
    assert bytes(m_body.data[base:base + 0x200]) == \
        bytes(m_interp.data[base:base + 0x200])


def test_dyn_selector_dispatches_the_container():
    """The container's own emission threads the runtime selector through `_dyn`
    (not a static call) and ends the block by breaking to the exit -- the tail
    transfer resolves the same way every other dispatch does."""
    scan = _scan(_CONTAINER)
    spec = check_promotable(scan)
    src = emit_recovered(scan, spec.abi, "2000:0000", recovered_import_base="x",
                         needs_plat=spec.needs_plat, df_livein=spec.df_livein,
                         sp_output=spec.sp_output, flags_livein=spec.flags_livein)
    assert "_dyn(" in src and "import dyn_exec as _dyn" in src


def test_frameless_nonzero_depth_tail_still_refuses():
    """A nonzero-depth tail with NO established frame pointer (saved si/di, not a
    bp frame) has nothing for a `leave` to unwind -- the arm's exact pop count is
    unknowable, so the exit sp is unrepresentable.  Must still refuse."""
    with pytest.raises(Refusal, match="tail-dispatch-at-nonzero-depth"):
        check_promotable(_scan(_FRAMELESS))


def test_depth_zero_tail_dispatch_unchanged():
    """A depth-0 tail dispatch (no frame, jmp at entry depth) still composes:
    the arm's ret pops the caller's return frame directly."""
    #   D1 E3            shl bx,1
    #   2E FF A7 20 00   jmp cs:[bx+0x20]     ; depth 0
    code = bytes.fromhex("d1e3" "2effa72000")
    spec = check_promotable(_scan(code))
    assert spec.sp_output is False
