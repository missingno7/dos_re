"""A selected authored implementation composes byte-for-byte through an adapter.

A consumer with a verified hand-recovered body supplies its CONTRACT (ret_kind,
inputs, outputs, ret_pop, ...) and the body itself (import_base.<name>, obeying
the CPUless body ABI ``func(mem[, plat], *, **inputs) -> (outputs, compat)``);
The catalog and execution plan own selection. This test exercises the generic
identity-preserving CPU-ABI adapter (:func:`emit_override_adapter`) after
selection; generated-promotion tools do not own an override registry.

This is a DIFFERENTIAL regression on the SEAM, not on a body the strict emitter
produced:

  * a caller that ``call far``s an override is composed against the seeded
    contract, its body exec'd, and its whole register file + stack memory diffed
    against stepping the identical caller+override bytes through the interpreter
    (``CPU8086``).  The override body models exactly the register/memory effect
    of the real bytes, so a correctly-seeded composition is byte-exact.
  * the emitted override adapter, run through the interpreter's CPU carrier from
    the override entry (a far frame on the stack), reproduces the same effect
    and applies the historical far RET -- proving the lifted-slot bridge.

This test asserts state equality. Timing equality is covered independently by
``test_cpuless_override_vtime`` and remains part of complete continuation
verification.
"""
from __future__ import annotations

import sys
import types

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit_cpuless import (CalleeContract, check_promotable,
                                      emit_override_adapter, emit_recovered,
                                      _contract_inputs)
from dos_re.memory import Memory

W16 = ("ax", "cx", "dx", "bx", "sp", "bp", "si", "di")

OVR_SEG, OVR_OFF = 0x2000, 0x0000
CALLER_OFF = 0x0100

# The overridden function's REAL bytes (what the interpreter runs).  It only
# touches AX -- so an override declaring outputs=['ax'] is register-exact and the
# full-register-file differential holds.
#   B8 34 12   mov ax, 0x1234
#   CB         retf
_OVR = bytes.fromhex("b83412" "cb")

# The caller: push a pascal word arg, call far the override, ret.
#   6A 05             push 5                (ip 0100)
#   9A 00 00 00 20    call far 2000:0000    (ip 0102)
#   C3                ret                   (ip 0107)
_CALLER = bytes.fromhex("6a05" "9a00000020" "c3")

OVR_NAME = "func_2000_0000"

# The AUTHORITATIVE override body: the consumer's hand-recovered implementation,
# obeying the CPUless body ABI.  Register-exact for its declared output (ax).
_OVR_BODY = f'''
def {OVR_NAME}(mem, *, sp=0, ss=0):
    # a hand-recovered body would marshal views/args here; this stand-in returns
    # the semantic result (ax) and touches nothing else.
    return {{"ax": 0x1234}}, {{"flags": 0, "fmask": 0, "cost": 1}}
'''

OVR_CONTRACT = CalleeContract(
    name=OVR_NAME, inputs=("sp", "ss"), outputs=("ax",),
    exit_flags=frozenset(), needs_plat=False, ret_kind="far",
    df_livein=False, sp_delta=0, ret_pop=0, sp_output=False, sp_deltas=(0,),
    flags_livein=False)


def _scan(code: bytes, entry: int):
    return scan_function(
        lambda off: code[off - entry] if 0 <= off - entry < len(code) else 0x90,
        entry)


def _install_body(base: str):
    pkg = types.ModuleType(base)
    pkg.__path__ = []
    sys.modules[base] = pkg
    mod = types.ModuleType(base + "." + OVR_NAME)
    exec(compile(_OVR_BODY, "<override>", "exec"), mod.__dict__)
    sys.modules[base + "." + OVR_NAME] = mod


def _interp_state(regs, mem, ss):
    """Step caller+override bytes from the caller entry to the caller's trailing
    ret (the ret pop is the adapter's job, taken one step later)."""
    for k, b in enumerate(_OVR):
        mem.data[(OVR_SEG << 4) + OVR_OFF + k] = b
    for k, b in enumerate(_CALLER):
        mem.data[(OVR_SEG << 4) + CALLER_OFF + k] = b
    st = CPUState(cs=OVR_SEG, ip=CALLER_OFF, ss=ss, ds=regs.get("ds", ss),
                  **{r: regs.get(r, 0) for r in W16 if r != "sp"})
    st.sp = regs["sp"]
    cpu = CPU8086(mem, st)
    ret_ip = CALLER_OFF + len(_CALLER) - 1
    for _ in range(64):
        if (cpu.s.cs & 0xFFFF) == OVR_SEG and (cpu.s.ip & 0xFFFF) == ret_ip:
            break
        cpu.step()
    else:
        raise AssertionError("caller did not reach its ret in budget")
    return cpu.s


def test_override_composes_into_a_caller_byte_exact():
    """Seeding an external override contract lets a caller compose it, and the
    caller's whole register file + stack is byte-identical to the interpreter."""
    base = "t_override_pkg"
    _install_body(base)

    far_callees = {(OVR_SEG, OVR_OFF): OVR_CONTRACT}
    caller_scan = _scan(_CALLER, CALLER_OFF)
    caller_spec = check_promotable(caller_scan, far_callees=far_callees)
    caller_src = emit_recovered(
        caller_scan, caller_spec.abi, "2000:0100",
        recovered_import_base=base, far_callees=far_callees,
        needs_plat=caller_spec.needs_plat, df_livein=caller_spec.df_livein,
        sp_output=caller_spec.sp_output, flags_livein=caller_spec.flags_livein)
    ns: dict = {"_PARITY": [0] * 256}
    exec(compile(caller_src, "<caller>", "exec"), ns)
    caller_fn = ns["func_2000_0100"]

    # the composed caller emits a direct call to the override body name.
    assert f"{OVR_NAME}(" in caller_src

    ss, sp0 = 0x3000, 0x0100
    inputs = {"bp": 0x6666, "si": 0x3333, "cx": 0x2222, "bx": 0x4444}
    m_body, m_interp = Memory(), Memory()

    kw = {r: inputs.get(r, 0) for r in _contract_inputs(caller_scan,
          caller_spec.abi) if r not in ("sp", "ss")}
    out, _compat = caller_fn(mem=m_body, ss=ss, sp=sp0, **kw)
    s = _interp_state({**inputs, "sp": sp0}, m_interp, ss)

    defaults = {**inputs, "sp": sp0}
    for r in ("ax", "cx", "dx", "bx", "bp", "si", "di", "sp"):
        expected = out[r] if r in out else defaults.get(r, 0)
        assert expected & 0xFFFF == getattr(s, r) & 0xFFFF, (
            f"{r}: body={expected & 0xFFFF:04X} interp={getattr(s, r) & 0xFFFF:04X}")
    assert out["ax"] & 0xFFFF == 0x1234       # the override's result
    # the far frame was popped in composition; this cdecl caller leaves its one
    # pushed arg on the stack (it never pops it), exactly as the interpreter does
    # -- the whole-register-file loop above already pinned sp == interp.
    assert (out["sp"] if "sp" in out else sp0) & 0xFFFF == (sp0 - 2) & 0xFFFF
    base_lin = ss << 4
    assert bytes(m_body.data[base_lin:base_lin + 0x200]) == \
        bytes(m_interp.data[base_lin:base_lin + 0x200])


def test_override_adapter_bridges_the_cpu_abi():
    """The emitted override adapter reads inputs from the CPU carrier, runs the
    authoritative body, writes the declared output, and applies the far RET --
    byte-identical to interpreting the override's own bytes."""
    base = "t_override_adapter_pkg"
    _install_body(base)
    sig = _OVR[:1]                       # arbitrary signature (self-disable off)
    ad_src = emit_override_adapter(
        "2000:0000", OVR_CONTRACT, signature=sig, recovered_import_base=base)
    ns: dict = {}
    exec(compile(ad_src, "<adapter>", "exec"), ns)
    adapter = ns["lifted_2000_0000"]
    assert getattr(adapter, "owns_time", False)

    # a caller has pushed a far return frame (ret off, ret cs) and jumped in.
    ss, sp = 0x3000, 0x0100
    ret_cs, ret_off = 0x1111, 0x0222
    m = Memory()
    m.data[(ss << 4) + sp] = ret_off & 0xFF
    m.data[(ss << 4) + sp + 1] = ret_off >> 8
    m.data[(ss << 4) + sp + 2] = ret_cs & 0xFF
    m.data[(ss << 4) + sp + 3] = ret_cs >> 8
    st = CPUState(cs=OVR_SEG, ip=OVR_OFF, ss=ss, ds=0x4000, ax=0x9999)
    st.sp = sp
    cpu = CPU8086(m, st)
    adapter(cpu)

    assert cpu.s.ax & 0xFFFF == 0x1234           # the body's result written back
    assert cpu.s.ip & 0xFFFF == ret_off          # far RET popped the frame
    assert cpu.s.cs & 0xFFFF == ret_cs
    assert cpu.s.sp & 0xFFFF == (sp + 4) & 0xFFFF  # far frame (4 bytes) popped


def test_override_body_is_the_single_running_implementation():
    """The override contract carries the body NAME; the adapter imports and runs
    exactly that, so there is one implementation per address (no generated twin
    runs in its slot)."""
    base = "t_override_single_pkg"
    _install_body(base)
    ad_src = emit_override_adapter(
        "2000:0000", OVR_CONTRACT, signature=_OVR[:1],
        recovered_import_base=base)
    assert f"from {base}.{OVR_NAME} import {OVR_NAME}" in ad_src
    assert f"{OVR_NAME}(cpu.mem" in ad_src
