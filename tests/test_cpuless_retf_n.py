"""A far ``retf N`` (pascal / stdcall CALLEE-CLEANUP) composes byte-for-byte.

The CPUless emitter already modelled the NEAR ``ret N`` arg-pop (the immediate
rides ``ret_pop``; the composing caller pops ``2 + N``).  The FAR ``retf N``
(0xCA) variant -- the pascal convention a Win16/Borland callee uses to clean the
caller's stacked args -- was REFUSED ``ret-n-stack-args (retf N needs far
variant)``.  A far frame is 4 bytes (CS:IP), not 2, so the only difference from
the near variant is the return-frame size (which rides ``ret_kind``); the arg
pop ``N`` is modelled identically.  The composing caller now pops ``4 + N`` for
a far/retf callee (the far frame + the pascal args), so the caller's stack is
balanced exactly as the interpreter leaves it.

This is a DIFFERENTIAL regression: a caller that pushes two pascal args and
``call far``s a ``retf 4`` callee is composed, its body exec'd, and its whole
register file + stack memory diffed against stepping the identical caller+callee
bytes through the interpreter (``CPU8086``).  It FAILS on the old gate -- the
callee refuses ``ret-n-stack-args``, so no ``CalleeContract`` exists and the
caller can never compose it.  Negative guards pin soundness (a non-uniform
``retf N`` across exits still refuses ``mixed-ret-pop``; a plain ``retf`` still
pops exactly the 4-byte frame).
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit_cpuless import (CalleeContract, Refusal, check_promotable,
                                      emit_recovered, _contract_inputs)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")

CALLEE_SEG, CALLEE_OFF = 0x2000, 0x0000
CALLER_OFF = 0x0100

# The far pascal callee: it computes a constant and cleans up 4 arg bytes.
#   B8 34 12   mov ax, 0x1234
#   CA 04 00   retf 4          <- far pascal callee-cleanup (pops the 2 args)
_CALLEE = bytes.fromhex("b83412" "ca0400")

# The caller: push two pascal word args, call the far callee, ret.
#   6A 05          push 5           (ip 0100)
#   6A 07          push 7           (ip 0102)
#   9A 00 00 00 20 call far 2000:0000   (ip 0104)
#   C3             ret              (ip 0109)
_CALLER = bytes.fromhex("6a05" "6a07" "9a00000020" "c3")


def _scan(code: bytes, entry: int):
    return scan_function(
        lambda off: code[off - entry] if 0 <= off - entry < len(code) else 0x90,
        entry)


def _callee_contract(spec, name):
    keep = frozenset(W16[:4] + ("si", "di", "bp", "ds", "es"))  # no sp
    scan = _scan(_CALLEE, CALLEE_OFF)
    out = (spec.abi.outputs & keep) - (
        frozenset() if spec.sp_output else frozenset({"sp"}))
    return CalleeContract(
        name=name, inputs=tuple(_contract_inputs(scan, spec.abi)),
        outputs=tuple(sorted(out)), exit_flags=spec.exit_flags,
        needs_plat=spec.needs_plat, ret_kind=spec.ret_kind,
        df_livein=spec.df_livein, sp_delta=spec.sp_delta,
        ret_pop=spec.ret_pop, sp_output=spec.sp_output,
        sp_deltas=spec.sp_deltas, flags_livein=spec.flags_livein)


def _compose():
    """Emit the retf-4 callee body + the caller that composes it.  Returns
    (caller_fn, callee_spec, caller_spec)."""
    base = "t_retfn_pkg"
    callee_scan = _scan(_CALLEE, CALLEE_OFF)
    # (1) the gate accepts the retf N callee -- this is what FAILS on the old
    #     code (Refusal "ret-n-stack-args (retf N needs far variant)").
    callee_spec = check_promotable(callee_scan)
    callee_name = "func_2000_0000"
    callee_src = emit_recovered(
        callee_scan, callee_spec.abi, "2000:0000", recovered_import_base=base,
        needs_plat=callee_spec.needs_plat, df_livein=callee_spec.df_livein,
        sp_output=callee_spec.sp_output, flags_livein=callee_spec.flags_livein)

    pkg = types.ModuleType(base)
    pkg.__path__ = []
    sys.modules[base] = pkg
    cmod = types.ModuleType(base + "." + callee_name)
    exec(compile(callee_src, "<callee>", "exec"), cmod.__dict__)
    sys.modules[base + "." + callee_name] = cmod

    caller_scan = _scan(_CALLER, CALLER_OFF)
    far_callees = {(CALLEE_SEG, CALLEE_OFF): _callee_contract(callee_spec,
                                                              callee_name)}
    caller_spec = check_promotable(caller_scan, far_callees=far_callees)
    caller_src = emit_recovered(
        caller_scan, caller_spec.abi, "2000:0100", recovered_import_base=base,
        far_callees=far_callees, needs_plat=caller_spec.needs_plat,
        df_livein=caller_spec.df_livein, sp_output=caller_spec.sp_output,
        flags_livein=caller_spec.flags_livein)
    ns: dict = {"_PARITY": [0] * 256}
    exec(compile(caller_src, "<caller>", "exec"), ns)
    caller_fn = ns["func_2000_0100"]
    return caller_fn, callee_spec, caller_spec, caller_src


def _interp_before_caller_ret(regs, mem, ss):
    """Step caller+callee bytes from the caller entry, stopping at the caller's
    trailing ``ret`` (the ret pop is the adapter's job, not the body's effect)."""
    for k, b in enumerate(_CALLEE):
        mem.data[(CALLEE_SEG << 4) + CALLEE_OFF + k] = b
    for k, b in enumerate(_CALLER):
        mem.data[(CALLEE_SEG << 4) + CALLER_OFF + k] = b
    st = CPUState(cs=CALLEE_SEG, ip=CALLER_OFF, ss=ss, ds=regs.get("ds", ss),
                  **{r: regs.get(r, 0) for r in W16 if r != "sp"})
    st.sp = regs["sp"]
    cpu = CPU8086(mem, st)
    ret_ip = CALLER_OFF + len(_CALLER) - 1        # the final 0xC3
    for _ in range(64):
        if (cpu.s.cs & 0xFFFF) == CALLEE_SEG and (cpu.s.ip & 0xFFFF) == ret_ip:
            break
        cpu.step()
    else:
        raise AssertionError("caller did not reach its ret in budget")
    return cpu.s


def test_far_retf_n_callee_composes_and_the_caller_stack_balances():
    caller_fn, callee_spec, caller_spec, _ = _compose()

    # the callee's exit ABI: a far pascal cleanup of 4 bytes.
    assert callee_spec.ret_kind == "far"
    assert callee_spec.ret_pop == 4
    assert callee_spec.sp_delta == -4        # exit depth 0 minus the 4-byte pop
    assert callee_spec.sp_output is False     # the arg pop is a static contract

    ss, sp0 = 0x3000, 0x0100
    inputs = {"bp": 0x6666, "si": 0x3333, "cx": 0x2222}
    m_body, m_interp = Memory(), Memory()

    kw = {r: inputs.get(r, 0) for r in _contract_inputs(
        _scan(_CALLER, CALLER_OFF), caller_spec.abi) if r not in ("sp", "ss")}
    out, _compat = caller_fn(mem=m_body, ss=ss, sp=sp0, **kw)

    s = _interp_before_caller_ret({**inputs, "sp": sp0}, m_interp, ss)

    # every register agrees; notably SP is balanced back to entry (the far frame
    # AND the two pascal args were popped -- pop_n = 4 + 4).
    # a register not in `out` was not written -> it kept its entry value; a
    # balanced caller does not export sp (the adapter re-derives it), so its
    # unchanged value IS the balanced entry sp.
    defaults = {**inputs, "sp": sp0}
    for r in ("ax", "cx", "dx", "bx", "bp", "si", "di", "sp"):
        expected = out[r] if r in out else defaults.get(r, 0)
        assert expected & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{r}: body={expected & 0xFFFF:04X} interp={getattr(s, r) & 0xFFFF:04X}")
    assert (out["sp"] if "sp" in out else sp0) & 0xFFFF == sp0    # fully balanced
    assert out["ax"] & 0xFFFF == 0x1234                          # callee result

    # the stack region the sequence touched is byte-identical.
    base = ss << 4
    assert bytes(m_body.data[base:base + 0x200]) == \
        bytes(m_interp.data[base:base + 0x200])


def test_far_retf_n_caller_pops_four_plus_n():
    """The generated caller body pops ``4 + N`` after a composed far/retf-N
    call: the 4-byte far return frame plus the N pascal args."""
    _, _, _, caller_src = _compose()
    assert "sp = (sp + 8) & 0xFFFF" in caller_src   # 4 (far frame) + 4 (args)


def test_plain_retf_still_pops_only_the_frame():
    """A far callee with a plain ``retf`` (no immediate) keeps ret_pop 0, so a
    caller pops exactly the 4-byte far frame -- the relaxation adds nothing to
    the no-argument case."""
    code = bytes.fromhex("b83412" "cb")             # mov ax,0x1234; retf
    spec = check_promotable(_scan(code, 0))
    assert spec.ret_kind == "far" and spec.ret_pop == 0


def test_mixed_retf_n_immediates_still_refuse():
    """Two exits with DIFFERENT ``retf N`` immediates cannot share one adapter
    RET -- the uniform-pop contract must still refuse ``mixed-ret-pop``."""
    #   3D 00 00   cmp ax,0
    #   74 04      jz +4  -> the retf 8
    #   CA 04 00   retf 4
    #   CA 08 00   retf 8
    code = bytes.fromhex("3d0000" "7403" "ca0400" "ca0800")
    with pytest.raises(Refusal, match="mixed-ret-pop"):
        check_promotable(_scan(code, 0))
