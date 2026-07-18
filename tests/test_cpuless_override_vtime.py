"""An override's VIRTUAL-TIME contract -- the seam that makes an authoritative
override admissible to an instruction-count-keyed differential.

``test_cpuless_override`` pins that an override composes byte-exactly in STATE.
State is not the whole contract: a composed callee also returns a virtual-time
``cost`` in the compat channel, and the caller accumulates it into ``_cost``,
which anchors every downstream platform effect (``plat.farcall``/``intr``/
``boundary``) and -- for a consumer whose demo/gate is keyed on instruction
count -- decides WHERE recorded input lands.  A GENERATED body is
instruction-exact by construction (per-block ``_cost += count``).  A
hand-recovered OVERRIDE is not: it does not execute the original's control flow,
so unless it DECLARES the original's per-invocation instruction count it silently
shifts the whole downstream timeline.

So an override now declares a virtual-time contract
(``virtual_time: {kind: static|model|island[, cost]}``, :func:`_read_virtual_time`)
and ``--overrides-time-exact-only`` seeds ONLY the exact ones, leaving an
``island`` address on its instruction-exact generated body.

The differential below is on the COST, in the same shape as the state one: the
composed caller's accumulated ``_cost`` is diffed against the interpreter's own
instruction count over the identical caller+override bytes.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from dos_re.cpu import CPU8086, CPUState
from dos_re.lift.cfg import scan_function
from dos_re.lift.emit_cpuless import (CalleeContract, check_promotable,
                                      emit_recovered, _contract_inputs)
from dos_re.memory import Memory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import cpuless_promote  # noqa: E402

OVR_SEG, OVR_OFF = 0x2000, 0x0000
CALLER_OFF = 0x0100
OVR_NAME = "func_2000_0000"

# The overridden function's REAL bytes: a straight-line body -- 3 instructions,
# entry through retf inclusive.  A single-path body's instruction count is a
# CONSTANT, which is exactly what a "static" virtual-time contract declares.
#   B8 34 12   mov ax, 0x1234
#   40         inc ax
#   CB         retf
_OVR = bytes.fromhex("b83412" "40" "cb")
_OVR_ASM_COST = 3

# The caller: push a pascal word arg, call far the override, ret.  3 instructions
# of its own.
_CALLER = bytes.fromhex("6a05" "9a00000020" "c3")
_CALLER_COST = 3

_OVR_BODY_TMPL = '''
def {name}(mem, *, sp=0, ss=0):
    return {{"ax": 0x1235}}, {{"flags": 0, "fmask": 0, "cost": {cost}}}
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


def _install_body(base: str, cost: int) -> None:
    pkg = types.ModuleType(base)
    pkg.__path__ = []
    sys.modules[base] = pkg
    mod = types.ModuleType(base + "." + OVR_NAME)
    exec(compile(_OVR_BODY_TMPL.format(name=OVR_NAME, cost=cost),
                 "<override>", "exec"), mod.__dict__)
    sys.modules[base + "." + OVR_NAME] = mod


def _composed_cost(base: str) -> int:
    """Run the composed caller and return its accumulated virtual time."""
    far_callees = {(OVR_SEG, OVR_OFF): OVR_CONTRACT}
    scan = _scan(_CALLER, CALLER_OFF)
    spec = check_promotable(scan, far_callees=far_callees)
    src = emit_recovered(scan, spec.abi, "2000:0100",
                         recovered_import_base=base, far_callees=far_callees,
                         needs_plat=spec.needs_plat, df_livein=spec.df_livein,
                         sp_output=spec.sp_output,
                         flags_livein=spec.flags_livein)
    ns: dict = {"_PARITY": [0] * 256}
    exec(compile(src, "<caller>", "exec"), ns)
    kw = {r: 0 for r in _contract_inputs(scan, spec.abi)
          if r not in ("sp", "ss")}
    _out, compat = ns["func_2000_0100"](mem=Memory(), ss=0x3000, sp=0x0100,
                                        **kw)
    return compat["cost"]


def _interp_cost() -> int:
    """The interpreter's instruction count over the identical bytes, from the
    caller's entry through its own trailing ret (inclusive)."""
    mem = Memory()
    for k, b in enumerate(_OVR):
        mem.data[(OVR_SEG << 4) + OVR_OFF + k] = b
    for k, b in enumerate(_CALLER):
        mem.data[(OVR_SEG << 4) + CALLER_OFF + k] = b
    st = CPUState(cs=OVR_SEG, ip=CALLER_OFF, ss=0x3000, ds=0x3000)
    st.sp = 0x0100
    cpu = CPU8086(mem, st)
    ret_ip = CALLER_OFF + len(_CALLER) - 1
    n = 0
    for _ in range(64):
        cpu.step()
        n += 1
        if (cpu.s.cs & 0xFFFF) == OVR_SEG and (cpu.s.ip & 0xFFFF) == ret_ip:
            break
    else:                                            # pragma: no cover
        raise AssertionError("caller did not reach its ret in budget")
    return n + 1                                     # + the caller's own ret


def test_static_cost_override_is_virtual_time_exact():
    """An override declaring the original's constant instruction count makes the
    composed caller's virtual time IDENTICAL to the interpreter's."""
    _install_body("t_ovr_vt_exact", _OVR_ASM_COST)
    interp = _interp_cost()
    assert interp == _CALLER_COST + _OVR_ASM_COST
    assert _composed_cost("t_ovr_vt_exact") == interp


def test_island_cost_override_drifts_the_timeline():
    """The ISLAND convention (one dispatch step) is NOT virtual-time-exact -- it
    under-counts by exactly the instructions the original executed.  This is the
    drift the contract exists to eliminate; pinning it keeps the seam honest."""
    _install_body("t_ovr_vt_island", 1)
    drift = _composed_cost("t_ovr_vt_island") - _interp_cost()
    assert drift == 1 - _OVR_ASM_COST < 0


def _contract(vt) -> dict:
    spec = {"name": OVR_NAME, "inputs": ["sp", "ss"], "outputs": ["ax"],
            "ret_kind": "far"}
    if vt is not None:
        spec["virtual_time"] = vt
    return {"2000:0000": spec}


def _load(tmp_path, vt):
    p = tmp_path / "ov.json"
    p.write_text(json.dumps({"overrides": _contract(vt)}), encoding="utf-8")
    return cpuless_promote._read_overrides(p)


def test_virtual_time_contract_parses_and_defaults_to_island(tmp_path):
    _c, vt = _load(tmp_path, None)
    assert vt["2000:0000"]["kind"] == "island"       # every pre-contract override
    _c, vt = _load(tmp_path, {"kind": "static", "cost": _OVR_ASM_COST})
    assert vt["2000:0000"] == {"kind": "static", "cost": _OVR_ASM_COST,
                               "evidence": ""}
    _c, vt = _load(tmp_path, {"kind": "model", "evidence": "per-path"})
    assert vt["2000:0000"]["kind"] == "model"


@pytest.mark.parametrize("vt", [
    {"kind": "guess"},                       # unknown kind
    {"kind": "static"},                      # no cost
    {"kind": "static", "cost": 0},           # a callee always costs >= 1 (ret)
    {"kind": "static", "cost": "12"},        # not an int
    {"kind": "static", "cost": True},        # bool is not a cost
    "static",                                # not a dict
])
def test_a_malformed_virtual_time_contract_fails_loud(tmp_path, vt):
    """Never guess a cost: a bad contract is a hard error, not a silent island."""
    with pytest.raises(SystemExit):
        _load(tmp_path, vt)


def test_only_exact_kinds_are_gate_admissible():
    """--overrides-time-exact-only seeds static/model and drops island; that
    partition is what keeps a mixed override graph gate-admissible."""
    assert set(cpuless_promote._VT_EXACT) == {"static", "model"}
    assert "island" in cpuless_promote._VT_KINDS
    assert "island" not in cpuless_promote._VT_EXACT
