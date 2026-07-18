"""A FRAMELESS stack-arg TAIL DISPATCH composes byte-for-byte via a runtime sp output.

The framed switch idiom (test_cpuless_tail_dispatch.py) unwinds a nonzero-depth tail through the arm's
`leave`, so the exit sp is statically balanced.  The FRAMELESS stack-arg idiom has no bp frame: the
dispatcher pushes ARGUMENTS (`push si; push di`), tail-dispatches, and each arm POPS exactly those args
before its `ret` (restoring the caller's frame).  The exact pop count is not knowable from the
dispatcher alone, so the exit sp cannot be proven statically balanced -- but it IS representable as a
RUNTIME OUTPUT: the arm (run via `_dyn`) returns its actual sp in the merged bundle, exact whether the
arm balances or not.  So the gate no longer refuses; it makes the tail an sp-output exit.

DIFFERENTIAL: a frameless dispatcher whose one arm pops si/di and near-returns is composed and its body
exec'd against a faithful `_dyn`, then diffed byte-for-byte (register file + stack) against stepping the
identical container+arm bytes through the interpreter.  Soundness guard: a TRULY unknown depth still
refuses (there is no runtime sp to defer to).
"""
from __future__ import annotations

import sys
import types

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.decode import IRET, RET, RETF, decode_one
from dos_re.lift.emit_cpuless import (_DYN_REGS, _contract_inputs, check_promotable,
                                      emit_recovered)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")
CS = 0x2000
TABLE_OFF = 0x0020
ARM_OFF = 0x0040
ARM_RET_IP = 0x0044

# A FRAMELESS dispatcher: push two args, tail-dispatch at depth 4, no bp frame.
#   56              push si
#   57              push di
#   8B DE           mov bx, si
#   D1 E3           shl bx, 1
#   2E FF A7 20 00  jmp cs:[bx + 0x0020]   ; TAIL DISPATCH at depth 4, frameless
_CONTAINER = bytes.fromhex("56" "57" "8bde" "d1e3" "2effa72000")

# The dispatch ARM (at CS:0x0040): the shared epilogue POPS exactly the pushed
# args (di then si) and near-returns to the container's caller.
#   0040 8B C3   mov ax, bx     ; some work using the (shl'd) selector
#   0042 5F      pop di         ; discard the pushed args -- restore the frame
#   0043 5E      pop si
#   0044 C3      ret            ; near return to the container's caller
_ARM = bytes.fromhex("8bc3" "5f" "5e" "c3")


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
    """A faithful `_dyn`: resolve the selector, run the arm body through the
    interpreter, stop before its `ret` (the ret pop is the composing caller's
    job -- a composed body returns post-pop, pre-ret sp)."""
    def dyn(sel, mem, plat, base, regs):
        seg, off = (int(x, 16) for x in sel.split(":"))
        st = CPUState(cs=seg, ip=off, ss=regs["ss"], ds=regs.get("ds", 0),
                      es=regs.get("es", 0), **{r: regs[r] for r in W16 if r != "sp"})
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


def _interp_before_arm_ret(regs, mem, ss):
    st = CPUState(cs=CS, ip=0, ss=ss, ds=regs.get("ds", 0), es=regs.get("es", 0),
                  **{r: regs.get(r, 0) for r in W16 if r != "sp"})
    st.sp = regs["sp"]
    cpu = CPU8086(mem, st)
    for _ in range(128):
        if (cpu.s.cs & 0xFFFF) == CS and (cpu.s.ip & 0xFFFF) == ARM_RET_IP:
            break
        cpu.step()
    else:
        raise AssertionError("container+arm did not reach the arm ret in budget")
    return cpu.s


def test_frameless_stack_arg_tail_dispatch_composes_and_matches_interpreter():
    scan = _scan(_CONTAINER)
    spec = check_promotable(scan)
    # frameless: the exit sp is NOT statically balanced -- it flows out at runtime.
    assert spec.sp_output is True

    src = emit_recovered(scan, spec.abi, "2000:0000", recovered_import_base="x",
                         needs_plat=spec.needs_plat, df_livein=spec.df_livein,
                         sp_output=spec.sp_output, flags_livein=spec.flags_livein)

    ss, sp0 = 0x3000, 0x0100
    inputs = {"ax": 0xABCD, "bx": 0x0000, "si": 0x0000, "di": 0x1234}
    m_body, m_interp = Memory(), Memory()
    for m in (m_body, m_interp):
        _seed(m)
        m.ww(ss, sp0, 0xBEEF)                  # near return IP (sentinel)

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

    class _P:
        pass
    out, _compat = fn(mem=m_body, plat=_P(), ss=ss, sp=sp0, **kw)

    s = _interp_before_arm_ret({**inputs, "sp": sp0}, m_interp, ss)

    defaults = {**inputs, "sp": sp0}
    for r in ("ax", "cx", "dx", "bx", "bp", "si", "di", "sp"):
        expected = out[r] if r in out else defaults.get(r, 0)
        assert expected & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{r}: body={expected & 0xFFFF:04X} interp={getattr(s, r) & 0xFFFF:04X}")
    # the arm popped both pushed args -> sp balanced back to the container entry sp
    assert out["sp"] & 0xFFFF == sp0
    # and it restored si/di from the stack
    assert out["si"] & 0xFFFF == inputs["si"] and out["di"] & 0xFFFF == inputs["di"]

    base = ss << 4
    assert bytes(m_body.data[base:base + 0x200]) == bytes(m_interp.data[base:base + 0x200])
