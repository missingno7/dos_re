"""A plan-selected override reaches an INTERNALLY-CALLED target (emit_cpuless).

The ExecutionPlan's promise is ``impl = overrides.get(addr, generated[addr])``
per address.  Before this fix the emitter bound internal callees by direct
import from one hardcoded corpus package, so an override of a function that
other GENERATED bodies call was silently bypassed at every internal call site
— which forced consumers to regenerate a full parallel corpus per composition
(measured on the first Win16 port: 83 generated modules bypassing overrides,
273 duplicated modules).

Now every module-level callee binding and the dynamic-dispatch path resolve
through ``_dyncall.CALLEE_OVERRIDES`` (populated at plan-bind time, BEFORE any
corpus module is imported; empty = the generated corpus, byte-identical
behaviour).  These tests build a real on-disk two-function corpus and prove
the override wins at an internal call site — they FAIL on the old emitter.
"""
from __future__ import annotations

import importlib
import sys

from dos_re.lift.cfg import scan_function
from dos_re.lift.emit_cpuless import (CalleeContract, DYNCALL_SUPPORT_SRC,
                                      check_promotable, emit_recovered,
                                      _contract_inputs)
from dos_re.memory import Memory

CALLEE_OFF, CALLER_OFF = 0x0200, 0x0100
CALLEE = "func_1000_0200"
CALLER = "func_1000_0100"

#   B8 11 11   mov ax, 0x1111        (the GENERATED result)
#   C3         ret
_CALLEE_BYTES = bytes.fromhex("b81111" "c3")
#   E8 FD 00   call +0x00FD -> 0x0200 (an INTERNAL near call)
#   C3         ret
_CALLER_BYTES = bytes.fromhex("e8fd00" "c3")


def _scan(code: bytes, entry: int):
    return scan_function(
        lambda off: code[off - entry] if 0 <= off - entry < len(code) else 0x90,
        entry)


CALLEE_CONTRACT = CalleeContract(
    name=CALLEE, inputs=("sp", "ss"), outputs=("ax",),
    exit_flags=frozenset(), needs_plat=False, ret_kind="near",
    df_livein=False, sp_delta=0, ret_pop=0, sp_output=False, sp_deltas=(0,),
    flags_livein=False)


def _write_corpus(tmp_path, pkg: str) -> None:
    """A real on-disk corpus package: caller + callee + dispatch + _dyncall."""
    root = tmp_path / pkg
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    (root / "dispatch.py").write_text(
        "DISPATCH = {\n"
        f"    '1000:0200': ('{pkg}.{CALLEE}', '{CALLEE}', None,"
        " ('sp', 'ss'), False, False, False),\n"
        "}\nHANDLERS = {}\n", encoding="utf-8")
    (root / "_dyncall.py").write_text(DYNCALL_SUPPORT_SRC, encoding="utf-8")

    callee_scan = _scan(_CALLEE_BYTES, CALLEE_OFF)
    callee_spec = check_promotable(callee_scan)
    (root / f"{CALLEE}.py").write_text(emit_recovered(
        callee_scan, callee_spec.abi, "1000:0200",
        recovered_import_base=pkg,
        needs_plat=callee_spec.needs_plat, df_livein=callee_spec.df_livein,
        sp_output=callee_spec.sp_output,
        flags_livein=callee_spec.flags_livein), encoding="utf-8")

    callees = {CALLEE_OFF: CALLEE_CONTRACT}
    caller_scan = _scan(_CALLER_BYTES, CALLER_OFF)
    caller_spec = check_promotable(caller_scan, callees=callees)
    (root / f"{CALLER}.py").write_text(emit_recovered(
        caller_scan, caller_spec.abi, "1000:0100",
        recovered_import_base=pkg, callees=callees,
        needs_plat=caller_spec.needs_plat, df_livein=caller_spec.df_livein,
        sp_output=caller_spec.sp_output,
        flags_livein=caller_spec.flags_livein), encoding="utf-8")

    sys.path.insert(0, str(tmp_path))


def _call_caller(pkg: str):
    caller = getattr(importlib.import_module(f"{pkg}.{CALLER}"), CALLER)
    out, _compat = caller(mem=Memory(), ss=0x3000, sp=0x0100)
    return out


def _override(mem, **kw):
    """A hand-recovered stand-in honouring the address's recovered contract."""
    return {"ax": 0x2222}, {"flags": 0, "fmask": 0, "cost": 1}


def test_generated_default_without_overrides(tmp_path):
    _write_corpus(tmp_path, "t_callee_default_pkg")
    try:
        out = _call_caller("t_callee_default_pkg")
        assert out["ax"] & 0xFFFF == 0x1111        # the generated body ran
    finally:
        sys.path.remove(str(tmp_path))


def test_override_reaches_an_internally_called_target(tmp_path):
    """THE regression: populate CALLEE_OVERRIDES before the corpus imports
    (exactly what the plan's bind step does) — the caller's INTERNAL call must
    run the override, not the generated body it statically imports."""
    pkg = "t_callee_override_pkg"
    _write_corpus(tmp_path, pkg)
    try:
        dyncall = importlib.import_module(f"{pkg}._dyncall")
        dyncall.CALLEE_OVERRIDES[CALLEE] = _override
        out = _call_caller(pkg)
        assert out["ax"] & 0xFFFF == 0x2222, (
            "the plan-selected override was BYPASSED at an internal call "
            "site (the emitter bound the callee by direct import)")
    finally:
        sys.path.remove(str(tmp_path))


def test_override_reaches_the_dynamic_dispatch_path(tmp_path):
    """The DISPATCH table path resolves through the same registry: a dynamic
    transfer to an overridden selector runs the override."""
    pkg = "t_callee_dyn_pkg"
    _write_corpus(tmp_path, pkg)
    try:
        dyncall = importlib.import_module(f"{pkg}._dyncall")
        dyncall.CALLEE_OVERRIDES[CALLEE] = _override
        merged, compat = dyncall.dyn_exec(
            "1000:0200", Memory(), None, 0,
            {"sp": 0x0100, "ss": 0x3000})
        assert merged["ax"] & 0xFFFF == 0x2222
        assert compat["cost"] == 1
    finally:
        sys.path.remove(str(tmp_path))
