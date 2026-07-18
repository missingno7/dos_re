"""A MANUFACTURED RETURN (`push <addr> ; jmp <indirect>`) is a computed CALL, not a tail.

A near indirect jmp is normally emitted as a TAIL dispatch: the arm's `ret` is taken to be the
container's exit, popping the container's caller's frame.  That is only sound at stack depth 0.  A
`ret` returns to WHATEVER IS ON TOP OF THE STACK, so a dispatcher that pushes a continuation first
gets control back at that address -- and the tail form silently drops everything from there onward
(the push is emitted faithfully; the continuation is simply never run).

The idiom is general x86: it is how a CALL with a computed target is written, since `call rel16`
cannot express one.  Depth alone cannot discriminate it from the FRAMELESS STACK-ARG tail
(test_cpuless_frameless_tail_dispatch.py), which also sits at nonzero depth and IS a genuine tail --
what separates them is the pushed VALUE being a statically-known block of this same function.

Three branches, all covered here:
  1. recognised + resumable  -> computed call, control resumes at the pushed block (DIFFERENTIAL
     against the interpreter over the identical bytes: register file + stack, byte-for-byte);
  2. recognised + NOT a block of this function -> REFUSE loudly (`manufactured-return-not-local`);
  3. not recognised (nothing pushed) -> the tail form, unchanged.
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.decode import IRET, RET, RETF, decode_one
from dos_re.lift.emit_cpuless import (_DYN_REGS, Refusal, _contract_inputs,
                                      _manufactured_return, check_promotable,
                                      emit_recovered)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")
CS = 0x2000
TABLE_OFF = 0x0020
ARM_OFF = 0x0040
ARM_RET_IP = 0x0042
JMP_IP = 0x0008
RESUME_IP = 0x000D
FINAL_RET_IP = 0x0010

# The DISPATCHER.  Note there is NO branch to RESUME: the continuation is reachable ONLY through
# the manufactured return, which is exactly the situation the old code could not see -- the CFG
# walk stopped at the indirect jmp, so RESUME was never scanned, never emitted, never run.
#   0000  B8 0D 00           mov  ax, 0x000D          ; the MANUFACTURED return address
#   0003  50                 push ax
#   0004  8B DE              mov  bx, si
#   0006  D1 E3              shl  bx, 1
#   0008  2E FF A7 20 00     jmp  cs:[bx+0x0020]      ; computed CALL through the table
#   000D  83 C0 07           add  ax, 7               ; RESUME -- reachable only via the arm's ret
#   0010  C3                 ret
_CONTAINER = bytes.fromhex("b80d00" "50" "8bde" "d1e3"
                           "2effa72000" "83c007" "c3")

# The dispatch ARM, outside the container's scan (at CS:0x0040).  Its `ret` consumes the
# manufactured address and so lands at RESUME, inside the container.
#   0040  8B C3   mov ax, bx
#   0042  C3      ret
_ARM = bytes.fromhex("8bc3" "c3")

# An ordinary depth-0 TAIL dispatch: nothing pushed, so the arm's ret IS this function's exit.
#   0000  8B DE           mov bx, si
#   0002  D1 E3           shl bx, 1
#   0004  2E FF A7 20 00  jmp cs:[bx+0x0020]
_CONTAINER_TAIL = bytes.fromhex("8bde" "d1e3" "2effa72000")


def _fetch(code, base):
    return lambda off: code[off - base] if 0 <= off - base < len(code) else 0x90


def _scan(code, base=0):
    return scan_function(_fetch(code, base), base)


def _seed(mem, container=_CONTAINER):
    for k, b in enumerate(container):
        mem.data[(CS << 4) + k] = b
    for k, b in enumerate(_ARM):
        mem.data[(CS << 4) + ARM_OFF + k] = b
    mem.ww(CS, TABLE_OFF, ARM_OFF)          # table[0] -> the arm


def _mock_dyn():
    """A faithful `_dyn`: run the arm through the interpreter and stop BEFORE its `ret`.

    The composed ABI's contract is that a recovered body returns pre-`ret`: the return-frame pop
    belongs to the composing caller.  That is precisely the `sp + 2` the computed-call form owes.
    """
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


def _emit(scan):
    spec = check_promotable(scan)
    src = emit_recovered(scan, spec.abi, f"{CS:04X}:0000", recovered_import_base="x",
                         needs_plat=spec.needs_plat, df_livein=spec.df_livein,
                         sp_output=spec.sp_output, flags_livein=spec.flags_livein)
    return spec, src


def _run_body(src, scan, spec, mem, ss, sp0, inputs):
    pkg = types.ModuleType("x")
    pkg.__path__ = []
    sys.modules["x"] = pkg
    dc = types.ModuleType("x._dyncall")
    dc.dyn_exec = _mock_dyn()
    sys.modules["x._dyncall"] = dc
    ns = {"_PARITY": [0] * 256}
    exec(compile(src, "<rec>", "exec"), ns)
    fn = next(v for k, v in ns.items() if k.startswith("func_"))
    kw = {r: inputs.get(r, 0) for r in _contract_inputs(scan, spec.abi)
          if r not in ("sp", "ss")}

    class _P:
        pass
    return fn(mem=mem, plat=_P(), ss=ss, sp=sp0, **kw)


def _interp_to(mem, ss, sp0, inputs, stop_ip):
    st = CPUState(cs=CS, ip=0, ss=ss, ds=0, es=0,
                  **{r: inputs.get(r, 0) for r in W16 if r != "sp"})
    st.sp = sp0
    cpu = CPU8086(mem, st)
    for _ in range(128):
        if (cpu.s.cs & 0xFFFF) == CS and (cpu.s.ip & 0xFFFF) == stop_ip:
            return cpu.s
        cpu.step()
    raise AssertionError("container+arm did not reach the stop ip in budget")


# ---------------------------------------------------------------------------------------------
# 1. recognised + resumable -> computed call
# ---------------------------------------------------------------------------------------------

def test_the_scan_follows_the_manufactured_return():
    """The CFG walk must treat the pushed address as an ARRIVAL, or the continuation is invisible.

    RESUME is not the target of any branch -- the only way in is the arm's `ret`.  Before this
    change the walk stopped at the indirect jmp and everything from RESUME on was simply absent
    from the function (that is how OVERKILL's object bounds-check went missing for 870 frames).
    """
    scan = _scan(_CONTAINER)
    assert RESUME_IP in scan.manufactured_returns
    assert RESUME_IP in scan.insts, "the resume point must be part of the function"
    assert FINAL_RET_IP in scan.insts, "and so must the continuation beyond it"
    # it is a FORCED block leader, exactly like a dynamic-dispatch alternate entry
    assert RESUME_IP in scan.block_leaders()


def test_manufactured_return_is_recognised_and_resumes_at_the_pushed_block():
    scan = _scan(_CONTAINER)
    jmp = next(i for i in scan.insts.values() if i.ip == JMP_IP)
    # the recogniser reads the pushed literal through the `mov ax,imm16` that fed the push
    assert _manufactured_return(scan, jmp, 0x0000) == RESUME_IP

    spec, src = _emit(scan)
    # the computed-call form: resume inside this function, and NO intra-function `_LOCAL`
    # fast path (a block goto cannot express "the arm's ret comes back to RESUME").
    assert "if _dt in _LOCAL:" not in src
    assert "sp = (sp + 2) & 0xFFFF" in src
    # the RESUME block must be reachable in the emitted dispatch, i.e. the jmp does not `break`
    # out of the function the way a tail does
    dispatch = src[src.index("_dt = ("):]
    assert "bb = " in dispatch.split("_cost += _dc['cost']")[1][:400]


def test_computed_call_body_matches_the_interpreter_byte_for_byte():
    scan = _scan(_CONTAINER)
    spec, src = _emit(scan)

    ss, sp0 = 0x3000, 0x0100
    inputs = {"ax": 0xABCD, "bx": 0x0000, "si": 0x0000, "di": 0x1234}
    m_body, m_interp = Memory(), Memory()
    for m in (m_body, m_interp):
        _seed(m)
        m.ww(ss, sp0, 0xBEEF)                  # the container's own near return IP (sentinel)

    out, _compat = _run_body(src, scan, spec, m_body, ss, sp0, inputs)
    s = _interp_to(m_interp, ss, sp0, inputs, FINAL_RET_IP)

    defaults = {**inputs, "sp": sp0}
    for r in ("ax", "cx", "dx", "bx", "bp", "si", "di", "sp"):
        expected = out[r] if r in out else defaults.get(r, 0)
        assert expected & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{r}: body={expected & 0xFFFF:04X} interp={getattr(s, r) & 0xFFFF:04X}")

    # THE POINT: the arm set ax=bx(=0), and the RESUME block then added 7.  Under the old tail
    # form the function exited at the jmp and ax would still be 0 -- the continuation dropped.
    assert out["ax"] & 0xFFFF == 0x0007
    # The arm's ret consumed the manufactured word, so the container is stack-BALANCED and sp is
    # not an output at all.  That is a consequence of the depth walk following the resume: read as
    # a tail, the jmp recorded a bogus +2 exit depth and made sp a runtime output.
    assert spec.sp_output is False
    assert "sp" not in out
    assert s.sp & 0xFFFF == sp0

    base = ss << 4
    assert bytes(m_body.data[base:base + 0x200]) == bytes(m_interp.data[base:base + 0x200])


# ---------------------------------------------------------------------------------------------
# 2. recognised + not a block of this function -> REFUSE
# ---------------------------------------------------------------------------------------------

def test_manufactured_return_absent_from_the_scan_refuses_loudly():
    """The emitter's guard: a recognised resume point that is not a block of this function.

    `scan_function` now follows the arrival, so it normally IS a block -- this is defence in
    depth for a scan produced by another path (or truncated by the region budget).  It is
    asserted directly, by deleting the continuation from an otherwise valid scan, because the
    property that matters is "never fall through to the silent tail", and that must hold for
    whatever scan the emitter is handed.
    """
    scan = _scan(_CONTAINER)
    for ip in [a for a in scan.insts if a >= RESUME_IP]:
        del scan.insts[ip]
    scan.manufactured_returns.clear()
    scan.exits[:] = [e for e in scan.exits if e.ip < RESUME_IP]
    jmp = scan.insts[JMP_IP]
    assert _manufactured_return(scan, jmp, 0x0000) == RESUME_IP   # positively recognised
    assert RESUME_IP not in scan.insts                            # but not ours to resume at

    with pytest.raises(Refusal) as e:
        _emit(scan)
    assert "manufactured-return-not-local" in str(e.value)


# ---------------------------------------------------------------------------------------------
# 3. not recognised -> the tail form, unchanged
# ---------------------------------------------------------------------------------------------

def test_plain_depth_zero_tail_dispatch_is_untouched():
    scan = _scan(_CONTAINER_TAIL)
    jmp = next(i for i in scan.insts.values() if i.ip == 0x0004)
    assert _manufactured_return(scan, jmp, 0x0000) is None

    spec, src = _emit(scan)
    # the tail keeps its intra-function fast path and exits at the dispatch
    assert "if _dt in _LOCAL:" in src
    assert "sp = (sp + 2) & 0xFFFF" not in src


def test_pushed_runtime_value_is_not_mistaken_for_a_return_address():
    """The FRAMELESS STACK-ARG idiom pushes ARGUMENTS, not a continuation.

    Its pushes are registers with no literal provenance (and there are two of them), so the
    recogniser must decline -- otherwise this change would break a deliberately supported tail.
    """
    #   56 push si / 57 push di / 8B DE mov bx,si / D1 E3 shl bx,1 / jmp cs:[bx+0x20]
    frameless = bytes.fromhex("56" "57" "8bde" "d1e3" "2effa72000")
    scan = _scan(frameless)
    jmp = next(i for i in scan.insts.values() if i.ip == 0x0006)
    assert _manufactured_return(scan, jmp, 0x0000) is None
