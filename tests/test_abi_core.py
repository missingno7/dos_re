"""De-stacked ABI core (emit_abi slice 2) + the seeded differential
(abi_diff): the ABI core must BE the mechanical core for every driven
state, minus the machine stack.
"""
from __future__ import annotations

import sys
import types

import pytest

from dos_re.lift import emit_abi, emit_cpuless
from dos_re.lift.abi_diff import TraceMem, diff_one
from dos_re.lift.contracts import build_scans, infer_contracts
from dos_re.lift.emit_cpuless import Refusal
from tests.test_contracts import _ir


def _emit_pair(key: str, hexbytes: str):
    """(mech_fn, abi_core_fn, proposal) for one synthetic leaf function."""
    ir = _ir({key: hexbytes})
    scans, _ = build_scans(ir)
    scan = scans[key]
    census = infer_contracts(ir)
    prop = census["functions"][key]
    assert not prop["refusals"], prop["refusals"]

    spec = emit_cpuless.check_promotable(scan)
    mech_src = emit_cpuless.emit_recovered(
        scan, spec.abi, key, needs_plat=spec.needs_plat,
        df_livein=spec.df_livein, sp_output=spec.sp_output,
        flags_livein=spec.flags_livein)
    stem = f"func_{emit_abi._stem(key)}"
    mech_mod = types.ModuleType(stem)
    exec(compile(mech_src, stem + ".py", "exec"), mech_mod.__dict__)

    abi_src = emit_abi.emit_abi_core(scan, prop, key)
    abi_mod = types.ModuleType("abi_" + emit_abi._stem(key))
    exec(compile(abi_src, "abi.py", "exec"), abi_mod.__dict__)
    return getattr(mech_mod, stem), abi_mod._abi_core, prop, abi_mod


#: push bx; mov bx,3; add ax,bx; pop bx; ret  -- local stack traffic only
LEAF_PUSH = "53 BB 03 00 01 D8 5B C3"
#: branchy leaf with memory writes: cmp ax,5; jz L; mov [di],ax; L: ret
LEAF_BR = "3D 05 00 74 02 89 05 C3"


def test_destacked_core_matches_mechanical_push_pop():
    mech, core, prop, mod = _emit_pair("1010:0300", LEAF_PUSH)
    rep = diff_one(mech, core, prop, states=48)
    assert rep["ok"], rep["mismatches"][:3]
    # the ABI core wrote NOTHING to memory (its stack is virtual)
    m = TraceMem(7)
    core(m, ax=1, bx=2)
    assert m.writes == []


def test_destacked_core_matches_mechanical_branchy_memwrite():
    mech, core, prop, mod = _emit_pair("1010:0400", LEAF_BR)
    rep = diff_one(mech, core, prop, states=48)
    assert rep["ok"], rep["mismatches"][:3]
    # semantic memory writes DO happen (and are compared by the diff)
    m = TraceMem(7)
    core(m, ax=1, di=0x10, ds=0x1234)
    assert m.writes == [(0x1234, 0x10, 1, 2)]


def test_public_entry_over_the_one_core():
    _, core, prop, mod = _emit_pair("1010:0300", LEAF_PUSH)
    pub = getattr(mod, "abi_1010_0300")
    out = pub(TraceMem(3), ax=2, bx=9)
    o, _ = core(TraceMem(3), ax=2, bx=9)
    assert out == tuple(o[r] for r in prop["returns"]) or out == o[prop["returns"][0]]


def test_gate_refuses_stack_addressed_memory():
    # mov ax,[bp+2]; ret -- bp EA defaults to SS: stack used as memory
    ir = _ir({"1010:0000": "8B 46 02 C3"})
    scans, _ = build_scans(ir)
    with pytest.raises(Refusal, match="stack-addressed-memory"):
        emit_abi.check_destackable(scans["1010:0000"])


def test_gate_refuses_ret_addr_touch_and_unbalance():
    # pop ax; ret -- pops the return address
    ir = _ir({"1010:0000": "58 C3"})
    scans, _ = build_scans(ir)
    with pytest.raises(Refusal, match="touches-return-address"):
        emit_abi.check_destackable(scans["1010:0000"])
    # push ax; jz over-the-pop; pop ax; ret -- one path leaks a slot
    ir = _ir({"1010:0000": "50 74 01 58 C3"})
    scans, _ = build_scans(ir)
    with pytest.raises(Refusal, match="unbalanced-stack|depth-join-mismatch"):
        emit_abi.check_destackable(scans["1010:0000"])


def test_gate_refuses_calls():
    ir = _ir({"1010:0000": "E8 FD 00 C3", "1010:0100": "C3"})
    scans, _ = build_scans(ir)
    with pytest.raises(Refusal, match="leaf-only"):
        emit_abi.check_destackable(scans["1010:0000"])
